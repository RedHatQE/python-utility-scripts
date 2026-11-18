from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import sys
from functools import lru_cache
from typing import Any, NoReturn

import click
from jira import JIRA, Issue, JIRAError
from simple_logger.logger import get_logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from apps.utils import ListParamType, all_python_files, get_util_config

LOGGER = get_logger(name=__name__)

SA_GATEWAY_URL_TEMPLATE = "https://api.atlassian.com/ex/jira/{cloud_id}"
SA_GATEWAY_URL_PREFIX = "https://api.atlassian.com/ex/jira/"

AUTH_MODE_SERVICE_ACCOUNT = "service-account"
AUTH_MODE_CLOUD_BASIC = "cloud-basic"
AUTH_MODE_DATACENTER_PAT = "datacenter-pat"

# A credential rejected with 401/403 is not transient, so retrying it only delays the failure.
AUTH_ERROR_STATUS_CODES = (401, 403)


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


def build_jira_client(url: str, token: str, user: str, cloud: bool, cloud_id: str) -> JIRA:
    """Build a Jira client for the selected authentication mode.

    Args:
        url (str): Jira site url or API gateway url.
        token (str): Jira PAT, API token or service-account scoped API token.
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


def _auth_failure_hint(mode: str) -> str:
    """Return the credential guidance for the given auth mode.

    Args:
        mode (str): An AUTH_MODE_* constant.

    Returns:
        str: A one-line hint telling the user what to check or fix.
    """
    if mode == AUTH_MODE_SERVICE_ACCOUNT:
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
    LOGGER.error(f"Jira authentication failed (mode: {mode}, server: {server}): {exp.status_code} {detail}")
    if exp.status_code in AUTH_ERROR_STATUS_CODES:
        LOGGER.error(_auth_failure_hint(mode=mode))

    sys.exit(1)


def _check_jira_auth(jira_obj: JIRA, jira_config_dict: dict[str, Any]) -> None:
    """Log the resolved auth mode, server url and identity.

    Exits with code 1 on authentication failure.

    Args:
        jira_obj (JIRA): An already built Jira client.
        jira_config_dict (dict[str, Any]): Processed Jira configuration (url, token, cloud, cloud_id).
    """
    mode, server = _jira_auth_mode(
        url=jira_config_dict["url"], cloud=jira_config_dict["cloud"], cloud_id=jira_config_dict["cloud_id"]
    )

    try:
        myself = jira_obj.myself()
    except JIRAError as exp:
        if exp.status_code in AUTH_ERROR_STATUS_CODES:
            _report_jira_auth_error(exp=exp, jira_config_dict=jira_config_dict)

        # Not a credential problem (404, connection error, 5xx): report the real cause, not token advice.
        # No exc_info: JIRAError.__str__ appends the raw response body and headers (unbounded, may hold Set-Cookie).
        LOGGER.error(
            f"Jira credential check failed ({type(exp).__name__}, status: {exp.status_code}): "
            f"{_error_detail(text=exp.text, token=jira_config_dict['token'])}"
        )
        sys.exit(1)

    LOGGER.info(
        f"Jira authentication OK - mode: {mode}, server: {server}, "
        f"user: {myself.get('displayName')}, email: {myself.get('emailAddress')}, "
        f"accountId: {myself.get('accountId')}"
    )


@lru_cache
def _credential_rejected(jira_obj: JIRA) -> bool:
    """Return True when Jira rejects the credential itself, not just the single issue that failed.

    A per-issue 401/403 can be a project permission gap (a per-issue 403 is normal in Data Center mode), so the
    verdict is confirmed with myself(). It is cached so a scan calls myself() at most once.

    Args:
        jira_obj (JIRA): An already built Jira client.

    Returns:
        bool: True when myself() also fails with 401/403.
    """
    try:
        jira_obj.myself()
    except JIRAError as exp:
        return exp.status_code in AUTH_ERROR_STATUS_CODES

    return False


def _retryable_jira_error(exp: BaseException) -> bool:
    """Return True when a failed Jira call is worth retrying.

    A rejected credential (401/403) fails identically on every attempt, so retrying it only multiplies the
    delay for every issue in the repo.

    Args:
        exp (BaseException): The exception raised by the Jira call.

    Returns:
        bool: True for a JIRAError that is not an authentication error.
    """
    return isinstance(exp, JIRAError) and exp.status_code not in AUTH_ERROR_STATUS_CODES


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

    except JIRAError as exp:
        status_code = exp.status_code
        jira_error_string += (
            f"{jira_id} JiraError status code: {status_code}, details: {_error_detail(text=exp.text, token=token)}]."
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
) -> dict[str, Any]:
    # Process all the arguments passed from command line or config file or environment variable
    config_dict = get_util_config(util_name="pyutils-jira", config_file_path=config_file_path)
    url = url or config_dict.get("url", "")
    token = token or config_dict.get("token", "")
    user = user or config_dict.get("user", "")
    cloud = cloud if cloud is not None else config_dict.get("cloud", False)
    if cloud_id is None:
        # An explicit empty --cloud-id must win over both, so an unset option is what falls back.
        cloud_id = os.getenv("JIRA_CLOUD_ID") or config_dict.get("cloud_id", "")

    if cloud_id and not re.fullmatch(r"[A-Za-z0-9-]{8,64}", cloud_id):
        # The cloud id goes into the API gateway url we send the bearer token to, so validate it first.
        LOGGER.error(
            f"Invalid Jira cloud id (from --cloud-id/JIRA_CLOUD_ID/config 'cloud_id'): {cloud_id}. "
            'Pass --cloud-id "" to ignore it.'
        )
        sys.exit(1)

    if cloud_id and url and not url.startswith(SA_GATEWAY_URL_PREFIX):
        # Logged here only: resolve_jira_server is also called for the auth-mode lookup, which would double it.
        LOGGER.warning(f"cloud_id provided, discarding supplied Jira url {url} in favor of the API gateway url")

    # Same predicate the client uses, so validation never rejects a config the client would accept.
    service_account_auth = _is_service_account_auth(url=url, cloud_id=cloud_id)

    if not token:
        LOGGER.error("Jira token is required." if service_account_auth else "Jira url and token are required.")
        sys.exit(1)

    if service_account_auth:
        LOGGER.info(
            "Using Jira service-account (scoped API token) authentication against "
            f"{resolve_jira_server(url=url, cloud_id=cloud_id)}"
        )
        if cloud or user:
            LOGGER.info("Ignoring cloud/user: service-account auth uses only the bearer token.")
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
    "--check-auth",
    help="Verify the Jira credentials against /myself and exit.",
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
    check_auth: bool,
) -> None:
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    if cloud_id is None:
        # Resolve the env fallback before the guard so it sees the value the rest of the run will use.
        # An explicit empty --cloud-id is not None and must stay the opt-out, so it is never replaced here.
        cloud_id = os.getenv("JIRA_CLOUD_ID")

    if not (config_file_path or (token and (url or cloud_id))):
        LOGGER.error("Config file or token plus url (or cloud id) are required.")
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
    )

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
                        if status_code in AUTH_ERROR_STATUS_CODES and _credential_rejected(jira_obj=jira_obj):
                            # A dead credential breaks every issue identically: report once and stop the scan.
                            mode, _ = _jira_auth_mode(
                                url=jira_config_dict["url"],
                                cloud=jira_config_dict["cloud"],
                                cloud_id=jira_config_dict["cloud_id"],
                            )
                            LOGGER.error(_auth_failure_hint(mode=mode))
                            sys.exit(1)
                        if jira_error_string:
                            jira_error[file_name] = jira_error_string

    if jira_error:
        _jira_error = "\n\t".join([f"{key}: {val}" for key, val in jira_error.items()])
        LOGGER.error(f"Following Jira ids failed jira version/statuscheck: \n\t{_jira_error}\n")
        sys.exit(1)


if __name__ == "__main__":
    get_jira_mismatch()
