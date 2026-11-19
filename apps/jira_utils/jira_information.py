from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import os
import re
import sys
import time
from functools import lru_cache
from typing import Any, NoReturn

import click
import requests
from jira import JIRA, Issue, JIRAError
from simple_logger.logger import get_logger
from tenacity import RetryError, retry, retry_if_exception, stop_after_attempt, wait_fixed

from apps.utils import ListParamType, all_python_files, get_util_config

LOGGER = get_logger(name=__name__)

SA_GATEWAY_URL_TEMPLATE = "https://api.atlassian.com/ex/jira/{cloud_id}"
SA_GATEWAY_URL_PREFIX = "https://api.atlassian.com/ex/jira/"

ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
# Re-mint this long before the access token expires, so a scan never runs on a token that dies mid-flight.
TOKEN_REFRESH_MARGIN_SECONDS = 120

AUTH_MODE_SERVICE_ACCOUNT = "service-account"
AUTH_MODE_CLOUD_BASIC = "cloud-basic"
AUTH_MODE_DATACENTER_PAT = "datacenter-pat"

# A credential rejected with 401/403 is not transient, so retrying it only delays the failure.
AUTH_ERROR_STATUS_CODES = (401, 403)


class JiraTokenMintError(RuntimeError):
    """Raised when an Atlassian OAuth 2.0 access token cannot be minted."""


# (client_id, sha256 of the client secret) -> (access_token, monotonic expiry). The plaintext secret is never a key.
_ATLASSIAN_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}


def _is_service_account_auth(url: str, cloud_id: str) -> bool:
    """Return True when service-account (Bearer) auth should be used.

    Args:
        url (str): Jira site url or API gateway url.
        cloud_id (str): Atlassian Cloud id; when set, service-account auth is used.

    Returns:
        bool: True if `cloud_id` is set or `url` already is an API gateway url.
    """
    # url may be unset when only cloud_id is configured, so it is never trusted to be a str here.
    return bool(cloud_id) or (url or "").startswith(SA_GATEWAY_URL_PREFIX)


def _jira_browse_url(url: str) -> str:
    """Return the url used for /browse/<key> link matching, or "" when `url` is an API gateway url.

    Source-code references point at the Jira site (e.g. https://redhat.atlassian.net/browse/KEY), never at the
    API gateway, so the site part is dropped for service-account auth to keep the match host-agnostic.

    Args:
        url (str): Jira site url or API gateway url.

    Returns:
        str: `url` unchanged, or "" for an API gateway url.
    """
    return "" if url.startswith(SA_GATEWAY_URL_PREFIX) else url


def _mask_cloud_id(cloud_id: str) -> str:
    """Return a fixed placeholder for `cloud_id`, never any part of the value itself.

    Args:
        cloud_id (str): Atlassian Cloud id; only its presence matters for the log line.

    Returns:
        str: The literal "<cloud-id>".
    """
    return "<cloud-id>"


def _mask_server(server: str) -> str:
    """Return `server` with the cloud id masked, for human-readable output only.

    The real server url is still what the client talks to; masking applies to log lines and messages only.

    Args:
        server (str): Jira site url or API gateway url.

    Returns:
        str: `https://api.atlassian.com/ex/jira/<cloud-id>` for a gateway url (the cloud id is not echoed),
            else `server` unchanged because a site url holds no cloud id.
    """
    if not server.startswith(SA_GATEWAY_URL_PREFIX):
        return server

    return f"{SA_GATEWAY_URL_PREFIX}{_mask_cloud_id(cloud_id=server.removeprefix(SA_GATEWAY_URL_PREFIX))}"


def resolve_jira_server(url: str, cloud_id: str) -> str:
    """Resolve the Jira API server URL for the given url/cloud id.

    A service-account scoped API token only works against the Atlassian API gateway
    (https://api.atlassian.com/ex/jira/<cloud_id>), never against the site url.

    Args:
        url (str): Jira site url or an already resolved API gateway url.
        cloud_id (str): Atlassian Cloud id; when set the gateway url is returned.

    Returns:
        str: The API gateway url if `cloud_id` is set or `url` already is a gateway url, else `url` unchanged.
    """
    if cloud_id:
        return SA_GATEWAY_URL_TEMPLATE.format(cloud_id=cloud_id)

    return url


def _cloud_id_from_env() -> str:
    """Return the Atlassian Cloud id from the environment, or "" when neither variable is set.

    Returns:
        str: `JIRA_CLOUD_ID`, falling back to its OAuth alias `ATLASSIAN_OAUTH_CLOUD_ID`.
    """
    return os.getenv("JIRA_CLOUD_ID") or os.getenv("ATLASSIAN_OAUTH_CLOUD_ID") or ""


