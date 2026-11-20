from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any, NoReturn
from urllib.parse import SplitResult, urlsplit, urlunsplit

import click
import requests
from jira import JIRA, Issue, JIRAError
from simple_logger.logger import get_logger
from tenacity import RetryError, retry, retry_if_exception, stop_after_attempt, wait_fixed

from apps.utils import ListParamType, all_python_files, get_util_config

LOGGER = get_logger(name=__name__)

SA_GATEWAY_URL_TEMPLATE = "https://api.atlassian.com/ex/jira/{cloud_id}"
SA_GATEWAY_URL_PREFIX = "https://api.atlassian.com/ex/jira/"
SA_GATEWAY_HOST = "api.atlassian.com"
# A cloud id goes into the API gateway url a bearer token is sent to, so its shape is validated before any request.
CLOUD_ID_PATTERN = r"[A-Za-z0-9-]{8,64}"
# A url inside free-form text ends at whitespace or at a delimiter; without that boundary a quoted url swallows the
# text around it (a JSON body's `","emailAddress":"a@b.c"`). A delimiter is not a "character no url can hold" - a
# url may legally carry `,()'[]` unencoded - so the class holds only the delimiters a url is quoted or wrapped with.
_URL_IN_TEXT_RE = re.compile(r"""[A-Za-z][A-Za-z0-9+.-]*://[^\s"'`<>{}]+""")
# The authority alone, for a url urlsplit refuses to parse: the scheme, then everything up to the last "@" before the
# path, query or fragment.
_AUTHORITY_USERINFO_RE = re.compile(r"(^[A-Za-z][A-Za-z0-9+.-]*://)[^/?#]*@")
# The jira library's session logs the full request url (which encodes the cloud id) at warning level on a retry.
_TRANSPORT_LOGGER_ROOTS = ("jira", "urllib3")

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


def _strip_userinfo(url: str) -> str:
    """Return `url` with its `user:password@` userinfo component dropped.

    The authority is parsed, never pattern-matched: a query or a path holding an "@" (`https://host?q=a@b`) is not a
    userinfo, while a password holding a space (`https://user:ab cd@host`) is one.

    Args:
        url (str): A single url.

    Returns:
        str: `url` without its userinfo component, unchanged when it has none. An authority that does not parse (see
            below) loses its userinfo too, so a credential is never returned by falling open.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        # urlsplit raises on an authority holding an unmatched bracket. Brackets survive requests' requote_uri
        # unencoded, so such a password still works with the client: returning the url unchanged would print the
        # credential here and would hide it from the transport filter, whose `has_userinfo` compares this stripped
        # form. The authority is matched instead, so the credential is dropped even when it cannot be parsed.
        return _AUTHORITY_USERINFO_RE.sub(r"\1", url)

    if "@" not in parts.netloc:
        return url

    # The LAST "@" is the userinfo delimiter (urlsplit and requests agree), so a password holding one is dropped
    # whole instead of leaving its tail (https://user:p@ss@host -> "ss@host").
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))


def _userinfo_fragment(url: str) -> str:
    """Return the `user:password` part of the userinfo of `url`, or "" when it carries none.

    For registering the one credential the run already knows as a literal secret: the text mask ends a url at a quote,
    so a password holding one (`https://user:pa'ss@host`) is not delimited there and would reach every log line.

    Args:
        url (str): A single url.

    Returns:
        str: Everything between the scheme and the last "@" of the authority, or "" when the url has no userinfo. An
            authority `urlsplit` refuses to parse (an unmatched bracket) is matched by `_AUTHORITY_USERINFO_RE`
            instead, exactly like `_strip_userinfo` does, because such a url still reaches the client.
    """
    try:
        authority = urlsplit(url).netloc
    except ValueError:
        match = _AUTHORITY_USERINFO_RE.search(url)
        authority = match.group(0)[len(match.group(1)) :] if match else ""

    # The LAST "@" is the userinfo delimiter (urlsplit and requests agree), so a password holding one stays whole.
    return authority.rpartition("@")[0]


def _mask_userinfo(text: str) -> str:
    """Return `text` with the userinfo component of every url it embeds dropped.

    For a url passed on its own use `_strip_userinfo`, which parses the whole value and therefore always strips. This
    text form has to delimit each url first: it ends at whitespace or at a quote/bracket, so a credential holding one
    of those cannot be delimited here and is not masked in free-form text (a url passed whole still is).

    Args:
        text (str): A log line, a message or an error body that may embed a url.

    Returns:
        str: `text` with the userinfo of each embedded url dropped, unchanged when there is none.
    """
    return _URL_IN_TEXT_RE.sub(lambda match: _strip_userinfo(url=match.group(0)), text)


def _gateway_url_parts(url: str) -> SplitResult | None:
    """Return the parsed parts of an Atlassian API gateway `url`, or None when `url` is not one.

    The comparison runs on the parsed form, so a scheme or host in another case (`HTTPS://API.atlassian.com/...`) and
    the default port (`:443`) select the same mode, and a query that merely mentions the gateway
    (`https://evil.example?a=@api.atlassian.com/ex/jira/<id>`) cannot pass for one.

    Args:
        url (str): Jira site url or API gateway url; may be empty when only a cloud id is configured.

    Returns:
        SplitResult | None: The parts of the gateway url (userinfo dropped), or None when `url` is not one. The host
            root itself (`https://api.atlassian.com`, with or without its trailing slash) counts as a gateway url: it
            carries no cloud id, and that is what the caller's missing-id guard needs to see.
    """
    # url is empty (or, from an empty YAML value, None) when only a cloud id is configured, so it is never trusted to
    # be a str here. The userinfo is dropped first: a gateway url carrying a credential still is a gateway url.
    try:
        parts = urlsplit(_strip_userinfo(url=url or ""))
        port = parts.port
    except ValueError:
        # A malformed authority (urlsplit itself) or a malformed port is not a gateway url either.
        return None

    # requests resolves a trailing-dot FQDN to the same host, so the dot is dropped before the comparison.
    if (
        parts.scheme.lower() != "https"
        or (parts.hostname or "").rstrip(".") != SA_GATEWAY_HOST
        or port not in (None, 443)
    ):
        return None

    # The gateway root, with or without its trailing slash, and anything below it. Only an exact `/ex/jira` segment
    # counts, or `/ex/jirafoo/<id>` would pass here while _effective_cloud_id derives no id from it.
    path = parts.path.lower()
    return parts if path in ("", "/", "/ex/jira") or path.startswith("/ex/jira/") else None


def _is_gateway_url(url: str) -> bool:
    """Return True when `url` points at the Atlassian API gateway, with or without its trailing slash.

    The bare form without the trailing slash is the likeliest typo, so it must still be recognised as a gateway url:
    mistaken for a Data Center url it silently switches the run to PAT mode against the gateway root.

    Args:
        url (str): Jira site url or API gateway url.

    Returns:
        bool: True for the gateway host itself (with or without its trailing slash), for the gateway url and for any
            url below it (a cloud id, or a deeper path).
    """
    return _gateway_url_parts(url=url) is not None


def _is_valid_cloud_id(cloud_id: str) -> bool:
    """Return True when `cloud_id` has the shape of an Atlassian cloud id.

    The same predicate guards the --cloud-id value and the id derived from a gateway url: both end up in the API
    gateway url the bearer token is sent to.

    Args:
        cloud_id (str): A configured or derived Atlassian cloud id.

    Returns:
        bool: True for letters, digits and dashes, 8 to 64 characters.
    """
    return bool(re.fullmatch(CLOUD_ID_PATTERN, cloud_id))


def _is_service_account_auth(url: str, cloud_id: str) -> bool:
    """Return True when service-account (Bearer) auth should be used.

    Args:
        url (str): Jira site url or API gateway url.
        cloud_id (str): Atlassian Cloud id; when set, service-account auth is used.

    Returns:
        bool: True if `cloud_id` is set or `url` already is an API gateway url.
    """
    return bool(cloud_id) or _is_gateway_url(url=url)


def _jira_browse_url(url: str) -> str:
    """Return the url used for /browse/<key> link matching, or "" when `url` is an API gateway url.

    Source-code references point at the Jira site (e.g. https://redhat.atlassian.net/browse/KEY), never at the
    API gateway, so the site part is dropped for service-account auth to keep the match host-agnostic.

    Args:
        url (str): Jira site url or API gateway url.

    Returns:
        str: `url` unchanged, or "" for an API gateway url.
    """
    # url is empty (or, from an empty YAML value, None) when only a cloud id is configured.
    return "" if not url or _is_gateway_url(url=url) else url


def _mask_cloud_id(cloud_id: str) -> str:
    """Return a fixed placeholder for `cloud_id`, never any part of the value itself.

    Args:
        cloud_id (str): Atlassian Cloud id; only its presence matters for the log line.

    Returns:
        str: The literal "<cloud-id>".
    """
    return "<cloud-id>"


def _mask_server(server: str) -> str:
    """Return `server` with the cloud id and any userinfo masked, for human-readable output only.

    The real server url is still what the client talks to; masking applies to log lines and messages only.

    Args:
        server (str): Jira site url or API gateway url.

    Returns:
        str: `https://api.atlassian.com/ex/jira/<cloud-id>` for a gateway url carrying a cloud id (the id is not
            echoed), else `server` unchanged apart from a dropped userinfo component. The bare gateway host carries no
            id, so it is returned as it is instead of gaining a path it never had.
    """
    server = _strip_userinfo(url=server)
    if _gateway_url_parts(url=server) is None:
        return server

    cloud_id = _effective_cloud_id(url=server, cloud_id="")
    if not cloud_id:
        return server

    return f"{SA_GATEWAY_URL_PREFIX}{_mask_cloud_id(cloud_id=cloud_id)}"


class _SecretMaskingFilter(logging.Filter):
    """Mask the cloud id, the bearer token and any url userinfo in a jira/urllib3 record."""

    def __init__(self) -> None:
        super().__init__()
        # value -> placeholder. One source of truth: this dict is both the dedupe and the list the masking walks, so
        # clearing it cannot leave a stale entry behind that silently swallows a re-registered secret.
        self.secrets: dict[str, str] = {}

    def add_secret(self, value: str, mask: str) -> None:
        """Add one secret to mask, ignoring an empty value and keeping the placeholder of a known one.

        Args:
            value (str): The secret to replace wherever it is logged.
            mask (str): The placeholder that replaces it.
        """
        if value:
            self.secrets.setdefault(value, mask)

    def clear_secrets(self) -> None:
        """Forget every registered secret, so a reset filter masks nothing."""
        self.secrets.clear()

    def _masked_text(self, text: str) -> str:
        """Return `text` with its url userinfo and every registered secret masked."""
        # The transport log line quotes the request url verbatim, so a url carrying a credential in its userinfo
        # component (scheme://<user>:<secret>@host) leaks it on every retry, cloud id or not.
        text = _mask_userinfo(text=text)
        for value, mask in self.secrets.items():
            text = text.replace(value, mask)

        return text

    def _masked(self, value: Any) -> Any:
        """Return `value` masked, keeping its type so the record still formats as it would have."""
        if isinstance(value, bytes):
            # python-jira logs the raw response body as bytes (LOG.debug("response.body:\n%s", response.content)), so
            # the text masking runs on the decoded form and the result is re-encoded to keep the payload's type.
            # surrogateescape round-trips a non-UTF-8 body byte for byte.
            return self._masked_text(value.decode(errors="surrogateescape")).encode(errors="surrogateescape")

        if isinstance(value, str):
            return self._masked_text(value)

        # Any other payload (an exception, a dict) only leaks through its string form, so it is replaced by that
        # form when masking changes it and passed through untouched otherwise (%d/%r keep working on an innocent arg).
        rendered = str(value)
        masked = self._masked_text(rendered)
        return masked if masked != rendered else value

    def _masked_exception(self, record: logging.LogRecord) -> None:
        """Mask the traceback text too, so an exception message cannot carry a secret past the filter."""
        # urllib3/connection.py logs its exceptions with exc_info=True and the request url in the message, and a
        # traceback of a url-parse failure quotes the same url, so the rendered traceback needs the same mask.
        exc_text = record.exc_text or (logging.Formatter().formatException(record.exc_info) if record.exc_info else "")
        if exc_text:
            record.exc_text = self._masked_text(exc_text)

    def filter(self, record: logging.LogRecord) -> bool:
        # Only the transport loggers are patched: a filter on a root handler otherwise rewrites this module's own
        # lines too, and the placeholder is substituted again wherever the text says "--cloud-id".
        if record.name.split(".")[0] not in _TRANSPORT_LOGGER_ROOTS:
            return True

        record.msg = self._masked(record.msg)

        if isinstance(record.args, tuple):
            record.args = tuple(self._masked(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            # Only the values: the keys are format specifiers, so masking one would break the record's formatting.
            record.args = {key: self._masked(arg) for key, arg in record.args.items()}

        self._masked_exception(record=record)
        return True


_LOG_FILTER = _SecretMaskingFilter()
_FILTER_INSTALLED = False
_PREVIOUS_RECORD_FACTORY: Callable[..., logging.LogRecord] | None = None
_INSTALLED_RECORD_FACTORY: Callable[..., logging.LogRecord] | None = None


def _exception_detail(exp: BaseException, token: str = "", cloud_id: str = "") -> str:
    """Return `exp` rendered single-line, bounded and stripped of the token, the cloud id and any url userinfo.

    A transport exception quotes the request url, which encodes the cloud id, so it can never be interpolated raw on
    this module's own logger: the installed record filter only patches the jira/urllib3 loggers (a filter on this
    module's lines would rewrite its own `--cloud-id` placeholders).

    Args:
        exp (BaseException): The exception whose message is about to be logged.
        token (str): The configured Jira token; redacted from the message.
        cloud_id (str): The resolved cloud id; redacted from the message.

    Returns:
        str: The message with the token, the cloud id and any url userinfo masked, collapsed and capped at 200 chars.
    """
    return _error_detail(text=str(exp), token=token, cloud_id=cloud_id)


def _install_log_secret_filter(cloud_id: str = "", server: str = "", token: str = "") -> None:
    """Mask `cloud_id`, the bearer `token` and any url userinfo in the jira/urllib3 transport logs.

    The client's own retry logs are below this module, so its masking never reaches them. The filter only masks and
    never drops a record: records keep their level, and the client keeps its retries. It is wrapped around the record
    factory rather than added to a logger or handler, because those never see a record logged by another logger
    without a handler of its own, which is exactly the jira library's situation here. The factory is installed once;
    every later call only widens the secret list, so a second install cannot nest another factory over the first.

    Args:
        cloud_id (str): The cloud id the run resolved; may be empty when only the url carries a credential.
        server (str): The server url the client talks to; a url supplying a userinfo needs the filter too.
        token (str): The configured Jira token or the minted access token; a real (>=8 characters) one is masked.
    """
    # A config file with a null `token:` hands back None: a non-str is treated as absent instead of trusted to be one.
    token = token if isinstance(token, str) else ""
    has_userinfo = _strip_userinfo(url=server) != server
    # A run with no cloud id, no real token and no userinfo url has nothing to mask.
    if not (cloud_id or has_userinfo or len(token) >= 8):
        return

    _LOG_FILTER.add_secret(value=cloud_id, mask=_mask_cloud_id(cloud_id=cloud_id))
    # Only a real token is masked: replacing a short value would corrupt unrelated words
    # ("connection" -> "con***ion"), exactly like _error_detail's guard.
    if len(token) >= 8:
        _LOG_FILTER.add_secret(value=token, mask="***")
    if has_userinfo:
        # The url in a log line ends at a quote, so a password holding one is not masked by the text regex. The
        # configured userinfo is known here, so it is registered as a literal: masking no longer depends on the regex.
        _LOG_FILTER.add_secret(value=_userinfo_fragment(url=server), mask="***")

    global _FILTER_INSTALLED, _PREVIOUS_RECORD_FACTORY, _INSTALLED_RECORD_FACTORY
    if _FILTER_INSTALLED:
        return

    previous_factory = logging.getLogRecordFactory()
    _PREVIOUS_RECORD_FACTORY = previous_factory
    _FILTER_INSTALLED = True

    def record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        _LOG_FILTER.filter(record=record)
        return record

    _INSTALLED_RECORD_FACTORY = record_factory
    logging.setLogRecordFactory(record_factory)


def _reset_log_secret_filter() -> None:
    """Undo the installed record factory and forget the masked secrets, so a later run starts clean.

    The factory is restored only while ours is still the current one, so a third-party factory installed after ours
    is left in place: our filter then simply runs secretless, because the secrets are cleared here either way. The
    install-once guard in `_install_log_secret_filter` is a separate concern: it only keeps a second install from
    nesting a factory over the first.
    """
    global _FILTER_INSTALLED, _PREVIOUS_RECORD_FACTORY, _INSTALLED_RECORD_FACTORY
    if _INSTALLED_RECORD_FACTORY is not None and logging.getLogRecordFactory() is _INSTALLED_RECORD_FACTORY:
        logging.setLogRecordFactory(_PREVIOUS_RECORD_FACTORY or _INSTALLED_RECORD_FACTORY)

    _FILTER_INSTALLED = False
    _PREVIOUS_RECORD_FACTORY = None
    _INSTALLED_RECORD_FACTORY = None
    _LOG_FILTER.clear_secrets()


def resolve_jira_server(url: str, cloud_id: str) -> str:
    """Resolve the Jira API server URL for the given url/cloud id.

    A service-account scoped API token only works against the Atlassian API gateway
    (https://api.atlassian.com/ex/jira/<cloud_id>), never against the site url.

    Args:
        url (str): Jira site url or an already resolved API gateway url.
        cloud_id (str): Atlassian Cloud id; when set the gateway url is returned.

    Returns:
        str: The API gateway url if `cloud_id` is set or `url` already is a gateway url, else `url` unchanged. A
            gateway url supplies its own id, so a path below it (`.../ex/jira/<id>/rest/api/2`) is dropped: it would
            otherwise be taken for the server and doubled by the path python-jira appends itself.
    """
    # The gateway url is rebuilt from the id alone, so a supplied gateway url only ever contributes its id.
    cloud_id = cloud_id or _effective_cloud_id(url=url, cloud_id="")
    if cloud_id:
        return SA_GATEWAY_URL_TEMPLATE.format(cloud_id=cloud_id)

    return url


def _cloud_id_from_env() -> str:
    """Return the Atlassian Cloud id from the environment, or "" when neither variable is set.

    Returns:
        str: `JIRA_CLOUD_ID`, falling back to its OAuth alias `ATLASSIAN_OAUTH_CLOUD_ID`.
    """
    return os.getenv("JIRA_CLOUD_ID") or os.getenv("ATLASSIAN_OAUTH_CLOUD_ID") or ""


def _client_id_from_env() -> str:
    """Return the OAuth 2.0 client id from the environment, or "" when neither variable is set.

    Returns:
        str: `JIRA_SA_CLIENT_ID`, falling back to its alias `ATLASSIAN_OAUTH_CLIENT_ID`.
    """
    return os.getenv("JIRA_SA_CLIENT_ID") or os.getenv("ATLASSIAN_OAUTH_CLIENT_ID") or ""


def _client_secret_from_env() -> str:
    """Return the OAuth 2.0 client secret from the environment, or "" when neither variable is set.

    Returns:
        str: `JIRA_SA_CLIENT_SECRET`, falling back to its alias `JIRA_OAUTH_CLIENT_SECRET`.
    """
    return os.getenv("JIRA_SA_CLIENT_SECRET") or os.getenv("JIRA_OAUTH_CLIENT_SECRET") or ""


def _effective_cloud_id(url: str, cloud_id: str) -> str:
    """Return the cloud id the run actually talks to, whichever source supplied it.

    Args:
        url (str): Jira site url or API gateway url.
        cloud_id (str): Configured Atlassian Cloud id; empty when none is configured.

    Returns:
        str: `cloud_id`, the bare id an API gateway url already encodes, or "" when there is neither.
    """
    if cloud_id:
        return cloud_id

    # The same parsed form _is_gateway_url and _mask_server use, so all three agree on what a gateway url is.
    parts = _gateway_url_parts(url=url)
    if parts is None:
        return ""

    # Only the first path segment after the prefix, and only when it is a cloud id: a traversal ("../../admin") or a
    # 1-character segment would otherwise pass the missing-id guard and then shred every error body it is matched in.
    match = re.match(r"/ex/jira/([^/]*)", parts.path, re.IGNORECASE)
    if not match or not _is_valid_cloud_id(cloud_id=match.group(1)):
        return ""

    return match.group(1)


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


def _error_detail(text: str | None, *, token: str = "", cloud_id: str = "") -> str:
    """Return a single-line, bounded and redacted rendering of a Jira error response body.

    The body is always kept but is bounded and stripped of the token, the cloud id and any url userinfo: none must
    reach a log, while the body is what makes a permission gap (e.g. a per-issue 403 with a healthy credential)
    diagnosable.

    A url passed on its own (`--url`, a server named in a message, the probe url) is parsed whole and its userinfo is
    always dropped, even when the authority does not parse. Inside free-form text a url ends at whitespace or at a
    quote/bracket, so a credential containing one of those cannot be delimited here and is not stripped from a body
    (the whole-url path still strips it).

    Redaction happens on the full body, before truncation: an Atlassian scoped token is ~192 characters, so a
    200-character window can be almost entirely secret, and a truncation that runs first leaves a token longer than
    the window (or split by the cut) unmatched and therefore leaked. A second pass on the collapsed form is required
    because a secret split by newlines (a line-wrapping proxy echoing it back) is unmatched on the raw body, so it must
    be re-matched after the collapse with whitespace allowed between its characters.

    Args:
        text (str | None): The raw response body from Jira.
        token (str): The configured Jira token; every occurrence is redacted. Empty or shorter than 8 characters
            means nothing is redacted.
        cloud_id (str): The cloud id the run resolved (from --cloud-id or a gateway url); every occurrence is
            replaced by '<cloud-id>'.

    Returns:
        str: The collapsed body truncated to 200 characters, with a real (>=8 chars) token replaced by '***', the
            cloud id replaced by '<cloud-id>' and the configured url userinfo (registered as a literal secret by
            `_install_log_secret_filter`) replaced by '***'.
    """
    # Userinfo is dropped before any bounding: a url in the body can embed the credential itself.
    full = _mask_userinfo(text=str(text or ""))

    # Only a real token is worth length-checking: any string under 8 characters cannot be one, and replacing a short
    # value (e.g. a dummy "t") would corrupt unrelated words ("connection" -> "connec***ion").
    secrets = [(token, "***")] if len(token) >= 8 else []
    # A cloud id only reaches here validated, so replacing it cannot shred the body.
    if cloud_id:
        secrets.append((cloud_id, _mask_cloud_id(cloud_id=cloud_id)))
    # This module's own lines bypass the record filter, so the literals it registered (the configured url userinfo,
    # which the text mask above cannot delimit when its password holds a quote) are applied here as well.
    secrets.extend(_LOG_FILTER.secrets.items())

    for value, mask in secrets:
        full = full.replace(value, mask)

    detail = " ".join(full.split())

    # The collapse joins split pieces with a space, so a secret broken by newlines is still not contiguous: re-match it
    # with any whitespace between its characters, otherwise both halves reach the log. A short value keeps the plain
    # replace above: a pattern of 1-7 characters with `\s*` between each one matches unrelated words.
    for value, mask in secrets:
        if len(value) >= 8:
            detail = re.sub(r"\s*".join(map(re.escape, value)), mask, detail)

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
    detail = _error_detail(
        text=exp.text,
        token=jira_config_dict["token"],
        cloud_id=_effective_cloud_id(url=jira_config_dict["url"], cloud_id=jira_config_dict["cloud_id"]),
    )
    LOGGER.error(
        f"Jira authentication failed (mode: {mode}, server: {_mask_server(server=server)}): {exp.status_code} {detail}"
    )
    if exp.status_code in AUTH_ERROR_STATUS_CODES:
        LOGGER.error(_auth_failure_hint(mode=mode, oauth_service_account=bool(jira_config_dict.get("client_secret"))))

    sys.exit(1)


def _probe_credential(
    jira_obj: JIRA, *, service_account: bool = False
) -> tuple[JIRAError | None, dict[str, Any] | None, bool]:
    """Probe the credential with myself(), falling back to the project endpoint for a scope-limited one.

    A service-account credential (an OAuth 2.0 pair as much as a pre-minted scoped API token) usually carries only
    read:jira-work/write:jira-work, which /myself is not part of, so a 401/403 there proves nothing about the
    credential. The project endpoint decides instead, but only a non-empty project list proves the token is honoured
    (that endpoint is also readable anonymously, which --check-auth rules out with a second, header-less request) and
    only its own 401/403 rejects the credential.

    Args:
        jira_obj (JIRA): An already built Jira client.
        service_account (bool): True for any service-account (Bearer) auth, whatever minted the token.

    Returns:
        tuple[JIRAError | None, dict[str, Any] | None, bool]: The error that failed the probe (None on success), the
            identity from myself(), which is None when a scope-limited credential was verified via projects(), and
            whether the credential itself was rejected (True only when a 401/403 is decisive).
    """
    try:
        return None, jira_obj.myself(), False
    except JIRAError as exp:
        if not (service_account and exp.status_code in AUTH_ERROR_STATUS_CODES):
            return exp, None, exp.status_code in AUTH_ERROR_STATUS_CODES

        try:
            projects = jira_obj.projects()
        except JIRAError as projects_exp:
            if projects_exp.status_code in AUTH_ERROR_STATUS_CODES:
                # Both endpoints reject the credential; the /myself error is the original one to report.
                return exp, None, True

            # Any other failure is an outage, not a rejection: reporting the /myself 401/403 for it would blame
            # the credential for a 503 or a connection error.
            return projects_exp, None, False

        # An empty list proves nothing about the bearer: the operation is also reachable anonymously.
        if not projects:
            return exp, None, False

        return None, None, False


def _rest_api_version(jira_obj: JIRA) -> str:
    """Return the REST API version the client itself uses (python-jira's default is "2").

    The anonymous project probe must hit the same version the authenticated project list went to, or it answers about
    a different endpoint: a v3 that is not served or not anonymously readable would report "not anonymous" and turn
    the guard into the false positive it exists to prevent.

    Args:
        jira_obj (JIRA): An already built Jira client.

    Returns:
        str: The client's `rest_api_version` option, or "2" when it carries none.
    """
    return getattr(jira_obj, "_options", {}).get("rest_api_version") or "2"


def _project_endpoint_is_anonymous(
    server: str, rest_api_version: str = "2", *, token: str = "", cloud_id: str = ""
) -> bool:
    """Return True when the project endpoint returns a non-empty project list with no Authorization header.

    That endpoint is readable anonymously on many sites, so a non-empty authenticated list proves nothing on its
    own: the same request without the header is what decides whether the bearer was honoured at all.

    Args:
        server (str): The resolved Jira API server url.
        rest_api_version (str): The REST API version the authenticated client itself uses; see `_rest_api_version`.
        token (str): The configured Jira token, redacted from a failure that quotes the request.
        cloud_id (str): The resolved cloud id, redacted from a failure that quotes the request url.

    Returns:
        bool: True when the unauthenticated request succeeds with a non-empty project list.

    Note:
        The probe must be header-less, and requests adds `Authorization` from two sources that `headers={}` cannot
        stop: the url userinfo, and `get_netrc_auth()` for a `~/.netrc` entry of the host. The url is rebuilt without
        its userinfo and the header `prepare_request` injected from netrc is popped, while the environment (CA bundle
        and proxy variables) is still merged into the send: a proxy or an extra CA bundle is how the site is reached
        at all in those environments, and dropping it would silently turn the guard into a no-op.

        Redirects are not followed: `resolve_redirects` calls `rebuild_auth`, which re-injects the netrc credential
        (of this host or of the redirect target's) on the next hop, so following one would put a credential on the
        wire. A 3xx is no evidence of anonymous readability anyway, which is exactly what the False verdict says: the
        status code is matched as a 2xx range rather than through `Response.ok`, which is also True for a 3xx.
    """
    if not server:
        return False

    url = f"{_strip_userinfo(url=server).rstrip('/')}/rest/api/{rest_api_version}/project"
    session = requests.Session()
    try:
        # Prepared explicitly so the header netrc injected can be dropped before the request leaves the process.
        request = session.prepare_request(requests.Request(method="GET", url=url))
        request.headers.pop("Authorization", None)
        # trust_env stays at its default, so REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE and the proxy variables are honoured.
        settings = session.merge_environment_settings(url, {}, None, None, None)
        # A redirect would re-inject a netrc credential on the next hop; a 3xx is not a header-less 2xx answer either.
        response = session.send(request=request, timeout=15, allow_redirects=False, **settings)
    except requests.RequestException as exp:
        # An unreachable anonymous probe is no evidence of anonymous readability, so the authenticated list stands.
        # WARNING, not DEBUG: a guard that could not run is a degraded guard, not a detail.
        LOGGER.warning(
            f"Anonymous project probe could not run ({type(exp).__name__}): "
            f"{_exception_detail(exp=exp, token=token, cloud_id=cloud_id)}"
        )
        return False
    finally:
        session.close()

    # Response.ok is True for a 3xx too, and redirects are not followed here, so a redirect body would otherwise reach
    # the parser: only a 2xx is an answer.
    if not 200 <= response.status_code < 300:
        LOGGER.debug(f"Anonymous project probe returned HTTP {response.status_code}: not anonymously readable")
        return False

    try:
        projects = response.json()
    except ValueError:
        LOGGER.debug("Anonymous project probe returned a non-JSON body: not anonymously readable")
        return False

    return isinstance(projects, list) and bool(projects)


def _report_jira_outage(
    prefix: str,
    exp: JIRAError | requests.RequestException,
    jira_config_dict: dict[str, Any],
    cloud_id: str,
) -> NoReturn:
    """Log a non-auth Jira failure as an outage in the single-line documented shape and exit non-zero.

    A connection error carries no status code, so `status: None` renders. No traceback and no credential advice:
    the credential is not what failed.

    Args:
        prefix (str): The failure line that names the phase (client construction, credential check).
        exp (JIRAError | requests.RequestException): The error raised by the failing Jira call.
        jira_config_dict (dict[str, Any]): Processed Jira configuration (url, token, cloud, cloud_id).
        cloud_id (str): The resolved cloud id, redacted from any error body.
    """
    # Only the response body for a JIRAError: its __str__ appends the raw body and headers (unbounded, may hold
    # Set-Cookie). A connection error has no body, so its own message is all there is.
    detail = _error_detail(
        text=exp.text if isinstance(exp, JIRAError) else str(exp),
        token=jira_config_dict["token"],
        cloud_id=cloud_id,
    )
    LOGGER.error(f"{prefix} ({type(exp).__name__}, status: {getattr(exp, 'status_code', None)}): {detail}")
    sys.exit(1)


def _check_jira_auth(jira_obj: JIRA, jira_config_dict: dict[str, Any]) -> None:
    """Log the resolved auth mode, server url and identity.

    The credential is probed with myself(); a scope-limited service-account credential is 401/403 there, so the
    project endpoint is probed instead and the success line reports project visibility rather than identity. A probe
    that proves nothing either way is reported as unverified, never as success: that includes a project list that the
    same endpoint also returns without any Authorization header.

    Exits with code 1 on authentication failure or an unverifiable credential.

    The anonymous project probe is a tripwire: in service-account mode the server is always the canonical API gateway
    url, so the header-less request can only ask the gateway's project path and can never be answered with a 2xx
    project list - the branch cannot fire against Atlassian with the current server resolution, and the tests reach it
    by mocking the premise. The guard still costs one header-less request per scope-limited --check-auth (which a
    proxy or a TLS-intercepting environment may turn into a WARNING).

    Args:
        jira_obj (JIRA): An already built Jira client.
        jira_config_dict (dict[str, Any]): Processed Jira configuration (url, token, cloud, cloud_id, client_secret).
    """
    mode, server = _jira_auth_mode(
        url=jira_config_dict["url"], cloud=jira_config_dict["cloud"], cloud_id=jira_config_dict["cloud_id"]
    )
    cloud_id = _effective_cloud_id(url=jira_config_dict["url"], cloud_id=jira_config_dict["cloud_id"])

    try:
        exp, myself, rejected = _probe_credential(
            jira_obj=jira_obj,
            service_account=_is_service_account_auth(
                url=jira_config_dict["url"], cloud_id=jira_config_dict["cloud_id"]
            ),
        )
    except requests.RequestException as request_exp:
        # An unreachable API, however the transport surfaced it, is an outage and not a rejected credential.
        _report_jira_outage(
            prefix="Jira credential check failed",
            exp=request_exp,
            jira_config_dict=jira_config_dict,
            cloud_id=cloud_id,
        )
    if exp is not None:
        if rejected:
            _report_jira_auth_error(exp=exp, jira_config_dict=jira_config_dict)

        if exp.status_code in AUTH_ERROR_STATUS_CODES:
            # /myself is out of this credential's scopes and no project is visible, so nothing proves the token.
            LOGGER.error(
                "Jira credential could not be verified: the current-user endpoint (/myself) is out of this "
                "credential's scopes and no projects are visible, so neither endpoint proves the credential is valid."
            )
        else:
            # Not a credential problem (404, connection error, 5xx): report the real cause, not token advice.
            _report_jira_outage(
                prefix="Jira credential check failed",
                exp=exp,
                jira_config_dict=jira_config_dict,
                cloud_id=cloud_id,
            )
        sys.exit(1)

    if myself is None:
        if _project_endpoint_is_anonymous(
            server=server,
            rest_api_version=_rest_api_version(jira_obj=jira_obj),
            token=jira_config_dict["token"],
            cloud_id=cloud_id,
        ):
            # The same list without a bearer: a dead token would look exactly as healthy, so nothing was proven.
            LOGGER.error(
                "Jira credential could not be verified: the project endpoint returns the same non-empty project "
                "list without an Authorization header, so a non-empty list does not prove this credential is "
                "honoured."
            )
            sys.exit(1)

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
def _credential_probe_verdict(
    jira_obj: JIRA, service_account: bool = False
) -> tuple[bool, requests.RequestException | None]:
    """Return the cached `(rejected, outage)` verdict of the credential probe.

    Keyed on the client and the flavour only: the redaction arguments the caller passes are not evidence about the
    credential, so they must not change which verdict is reused, and the plaintext token is never a cache key.

    Args:
        jira_obj (JIRA): An already built Jira client.
        service_account (bool): True for any service-account (Bearer) auth, whatever minted the token.

    Returns:
        tuple[bool, requests.RequestException | None]: Whether the credential is rejected, and the transport error
            that left the probe unable to decide (None when it decided).
    """
    try:
        _, _, rejected = _probe_credential(jira_obj=jira_obj, service_account=service_account)
    except requests.RequestException as exp:
        # An unreachable API is an outage, not a rejection: the per-issue failures are reported instead of the scan
        # being aborted with a credential hint that would blame the wrong thing.
        return False, exp

    return rejected, None


def _credential_rejected(jira_obj: JIRA, service_account: bool = False, *, token: str = "", cloud_id: str = "") -> bool:
    """Return True when Jira rejects the credential itself, not just the single issue that failed.

    A per-issue 401/403 can be a project permission gap (a per-issue 403 is normal in Data Center mode) or, for a
    scope-limited service-account credential, the missing current-user scope, so the verdict is confirmed with the
    same probe --check-auth uses. The probe (not this wrapper, which only adds the redacted outage message) is cached
    per client and flavour, so a scan probes at most once, and the caller clears that cache on a later 401 so a
    bearer expiring mid-scan is not hidden by an earlier 403 verdict.

    Args:
        jira_obj (JIRA): An already built Jira client.
        service_account (bool): True for any service-account (Bearer) auth, whatever minted the token.
        token (str): The configured Jira token, redacted from a failure that quotes the request.
        cloud_id (str): The resolved cloud id, redacted from a failure that quotes the request url.

    Returns:
        bool: True only when the 401/403 is decisive - for a Bearer credential that means BOTH probe endpoints
            reject it. False for an empty project list, for an outage and for an unreachable API. The two-endpoint
            probe is the same one --check-auth uses, WITHOUT its extra anonymous cross-check.
    """
    misses_before = _credential_probe_verdict.cache_info().misses
    rejected, outage = _credential_probe_verdict(jira_obj=jira_obj, service_account=service_account)
    # A cached verdict reuses the same exception object, so logging on a hit would repeat the line once per failing
    # issue in a scan: only the probe that actually ran reports the outage.
    if outage is not None and _credential_probe_verdict.cache_info().misses != misses_before:
        LOGGER.warning(
            f"Jira credential probe failed ({type(outage).__name__}), treating it as an outage: "
            f"{_exception_detail(exp=outage, token=token, cloud_id=cloud_id)}"
        )

    return rejected


# The once-per-scan reset clears the real (cached) verdict; the wrapper only adds the redacted outage message to it.
_credential_rejected.cache_clear = _credential_probe_verdict.cache_clear  # type: ignore[attr-defined]


def _retryable_jira_error(exp: BaseException) -> bool:
    """Return True when a failed Jira call is worth retrying.

    Only a transient failure is retried: a `JIRAError` carrying no status code at all, or a 5xx. Any 4xx is permanent
    (a rejected credential, a missing issue, a bad request), so retrying it only multiplies the delay for every issue
    in the repo. A raw `requests.Timeout`/`ConnectionError` is not retried here: it is reported as that issue's own
    finding instead. A 429 (and a 503 carrying `Retry-After`) is already retried by python-jira's ResilientSession,
    with its own backoff and jitter and `Retry-After` honoured best-effort, so a 429 is left to the library. How long
    it sleeps there depends on the installed python-jira version, so no fixed formula or minimum wait is promised
    here. A 503 carrying `Retry-After` is still retried by both layers, serially, and a 503 without one only by this
    one - a pre-existing cost the code still incurs.

    Args:
        exp (BaseException): The exception raised by the Jira call.

    Returns:
        bool: True for a JIRAError without a status code or with a 5xx.
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
    cloud_id: str = "",
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
        cloud_id (str): The resolved cloud id, redacted from any error body.

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

    except (JIRAError, RetryError, requests.RequestException) as exp:
        # tenacity wraps an exhausted retry in RetryError and re-raises a raw transport error as it is: either one
        # left unwrapped escapes the worker future as a traceback and aborts the whole scan.
        cause: BaseException | None = exp.last_attempt.exception() if isinstance(exp, RetryError) else exp
        jira_exp = cause if isinstance(cause, JIRAError) else None
        status_code = jira_exp.status_code if jira_exp else None
        if jira_exp:
            jira_error_string += (
                f"{jira_id} JiraError status code: {status_code}, details: "
                f"{_error_detail(text=jira_exp.text, token=token, cloud_id=cloud_id)}."
            )
        else:
            # A transport error is not a JIRAError, so it is reported per issue and the scan keeps its findings.
            jira_error_string += (
                f"{jira_id} Jira connection failed ({type(cause).__name__}): "
                f"{_error_detail(text=str(cause), token=token, cloud_id=cloud_id)}."
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
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, Any]:
    # Process all the arguments passed from command line or config file or environment variable
    config_dict = get_util_config(util_name="pyutils-jira", config_file_path=config_file_path)
    url = url or config_dict.get("url", "")
    # A YAML `token:` parses to None, and a config file holding a cloud id and an OAuth pair may legitimately leave
    # the pre-minted token out: the null is normalised here so every consumer sees a str, and the run still mints.
    token = token or config_dict.get("token") or ""
    user = user or config_dict.get("user", "")
    # An explicit empty --client-id/--client-secret must opt out over the config file pair, because that pair would
    # otherwise switch the run to OAuth and quietly discard the configured token; an unset option is what falls back.
    if client_id is None:
        client_id = _client_id_from_env() or config_dict.get("client_id", "")
    if client_secret is None:
        client_secret = _client_secret_from_env() or config_dict.get("client_secret", "")
    cloud = cloud if cloud is not None else config_dict.get("cloud", False)
    if cloud_id is None:
        # An explicit empty --cloud-id must win over both, so an unset option is what falls back. A YAML `cloud_id:`
        # parses to None, which is normalised to "" like the token.
        cloud_id = _cloud_id_from_env() or config_dict.get("cloud_id") or ""

    if cloud_id and not isinstance(cloud_id, str):
        # A YAML `cloud_id: 12345678` parses as an int, and the shape check below would raise instead of reporting.
        # The type is rejected first, with the fix in the message: a numeric id quoted as a string stays a valid id.
        LOGGER.error(
            "Invalid Jira cloud id type (from --cloud-id/JIRA_CLOUD_ID/ATLASSIAN_OAUTH_CLOUD_ID/config 'cloud_id'): "
            f"expected a string, got {type(cloud_id).__name__}. Quote the value in the config file, e.g. "
            'cloud_id: "<cloud-id>".'
        )
        sys.exit(1)

    if cloud_id and not _is_valid_cloud_id(cloud_id=cloud_id):
        # The cloud id goes into the API gateway url we send the bearer token to, so validate it first.
        LOGGER.error(
            "Invalid Jira cloud id (from --cloud-id/JIRA_CLOUD_ID/ATLASSIAN_OAUTH_CLOUD_ID/"
            f"config 'cloud_id'): {_mask_cloud_id(cloud_id=cloud_id)}. "
            'Pass --cloud-id "" to ignore it.'
        )
        sys.exit(1)

    # The gateway path ends at "ex/jira/" with no id, so the run would build the gateway root as the server and every
    # request would fail there instead of failing here with the reason. The bare url without its trailing slash (the
    # likeliest typo) carries no id either, so the id is derived exactly as the run does: userinfo stripped, then the
    # bare id.
    if not cloud_id and _is_gateway_url(url=url) and not _effective_cloud_id(url=url, cloud_id=cloud_id):
        # An id segment that is present but not a cloud id gets its own message: telling the user to append an id that
        # is already there sends them looking for the wrong thing.
        gateway_parts = _gateway_url_parts(url=url)
        has_id_segment = bool(
            gateway_parts is not None and re.match(r"/ex/jira/[^/]", gateway_parts.path, re.IGNORECASE)
        )
        if has_id_segment:
            LOGGER.error(
                "Invalid Jira cloud id in the supplied gateway url: expected letters, digits and dashes, "
                "8-64 characters."
            )
        else:
            LOGGER.error(
                "The supplied Jira API gateway url is missing its cloud id: append it, or pass --cloud-id "
                f"(expected {SA_GATEWAY_URL_PREFIX}<cloud-id>)."
            )
        sys.exit(1)

    if cloud_id and url and not _is_gateway_url(url=url):
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
    "Defaults to JIRA_SA_CLIENT_ID (aliases: ATLASSIAN_OAUTH_CLIENT_ID); pass an empty --client-id together with an "
    "empty --client-secret to opt out of the OAuth credential regardless of both - only the pair opts out.",
    type=click.STRING,
    default=None,
)
@click.option(
    "--client-secret",
    help="OAuth 2.0 client secret of the Atlassian service account; it is exchanged for a short-lived access "
    "token. Defaults to JIRA_SA_CLIENT_SECRET (aliases: JIRA_OAUTH_CLIENT_SECRET); pass an empty --client-secret "
    "together with an empty --client-id to opt out of the OAuth credential regardless of both - only the pair opts "
    "out.",
    type=click.STRING,
    default=None,
)
@click.option(
    "--check-auth",
    help="Verify the Jira credentials against /myself (for any service-account/Bearer credential it falls back to "
    "the project endpoint) and exit. Exits 0 when the credential is verified, 1 when it is rejected, when an "
    "outage gets in the way or when neither endpoint can verify it.",
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
    # The OAuth pair has no os.getenv() option default (an unset option must stay distinguishable from an empty one),
    # so it is resolved here too: otherwise an env-only pair reads as an empty invocation to the guard below and the
    # dedicated cloud-id message is never reached.
    if client_id is None:
        client_id = _client_id_from_env() or None
    if client_secret is None:
        client_secret = _client_secret_from_env() or None

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
        client_id=client_id,
        client_secret=client_secret,
    )
    resolved_cloud_id = _effective_cloud_id(url=jira_config_dict["url"], cloud_id=jira_config_dict["cloud_id"])
    # Installed at the CLI boundary rather than in the config resolver, so the resolver stays side-effect free while
    # the filter still lands before the first request and even a client retry log carries the mask.
    _install_log_secret_filter(
        cloud_id=resolved_cloud_id,
        server=resolve_jira_server(url=jira_config_dict["url"], cloud_id=jira_config_dict["cloud_id"]),
        token=jira_config_dict["token"],
    )

    # Validated once, on its own message: python-jira urlparses the server too, and a bare ValueError from inside its
    # constructor (a malformed serverInfo, an int() on a bad option) is a programming error that must keep its
    # traceback instead of being reported as an outage.
    server = resolve_jira_server(url=jira_config_dict["url"], cloud_id=jira_config_dict["cloud_id"])
    try:
        urlsplit(server)
    except ValueError:
        LOGGER.error(
            f"Invalid Jira server url (unparseable, e.g. an unbracketed IPv6 host): {_mask_server(server=server)}"
        )
        sys.exit(1)

    # Mint before the client is built, so every reporter below redacts the live bearer even when JIRA() fails.
    if jira_config_dict["client_id"] and jira_config_dict["client_secret"]:
        try:
            jira_config_dict["token"] = mint_atlassian_token(
                client_id=jira_config_dict["client_id"], client_secret=jira_config_dict["client_secret"]
            )
        except JiraTokenMintError as exp:
            LOGGER.error(f"Jira authentication failed: {exp}")
            sys.exit(1)

    # The minted access token replaces the configured one, so it is masked from the transport logs as well.
    _install_log_secret_filter(token=jira_config_dict["token"])

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
        _report_jira_outage(
            prefix="Jira client construction failed",
            exp=exp,
            jira_config_dict=jira_config_dict,
            cloud_id=resolved_cloud_id,
        )
    except requests.RequestException as exp:
        # An unreachable API can surface raw from the constructor rather than as a JIRAError: it is an outage too.
        _report_jira_outage(
            prefix="Jira client construction failed",
            exp=exp,
            jira_config_dict=jira_config_dict,
            cloud_id=resolved_cloud_id,
        )

    if check_auth:
        _check_jira_auth(jira_obj=jira_obj, jira_config_dict=jira_config_dict)
        return

    jira_error: dict[str, str] = {}
    # The probe's projects() fallback covers any service-account (Bearer) auth, not just the OAuth pair.
    service_account_auth = _is_service_account_auth(url=jira_config_dict["url"], cloud_id=jira_config_dict["cloud_id"])

    # At most one re-probe per scan: the first 401 clears the cached verdict so a bearer that expired mid-scan is
    # noticed, while every later 401 reuses the fresh verdict instead of costing another probe.
    credential_reprobed = False

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
                            cloud_id=resolved_cloud_id,
                        )
                    }

                    for future in concurrent.futures.as_completed(future_to_jiras):
                        file_name, jira_error_string, status_code = future.result()
                        if status_code == 401 and not credential_reprobed:
                            # A 401 can be a bearer that expired after an earlier 403 was judged harmless, so the
                            # cached verdict is re-probed once, then reused for the rest of the scan.
                            credential_reprobed = True
                            _credential_rejected.cache_clear()  # type: ignore[attr-defined]
                        if status_code in AUTH_ERROR_STATUS_CODES and _credential_rejected(
                            jira_obj=jira_obj,
                            service_account=service_account_auth,
                            token=jira_config_dict["token"],
                            cloud_id=resolved_cloud_id,
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
                            # Two failing issues in the same file must both reach the report.
                            if existing_error := jira_error.get(file_name):
                                jira_error_string = f"{existing_error} {jira_error_string}"
                            jira_error[file_name] = jira_error_string

    if jira_error:
        _jira_error = "\n\t".join([f"{key}: {val}" for key, val in jira_error.items()])
        LOGGER.error(f"Following Jira ids failed jira version/statuscheck: \n\t{_jira_error}\n")
        sys.exit(1)


if __name__ == "__main__":
    get_jira_mismatch()