def mint_atlassian_token(client_id: str, client_secret: str) -> str:
    """Mint (or reuse) a short-lived Atlassian access token from an OAuth 2.0 service-account credential.

    Args:
        client_id (str): The OAuth 2.0 client id.
        client_secret (str): The OAuth 2.0 client secret; never logged or returned to the caller.

    Returns:
        str: A bearer access token, either cached or freshly minted.

    Raises:
        JiraTokenMintError: On a network failure, a non-200 response or a malformed payload.
    """
    now = time.monotonic()
    # Drop expired entries on every mint, so the cache cannot grow without bound over a long-lived process.
    for expired_key in [key for key, (_, expiry) in _ATLASSIAN_TOKEN_CACHE.items() if expiry <= now]:
        del _ATLASSIAN_TOKEN_CACHE[expired_key]

    cache_key = (client_id, hashlib.sha256(client_secret.encode()).hexdigest())
    cached = _ATLASSIAN_TOKEN_CACHE.get(cache_key)
    if cached and cached[1] - TOKEN_REFRESH_MARGIN_SECONDS > now:
        return cached[0]

    try:
        response = requests.post(
            ATLASSIAN_TOKEN_URL,
            json={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
            timeout=15,
        )
    except requests.RequestException as exp:
        # Only the exception text: the request body holds the client secret.
        raise JiraTokenMintError(
            f"could not mint an Atlassian access token from the OAuth 2.0 credential ({exp})"
        ) from exp

    if response.status_code != 200:
        raise JiraTokenMintError(
            "could not mint an Atlassian access token from the OAuth 2.0 credential "
            f"(HTTP {response.status_code} - check the client id and secret)"
        )

    try:
        payload = response.json()
    except ValueError as exp:
        raise JiraTokenMintError(
            "could not mint an Atlassian access token from the OAuth 2.0 credential "
            f"(HTTP {response.status_code} - malformed response payload)"
        ) from exp

    access_token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise JiraTokenMintError(
            "could not mint an Atlassian access token from the OAuth 2.0 credential "
            f"(HTTP {response.status_code} - the response payload has no access_token)"
        )

    try:
        expires_in = int(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        expires_in = 3600

    _ATLASSIAN_TOKEN_CACHE[cache_key] = (access_token, time.monotonic() + expires_in)
    LOGGER.info(f"Minted an Atlassian OAuth 2.0 access token, valid for {expires_in} seconds")
    return access_token


def build_jira_client(url: str, token: str, user: str, cloud: bool, cloud_id: str) -> JIRA:
    """Build a Jira client for the selected authentication mode.

    `token` must already be the effective bearer: the OAuth 2.0 access token is minted before this call, so the
    caller can redact the live credential from any error raised here.

    Args:
        url (str): Jira site url or API gateway url.
        token (str): Jira PAT, API token, service-account scoped API token or minted OAuth access token.
        user (str): Jira user email (cloud basic auth only).
        cloud (bool): Whether to use cloud basic auth.
        cloud_id (str): Atlassian Cloud id; enables service-account (Bearer) auth.

    Returns:
        JIRA: A configured Jira client.

    Raises:
        JIRAError: If Jira rejects the credential while building the client (serverInfo is called eagerly).
    """
    mode, server = _jira_auth_mode(url=url, cloud=cloud, cloud_id=cloud_id)
    if mode == AUTH_MODE_SERVICE_ACCOUNT:
        return JIRA(server=server, token_auth=token)

    if mode == AUTH_MODE_CLOUD_BASIC:
        return JIRA(server=server, basic_auth=(user, token))

    return JIRA(token_auth=token, options={"server": server})


def _jira_auth_mode(url: str, cloud: bool, cloud_id: str) -> tuple[str, str]:
    """Return the resolved authentication mode and server url for the given config values.

    Single source of truth for the auth-mode decision: both the client factory and the error reporter use it.

    Args:
        url (str): Jira site url or API gateway url.
        cloud (bool): Whether cloud basic auth is requested.
        cloud_id (str): Atlassian Cloud id; enables service-account (Bearer) auth.

    Returns:
        tuple[str, str]: The auth mode constant (AUTH_MODE_*) and the server url to use.
    """
    if _is_service_account_auth(url=url, cloud_id=cloud_id):
        return AUTH_MODE_SERVICE_ACCOUNT, resolve_jira_server(url=url, cloud_id=cloud_id)

    return (AUTH_MODE_CLOUD_BASIC if cloud else AUTH_MODE_DATACENTER_PAT), url


def _credential_label(url: str, cloud: bool, cloud_id: str) -> str:
    """Return the human name of the credential the given config authenticates with.

    Args:
        url (str): Jira site url or API gateway url.
        cloud (bool): Whether cloud basic auth is requested.
        cloud_id (str): Atlassian Cloud id; enables service-account (Bearer) auth.

    Returns:
        str: A one-line label naming the credential that is actually used.
    """
    mode, _ = _jira_auth_mode(url=url, cloud=cloud, cloud_id=cloud_id)
    return {
        AUTH_MODE_SERVICE_ACCOUNT: "the pre-minted scoped API token",
        AUTH_MODE_CLOUD_BASIC: "cloud basic auth (user email plus API token)",
        AUTH_MODE_DATACENTER_PAT: "the Data Center personal access token (PAT)",
    }[mode]


def _error_detail(text: str | None, *, token: str = "") -> str:
    """Return a single-line, bounded and redacted rendering of a Jira error response body.

    The body is always kept but is bounded and stripped of the token: the token must never reach a log, while the
    body is what makes a permission gap (e.g. a per-issue 403 with a healthy credential) diagnosable.

    Redaction happens on the full body, before truncation: an Atlassian scoped token is ~192 characters, so a
    200-character window can be almost entirely secret, and a truncation that runs first leaves a token longer than
    the window (or split by the cut) unmatched and therefore leaked. A second pass on the collapsed form is required
    because a token split by newlines (a line-wrapping proxy echoing it back) is unmatched on the raw body, so it must
    be re-matched after the collapse with whitespace allowed between its characters.

    Args:
        text (str | None): The raw response body from Jira.
        token (str): The configured Jira token; every occurrence is redacted. Empty or shorter than 8 characters
            means nothing is redacted.

    Returns:
        str: The collapsed body truncated to 200 characters, with a real (>=8 chars) token replaced by '***'.
    """
    full = str(text or "")

    # Only a real Atlassian token is worth redacting: any string under 8 characters cannot be one, and replacing a
    # short value (e.g. a dummy "t") would corrupt unrelated words in the message ("connection" -> "connec***ion").
    if len(token) >= 8:
        full = full.replace(token, "***")

    detail = " ".join(full.split())

    # The collapse joins split pieces with a space, so a token broken by newlines is still not contiguous: re-match it
    # with any whitespace between its characters, otherwise both halves reach the log.
    if len(token) >= 8:
        detail = re.sub(r"\s*".join(map(re.escape, token)), "***", detail)

    return detail[:200]


def _auth_failure_hint(mode: str, oauth_service_account: bool = False) -> str:
    """Return the credential guidance for the given auth mode.

    Args:
        mode (str): An AUTH_MODE_* constant.
        oauth_service_account (bool): True when the OAuth 2.0 credential (client id/secret) is in play.

    Returns:
        str: A one-line hint telling the user what to check or fix.
    """
    if mode == AUTH_MODE_SERVICE_ACCOUNT:
        if oauth_service_account:
            # The access token is minted fresh on every expiry, so a rejection is the credential itself or its scopes.
            return (
                "The OAuth 2.0 service-account credential was rejected: verify --client-id/--client-secret, that the "
                "credential is enabled in Atlassian admin, that --cloud-id matches the Jira site, and that it has the "
                "scopes the operation needs."
            )

        return (
            "The service-account scoped API token was rejected. It is likely expired, or the cloud id (--cloud-id) "
            "does not match the Jira site the token is scoped to. Re-mint the token, export it as JIRA_TOKEN and "
            "verify --cloud-id against your Jira site."
        )

    return "Check JIRA_TOKEN and, for cloud basic auth, JIRA_USER."


def _report_jira_auth_error(exp: JIRAError, jira_config_dict: dict[str, Any]) -> NoReturn:
    """Log an actionable authentication error and exit non-zero.

    Args:
        exp (JIRAError): The error raised while building the client or calling myself().
        jira_config_dict (dict[str, Any]): Processed Jira configuration (url, token, cloud, cloud_id).
    """
    mode, server = _jira_auth_mode(
        url=jira_config_dict["url"], cloud=jira_config_dict["cloud"], cloud_id=jira_config_dict["cloud_id"]
    )
    detail = _error_detail(text=exp.text, token=jira_config_dict["token"])
    LOGGER.error(
        f"Jira authentication failed (mode: {mode}, server: {_mask_server(server=server)}): {exp.status_code} {detail}"
    )
    if exp.status_code in AUTH_ERROR_STATUS_CODES:
        LOGGER.error(_auth_failure_hint(mode=mode, oauth_service_account=bool(jira_config_dict.get("client_secret"))))

    sys.exit(1)


def _probe_credential(
    jira_obj: JIRA, *, service_account: bool = False
) -> tuple[JIRAError | None, dict[str, Any] | None]:
    """Probe the credential with myself(), falling back to the project endpoint for a scope-limited one.

    A service-account credential (an OAuth 2.0 pair as much as a pre-minted scoped API token) usually carries only
    read:jira-work/write:jira-work, which /myself is not part of, so a 401/403 there proves nothing about the
    credential. The project endpoint decides instead.

    Args:
        jira_obj (JIRA): An already built Jira client.
        service_account (bool): True for any service-account (Bearer) auth, whatever minted the token.

    Returns:
        tuple[JIRAError | None, dict[str, Any] | None]: The error that failed the probe (None on success) and the
            identity from myself(), which is None when a scope-limited credential was verified via projects().
    """
    try:
        return None, jira_obj.myself()
    except JIRAError as exp:
        if not (service_account and exp.status_code in AUTH_ERROR_STATUS_CODES):
            return exp, None

        try:
            jira_obj.projects()
        except JIRAError:
            return exp, None

        return None, None


def _check_jira_auth(jira_obj: JIRA, jira_config_dict: dict[str, Any]) -> None:
    """Log the resolved auth mode, server url and identity.

    The credential is probed with myself(); a scope-limited service-account credential is 401/403 there, so the
    project endpoint is probed instead and the success line reports project visibility rather than identity.

    Exits with code 1 on authentication failure.

    Args:
        jira_obj (JIRA): An already built Jira client.
        jira_config_dict (dict[str, Any]): Processed Jira configuration (url, token, cloud, cloud_id, client_secret).
    """
    mode, server = _jira_auth_mode(
        url=jira_config_dict["url"], cloud=jira_config_dict["cloud"], cloud_id=jira_config_dict["cloud_id"]
    )

    exp, myself = _probe_credential(
        jira_obj=jira_obj,
        service_account=_is_service_account_auth(url=jira_config_dict["url"], cloud_id=jira_config_dict["cloud_id"]),
    )
    if exp is not None:
        if exp.status_code in AUTH_ERROR_STATUS_CODES:
            _report_jira_auth_error(exp=exp, jira_config_dict=jira_config_dict)

        # Not a credential problem (404, connection error, 5xx): report the real cause, not token advice.
        # No exc_info: JIRAError.__str__ appends the raw response body and headers (unbounded, may hold Set-Cookie).
        LOGGER.error(
            f"Jira credential check failed ({type(exp).__name__}, status: {exp.status_code}): "
            f"{_error_detail(text=exp.text, token=jira_config_dict['token'])}"
        )
        sys.exit(1)

    if myself is None:
        LOGGER.info(
            f"Jira authentication OK - mode: {mode}, server: {_mask_server(server=server)}, service account: "
            "project visibility verified; the current-user endpoint (/myself) is unavailable for this "
            "credential's scopes"
        )
        return

    LOGGER.info(
        f"Jira authentication OK - mode: {mode}, server: {_mask_server(server=server)}, "
        f"user: {myself.get('displayName')}, email: {myself.get('emailAddress')}, "
        f"accountId: {myself.get('accountId')}"
    )


@lru_cache
def _credential_rejected(jira_obj: JIRA, service_account: bool = False) -> bool:
    """Return True when Jira rejects the credential itself, not just the single issue that failed.

    A per-issue 401/403 can be a project permission gap (a per-issue 403 is normal in Data Center mode) or, for a
    scope-limited service-account credential, the missing current-user scope, so the verdict is confirmed with the
    same probe --check-auth uses. It is cached (per client and flavour) so a scan probes at most once.

    Args:
        jira_obj (JIRA): An already built Jira client.
        service_account (bool): True for any service-account (Bearer) auth, whatever minted the token.

    Returns:
        bool: True when the probe also fails with 401/403.
    """
    exp, _ = _probe_credential(jira_obj=jira_obj, service_account=service_account)
    return exp is not None and exp.status_code in AUTH_ERROR_STATUS_CODES


def _retryable_jira_error(exp: BaseException) -> bool:
    """Return True when a failed Jira call is worth retrying.

    Only a transient failure is retried: no status code at all (a connection error) or a 5xx. A 4xx is permanent
    (a rejected credential, a missing issue, a bad request), so retrying it only multiplies the delay for every
    issue in the repo.

    Args:
        exp (BaseException): The exception raised by the Jira call.

    Returns:
        bool: True for a JIRAError without a status code or with a 5xx status code.
    """
    return isinstance(exp, JIRAError) and (exp.status_code is None or exp.status_code >= 500)


@retry(
    retry=retry_if_exception(_retryable_jira_error),
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
)
@lru_cache
def get_issue(
    jira: JIRA,
    jira_id: str,
) -> Issue:
    LOGGER.debug(f"Retry staistics for {jira_id}: {get_issue.statistics}")
    return jira.issue(id=jira_id, fields="status, issuetype, fixVersions")


def get_jira_ids_from_file_content(file_content: str, issue_pattern: str, jira_url: str) -> set[str]:
    """
    Try to find all Jira tickets in a given file content.

    Looking for the following patterns:
    - jira_id=ABC-12345  # When jira id is present in a function call
    - <jira_url>/browse/ABC-12345  # when jira is in a link in comments
    - pytest.mark.jira_utils(ABC-12345)  # when jira is in a marker

    Args:
        file_content (str): The content of a given file.
        issue_pattern (str): regex pattern for jira ids
        jira_url (str): Jira site url used as the (escaped) regex prefix before /browse/. An empty value matches
            /browse/<key> on any http(s) host, which is what service-account/gateway auth uses because the
            gateway url never appears in source code. The value is used as a regex prefix, so it is escaped.

    Returns:
        set: A set of jira tickets.
    """
    url_prefix = re.escape(jira_url.rstrip("/")) if jira_url else r"https?://[^\s/]+"
    _pytest_jira_marker_bugs = re.findall(rf"pytest.mark.jira.*?{issue_pattern}.*", file_content, re.DOTALL)
    _jira_id_arguments = re.findall(rf"jira_id\s*=[\s*\"\']*{issue_pattern}.*", file_content)
    _jira_url_jiras = re.findall(rf"{url_prefix}/browse/{issue_pattern}", file_content)

    return set(_pytest_jira_marker_bugs + _jira_id_arguments + _jira_url_jiras)


def get_jiras_from_python_files(issue_pattern: str, jira_url: str) -> dict[str, set[str]]:
    """
    Get all python files from the current directory and get list of jira ids from each of them

    Args:
        issue_pattern (str): regex pattern for jira ids
        jira_url (str): jira url that could be used to look for possible presence of jira references in a file.
            An empty value matches /browse/<key> on any http(s):// host (used for service-account/gateway auth);
            scheme-less links are ignored.

    Returns:
        Dict: A dict of filenames and associated jira tickets.

    Note: any line containing <skip-jira_utils-check> would be not be checked for presence of a jira id
    """
    jira_found: dict[str, set[str]] = {}
    for filename in all_python_files():
        file_content = []
        with open(filename) as fd:
            file_content = fd.readlines()
        # if <skip-jira-utils-check> appears in a line, exclude that line from jira check
        if unique_jiras := get_jira_ids_from_file_content(
            file_content="\n".join([line for line in file_content if "<skip-jira-utils-check>" not in line]),
            issue_pattern=issue_pattern,
            jira_url=jira_url,
        ):
            jira_found[filename] = unique_jiras

    if jira_found:
        _jira_found = "\n\t".join([f"{key}: {val}" for key, val in jira_found.items()])
        LOGGER.debug(f"Following jiras are found: \n\t{_jira_found}")

    return jira_found


def get_jira_information(
    jira_object: JIRA,
    jira_id: str,
    skip_project_ids: list[str],
    resolved_status: list[str],
    jira_target_versions: list[str],
    target_version_str: str,
    file_name: str,
    *,
    token: str = "",
) -> tuple[str, str, int | None]:
    """Check a single Jira issue against the resolved-status and target-version rules.

    Args:
        jira_object (JIRA): An already built Jira client.
        jira_id (str): The Jira issue key.
        skip_project_ids (list[str]): Project key prefixes whose version check is skipped.
        resolved_status (list[str]): Statuses considered resolved.
        jira_target_versions (list[str]): Accepted target versions.
        target_version_str (str): Version string used for issues not yet targeted.
        file_name (str): File the issue was found in.
        token (str): The configured Jira token, redacted from any error body.

    Returns:
        tuple[str, str, int | None]: The file name, the error string (empty when the issue is fine) and the HTTP
            status code of the failed Jira call, or None when the call succeeded.
    """
    jira_error_string = ""
    status_code: int | None = None
    re_compile = rf"(?<![\d.])\d+\.\d+(?:\.(?:\d+|z))?|{target_version_str}\b"

    try:
        # check resolved status:
        jira_issue_metadata = get_issue(jira=jira_object, jira_id=jira_id).fields
        current_jira_status = jira_issue_metadata.status.name.lower()
        LOGGER.debug(f"Jira: {jira_id}, status: {current_jira_status}")
        if current_jira_status in resolved_status:
            jira_error_string += f"{jira_id} current status: {current_jira_status} is resolved."

        # validate a correct target version if provided:
        if jira_target_versions:
            if skip_project_ids and jira_id.startswith(tuple(skip_project_ids)):
                return file_name, jira_error_string, None

            current_target_versions = [target_version_str]
            # If a bug has fix version(s), extract using regex
            if jira_fix_versions := jira_issue_metadata.fixVersions:
                jira_fix_versions = ",".join([jira_fix_version.name for jira_fix_version in jira_fix_versions])
                current_target_versions = re.findall(re_compile, jira_fix_versions)

            if any(version in jira_target_versions for version in current_target_versions):
                return file_name, jira_error_string, None

            else:
                jira_error_string += (
                    f"{jira_id} target versions: {current_target_versions}, do not match expected "
                    f"version {jira_target_versions}."
                )

    except (JIRAError, RetryError) as exp:
        # tenacity wraps an exhausted retry in RetryError, which is not a JIRAError: left unwrapped it escapes the
        # worker future as a traceback and aborts the whole scan.
        cause: BaseException | None = exp.last_attempt.exception() if isinstance(exp, RetryError) else exp
        # Only a JIRAError is retried, so the last attempt always is one; the guard keeps a stray cause readable.
        jira_exp = cause if isinstance(cause, JIRAError) else None
        status_code = jira_exp.status_code if jira_exp else None
        jira_error_string += (
            f"{jira_id} JiraError status code: {status_code}, details: "
            f"{_error_detail(text=jira_exp.text if jira_exp else str(cause), token=token)}]."
        )

    return file_name, jira_error_string, status_code


def process_jira_command_line_config_file(
    config_file_path: str,
    url: str,
    token: str,
    issue_pattern: str,
    resolved_statuses: list[str],
    version_string_not_targeted_jiras: str,
    target_versions: list[str],
    skip_projects: list[str],
    user: str,
    cloud: bool | None,
    cloud_id: str | None = None,
    client_id: str = "",
    client_secret: str = "",
) -> dict[str, Any]:
    # Process all the arguments passed from command line or config file or environment variable
    config_dict = get_util_config(util_name="pyutils-jira", config_file_path=config_file_path)
    url = url or config_dict.get("url", "")
    token = token or config_dict.get("token", "")
    user = user or config_dict.get("user", "")
    # An env-provided value arrives as the CLI default, exactly like --token, so plain CLI-then-config is the order.
    client_id = client_id or config_dict.get("client_id", "")
    client_secret = client_secret or config_dict.get("client_secret", "")
    cloud = cloud if cloud is not None else config_dict.get("cloud", False)
    if cloud_id is None:
        # An explicit empty --cloud-id must win over both, so an unset option is what falls back.
        cloud_id = _cloud_id_from_env() or config_dict.get("cloud_id", "")

    if cloud_id and not re.fullmatch(r"[A-Za-z0-9-]{8,64}", cloud_id):
        # The cloud id goes into the API gateway url we send the bearer token to, so validate it first.
        LOGGER.error(
            "Invalid Jira cloud id (from --cloud-id/JIRA_CLOUD_ID/ATLASSIAN_OAUTH_CLOUD_ID/"
            f"config 'cloud_id'): {_mask_cloud_id(cloud_id=cloud_id)}. "
            'Pass --cloud-id "" to ignore it.'
        )
        sys.exit(1)

    if cloud_id and url and not url.startswith(SA_GATEWAY_URL_PREFIX):
        # Logged here only: resolve_jira_server is also called for the auth-mode lookup, which would double it.
        LOGGER.info(
            f"cloud_id provided, discarding supplied Jira url {_mask_server(server=url)} in favor of the API gateway url"
        )

    # Same predicate the client uses, so validation never rejects a config the client would accept. A gateway url
    # already encodes the cloud id, so it satisfies the OAuth cloud-id requirement exactly like --cloud-id does.
    service_account_auth = _is_service_account_auth(url=url, cloud_id=cloud_id)

    if bool(client_id) != bool(client_secret):
        # A stray half of the pair must not break a run that has another complete credential, but the identity
        # switches, so it is an error: a stray JIRA_TOKEN in the env would otherwise silently take over.
        if token and (url or cloud_id):
            LOGGER.warning(
                "Incomplete OAuth 2.0 service-account credential (both --client-id and --client-secret are "
                "required): ignoring it and authenticating with "
                f"{_credential_label(url=url, cloud=cloud, cloud_id=cloud_id)} instead."
            )
            client_id = client_secret = ""
        else:
            LOGGER.error(
                "Both --client-id and --client-secret are required for the OAuth 2.0 service-account credential."
            )
            sys.exit(1)

    if client_id and not service_account_auth:
        # An env-exported pair is a documented convention, so it must not break a run on another credential.
        if token and url:
            LOGGER.warning(
                "OAuth 2.0 service-account credential ignored for lack of a cloud id (pass --cloud-id or set "
                f"JIRA_CLOUD_ID): authenticating with {_credential_label(url=url, cloud=cloud, cloud_id=cloud_id)} "
                "instead."
            )
            client_id = client_secret = ""
        else:
            LOGGER.error(
                "A Jira cloud id is required for OAuth service-account authentication: pass --cloud-id "
                '(or set JIRA_CLOUD_ID), or opt out of the OAuth credential with --client-id "" --client-secret "".'
            )
            sys.exit(1)

    oauth_service_account = bool(client_id and client_secret)

    if not token and not oauth_service_account:
        LOGGER.error("Jira token is required." if service_account_auth else "Jira url and token are required.")
        sys.exit(1)

    if service_account_auth:
        flavour = "OAuth 2.0 client credentials" if oauth_service_account else "pre-minted scoped API token"
        LOGGER.info(
            f"Using Jira service-account ({flavour}) authentication against "
            f"{_mask_server(server=resolve_jira_server(url=url, cloud_id=cloud_id))}"
        )
    elif cloud and not user:
        LOGGER.error("Jira user is required for cloud Jira.")
        sys.exit(1)

    elif not url:
        LOGGER.error("Jira url and token are required.")
        sys.exit(1)

    return {
        "url": url,
        "token": token,
        "issue_pattern": issue_pattern or config_dict.get("issue_pattern", ""),
        "resolved_status": resolved_statuses or config_dict.get("resolved_statuses", []),
        "not_targeted_version_str": config_dict.get(
            "version_string_not_targeted_jiras", version_string_not_targeted_jiras
        ),
        "target_versions": target_versions or config_dict.get("target_versions", []),
        "skip_project_ids": skip_projects or config_dict.get("skip_project_ids", []),
        "user": user,
        "cloud": cloud,
        "cloud_id": cloud_id,
        "client_id": client_id,
        "client_secret": client_secret,
    }


@click.command()
@click.option(
    "--config-file-path",
    help="Provide absolute path to the jira_utils config file.",
    type=click.Path(exists=True),
)
@click.option(
    "--target-versions",
    help="Provide comma separated list of Jira target version, for version validation against a repo branch.",
    type=ListParamType(),
)
@click.option(
    "--skip-projects",
    help="Provide comma separated list of Jira Project keys, against which version check should be skipped.",
    type=ListParamType(),
)
@click.option(
    "--url",
    help="Provide the Jira server URL",
    type=click.STRING,
    default=os.getenv("JIRA_SERVER_URL") or os.getenv("JIRA_URL"),
)
@click.option(
    "--token",
    help="Provide the Jira token.",
    type=click.STRING,
    default=os.getenv("JIRA_TOKEN"),
)
@click.option(
    "--user",
    help="Provide the Jira user email (required for cloud Jira).",
    type=click.STRING,
    default=os.getenv("JIRA_USER"),
)
@click.option(
    "--issue-pattern",
    help="Provide the regex for Jira ids",
    type=click.STRING,
    show_default=True,
    default="([A-Z]+-[0-9]+)",
)
@click.option(
    "--resolved-statuses",
    help="Comma separated list of Jira resolved statuses",
    type=ListParamType(),
    show_default=True,
    default="verified, release pending, closed, resolved",
)
@click.option(
    "--version-string-not-targeted-jiras",
    help="Provide possible version strings for not yet targeted jiras",
    type=click.STRING,
    show_default=True,
    default="vfuture",
)
@click.option("--verbose", default=False, is_flag=True)
@click.option(
    "--cloud",
    help="Use cloud Jira authentication (basic_auth with user email and API token).",
    is_flag=True,
    default=None,
)
@click.option(
    "--cloud-id",
    help="Atlassian Cloud id for service-account (scoped API token) auth; the client then uses "
    "https://api.atlassian.com/ex/jira/<cloud-id> with a Bearer token. Defaults to JIRA_CLOUD_ID or the "
    'config file cloud_id; pass --cloud-id "" to disable service-account auth regardless of both.',
    type=click.STRING,
    default=None,
)
@click.option(
    "--client-id",
    help="OAuth 2.0 client id of the Atlassian service account; it is exchanged for a short-lived access token. "
    "Defaults to JIRA_SA_CLIENT_ID (aliases: ATLASSIAN_OAUTH_CLIENT_ID).",
    type=click.STRING,
    default=os.getenv("JIRA_SA_CLIENT_ID") or os.getenv("ATLASSIAN_OAUTH_CLIENT_ID"),
)
@click.option(
    "--client-secret",
    help="OAuth 2.0 client secret of the Atlassian service account; it is exchanged for a short-lived access "
    "token. Defaults to JIRA_SA_CLIENT_SECRET (aliases: JIRA_OAUTH_CLIENT_SECRET).",
    type=click.STRING,
    default=os.getenv("JIRA_SA_CLIENT_SECRET") or os.getenv("JIRA_OAUTH_CLIENT_SECRET"),
)
@click.option(
    "--check-auth",
    help="Verify the Jira credentials against /myself (for a scope-limited OAuth 2.0 service-account credential it "
    "falls back to the project endpoint) and exit.",
    is_flag=True,
    default=False,
)
def get_jira_mismatch(
    config_file_path: str,
    target_versions: list[str],
    url: str,
    token: str,
    user: str,
    skip_projects: list[str],
    resolved_statuses: list[str],
    issue_pattern: str,
    version_string_not_targeted_jiras: str,
    verbose: bool,
    cloud: bool | None,
    cloud_id: str | None,
    client_id: str | None,
    client_secret: str | None,
    check_auth: bool,
) -> None:
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    if cloud_id is None:
        # Resolve the env fallback before the guard so it sees the value the rest of the run will use.
        # An explicit empty --cloud-id is not None and must stay the opt-out, so it is never replaced here.
        cloud_id = _cloud_id_from_env() or None

    # Only an entirely empty invocation is rejected here: every credential combination is validated (with its own
    # message) in process_jira_command_line_config_file, so the documented exit messages stay reachable.
    if not (config_file_path or url or token or cloud_id or client_id or client_secret):
        LOGGER.error(
            "No Jira configuration supplied: pass --config-file-path, or a credential (--token with --url or "
            "--cloud-id, or --client-id plus --client-secret with --cloud-id)."
        )
        sys.exit(1)

    # Process all the arguments passed from command line or config file or environment variable
    jira_config_dict = process_jira_command_line_config_file(
        config_file_path=config_file_path,
        url=url,
        token=token,
        resolved_statuses=resolved_statuses,
        issue_pattern=issue_pattern,
        skip_projects=skip_projects,
        version_string_not_targeted_jiras=version_string_not_targeted_jiras,
        target_versions=target_versions,
        user=user,
        cloud=cloud,
        cloud_id=cloud_id,
        client_id=client_id or "",
        client_secret=client_secret or "",
    )

    # Mint before the client is built, so every reporter below redacts the live bearer even when JIRA() fails.
    if jira_config_dict["client_id"] and jira_config_dict["client_secret"]:
        try:
            jira_config_dict["token"] = mint_atlassian_token(
                client_id=jira_config_dict["client_id"], client_secret=jira_config_dict["client_secret"]
            )
        except JiraTokenMintError as exp:
            LOGGER.error(f"Jira authentication failed: {exp}")
            sys.exit(1)

    try:
        jira_obj = build_jira_client(
            url=jira_config_dict["url"],
            token=jira_config_dict["token"],
            user=jira_config_dict["user"],
            cloud=jira_config_dict["cloud"],
            cloud_id=jira_config_dict["cloud_id"],
        )
    except JIRAError as exp:
        # JIRA's constructor already calls serverInfo, so a rejected credential fails here too.
        if exp.status_code in AUTH_ERROR_STATUS_CODES:
            _report_jira_auth_error(exp=exp, jira_config_dict=jira_config_dict)

        # Not a credential problem (connection error, 5xx, no status): report the real cause, not token advice.
        # No exc_info: JIRAError.__str__ appends the raw response body and headers, which defeats the 200-char
        # bound and the single-line redaction above.
        LOGGER.error(
            f"Jira client construction failed ({type(exp).__name__}, status: {exp.status_code}): "
            f"{_error_detail(text=exp.text, token=jira_config_dict['token'])}"
        )
        sys.exit(1)

    if check_auth:
        _check_jira_auth(jira_obj=jira_obj, jira_config_dict=jira_config_dict)
        return

    jira_error: dict[str, str] = {}
    # The probe's projects() fallback covers any service-account (Bearer) auth, not just the OAuth pair.
    service_account_auth = _is_service_account_auth(url=jira_config_dict["url"], cloud_id=jira_config_dict["cloud_id"])

    if jira_id_dict := get_jiras_from_python_files(
        issue_pattern=jira_config_dict["issue_pattern"],
        jira_url=_jira_browse_url(jira_config_dict["url"]),
    ):
        with concurrent.futures.ThreadPoolExecutor() as executor:
            for file_name, ids in jira_id_dict.items():
                for jira_id in ids:
                    future_to_jiras = {
                        executor.submit(
                            get_jira_information,
                            jira_object=jira_obj,
                            jira_id=jira_id,
                            skip_project_ids=jira_config_dict["skip_project_ids"],
                            resolved_status=jira_config_dict["resolved_status"],
                            jira_target_versions=jira_config_dict["target_versions"],
                            target_version_str=jira_config_dict["not_targeted_version_str"],
                            file_name=file_name,
                            token=jira_config_dict["token"],
                        )
                    }

                    for future in concurrent.futures.as_completed(future_to_jiras):
                        file_name, jira_error_string, status_code = future.result()
                        if status_code in AUTH_ERROR_STATUS_CODES and _credential_rejected(
                            jira_obj=jira_obj, service_account=service_account_auth
                        ):
                            # A dead credential breaks every issue identically: report once and stop the scan.
                            mode, _ = _jira_auth_mode(
                                url=jira_config_dict["url"],
                                cloud=jira_config_dict["cloud"],
                                cloud_id=jira_config_dict["cloud_id"],
                            )
                            LOGGER.error(
                                _auth_failure_hint(
                                    mode=mode, oauth_service_account=bool(jira_config_dict.get("client_secret"))
                                )
                            )
                            sys.exit(1)
                        if jira_error_string:
                            jira_error[file_name] = jira_error_string

    if jira_error:
        _jira_error = "\n\t".join([f"{key}: {val}" for key, val in jira_error.items()])
        LOGGER.error(f"Following Jira ids failed jira version/statuscheck: \n\t{_jira_error}\n")
        sys.exit(1)


if __name__ == "__main__":
    get_jira_mismatch()
