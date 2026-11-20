import hashlib
import importlib.util
import inspect
import logging
import os
import shlex
import subprocess

import pytest
import requests
from click.testing import CliRunner
from jira.exceptions import JIRAError
from pyhelper_utils.shell import run_command
from simple_logger.logger import get_logger
from tenacity import RetryError

from apps.jira_utils.jira_information import (
    _ATLASSIAN_TOKEN_CACHE,
    ATLASSIAN_TOKEN_URL,
    AUTH_MODE_CLOUD_BASIC,
    AUTH_MODE_DATACENTER_PAT,
    AUTH_MODE_SERVICE_ACCOUNT,
    SA_GATEWAY_HOST,
    SA_GATEWAY_URL_PREFIX,
    SA_GATEWAY_URL_TEMPLATE,
    TOKEN_REFRESH_MARGIN_SECONDS,
    JiraTokenMintError,
    _auth_failure_hint,
    _check_jira_auth,
    _credential_rejected,
    _effective_cloud_id,
    _error_detail,
    _install_log_secret_filter,
    _is_gateway_url,
    _jira_auth_mode,
    _jira_browse_url,
    _mask_server,
    _mask_userinfo,
    _project_endpoint_is_anonymous,
    _report_jira_auth_error,
    _reset_log_secret_filter,
    _retryable_jira_error,
    _strip_userinfo,
    build_jira_client,
    get_issue,
    get_jira_ids_from_file_content,
    get_jira_information,
    get_jira_mismatch,
    mint_atlassian_token,
    process_jira_command_line_config_file,
    resolve_jira_server,
)

LOGGER = get_logger(name=__name__)
BASE_COMMAND = "uv run apps/jira_utils/jira_information.py --verbose  "

CLOUD_ID = "a1b2c3d4-0000-1111-2222-333344445555"
GATEWAY_URL = SA_GATEWAY_URL_TEMPLATE.format(cloud_id=CLOUD_ID)
# Only human-readable output is masked; the client still receives GATEWAY_URL.
MASKED_CLOUD_ID = "<cloud-id>"
MASKED_GATEWAY_URL = SA_GATEWAY_URL_TEMPLATE.format(cloud_id=MASKED_CLOUD_ID)
# A supplied url can embed a credential in its userinfo component, which the transport logs quote verbatim.
USERINFO_PASSWORD = "sup3r-s3cret"  # pragma: allowlist secret
USERINFO_GATEWAY_URL = f"https://user:{USERINFO_PASSWORD}@api.atlassian.com/ex/jira/{CLOUD_ID}"
USERINFO_DATACENTER_URL = f"https://user:{USERINFO_PASSWORD}@issues.redhat.com"
USERINFO_SITE_URL = f"https://user:{USERINFO_PASSWORD}@site.atlassian.net"
# A password holding a quote: the text regex ends a url at a quote, so such a credential is never delimited there.
USERINFO_QUOTED_PASSWORD = "pa'ss-w0rd"  # pragma: allowlist secret
USERINFO_QUOTED_SITE_URL = f"https://user:{USERINFO_QUOTED_PASSWORD}@site.atlassian.net"
# --cloud-id "" overrides the JIRA_CLOUD_ID env default so the run stays env independent.
DATACENTER_ARGS = ("--url", "https://issues.redhat.com", "--token", "t", "--cloud-id", "")
# A pre-minted scoped API token: no client id/secret, so only the cloud id marks it service-account auth.
SCOPED_TOKEN_ARGS = ("--cloud-id", CLOUD_ID, "--token", "scoped-token")
OAUTH_CLIENT_ID = "oauth-client-id"
OAUTH_CLIENT_SECRET = "s3cr3t-client-secret"  # pragma: allowlist secret
# --cloud-id is explicit so the OAuth runs never depend on JIRA_CLOUD_ID / ATLASSIAN_OAUTH_CLOUD_ID.
OAUTH_ARGS = ("--cloud-id", CLOUD_ID, "--client-id", OAUTH_CLIENT_ID, "--client-secret", OAUTH_CLIENT_SECRET)
JIRA_SOURCE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "apps", "jira_utils", "jira_information.py")
)


def _mock_anonymous_probe(mocker, status_code=401, projects=None):
    """Mock the header-less project probe a --check-auth run makes, rejected by default (offline).

    Returns the mocked session, so a test can assert on the prepared request the probe sent.
    """
    response = mocker.MagicMock()
    response.status_code = status_code
    response.ok = 200 <= status_code < 300
    response.json.return_value = projects if projects is not None else []
    mock_session = mocker.patch("apps.jira_utils.jira_information.requests.Session").return_value
    # A plain dict for the prepared headers: the probe drops a stray Authorization header from it before sending.
    mock_session.prepare_request.return_value.headers = {}
    mock_session.send.return_value = response
    return mock_session


def _run_jira_command(mocker, *args):
    """Invoke the click command with the Jira client and scan path mocked out."""
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    mock_get_jiras = mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})
    # --check-auth can probe the project endpoint without a header; that request must never leave the process.
    _mock_anonymous_probe(mocker)
    result = CliRunner().invoke(get_jira_mismatch, list(args))
    return result, mock_jira_cls, mock_get_jiras


def _run_oauth_command(
    mocker,
    *args,
    myself_error=None,
    projects_error=None,
    projects_return=None,
    anonymous_status=401,
    anonymous_projects=None,
    rest_api_version=None,
):
    """Invoke the click command with the token mint and the Jira client mocked out (offline)."""
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    _mock_anonymous_probe(mocker, status_code=anonymous_status, projects=anonymous_projects)
    mock_mint = mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token", return_value="minted-token")
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    # A real JIRA carries its REST API version in _options; the anonymous probe must follow it, so the mock must too.
    mock_jira_cls.return_value._options = {"rest_api_version": rest_api_version} if rest_api_version else {}
    if myself_error is not None:
        mock_jira_cls.return_value.myself.side_effect = myself_error
    if projects_error is not None:
        mock_jira_cls.return_value.projects.side_effect = projects_error
    if projects_return is not None:
        mock_jira_cls.return_value.projects.return_value = projects_return
    mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})
    result = CliRunner().invoke(get_jira_mismatch, list(args))
    return result, mock_jira_cls, mock_mint


def _run_scan(
    mocker,
    scan_args,
    issues,
    per_issue_status=None,
    myself_side_effect=None,
    projects_side_effect=None,
    projects_return=None,
    per_issue_results=None,
):
    """Run a scan over `issues`, where every issue fails with `per_issue_status` unless `per_issue_results` is given."""
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    if myself_side_effect is not None:
        mock_jira_cls.return_value.myself.side_effect = myself_side_effect
    if projects_side_effect is not None:
        mock_jira_cls.return_value.projects.side_effect = projects_side_effect
    if projects_return is not None:
        mock_jira_cls.return_value.projects.return_value = projects_return
    mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={"file.py": issues})
    if per_issue_results is not None:
        mock_get_jira_information = mocker.patch(
            "apps.jira_utils.jira_information.get_jira_information", side_effect=list(per_issue_results)
        )
    else:
        mock_get_jira_information = mocker.patch(
            "apps.jira_utils.jira_information.get_jira_information",
            return_value=("file.py", f"error {per_issue_status}", per_issue_status),
        )
    result = CliRunner().invoke(get_jira_mismatch, list(scan_args))
    return result, mock_jira_cls, mock_get_jira_information


def _patch_logger(mocker):
    """Return a mock of the module logger; it does not propagate, so CLI logs are asserted on its calls."""
    return mocker.patch("apps.jira_utils.jira_information.LOGGER")


LOGGED_LEVELS = ("debug", "info", "success", "warning", "error", "exception", "critical")


def _logged(mock_logger, level):
    """Return the messages logged at `level` through `mock_logger`."""
    return [call.args[0] for call in getattr(mock_logger, level).call_args_list]


def _all_logged(mock_logger):
    """Return every (level, message) `mock_logger` emitted, so a run's total log volume can be asserted."""
    return [(level, message) for level in LOGGED_LEVELS for message in _logged(mock_logger, level)]


def _token_cache_key(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET):
    """Return the cache key mint_atlassian_token is expected to build: no plaintext secret in it."""
    return client_id, hashlib.sha256(client_secret.encode()).hexdigest()


def _hint_logs(messages, mode, oauth_service_account=False):
    """Return the credential hints for `mode` among the given log messages."""
    return [
        message
        for message in messages
        if message == _auth_failure_hint(mode=mode, oauth_service_account=oauth_service_account)
    ]


JIRA_ENV_VARS = (
    "JIRA_CLOUD_ID",
    "JIRA_URL",
    "JIRA_SERVER_URL",
    "JIRA_TOKEN",
    "JIRA_USER",
    "JIRA_SA_CLIENT_ID",
    "JIRA_SA_CLIENT_SECRET",
    "JIRA_OAUTH_CLIENT_SECRET",
    "ATLASSIAN_OAUTH_CLIENT_ID",
    "ATLASSIAN_OAUTH_CLOUD_ID",
)


def _clear_jira_env(monkeypatch):
    """Drop inherited Jira env defaults so config tests assert on their own arguments only."""
    for env_var in JIRA_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)


@pytest.fixture(autouse=True)
def _empty_atlassian_token_cache():
    """Keep a token minted in one test from being reused by the next one."""
    _ATLASSIAN_TOKEN_CACHE.clear()
    _credential_rejected.cache_clear()
    yield
    _ATLASSIAN_TOKEN_CACHE.clear()
    _credential_rejected.cache_clear()


@pytest.fixture(autouse=True)
def _hermetic_jira_env(monkeypatch):
    """Drop the ambient Jira env and the option defaults frozen from it.

    `cloud_id` and the OAuth client pair are resolved from the env at call time, while url/token/user arrive as
    os.getenv() click defaults that were frozen at import, so all of them must be neutralised for a test to be
    independent of the developer's own Jira credential. A test that wants an env-provided value sets it itself,
    which makes the dependency explicit.
    """
    _clear_jira_env(monkeypatch)
    _patch_option_defaults(monkeypatch, url=None, token=None, user=None, client_id=None, client_secret=None)


@pytest.fixture(autouse=True)
def _no_real_atlassian_mint(monkeypatch):
    """Refuse a real token mint, so a test that forgets to mock it can never POST a live client secret.

    Both the mint and the HTTP call are guarded: the mint gives the clear message, the POST covers a future
    transport and the direct `mint_atlassian_token` calls that bypass the module attribute.
    """

    def _blocked(*args, **kwargs):
        raise AssertionError("a real Atlassian token mint was attempted: mock mint_atlassian_token/requests.post")

    monkeypatch.setattr("apps.jira_utils.jira_information.mint_atlassian_token", _blocked)
    monkeypatch.setattr("apps.jira_utils.jira_information.requests.post", _blocked)


@pytest.fixture(autouse=True)
def _restore_log_state():
    """Undo what a CLI run leaves process-wide, so neither the record factory nor a logger level leaks on.

    The transport logger levels are pinned for the test: `simple_logger` calls `logging.setLoggerClass` at import and
    its logger defaults to INFO, so a `jira` logger created after another module's import is born at INFO and
    `caplog.set_level(DEBUG)` (which only raises the root) would not let a DEBUG record through. Without this the
    transport-filter tests pass or fail depending on collection order. The factory is restored by the reset helper
    alone, so the production path is the one the suite exercises.
    """
    loggers = [logging.getLogger(name) for name in ("jira", "jira.resilientsession", "urllib3")]
    saved_levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.DEBUG)

    yield

    _reset_log_secret_filter()
    for logger, saved_level in zip(loggers, saved_levels):
        logger.setLevel(saved_level)


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch):
    """Refuse any real HTTP call: every request here must be mocked, the anonymous project probe included."""

    def _blocked(*args, **kwargs):
        raise AssertionError("a real HTTP request was attempted: mock requests.get/post in this test")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _blocked)


def _patch_token_post(mocker, payload=None, status_code=200):
    """Mock the Atlassian OAuth token endpoint (offline) and silence the mint log line.

    simple_logger's DuplicateFilter is process-wide, so the identical mint line of every test here would be folded
    into the next test's record; the log line is not under test, so it is mocked out.
    """
    mocker.patch("apps.jira_utils.jira_information.LOGGER")
    response = mocker.MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return mocker.patch("apps.jira_utils.jira_information.requests.post", return_value=response)


def _patch_option_defaults(monkeypatch, **defaults):
    """Re-evaluate the os.getenv() option defaults that were frozen at import time."""
    for option in get_jira_mismatch.params:
        if option.name in defaults:
            monkeypatch.setattr(option, "default", defaults[option.name])


def _source_option_defaults():
    """Return the click defaults a fresh execution of the source module body computes from the current env."""
    spec = importlib.util.spec_from_file_location("jira_information_env_defaults", JIRA_SOURCE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {option.name: option.default for option in module.get_jira_mismatch.params}


def test_process_jira_command_line_config_file_empty_config_token() -> None:
    config_file = os.path.join(os.path.dirname(__file__), "test_jira_cfg_file.yaml")
    # Strip Jira env defaults (both spellings) so the assertion reflects the config file only.
    env = {key: value for key, value in os.environ.items() if not key.startswith(("JIRA_", "ATLASSIAN_OAUTH_"))}
    rc, _, err = run_command(
        command=shlex.split(f"{BASE_COMMAND} --config-file-path {config_file}"),
        verify_stderr=False,
        check=False,
        capture_output=False,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert "Jira url and token are required." in err
    assert not rc


def test_process_jira_command_line_config_file_valid_config(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    config_file_path = "/path/to/config"
    url = "https://example.com"
    token = "1234567890"
    issue_pattern = "*"
    resolved_statuses = ["RESOLVED"]
    version_string_not_targeted_jiras = "v1.*"
    target_versions = ["v2", "v3"]
    skip_projects = [1, 2]
    mock_get_util_config = mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={
            "url": url,
            "token": token,
            "issue_pattern": "*",
            "resolved_statuses": ["RESOLVED"],
            "version_string_not_targeted_jiras": version_string_not_targeted_jiras,
            "target_versions": target_versions,
            "skip_project_ids": skip_projects,
            "user": "",
            "cloud": False,
        },
    )
    result = process_jira_command_line_config_file(
        config_file_path,
        url,
        token,
        issue_pattern,
        resolved_statuses,
        version_string_not_targeted_jiras,
        target_versions,
        skip_projects,
        user="",
        cloud=False,
    )
    assert result == {
        "url": url,
        "token": token,
        "issue_pattern": issue_pattern,
        "resolved_status": resolved_statuses,
        "not_targeted_version_str": version_string_not_targeted_jiras,
        "target_versions": target_versions,
        "skip_project_ids": skip_projects,
        "user": "",
        "cloud": False,
        "cloud_id": "",
        # Unconditional keys: empty when no OAuth 2.0 credential is configured.
        "client_id": "",
        "client_secret": "",
    }
    mock_get_util_config.assert_called_once()


def test_process_jira_command_line_config_file_cloud_valid(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    config_file_path = "/path/to/config"
    url = "https://example.com"
    token = "1234567890"
    user = "test@example.com"

    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={
            "url": url,
            "token": token,
            "cloud": True,
            "user": user,
        },
    )
    result = process_jira_command_line_config_file(
        config_file_path=config_file_path,
        url=url,
        token=token,
        issue_pattern="*",
        resolved_statuses=["RESOLVED"],
        version_string_not_targeted_jiras="v1.*",
        target_versions=["v2"],
        skip_projects=[],
        user=user,
        cloud=True,
    )
    assert result["cloud"] is True
    assert result["user"] == user


def test_process_jira_command_line_config_file_cloud_missing_user(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    config_file_path = "/path/to/config"
    url = "https://example.com"
    token = "1234567890"

    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={
            "url": url,
            "token": token,
        },
    )
    with pytest.raises(SystemExit):
        process_jira_command_line_config_file(
            config_file_path=config_file_path,
            url=url,
            token=token,
            issue_pattern="*",
            resolved_statuses=["RESOLVED"],
            version_string_not_targeted_jiras="v1.*",
            target_versions=["v2"],
            skip_projects=[],
            user="",
            cloud=True,
        )


def test_process_cloud_from_config_when_cli_not_passed(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={"url": "https://example.com", "token": "tok", "cloud": True, "user": "u@e.com"},
    )
    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="https://example.com",
        token="tok",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="u@e.com",
        cloud=None,
    )
    assert result["cloud"] is True
    assert result["user"] == "u@e.com"


def test_process_cloud_defaults_false_when_not_in_config(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={"url": "https://example.com", "token": "tok"},
    )
    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="https://example.com",
        token="tok",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=None,
    )
    assert result["cloud"] is False


def test_process_cloud_from_config_missing_user_exits(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={"url": "https://example.com", "token": "tok", "cloud": True},
    )
    with pytest.raises(SystemExit):
        process_jira_command_line_config_file(
            config_file_path="/path",
            url="https://example.com",
            token="tok",
            issue_pattern="*",
            resolved_statuses=[],
            version_string_not_targeted_jiras="",
            target_versions=[],
            skip_projects=[],
            user="",
            cloud=None,
        )


def test_process_user_from_config_fallback(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={"url": "https://example.com", "token": "tok", "user": "config@example.com", "cloud": True},
    )
    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="https://example.com",
        token="tok",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=True,
    )
    assert result["user"] == "config@example.com"
    assert result["cloud"] is True


def test_process_cloud_false_explicit_ignores_config(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={"url": "https://example.com", "token": "tok", "cloud": True},
    )
    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="https://example.com",
        token="tok",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=False,
    )
    assert result["cloud"] is False


def test_process_server_mode_default(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={"url": "https://example.com", "token": "tok"},
    )
    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="https://example.com",
        token="tok",
        issue_pattern="([A-Z]+-[0-9]+)",
        resolved_statuses=["resolved"],
        version_string_not_targeted_jiras="vfuture",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=False,
    )
    assert result["cloud"] is False
    assert result["user"] == ""
    assert result["url"] == "https://example.com"
    assert result["token"] == "tok"


def test_process_cloud_id_from_config_when_cli_not_passed(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={"url": "https://example.com", "token": "tok", "cloud_id": "config-cloud-id"},
    )
    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="https://example.com",
        token="tok",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=None,
    )
    assert result["cloud_id"] == "config-cloud-id"


def test_process_cloud_id_from_cli_wins_over_config(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={"url": "https://example.com", "token": "tok", "cloud_id": "config-cloud-id"},
    )
    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="https://example.com",
        token="tok",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=None,
        cloud_id="cli-cloud-id",
    )
    assert result["cloud_id"] == "cli-cloud-id"


def test_process_null_cloud_id_is_normalised_to_empty(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        # A YAML `cloud_id:` (nothing) parses to None: it must read as "no cloud id", not crash the shape check.
        return_value={"url": "https://example.com", "token": "tok", "cloud_id": None},
    )
    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="https://example.com",
        token="tok",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=None,
    )
    assert result["cloud_id"] == ""


def test_process_int_cloud_id_exits_with_the_quoting_hint(mocker, monkeypatch, caplog):
    _clear_jira_env(monkeypatch)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        # `cloud_id: 12345678` parses from YAML as an int: re.fullmatch would raise instead of reporting it.
        return_value={"url": "https://example.com", "token": "tok", "cloud_id": 12345678},
    )
    with pytest.raises(SystemExit) as exc_info:
        process_jira_command_line_config_file(
            config_file_path="/path",
            url="https://example.com",
            token="tok",
            issue_pattern="*",
            resolved_statuses=[],
            version_string_not_targeted_jiras="",
            target_versions=[],
            skip_projects=[],
            user="",
            cloud=None,
        )
    assert exc_info.value.code == 1
    # The diagnostic must say how to fix it, and must not echo the value it could not use.
    assert "Invalid Jira cloud id type" in caplog.text
    assert "expected a string, got int" in caplog.text
    assert "Quote the value" in caplog.text
    assert "12345678" not in caplog.text


def test_process_quoted_numeric_cloud_id_stays_valid(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={"url": "https://example.com", "token": "tok", "cloud_id": "12345678"},
    )
    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="https://example.com",
        token="tok",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=None,
    )
    # The same id quoted as a string is a valid cloud id shape: the type guard never rejects it.
    assert result["cloud_id"] == "12345678"


def test_process_service_account_valid_without_url_and_user(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={},
    )
    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="",
        token="tok",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=None,
        cloud_id="cloud-id",
    )
    assert result["cloud_id"] == "cloud-id"
    assert result["url"] == ""
    assert result["user"] == ""


def test_process_service_account_missing_token_exits(mocker, monkeypatch, caplog):
    _clear_jira_env(monkeypatch)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    with pytest.raises(SystemExit):
        process_jira_command_line_config_file(
            config_file_path="/path",
            url="",
            token="",
            issue_pattern="*",
            resolved_statuses=[],
            version_string_not_targeted_jiras="",
            target_versions=[],
            skip_projects=[],
            user="",
            cloud=None,
            cloud_id="cloud-id",
        )
    assert "Jira token is required." in caplog.text


def test_process_non_service_account_missing_url_exits(mocker, monkeypatch, caplog):
    _clear_jira_env(monkeypatch)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    with pytest.raises(SystemExit):
        process_jira_command_line_config_file(
            config_file_path="/path",
            url="",
            token="tok",
            issue_pattern="*",
            resolved_statuses=[],
            version_string_not_targeted_jiras="",
            target_versions=[],
            skip_projects=[],
            user="",
            cloud=False,
        )
    assert "Jira url and token are required." in caplog.text


@pytest.mark.parametrize(
    "client_id,client_secret",
    [
        pytest.param("client-id", "", id="client-id-without-secret"),
        pytest.param("", "client-secret", id="client-secret-without-id"),
    ],
)
def test_process_incomplete_oauth_credential_exits(mocker, monkeypatch, client_id, client_secret):
    _clear_jira_env(monkeypatch)
    mock_logger = _patch_logger(mocker)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    with pytest.raises(SystemExit) as exc_info:
        process_jira_command_line_config_file(
            config_file_path="/path",
            url="",
            token="",
            issue_pattern="*",
            resolved_statuses=[],
            version_string_not_targeted_jiras="",
            target_versions=[],
            skip_projects=[],
            user="",
            cloud=None,
            cloud_id=CLOUD_ID,
            client_id=client_id,
            client_secret=client_secret,
        )
    assert exc_info.value.code == 1
    # Half a credential is useless, so the message names both options instead of only the missing one.
    errors = "\n".join(_logged(mock_logger, "error"))
    assert "--client-id" in errors
    assert "--client-secret" in errors


def test_process_oauth_credential_without_cloud_id_exits(mocker, monkeypatch, caplog):
    _clear_jira_env(monkeypatch)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    with pytest.raises(SystemExit) as exc_info:
        process_jira_command_line_config_file(
            config_file_path="/path",
            url="https://example.atlassian.net",
            token="",
            issue_pattern="*",
            resolved_statuses=[],
            version_string_not_targeted_jiras="",
            target_versions=[],
            skip_projects=[],
            user="",
            cloud=None,
            cloud_id="",
            client_id="client-id",
            client_secret="client-secret",  # pragma: allowlist secret
        )
    assert exc_info.value.code == 1
    # The minted token only works against the API gateway, which cannot be resolved without a cloud id.
    assert "cloud id is required" in caplog.text
    assert "--cloud-id" in caplog.text


def test_process_client_credentials_from_config(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mock_logger = _patch_logger(mocker)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={"client_id": "config-client-id", "client_secret": "config-client-secret", "cloud_id": CLOUD_ID},
    )
    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="",
        token="",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=None,
    )
    assert result["client_id"] == "config-client-id"
    assert result["client_secret"] == "config-client-secret"  # pragma: allowlist secret
    assert result["cloud_id"] == CLOUD_ID
    # No token is needed at all: the credential is exchanged for one, which the log line confirms.
    assert result["token"] == ""
    assert [message for message in _logged(mock_logger, "info") if "OAuth 2.0 client credentials" in message]


def test_process_client_id_from_cli_wins_over_config(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={"client_id": "config-client-id", "client_secret": "config-client-secret", "cloud_id": CLOUD_ID},
    )
    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="",
        token="",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=None,
        client_id="cli-client-id",
    )
    assert result["client_id"] == "cli-client-id"
    assert result["client_secret"] == "config-client-secret"  # pragma: allowlist secret


CONFIG_FILE_PATH = os.path.join(os.path.dirname(__file__), "test_jira_cfg_file.yaml")


def test_process_empty_oauth_options_opt_out_of_the_config_pair(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    # The pair is in the env as well, so the opt-out has to beat both the env and the config file.
    monkeypatch.setenv("JIRA_SA_CLIENT_ID", OAUTH_CLIENT_ID)
    monkeypatch.setenv("JIRA_SA_CLIENT_SECRET", OAUTH_CLIENT_SECRET)
    mock_logger = _patch_logger(mocker)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={
            "url": "https://issues.redhat.com",
            "token": "config-token",
            "cloud_id": CLOUD_ID,
            "client_id": "config-client-id",
            "client_secret": "config-client-secret",  # pragma: allowlist secret
        },
    )
    mock_mint = mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token", return_value="minted-token")
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})

    # An explicit empty pair is the documented opt-out, so it must not be replaced by the env or the config pair.
    result = CliRunner().invoke(
        get_jira_mismatch, ["--config-file-path", CONFIG_FILE_PATH, "--client-id", "", "--client-secret", ""]
    )

    assert result.exit_code == 0, _logged(mock_logger, "error")
    mock_mint.assert_not_called()
    # The configured scoped token is what the run authenticates with.
    mock_jira_cls.assert_called_once_with(server=GATEWAY_URL, token_auth="config-token")


@pytest.mark.parametrize(
    "env_pair,expected",
    [
        pytest.param(True, (OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET), id="env-wins-over-config"),
        pytest.param(False, ("config-client-id", "config-client-secret"), id="config-when-env-unset"),
    ],
)
def test_process_omitted_oauth_options_fall_back_to_env_then_config(mocker, monkeypatch, env_pair, expected):
    _clear_jira_env(monkeypatch)
    if env_pair:
        monkeypatch.setenv("JIRA_SA_CLIENT_ID", OAUTH_CLIENT_ID)
        monkeypatch.setenv("JIRA_SA_CLIENT_SECRET", OAUTH_CLIENT_SECRET)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={"client_id": "config-client-id", "client_secret": "config-client-secret"},
    )

    # Neither option is passed: the omitted state (None) is the one that falls back, in env-then-config order.
    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="",
        token="",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=None,
        cloud_id=CLOUD_ID,
    )

    assert (result["client_id"], result["client_secret"]) == expected


@pytest.mark.parametrize(
    "env_var",
    [pytest.param("JIRA_CLOUD_ID", id="jira-cloud-id"), pytest.param("ATLASSIAN_OAUTH_CLOUD_ID", id="oauth-alias")],
)
def test_process_cloud_id_from_env(mocker, monkeypatch, env_var):
    _clear_jira_env(monkeypatch)
    monkeypatch.setenv(env_var, CLOUD_ID)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="",
        token="tok",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=None,
    )
    assert result["cloud_id"] == CLOUD_ID


def test_process_oauth_credential_cloud_id_from_oauth_env_alias(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    monkeypatch.setenv("ATLASSIAN_OAUTH_CLOUD_ID", CLOUD_ID)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})

    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="",
        token="",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=None,
        client_id=OAUTH_CLIENT_ID,
        client_secret=OAUTH_CLIENT_SECRET,
    )

    # The env alias is resolved before the "cloud id is required" guard, so a full env OAuth run is not rejected.
    assert result["cloud_id"] == CLOUD_ID
    assert result["client_id"] == OAUTH_CLIENT_ID


@pytest.mark.parametrize(
    "url,cloud_id,expected",
    [
        pytest.param("", "cloud-id", SA_GATEWAY_URL_TEMPLATE.format(cloud_id="cloud-id"), id="gateway_from_cloud_id"),
        pytest.param(
            "https://example.atlassian.net",
            "cloud-id",
            SA_GATEWAY_URL_TEMPLATE.format(cloud_id="cloud-id"),
            id="gateway_wins_over_site_url",
        ),
        pytest.param(
            SA_GATEWAY_URL_TEMPLATE.format(cloud_id="cloud-id"),
            "",
            SA_GATEWAY_URL_TEMPLATE.format(cloud_id="cloud-id"),
            id="gateway_url_unchanged",
        ),
        # A path below the cloud id is not the server: python-jira appends its own `/rest/api/2/...` to whatever it
        # is given, so the id is derived from the url and the canonical gateway url is rebuilt.
        pytest.param(
            f"{SA_GATEWAY_URL_TEMPLATE.format(cloud_id='cloud-id')}/rest/api/2",
            "",
            SA_GATEWAY_URL_TEMPLATE.format(cloud_id="cloud-id"),
            id="gateway_url_deeper_path_canonicalised",
        ),
        pytest.param("https://example.atlassian.net", "", "https://example.atlassian.net", id="site_url_unchanged"),
    ],
)
def test_resolve_jira_server(url, cloud_id, expected):
    assert resolve_jira_server(url=url, cloud_id=cloud_id) == expected


def test_build_jira_client_service_account(mocker):
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    result = build_jira_client(
        url="https://example.atlassian.net", token="tok", user="", cloud=False, cloud_id="cloud-id"
    )
    mock_jira_cls.assert_called_once_with(server=SA_GATEWAY_URL_TEMPLATE.format(cloud_id="cloud-id"), token_auth="tok")
    assert result is mock_jira_cls.return_value


def test_build_jira_client_cloud_basic(mocker):
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    result = build_jira_client(
        url="https://example.atlassian.net", token="tok", user="user@example.com", cloud=True, cloud_id=""
    )
    mock_jira_cls.assert_called_once_with(
        server="https://example.atlassian.net", basic_auth=("user@example.com", "tok")
    )
    assert result is mock_jira_cls.return_value


def test_build_jira_client_datacenter_pat(mocker):
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    result = build_jira_client(url="https://example.com", token="tok", user="", cloud=False, cloud_id="")
    mock_jira_cls.assert_called_once_with(token_auth="tok", options={"server": "https://example.com"})
    assert result is mock_jira_cls.return_value


def test_build_jira_client_site_url_with_cloud_id_uses_gateway(mocker):
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    build_jira_client(url="https://example.atlassian.net", token="tok", user="", cloud=True, cloud_id="cloud-id")
    mock_jira_cls.assert_called_once_with(server=SA_GATEWAY_URL_TEMPLATE.format(cloud_id="cloud-id"), token_auth="tok")


def test_build_jira_client_takes_no_client_credential():
    # Minting runs in get_jira_mismatch before the client is built, so the live bearer is in jira_config_dict["token"]
    # by the time any error is reported. A mint lazy enough to happen in here would bypass that redaction.
    parameters = inspect.signature(build_jira_client).parameters
    assert "client_id" not in parameters
    assert "client_secret" not in parameters


def test_build_jira_client_pre_minted_token_does_not_mint(mocker):
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    mock_mint = mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token")

    build_jira_client(url="", token="pre-minted-token", user="", cloud=False, cloud_id=CLOUD_ID)

    mock_mint.assert_not_called()
    mock_jira_cls.assert_called_once_with(server=GATEWAY_URL, token_auth="pre-minted-token")


def test_mint_atlassian_token_returns_minted_token(mocker):
    mock_post = _patch_token_post(mocker, payload={"access_token": "minted-token", "expires_in": 3600})

    assert mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET) == "minted-token"

    mock_post.assert_called_once_with(
        ATLASSIAN_TOKEN_URL,
        json={"grant_type": "client_credentials", "client_id": OAUTH_CLIENT_ID, "client_secret": OAUTH_CLIENT_SECRET},
        timeout=15,
    )


def test_mint_atlassian_token_reuses_cached_token(mocker):
    mock_post = _patch_token_post(mocker, payload={"access_token": "minted-token", "expires_in": 3600})

    assert mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET) == "minted-token"
    assert mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET) == "minted-token"

    # A token that is still valid must not cost a second round trip.
    assert mock_post.call_count == 1


def test_mint_atlassian_token_mints_per_credential(mocker):
    responses = []
    for access_token in ("token-one", "token-two"):
        response = mocker.MagicMock()
        response.status_code = 200
        response.json.return_value = {"access_token": access_token, "expires_in": 3600}
        responses.append(response)
    mock_post = _patch_token_post(mocker)
    mock_post.side_effect = responses

    assert mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET) == "token-one"
    assert mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret="rotated-secret") == "token-two"

    # The cache is keyed by the whole pair: a rotated secret mints its own token instead of reusing the old one.
    assert mock_post.call_count == 2


def test_mint_atlassian_token_remints_inside_refresh_margin(mocker):
    now = [1000.0]
    mock_time = mocker.patch("apps.jira_utils.jira_information.time", mocker.MagicMock())
    mock_time.monotonic.side_effect = lambda: now[0]
    mock_post = _patch_token_post(mocker, payload={"access_token": "minted-token", "expires_in": 3600})

    assert mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET) == "minted-token"

    # The offsets below assume the documented 120 second margin, so a changed constant must fail here.
    assert TOKEN_REFRESH_MARGIN_SECONDS == 120

    # One second outside the margin: still valid, so the cached token is reused.
    now[0] += 3600 - 121
    assert mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET) == "minted-token"
    assert mock_post.call_count == 1

    # Exactly at the margin: re-minted now so the token cannot die mid-scan.
    now[0] += 1
    assert mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET) == "minted-token"
    assert mock_post.call_count == 2

    # Strictly inside the margin of the freshly minted token: re-minted as well.
    now[0] += 3600 - 119
    assert mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET) == "minted-token"
    assert mock_post.call_count == 3


@pytest.mark.parametrize("status_code", [pytest.param(401, id="401"), pytest.param(500, id="500")])
def test_mint_atlassian_token_http_error(mocker, status_code):
    _patch_token_post(mocker, payload={"access_token": "minted-token"}, status_code=status_code)

    with pytest.raises(JiraTokenMintError) as exc_info:
        mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET)

    assert f"HTTP {status_code}" in str(exc_info.value)
    # The credential must never reach a log line through the error message.
    assert OAUTH_CLIENT_SECRET not in str(exc_info.value)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="empty-payload"),
        pytest.param({"expires_in": 3600}, id="no-access-token"),
        pytest.param({"access_token": "", "expires_in": 3600}, id="empty-access-token"),
    ],
)
def test_mint_atlassian_token_payload_without_access_token(mocker, payload):
    _patch_token_post(mocker, payload=payload)

    with pytest.raises(JiraTokenMintError) as exc_info:
        mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET)

    assert "no access_token" in str(exc_info.value)
    assert "HTTP 200" in str(exc_info.value)


def test_mint_atlassian_token_network_error(mocker):
    mocker.patch(
        "apps.jira_utils.jira_information.requests.post",
        side_effect=requests.ConnectionError("connection refused"),
    )

    with pytest.raises(JiraTokenMintError) as exc_info:
        mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET)

    assert "connection refused" in str(exc_info.value)
    assert OAUTH_CLIENT_SECRET not in str(exc_info.value)


@pytest.mark.parametrize("expires_in", [pytest.param("soon", id="non-numeric"), pytest.param(None, id="null")])
def test_mint_atlassian_token_non_int_expires_falls_back(mocker, expires_in):
    mock_time = mocker.patch("apps.jira_utils.jira_information.time", mocker.MagicMock())
    mock_time.monotonic.return_value = 1000.0
    _patch_token_post(mocker, payload={"access_token": "minted-token", "expires_in": expires_in})

    assert mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET) == "minted-token"

    # An unusable lifetime falls back to the 3600 second default instead of raising or caching a dead token.
    assert _ATLASSIAN_TOKEN_CACHE[_token_cache_key()] == ("minted-token", 1000.0 + 3600)


def test_mint_atlassian_token_cache_key_hides_secret(mocker):
    _patch_token_post(mocker, payload={"access_token": "minted-token", "expires_in": 3600})

    mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET)

    (cache_key,) = _ATLASSIAN_TOKEN_CACHE
    # A plaintext secret in the key would leak through any dump or debugger view of the module state.
    assert cache_key == _token_cache_key()
    assert OAUTH_CLIENT_SECRET not in "".join(cache_key)


def test_mint_atlassian_token_prunes_expired_entry(mocker):
    now = [1000.0]
    mock_time = mocker.patch("apps.jira_utils.jira_information.time", mocker.MagicMock())
    mock_time.monotonic.side_effect = lambda: now[0]
    _patch_token_post(mocker, payload={"access_token": "minted-token", "expires_in": 3600})

    mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET)
    expired_key = _token_cache_key()
    assert set(_ATLASSIAN_TOKEN_CACHE) == {expired_key}

    # A long-lived process must not accumulate dead entries: the next mint prunes them and stores its own.
    now[0] += 3600
    mint_atlassian_token(client_id=OAUTH_CLIENT_ID, client_secret="rotated-secret")

    assert expired_key not in _ATLASSIAN_TOKEN_CACHE
    assert set(_ATLASSIAN_TOKEN_CACHE) == {_token_cache_key(client_secret="rotated-secret")}  # pragma: allowlist secret


def test_auth_failure_hint_oauth_service_account():
    hint = _auth_failure_hint(mode=AUTH_MODE_SERVICE_ACCOUNT, oauth_service_account=True)

    assert "--client-id" in hint
    assert "--client-secret" in hint
    assert "scopes" in hint
    # The access token is minted on demand, so telling the user to re-mint it would be wrong.
    assert "re-mint" not in hint.lower()


def test_auth_failure_hint_pre_minted_service_account():
    hint = _auth_failure_hint(mode=AUTH_MODE_SERVICE_ACCOUNT)

    assert "Re-mint" in hint
    assert "--client-id" not in hint


@pytest.mark.parametrize(
    "env_var,alias_var,option_name,expected",
    [
        pytest.param("ATLASSIAN_OAUTH_CLIENT_ID", None, "client_id", OAUTH_CLIENT_ID, id="client-id-alias"),
        pytest.param("JIRA_OAUTH_CLIENT_SECRET", None, "client_secret", OAUTH_CLIENT_SECRET, id="client-secret-alias"),
        pytest.param("JIRA_SA_CLIENT_ID", "ATLASSIAN_OAUTH_CLIENT_ID", "client_id", OAUTH_CLIENT_ID, id="client-id"),
        pytest.param(
            "JIRA_SA_CLIENT_SECRET",
            "JIRA_OAUTH_CLIENT_SECRET",
            "client_secret",
            OAUTH_CLIENT_SECRET,
            id="client-secret",
        ),
    ],
)
def test_oauth_credential_env_names_resolve_at_call_time(
    mocker, monkeypatch, env_var, alias_var, option_name, expected
):
    # The click defaults are None by design, so the env read happens when the config is resolved. The hermetic
    # fixture drops the primary JIRA_SA_* names, so the alias param can only be supplied by the alias.
    monkeypatch.setenv(env_var, expected)
    if alias_var:
        # Both spellings set: the primary name wins, so the alias value must never be picked up.
        monkeypatch.setenv(alias_var, "alias-value-that-must-lose")
    # The resolved-auth log line is not under test and repeating it would trip simple_logger's process-wide
    # DuplicateFilter into a summary line that leaks into the next test's caplog records.
    _patch_logger(mocker)
    mocker.patch(
        "apps.jira_utils.jira_information.get_util_config",
        return_value={"client_id": "config-client-id", "client_secret": "config-client-secret"},
    )

    result = process_jira_command_line_config_file(
        config_file_path="/path",
        url="",
        token="",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=None,
        cloud_id=CLOUD_ID,
    )

    # Env beats the config pair; the member with no env value still falls back to the config pair.
    expected_pair = {"client_id": "config-client-id", "client_secret": "config-client-secret"}
    expected_pair[option_name] = expected
    assert (result["client_id"], result["client_secret"]) == (
        expected_pair["client_id"],
        expected_pair["client_secret"],
    )


@pytest.mark.parametrize("env_var", ["JIRA_SA_CLIENT_ID", "JIRA_SA_CLIENT_SECRET"])
def test_oauth_credential_options_do_not_freeze_the_env_as_a_default(monkeypatch, env_var):
    # A getenv() default frozen at import cannot be told apart from an explicit option, so it would bypass both the
    # explicit-empty opt-out and the call-time resolution; the option defaults must stay None whatever the env says.
    monkeypatch.setenv(env_var, "env-value")

    defaults = _source_option_defaults()

    assert defaults["client_id"] is None
    assert defaults["client_secret"] is None


@pytest.mark.parametrize(
    "url,expected",
    [
        pytest.param(SA_GATEWAY_URL_TEMPLATE.format(cloud_id="cloud-id"), "", id="gateway_url_dropped"),
        # A userinfo-bearing gateway url still encodes its cloud id, so the prefix must stay host-agnostic.
        pytest.param(USERINFO_GATEWAY_URL, "", id="userinfo_gateway_url_dropped"),
        pytest.param("https://redhat.atlassian.net", "https://redhat.atlassian.net", id="site_url_unchanged"),
        pytest.param("", "", id="empty_url_unchanged"),
    ],
)
def test_jira_browse_url(url, expected):
    assert _jira_browse_url(url=url) == expected


@pytest.mark.parametrize(
    "test_params",
    [
        {  # Test case 1: Issue with no jira target versions and not resolved status
            "jira_id": "issue1",
            "resolved_status": ["resolved"],
            "jira_target_versions": [],
            "target_version_str": "1.0",
            "file_name": "file1.txt",
            "skip_project_ids": [],
            "expected_jira_error_string": "",
            "test_jira_versions": ["1.0"],
        },
        {  # Test case 2: Issue with no jira target versions, but resolved status
            "jira_id": "issue2",
            "resolved_status": ["open"],
            "jira_target_versions": [],
            "target_version_str": "1.0",
            "file_name": "file2.txt",
            "skip_project_ids": [],
            "expected_jira_error_string": "issue2 current status: open is resolved.",
            "test_jira_versions": ["1.0"],
        },
        {  # Test case 3: Issue with no jira target versions, default resolved status
            "jira_id": "issue3",
            "resolved_status": [],
            "jira_target_versions": [],
            "target_version_str": "",
            "file_name": "file3.txt",
            "skip_project_ids": [],
            "expected_jira_error_string": "",
            "test_jira_versions": ["1.1"],
        },
        {  # Test case 4: Issue with not resolved state, but matching jira target version
            "jira_id": "issue4",
            "resolved_status": ["resolved"],
            "jira_target_versions": ["1.0"],
            "target_version_str": "1.0",
            "file_name": "file4.txt",
            "skip_project_ids": [],
            "expected_jira_error_string": "",
            "test_jira_versions": ["1.0"],
        },
        {  # Test case 5: Issue with not resolved state, and not matching jira target version
            "jira_id": "issue5",
            "resolved_status": ["resolved"],
            "jira_target_versions": ["1.1"],
            "target_version_str": "1.0",
            "file_name": "file5.txt",
            "skip_project_ids": [],
            "expected_jira_error_string": "issue5 target versions: ['1.0'], do not match expected version ['1.1'].",
            "test_jira_versions": ["1.0"],
        },
        {  # Test case 6: Issue that would be skipped for version check because of skip
            "jira_id": "issue6",
            "resolved_status": ["resolved"],
            "jira_target_versions": ["1.0"],
            "target_version_str": "1.0",
            "file_name": "file6.txt",
            "skip_project_ids": ["issue"],
            "expected_jira_error_string": "",
            "test_jira_versions": ["1.1"],
        },
        {  # Test case 7: Issue that would be skipped for version check but fail resolved check
            "jira_id": "issue7",
            "resolved_status": ["open"],
            "jira_target_versions": ["1.0"],
            "target_version_str": "1.0",
            "file_name": "file6.txt",
            "skip_project_ids": ["issue"],
            "expected_jira_error_string": "issue7 current status: open is resolved.",
            "test_jira_versions": ["1.1"],
        },
        {  # Test case 8: Issue with unresolved state, and matching jira z target version
            "jira_id": "issue8",
            "resolved_status": [],
            "jira_target_versions": ["1.2.z"],
            "target_version_str": "1.2.z",
            "file_name": "file4.txt",
            "skip_project_ids": [],
            "expected_jira_error_string": "",
            "test_jira_versions": ["1.2.z"],
        },
        {  # Test case 9: Issue with unresolved state, and jira z target version not matching expected versions
            "jira_id": "issue9",
            "resolved_status": [],
            "jira_target_versions": ["1.2.3"],
            "target_version_str": "1.2.z",
            "file_name": "file4.txt",
            "skip_project_ids": [],
            "expected_jira_error_string": "issue9 target versions: ['1.2.z'], do not match expected version ['1.2.3'].",
            "test_jira_versions": ["1.2.z"],
        },
        {  # Test case 10: Issue with unresolved state, and matching jira z target versions
            "jira_id": "issue10",
            "resolved_status": [],
            "jira_target_versions": ["1.2.z", "1.3.z"],
            "target_version_str": "",
            "file_name": "file4.txt",
            "skip_project_ids": [],
            "expected_jira_error_string": "",
            "test_jira_versions": ["1.2.z", "1.4.0"],
        },
        {  # Test case 11: Issue with unresolved state, and jira target version not matching expected versions
            "jira_id": "issue11",
            "resolved_status": [],
            "jira_target_versions": ["1.2.3"],
            "target_version_str": "",
            "file_name": "file4.txt",
            "skip_project_ids": [],
            "expected_jira_error_string": "issue11 target versions: ['1.2.z', '1.4.0'], do not match expected version ['1.2.3'].",
            "test_jira_versions": ["1.2.z", "1.4.0"],
        },
        {  # Test case 12: Issue with unresolved state, and jira target versions not matching expected versions
            "jira_id": "issue12",
            "resolved_status": [],
            "jira_target_versions": ["1.2.3", "1.5.0"],
            "target_version_str": "",
            "file_name": "file4.txt",
            "skip_project_ids": [],
            "expected_jira_error_string": "issue12 target versions: ['1.2.z', '1.4.0'], do not match expected version ['1.2.3', '1.5.0'].",
            "test_jira_versions": ["1.2.z", "1.4.0"],
        },
    ],
    ids=[
        "test_no_jira_versions_no_resolved_status",
        "test_no_jira_versions_resolved_status",
        "test_no_jira_versions_default_resolved_status",
        "test_matching_target_versions",
        "test_no_target_versions_not_resolved_state",
        "test_skip_version_check",
        "test_skip_version_check_fail_status_check",
        "test_matching_target_z_version",
        "test_non_matching_target_z_version",
        "test_matching_target_version_with_versions",
        "test_non_matching_target_version_with_versions",
        "test_matching_target_versions_with_versions",
    ],
)
def test_get_jira_information(mocker, test_params):
    mock_jira = mocker.MagicMock()
    mock_jira.fields.status.name = "open"
    mock_jira.fixVersions = [mocker.MagicMock()]
    mocker.patch("apps.jira_utils.jira_information.get_issue", return_value=mock_jira)

    if test_jira_versions := test_params.get("test_jira_versions"):
        mocker.patch(
            "apps.jira_utils.jira_information.re.findall",
            return_value=test_jira_versions,
        )

    jira_id = test_params.get("jira_id")
    resolved_status = test_params.get("resolved_status")
    target_version_str = test_params.get("target_version_str")
    skip_project_ids = test_params.get("skip_project_ids")
    jira_target_versions = test_params.get("jira_target_versions")
    file_name = test_params.get("file_name")
    expected_jira_error_string = test_params.get("expected_jira_error_string")

    result = get_jira_information(
        jira_object=mock_jira,
        jira_id=jira_id,
        resolved_status=resolved_status,
        target_version_str=target_version_str,
        skip_project_ids=skip_project_ids,
        jira_target_versions=jira_target_versions,
        file_name=file_name,
    )

    # status_code is None: the issue was fetched successfully.
    assert result == (file_name, expected_jira_error_string, None)


@pytest.mark.parametrize(
    "content_and_expected",
    [
        pytest.param(
            {"content": "pytest.mark.jira(ABC-1111)", "expected": {"ABC-1111"}},
            id="pytest_mark_jira",
        ),
        pytest.param(
            {"content": "JIRA ID is jira_id=ABC-1111", "expected": {"ABC-1111"}},
            id="jira_id=",
        ),
        pytest.param(
            {
                "content": "JIRA URL is https://example.com/browse/ABC-1111",
                "expected": {"ABC-1111"},
            },
            id="jira_url=",
        ),
        pytest.param(
            {
                "content": "pytest.mark.jira(ABC-1111)\nJIRA ID is jira_id=ABC-1112\nJIRA URL is https://example.com/browse/ABC-1113",
                "expected": {"ABC-1111", "ABC-1112", "ABC-1113"},
            },
            id="multiple_jira",
        ),
        pytest.param(
            {
                "content": "pytest.mark.jira(ABC-1111)\nJIRA ID is jira_id=ABC-1111\nJIRA URL is https://example.com/browse/ABC-1111",
                "expected": {"ABC-1111"},
            },
            id="multiple_jira_same_ids",
        ),
        pytest.param({"content": "No Jiera", "expected": set()}, id="no_jira"),
    ],
)
def test_get_jira_ids_from_file_content(content_and_expected):
    jira_ids = get_jira_ids_from_file_content(
        file_content=content_and_expected["content"],
        issue_pattern=r"([A-Z]+-[0-9]+)",
        jira_url="https://example.com",
    )
    assert jira_ids == content_and_expected["expected"]


@pytest.mark.parametrize(
    "jira_url,expected",
    [
        pytest.param("", {"CNV-1234"}, id="empty_url_matches_site_link"),
        pytest.param(SA_GATEWAY_URL_TEMPLATE.format(cloud_id="cloud-id"), set(), id="gateway_url_matches_nothing"),
    ],
)
def test_get_jira_ids_from_file_content_matches_site_link(jira_url, expected):
    # Source links point at the site host, so a gateway url must not be required for the /browse/ pattern.
    jira_ids = get_jira_ids_from_file_content(
        file_content="JIRA URL is https://redhat.atlassian.net/browse/CNV-1234",
        issue_pattern=r"([A-Z]+-[0-9]+)",
        jira_url=jira_url,
    )
    assert jira_ids == expected


def test_jira_api_error(mocker):
    mock_jira = mocker.MagicMock()
    mocker.patch(
        "apps.jira_utils.jira_information.get_issue", side_effect=JIRAError(status_code=404, text="Issue not found")
    )

    result = get_jira_information(
        jira_object=mock_jira,
        jira_id="404",
        resolved_status=[],
        target_version_str="",
        skip_project_ids=[],
        jira_target_versions=[],
        file_name="",
    )

    assert result == ("", "404 JiraError status code: 404, details: Issue not found.", 404)


def test_get_jira_information_retry_error_reports_per_issue_error(mocker, caplog):
    caplog.set_level(logging.DEBUG)
    mocker.patch.object(get_issue.retry, "sleep")
    mock_jira = mocker.MagicMock()
    mock_jira.issue.side_effect = JIRAError(status_code=500, text="Server error")

    # The real retry wrapper is in play: tenacity raises RetryError, which is not a JIRAError.
    result = get_jira_information(
        jira_object=mock_jira,
        jira_id="ABC-500",
        resolved_status=[],
        target_version_str="",
        skip_project_ids=[],
        jira_target_versions=[],
        file_name="file.py",
    )

    assert result == ("file.py", "ABC-500 JiraError status code: 500, details: Server error.", 500)
    # No traceback: JIRAError.__str__ re-renders the raw body and headers, and this must stay a single line.
    assert [record for record in caplog.records if record.exc_info] == []
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize("status_code", [401, 403])
def test_jira_auth_error_keeps_per_issue_text_short(mocker, status_code):
    mocker.patch(
        "apps.jira_utils.jira_information.get_issue",
        side_effect=JIRAError(status_code=status_code, text="x" * 300 + "\n\nsecond line\nthird line"),
    )

    result = get_jira_information(
        jira_object=mocker.MagicMock(),
        jira_id="ABC-1",
        resolved_status=[],
        target_version_str="",
        skip_project_ids=[],
        jira_target_versions=[],
        file_name="",
    )

    details = result[1].removeprefix(f"ABC-1 JiraError status code: {status_code}, details: ").removesuffix(".")
    # Only a confirmed credential rejection omits the body; a per-issue 401/403 keeps it, collapsed and bounded.
    assert set(details) == {"x"}
    assert len(details) == 200
    assert "--cloud-id" not in result[1]
    assert "Re-mint" not in result[1]
    assert result[2] == status_code


def test_jira_per_issue_forbidden_surfaces_permission_gap_body(mocker):
    mocker.patch(
        "apps.jira_utils.jira_information.get_issue",
        side_effect=JIRAError(status_code=403, text="You do not have permission to see this issue"),
    )

    result = get_jira_information(
        jira_object=mocker.MagicMock(),
        jira_id="ABC-1",
        resolved_status=[],
        target_version_str="",
        skip_project_ids=[],
        jira_target_versions=[],
        file_name="",
    )

    # A 403 with a healthy credential is a permission gap, so the body must reach the user.
    assert result[1] == "ABC-1 JiraError status code: 403, details: You do not have permission to see this issue."
    assert result[2] == 403


def test_auth_failure_hint_service_account():
    hint = _auth_failure_hint(mode=AUTH_MODE_SERVICE_ACCOUNT)
    assert "Re-mint" in hint
    assert "expired" in hint
    assert "--cloud-id" in hint


@pytest.mark.parametrize("mode", [AUTH_MODE_CLOUD_BASIC, AUTH_MODE_DATACENTER_PAT])
def test_auth_failure_hint_generic(mode):
    hint = _auth_failure_hint(mode=mode)
    assert "JIRA_TOKEN" in hint
    assert "JIRA_USER" in hint
    # A datacenter/user-token user has no cloud id to pass.
    assert "--cloud-id" not in hint


@pytest.mark.parametrize("status_code", [401, 403])
def test_report_jira_auth_error_redacts_long_token(caplog, status_code):
    caplog.set_level(logging.DEBUG)
    token = "s3cr3t-token-value"
    body = f"{token}\n\nsecond line\nthird line" + "x" * 300

    with pytest.raises(SystemExit) as exc_info:
        _report_jira_auth_error(
            exp=JIRAError(status_code=status_code, text=body),
            jira_config_dict={"url": "", "cloud": False, "cloud_id": "cloud-id", "token": token},
        )

    assert exc_info.value.code == 1
    messages = [record.getMessage() for record in caplog.records]
    # Raw newlines from Jira's response are collapsed into a single line.
    assert all("\n" not in message for message in messages)
    detail_messages = [message for message in messages if f": {status_code} " in message]
    assert len(detail_messages) == 1
    detail = detail_messages[0].split(f": {status_code} ", 1)[1]
    # Single line, exactly 200 characters, token redacted.
    assert len(detail) == 200
    assert detail.startswith("*** second line third line")
    assert detail[26:] == "x" * 174
    assert token not in detail
    assert AUTH_MODE_SERVICE_ACCOUNT in detail_messages[0]
    assert len(_hint_logs(messages, AUTH_MODE_SERVICE_ACCOUNT)) == 1


LONG_TOKEN = "sa-" + "A" * 247  # 250 chars: longer than the 200-character detail window.
STRADDLING_TOKEN = "SCOPED-TOKEN-PREFIX-" + "m" * 155 + "-END-OF-TOKEN-MARKER"  # 195 chars.


@pytest.mark.parametrize("status_code", [401, 403])
@pytest.mark.parametrize(
    ("token", "body", "expected_len"),
    [
        # Longer than the window: redaction must run before truncation or the cut token leaks.
        (LONG_TOKEN, f"Jira rejected the token {LONG_TOKEN} for this site", None),
        # Straddles the cut (100 x's before, 100 y's after): truncating first keeps the token head in the detail.
        (STRADDLING_TOKEN, "x" * 100 + STRADDLING_TOKEN + "y" * 100, 200),
        # Newlines around the token are collapsed only after redaction, so `***` survives into the single line.
        (STRADDLING_TOKEN, f"\n\n  {STRADDLING_TOKEN}  \n\nnext line", None),
        # Token genuinely split by a newline: only contiguous after the collapse, so a second pass must redact it.
        (LONG_TOKEN, f"Jira rejected the token {LONG_TOKEN[:125]}\n{LONG_TOKEN[125:]} for this site", None),
    ],
    ids=["longer_than_window", "straddling_boundary", "token_between_newlines", "token_split_by_newline"],
)
def test_report_jira_auth_error_redacts_token_longer_than_window(caplog, status_code, token, body, expected_len):
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SystemExit) as exc_info:
        _report_jira_auth_error(
            exp=JIRAError(status_code=status_code, text=body),
            jira_config_dict={"url": "", "cloud": False, "cloud_id": "cloud-id", "token": token},
        )

    assert exc_info.value.code == 1
    messages = [record.getMessage() for record in caplog.records]
    assert all("\n" not in message for message in messages)
    detail_messages = [message for message in messages if f": {status_code} " in message]
    assert len(detail_messages) == 1
    detail = detail_messages[0].split(f": {status_code} ", 1)[1]
    # Bounded, redacted, and no slice of the secret anywhere in the rendered detail.
    assert len(detail) <= 200
    assert "***" in detail
    assert token[:40] not in detail
    assert token[-40:] not in detail
    # Neither half of a newline-split token may survive the collapse.
    assert token[: len(token) // 2] not in detail
    assert token[len(token) // 2 :] not in detail
    if expected_len is not None:
        assert len(detail) == expected_len


MINTED_TOKEN = "atlassian-" + "B" * 250  # 260 chars: longer than the 200-character detail window.


@pytest.mark.parametrize("status_code", [pytest.param(401, id="401"), pytest.param(403, id="403")])
def test_get_jira_mismatch_minted_token_never_reaches_a_log(mocker, monkeypatch, status_code):
    _clear_jira_env(monkeypatch)
    mock_logger = _patch_logger(mocker)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token", return_value=MINTED_TOKEN)
    # A rejected access token is echoed back in the response body, which is where the live bearer would leak.
    mocker.patch(
        "apps.jira_utils.jira_information.JIRA",
        side_effect=JIRAError(status_code=status_code, text=f"Token {MINTED_TOKEN} is invalid\n\nsecond line"),
    )
    mock_get_jiras = mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})

    result = CliRunner().invoke(get_jira_mismatch, [*OAUTH_ARGS, "--check-auth"])

    assert result.exit_code == 1
    messages = _logged(mock_logger, "error") + _logged(mock_logger, "info") + [result.output]
    detail_messages = [message for message in messages if f": {status_code} " in message]
    assert len(detail_messages) == 1
    detail = detail_messages[0].split(f": {status_code} ", 1)[1]
    # Only true because the mint runs before the client is built: the redactor must see the live bearer.
    assert detail.startswith("Token *** is invalid second line")
    assert not [message for message in messages if MINTED_TOKEN[:40] in message or MINTED_TOKEN[-40:] in message]
    mock_get_jiras.assert_not_called()


@pytest.mark.parametrize("status_code", [401, 403])
def test_report_jira_auth_error_keeps_body_with_short_token(caplog, status_code):
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SystemExit) as exc_info:
        _report_jira_auth_error(
            exp=JIRAError(status_code=status_code, text="connection refused"),
            jira_config_dict={"url": "", "cloud": False, "cloud_id": "cloud-id", "token": "t"},
        )

    assert exc_info.value.code == 1
    messages = [record.getMessage() for record in caplog.records]
    detail_messages = [message for message in messages if f": {status_code} " in message]
    assert len(detail_messages) == 1
    # A single-character token cannot be a real one, so redacting it must not corrupt unrelated words.
    assert detail_messages[0].split(f": {status_code} ", 1)[1] == "connection refused"
    assert "***" not in caplog.text
    assert len(_hint_logs(messages, AUTH_MODE_SERVICE_ACCOUNT)) == 1


def test_report_jira_auth_error_collapses_non_auth_detail(caplog):
    caplog.set_level(logging.DEBUG)
    long_multiline_text = "x" * 300 + "\n\nsecond line\nthird line"

    with pytest.raises(SystemExit) as exc_info:
        _report_jira_auth_error(
            exp=JIRAError(status_code=500, text=long_multiline_text),
            jira_config_dict={"url": "", "cloud": False, "cloud_id": "cloud-id", "token": "s3cr3t-token-value"},
        )

    assert exc_info.value.code == 1
    messages = [record.getMessage() for record in caplog.records]
    # Raw newlines from Jira's response are collapsed into a single line of at most 200 characters.
    assert all("\n" not in message for message in messages)
    detail_messages = [message for message in messages if "500" in message]
    assert len(detail_messages) == 1
    assert "x" * 200 in detail_messages[0]
    assert "x" * 201 not in detail_messages[0]
    assert "second line" not in detail_messages[0]
    # A 500 is not a credential problem, so no token advice is emitted.
    assert len(_hint_logs(messages, AUTH_MODE_SERVICE_ACCOUNT)) == 0
    assert "s3cr3t-token-value" not in caplog.text


def test_check_jira_auth_success_logs_identity(mocker, caplog):
    caplog.set_level(logging.DEBUG)
    mock_jira = mocker.MagicMock()
    mock_jira.myself.return_value = {"displayName": "display-name", "emailAddress": "a@b.c", "accountId": "id-1"}

    _check_jira_auth(
        jira_obj=mock_jira,
        jira_config_dict={"url": "", "cloud": False, "cloud_id": "cloud-id", "token": "s3cr3t-token-value"},
    )

    assert AUTH_MODE_SERVICE_ACCOUNT in caplog.text
    assert SA_GATEWAY_URL_TEMPLATE.format(cloud_id=MASKED_CLOUD_ID) in caplog.text
    assert SA_GATEWAY_URL_TEMPLATE.format(cloud_id="cloud-id") not in caplog.text
    assert "display-name" in caplog.text
    assert "s3cr3t-token-value" not in caplog.text


def test_cloud_id_never_appears_in_logs(mocker, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _clear_jira_env(monkeypatch)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    # The project-visibility verdict below makes the header-less probe, which must never leave the process.
    _mock_anonymous_probe(mocker)

    def assert_masked(*expected):
        """Assert every record avoids CLOUD_ID while the given text was actually logged."""
        messages = [record.getMessage() for record in caplog.records]
        # Without this the guard would pass on a run that logged nothing at all.
        assert messages, "no log records captured"
        assert all(CLOUD_ID not in message for message in messages), messages
        assert CLOUD_ID not in caplog.text
        for text in expected:
            assert any(text in message for message in messages), (text, messages)
        caplog.clear()

    # The resolved-flavour line names the gateway url it authenticates against.
    process_jira_command_line_config_file(
        config_file_path="/path",
        url="",
        token="t",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=None,
        cloud_id=CLOUD_ID,
    )
    assert_masked(
        f"Using Jira service-account (pre-minted scoped API token) authentication against {MASKED_GATEWAY_URL}"
    )

    # A 401 in service-account mode goes through the auth-error reporter, whose server field is masked too.
    with pytest.raises(SystemExit):
        _report_jira_auth_error(
            exp=JIRAError(status_code=401, text="Unauthorized"),
            jira_config_dict={"url": "", "cloud": False, "cloud_id": CLOUD_ID, "token": "s3cr3t-token-value"},
        )
    assert_masked("Jira authentication failed", f"server: {MASKED_GATEWAY_URL}")

    # --check_jira_auth, success and failure.
    mock_jira = mocker.MagicMock()
    mock_jira.myself.return_value = {"displayName": "display-name", "emailAddress": "a@b.c", "accountId": "id-1"}
    _check_jira_auth(
        jira_obj=mock_jira,
        jira_config_dict={"url": "", "cloud": False, "cloud_id": CLOUD_ID, "token": "t"},
    )
    assert_masked(f"Jira authentication OK - mode: {AUTH_MODE_SERVICE_ACCOUNT}, server: {MASKED_GATEWAY_URL}")

    # A scope-limited credential is 401/403 on /myself, so its success line is the project-visibility variant. An
    # empty project list proves nothing, so the (non-empty) list is what makes the probe conclusive.
    mock_jira.myself.side_effect = JIRAError(status_code=401, text="Unauthorized")
    mock_jira.projects.return_value = [{"key": "ABC"}]
    _check_jira_auth(
        jira_obj=mock_jira,
        jira_config_dict={"url": "", "cloud": False, "cloud_id": CLOUD_ID, "token": "t"},
    )
    assert_masked(
        f"Jira authentication OK - mode: {AUTH_MODE_SERVICE_ACCOUNT}, server: {MASKED_GATEWAY_URL}, "
        "service account: project visibility verified"
    )

    mock_jira.projects.side_effect = JIRAError(status_code=401, text="Unauthorized")
    with pytest.raises(SystemExit):
        _check_jira_auth(
            jira_obj=mock_jira,
            jira_config_dict={"url": "", "cloud": False, "cloud_id": CLOUD_ID, "token": "t"},
        )
    assert_masked("Jira authentication failed", f"server: {MASKED_GATEWAY_URL}")

    # The invalid-cloud-id error names the failure without echoing the value, so a typo stays diagnosable.
    with pytest.raises(SystemExit):
        process_jira_command_line_config_file(
            config_file_path="/path",
            url="",
            token="t",
            issue_pattern="*",
            resolved_statuses=[],
            version_string_not_targeted_jiras="",
            target_versions=[],
            skip_projects=[],
            user="",
            cloud=None,
            cloud_id=f"{CLOUD_ID}!",
        )
    assert_masked("Invalid Jira cloud id", MASKED_CLOUD_ID)


REPORT_TOKEN = "s3cr3t-token-value"
# The cloud id shown up-front in the error detail, split across lines (a wrapping proxy) and past the 200-char cut.
CLOUD_ID_ECHOES = (
    f"denied for cloud {CLOUD_ID} with {REPORT_TOKEN}",
    "denied for cloud " + "\n".join(CLOUD_ID),
    "z" * 180 + CLOUD_ID,
)


@pytest.mark.parametrize("body", [pytest.param(body, id=f"body-{index}") for index, body in enumerate(CLOUD_ID_ECHOES)])
@pytest.mark.parametrize(
    "jira_config_dict",
    [
        pytest.param({"url": "", "cloud": False, "cloud_id": CLOUD_ID, "token": REPORT_TOKEN}, id="cloud-id-option"),
        pytest.param({"url": GATEWAY_URL, "cloud": False, "cloud_id": "", "token": REPORT_TOKEN}, id="gateway-url"),
    ],
)
def test_report_jira_auth_error_masks_the_cloud_id_in_the_detail(caplog, jira_config_dict, body):
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SystemExit) as exc_info:
        _report_jira_auth_error(exp=JIRAError(status_code=401, text=body), jira_config_dict=jira_config_dict)

    assert exc_info.value.code == 1
    messages = [record.getMessage() for record in caplog.records]
    detail_messages = [message for message in messages if ": 401 " in message]
    assert len(detail_messages) == 1
    detail = detail_messages[0].split(": 401 ", 1)[1]
    # The cloud id is masked whether it came from --cloud-id or from the gateway url it is embedded in.
    assert CLOUD_ID not in caplog.text
    assert MASKED_CLOUD_ID in detail
    assert REPORT_TOKEN not in caplog.text
    # Masking runs on the full body, so the 200-character bound still holds and a cut through the echoed id leaks
    # nothing: a truncation-first implementation would leave a prefix of the cloud id in the message.
    assert len(detail) <= 200
    assert CLOUD_ID[:20] not in detail


@pytest.mark.parametrize(
    "derived_cloud_id",
    [pytest.param(CLOUD_ID, id="uuid")],
)
def test_error_detail_redacts_a_cloud_id_derived_from_a_gateway_url(caplog, derived_cloud_id):
    caplog.set_level(logging.DEBUG)
    # A gateway url with a trailing slash still encodes only the id.
    gateway_url = f"{SA_GATEWAY_URL_TEMPLATE.format(cloud_id=derived_cloud_id)}/"

    with pytest.raises(SystemExit) as exc_info:
        _report_jira_auth_error(
            exp=JIRAError(status_code=401, text=f"denied for tenant {derived_cloud_id}"),
            jira_config_dict={"url": gateway_url, "cloud": False, "cloud_id": "", "token": REPORT_TOKEN},
        )

    assert exc_info.value.code == 1
    detail_messages = [record.getMessage() for record in caplog.records if ": 401 " in record.getMessage()]
    assert len(detail_messages) == 1
    detail = detail_messages[0].split(": 401 ", 1)[1]
    # The bare id is what a body would echo, so that is what the redaction has to match.
    assert derived_cloud_id not in detail
    assert MASKED_CLOUD_ID in detail
    assert derived_cloud_id not in caplog.text


def test_error_detail_redacts_a_cloud_id_derived_from_a_userinfo_gateway_url(caplog):
    caplog.set_level(logging.DEBUG)

    # The gateway url supplies the cloud id only through its own path, and its userinfo must not hide it from the
    # derivation: the id is then redacted out of the body exactly like a --cloud-id one, and the password is masked.
    with pytest.raises(SystemExit) as exc_info:
        _report_jira_auth_error(
            exp=JIRAError(status_code=401, text=f"denied for tenant {CLOUD_ID}"),
            jira_config_dict={"url": USERINFO_GATEWAY_URL, "cloud": False, "cloud_id": "", "token": REPORT_TOKEN},
        )

    assert exc_info.value.code == 1
    detail_messages = [record.getMessage() for record in caplog.records if ": 401 " in record.getMessage()]
    assert len(detail_messages) == 1
    detail = detail_messages[0].split(": 401 ", 1)[1]
    assert MASKED_CLOUD_ID in detail
    assert CLOUD_ID not in caplog.text
    assert USERINFO_PASSWORD not in caplog.text
    assert "user:" not in caplog.text


@pytest.mark.parametrize("status_code", [pytest.param(401, id="401"), pytest.param(403, id="403")])
def test_error_detail_masks_the_userinfo_of_a_body_url(caplog, status_code):
    caplog.set_level(logging.DEBUG)
    # A transport error and an echoing proxy both quote the url whole, credential included.
    body = (
        f"Invalid URL {USERINFO_SITE_URL}/: No host supplied "
        f"for tenant {CLOUD_ID} with token {REPORT_TOKEN} " + "x" * 300
    )

    with pytest.raises(SystemExit) as exc_info:
        _report_jira_auth_error(
            exp=JIRAError(status_code=status_code, text=body),
            jira_config_dict={"url": "", "cloud": False, "cloud_id": CLOUD_ID, "token": REPORT_TOKEN},
        )

    assert exc_info.value.code == 1
    detail_messages = [record.getMessage() for record in caplog.records if f": {status_code} " in record.getMessage()]
    assert len(detail_messages) == 1
    detail = detail_messages[0].split(f": {status_code} ", 1)[1]
    # The url stays readable while its userinfo is gone, before anything else touches the body.
    assert detail.startswith("Invalid URL https://site.atlassian.net/: No host supplied")
    assert "user:" not in detail
    assert "sup3r-s3cret" not in caplog.text
    # The same string still gets the token and cloud-id redaction, and the masking does not cost it the 200 cap.
    assert "with token ***" in detail
    assert MASKED_CLOUD_ID in detail
    assert REPORT_TOKEN not in caplog.text
    assert CLOUD_ID not in caplog.text
    assert len(detail) == 200


def test_transport_log_filter_masks_a_configured_password_holding_a_quote(mocker, caplog):
    caplog.set_level(logging.DEBUG)
    # The text mask ends a url at a quote, so this password is not delimited there: the filter must mask the userinfo
    # it was configured with instead of relying on that regex.
    _run_jira_command(mocker, "--url", USERINFO_QUOTED_SITE_URL, "--token", "t", "--cloud-id", "", "--check-auth")

    logging.getLogger("jira.resilientsession").warning("GET %s/rest/api/2/issue/ABC-1", USERINFO_QUOTED_SITE_URL)
    # A body echoing the same url must not leak it either: this module's own lines bypass the record filter.
    detail = _error_detail(text=f'{{"url": "{USERINFO_QUOTED_SITE_URL}"}}', token="t")

    messages = [record.getMessage() for record in caplog.records if record.name == "jira.resilientsession"]
    assert len(messages) == 1
    assert messages[0] == "GET https://***@site.atlassian.net/rest/api/2/issue/ABC-1"
    assert USERINFO_QUOTED_PASSWORD not in caplog.text
    # The url stays readable around the redaction, which is what makes the log line still useful.
    assert "https://***@site.atlassian.net" in detail
    assert USERINFO_QUOTED_PASSWORD not in detail


def test_discarded_site_url_is_logged_without_its_userinfo(mocker):
    mock_logger = _patch_logger(mocker)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})

    process_jira_command_line_config_file(
        config_file_path="/path",
        url=USERINFO_SITE_URL,
        token="t",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=None,
        cloud_id=CLOUD_ID,
    )

    discarded = [message for message in _logged(mock_logger, "info") if "discarding supplied Jira url" in message]
    assert len(discarded) == 1
    # The url is only logged to say it was discarded, so the embedded credential must not come along.
    assert "https://site.atlassian.net" in discarded[0]
    assert "sup3r-s3cret" not in discarded[0]
    assert "user:" not in discarded[0]


@pytest.mark.parametrize(
    "server,expected_server,expected_userinfo",
    [
        # urlsplit and requests treat the LAST "@" as the userinfo delimiter, so a password holding one must be
        # masked in full: stopping at the first one leaves the tail ("ss@issues.redhat.com") in the log.
        pytest.param(
            "https://user:p@ss@issues.redhat.com",
            "https://issues.redhat.com",
            "https://issues.redhat.com",
            id="password-with-at",
        ),
        # An encoded "@" never splits the authority, so it is one userinfo component like any other.
        pytest.param(
            "https://user:p%40ss@issues.redhat.com",  # pragma: allowlist secret
            "https://issues.redhat.com",
            "https://issues.redhat.com",
            id="encoded-at",
        ),
        pytest.param(
            "https://issues.redhat.com",
            "https://issues.redhat.com",
            "https://issues.redhat.com",
            id="no-userinfo-unchanged",
        ),
        # The cloud id is still masked by _mask_server, and only after the userinfo is dropped: the gateway prefix
        # check needs it gone first.
        pytest.param(
            f"https://user:p@ss@api.atlassian.com/ex/jira/{CLOUD_ID}",
            MASKED_GATEWAY_URL,
            f"{SA_GATEWAY_URL_PREFIX}{CLOUD_ID}",
            id="gateway-url",
        ),
    ],
)
def test_mask_server_drops_the_whole_userinfo(server, expected_server, expected_userinfo):
    assert _mask_server(server=server) == expected_server
    assert _strip_userinfo(url=server) == expected_userinfo


def test_mask_userinfo_leaves_a_free_form_user_at_host_alone():
    # Error text is passed in whole, so the url must be delimited by a scheme and its authority: a bare
    # "user@host" after whitespace is a mail address in the message, not a credential.
    text = "connection failed for https://user:p@ss@site.atlassian.net as jdoe@issues.redhat.com"
    expected = "connection failed for https://site.atlassian.net as jdoe@issues.redhat.com"

    assert _mask_userinfo(text=text) == expected
    # Only a real url has an authority to parse, so a bare address is never taken for a credential.
    assert _strip_userinfo(url="jdoe@issues.redhat.com") == "jdoe@issues.redhat.com"


@pytest.mark.parametrize(
    "server,expected",
    [
        # requests accepts a space in a userinfo password and sends it as Basic auth, so urlsplit's whole authority
        # has to be dropped: a pattern match over the authority stops at the space and leaks the credential.
        pytest.param("https://bob:ab cd@jira.example.com/x", "https://jira.example.com/x", id="password-with-space"),
        # urlsplit raises on an authority holding an unmatched bracket, so the parse-based drop cannot run: brackets
        # survive requests' requote_uri unencoded, so such a password works with the client and must still be dropped.
        pytest.param("https://user:pa[ss@site.atlassian.net", "https://site.atlassian.net", id="bracketed-userinfo"),
        # An "@" after the path or in the query is not a userinfo, and the host must survive untouched.
        pytest.param("https://jira.example.com?q=a@b", "https://jira.example.com?q=a@b", id="query-with-at"),
        pytest.param("https://jira.example.com/x?a=b@c", "https://jira.example.com/x?a=b@c", id="path-and-query"),
    ],
)
def test_strip_userinfo_parses_the_authority(server, expected):
    assert _strip_userinfo(url=server) == expected
    assert _mask_server(server=server) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # A JSON body quoting a url: the quote delimits the url, so the email address in the next key is not eaten.
        pytest.param(
            '{"self":"https://x.atlassian.net","emailAddress":"a@b.c"}',
            '{"self":"https://x.atlassian.net","emailAddress":"a@b.c"}',
            id="json-body-quoting-a-url",
        ),
        pytest.param(
            "see https://bob:ab-cd@jira.example.com/x for details",  # pragma: allowlist secret
            "see https://jira.example.com/x for details",
            id="url-in-prose",
        ),
        # A `,` is legal unencoded in a url (requests' requote_uri leaves it alone), so it cannot delimit one: the
        # credential must be stripped here exactly as _strip_userinfo strips it from the url on its own.
        pytest.param(
            "see https://bob:ab-cd@jira.example.com/x, then wait",  # pragma: allowlist secret
            "see https://jira.example.com/x, then wait",
            id="url-followed-by-a-comma",
        ),
        pytest.param(
            "(see https://bob:ab-cd@jira.example.com/x)",  # pragma: allowlist secret
            "(see https://jira.example.com/x)",
            id="url-in-parentheses",
        ),
    ],
)
def test_mask_userinfo_delimits_a_url_inside_free_form_text(text, expected):
    assert _mask_userinfo(text=text) == expected


@pytest.mark.parametrize(
    "gateway_url",
    [
        pytest.param(f"HTTPS://API.Atlassian.com/ex/jira/{CLOUD_ID}", id="uppercase-scheme-and-host"),
        pytest.param(f"https://{SA_GATEWAY_HOST}:443/ex/jira/{CLOUD_ID}", id="explicit-default-port"),
        pytest.param(f"HTTPS://{SA_GATEWAY_HOST}:443/EX/JIRA/{CLOUD_ID}", id="uppercase-path"),
    ],
)
def test_gateway_url_is_recognised_whatever_its_case_and_port(gateway_url):
    assert _is_gateway_url(url=gateway_url) is True
    # All three decisions agree, or the cloud id leaks from the very lines that mask the url.
    assert _mask_server(server=gateway_url) == MASKED_GATEWAY_URL
    assert _effective_cloud_id(url=gateway_url, cloud_id="") == CLOUD_ID
    mode, _ = _jira_auth_mode(url=gateway_url, cloud=False, cloud_id="")
    assert mode == AUTH_MODE_SERVICE_ACCOUNT
    # A query that merely mentions the gateway is not one, so it cannot mislabel the auth mode either.
    assert _is_gateway_url(url=f"https://evil.example?a=@api.atlassian.com/ex/jira/{CLOUD_ID}") is False


@pytest.mark.parametrize(
    "gateway_url",
    [
        # requests resolves a trailing-dot FQDN to the same host, so the cloud id must not be echoed for it.
        pytest.param(f"https://{SA_GATEWAY_HOST}./ex/jira/{CLOUD_ID}", id="trailing-dot-fqdn"),
    ],
)
def test_gateway_url_with_a_trailing_dot_host_is_recognised(gateway_url):
    assert _is_gateway_url(url=gateway_url) is True
    assert _effective_cloud_id(url=gateway_url, cloud_id="") == CLOUD_ID
    assert _mask_server(server=gateway_url) == MASKED_GATEWAY_URL


@pytest.mark.parametrize(
    "server",
    [
        pytest.param(f"https://{SA_GATEWAY_HOST}", id="bare-gateway-host"),
        pytest.param(f"https://{SA_GATEWAY_HOST}/", id="bare-gateway-host-trailing-slash"),
    ],
)
def test_the_bare_gateway_host_is_a_gateway_url_with_no_id(server):
    # Failing fast is the point: taken for a Data Center url it silently switches the run to PAT mode.
    assert _is_gateway_url(url=server) is True
    mode, _ = _jira_auth_mode(url=server, cloud=False, cloud_id="")
    assert mode == AUTH_MODE_SERVICE_ACCOUNT
    assert _effective_cloud_id(url=server, cloud_id="") == ""
    # There is no id segment, so nothing is rewritten into a path the input never had.
    assert _mask_server(server=server) == server


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(f"https://{SA_GATEWAY_HOST}:notaport/ex/jira/{CLOUD_ID}", id="non-numeric-port"),
        # urlsplit itself raises ValueError here, and these helpers exist to render a bad url safely, never to crash.
        pytest.param("https://[bad", id="unparseable-authority"),
    ],
)
def test_url_helpers_treat_a_malformed_url_as_a_plain_url(url):
    assert _is_gateway_url(url=url) is False
    assert _strip_userinfo(url=url) == url
    assert _mask_server(server=url) == url


def test_a_path_merely_starting_with_the_gateway_prefix_is_not_a_gateway_url():
    url = f"https://{SA_GATEWAY_HOST}/ex/jirafoo/{CLOUD_ID}"

    # _effective_cloud_id matches /ex/jira/ only, so a looser gateway predicate fails the run with a misleading
    # "missing its cloud id" and rewrites a non-gateway path in the logs.
    assert _is_gateway_url(url=url) is False
    assert _effective_cloud_id(url=url, cloud_id="") == ""
    assert _mask_server(server=url) == url


@pytest.mark.parametrize(
    "garbage_segment",
    [pytest.param("../../admin", id="traversal"), pytest.param("z", id="one-character")],
)
def test_garbage_derived_cloud_id_leaves_error_bodies_readable(caplog, garbage_segment):
    caplog.set_level(logging.DEBUG)
    gateway_url = f"{SA_GATEWAY_URL_PREFIX}{garbage_segment}"
    body = "Issue ABC-1: cannot be carried out"

    # A segment that is not a cloud id is never used as a redaction pattern, so it cannot pass the missing-id guard
    # and then shred every error body it happens to match (".." would turn "cannot" into a placeholder).
    assert _effective_cloud_id(url=gateway_url, cloud_id="") == ""
    assert _error_detail(text=body, cloud_id=_effective_cloud_id(url=gateway_url, cloud_id="")) == body

    with pytest.raises(SystemExit):
        _report_jira_auth_error(
            exp=JIRAError(status_code=401, text=body),
            jira_config_dict={"url": gateway_url, "cloud": False, "cloud_id": "", "token": REPORT_TOKEN},
        )
    detail_messages = [record.getMessage() for record in caplog.records if ": 401 " in record.getMessage()]
    assert len(detail_messages) == 1
    assert detail_messages[0].endswith(body)


TRANSPORT_CLOUD_ID = "ffffffff-1111-2222-3333-444444444444"
RESOLVER_CLOUD_ID = "dddddddd-9999-aaaa-bbbb-cccccccccccc"


def test_transport_retry_log_masks_the_cloud_id_at_its_original_level(mocker, caplog):
    caplog.set_level(logging.DEBUG)
    # The filter is installed at the CLI boundary, before the client makes its first request.
    _run_jira_command(mocker, "--cloud-id", TRANSPORT_CLOUD_ID, "--token", "t", "--check-auth")

    # The jira library logs the full request url (which encodes the cloud id) on a retry, below this module.
    logging.getLogger("jira.resilientsession").warning(
        "GET https://api.atlassian.com/ex/jira/%s/rest/api/2/issue/ABC-1", TRANSPORT_CLOUD_ID
    )

    records = [record for record in caplog.records if record.name == "jira.resilientsession"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert TRANSPORT_CLOUD_ID not in message
    assert f"ex/jira/{MASKED_CLOUD_ID}/rest" in message
    # The mask must not cost the record its level or its delivery to the handler.
    assert records[0].levelno == logging.WARNING
    assert records[0].levelname == "WARNING"
    assert TRANSPORT_CLOUD_ID not in caplog.text


FILTER_CLOUD_ID = "eeeeeeee-5555-6666-7777-888888888888"


def test_transport_log_filter_masks_bytes_and_non_str_payloads(caplog):
    caplog.set_level(logging.DEBUG)
    _install_log_secret_filter(cloud_id=FILTER_CLOUD_ID)
    transport = logging.getLogger("jira.resilientsession")

    # python-jira logs the raw response body as bytes (response.content), so the mask keeps the payload's type and
    # encodes the same <cloud-id> placeholder a str payload gets.
    transport.debug("response.body:\n%s", f'{{"cloudId": "{FILTER_CLOUD_ID}"}}'.encode())
    # Any other arg (an exception, a dict) only leaks through its string form.
    transport.warning("GET %s failed: %s", GATEWAY_URL, RuntimeError(f"boom for {FILTER_CLOUD_ID}"))
    transport.info("payload %s", {"cloudId": FILTER_CLOUD_ID})
    # A record that never mentions the cloud id is passed through untouched, level included.
    transport.warning("retrying in 1s")

    records = [record for record in caplog.records if record.name == "jira.resilientsession"]
    assert len(records) == 4
    assert all(FILTER_CLOUD_ID not in record.getMessage() for record in records)
    assert isinstance(records[0].args[0], bytes)
    assert records[0].args[0] == b'{"cloudId": "<cloud-id>"}'
    assert MASKED_CLOUD_ID in records[1].getMessage()
    assert MASKED_CLOUD_ID in records[2].getMessage()
    assert records[3].getMessage() == "retrying in 1s"
    assert [record.levelno for record in records] == [logging.DEBUG, logging.WARNING, logging.INFO, logging.WARNING]


def test_transport_log_filter_masks_userinfo_in_a_bytes_payload(caplog):
    caplog.set_level(logging.DEBUG)
    _install_log_secret_filter(cloud_id=FILTER_CLOUD_ID)
    transport = logging.getLogger("jira.resilientsession")
    body = f'{{"url": "https://user:{USERINFO_PASSWORD}@api.atlassian.com/ex/jira/{FILTER_CLOUD_ID}"}}'

    transport.debug("response.body:\n%s", body.encode())
    # A non-UTF-8 body must survive the decode/encode pair byte for byte, userinfo masking included.
    transport.debug("response.body:\n%s", b"boom \xff\xfe for https://user:p@ss@site.atlassian.net")

    records = [record for record in caplog.records if record.name == "jira.resilientsession"]
    assert len(records) == 2
    assert all(isinstance(record.args[0], bytes) for record in records)
    assert records[0].args[0] == f'{{"url": "{SA_GATEWAY_URL_PREFIX}{MASKED_CLOUD_ID}"}}'.encode()
    assert records[1].args[0] == b"boom \xff\xfe for https://site.atlassian.net"
    assert [record.levelno for record in records] == [logging.DEBUG, logging.DEBUG]
    assert USERINFO_PASSWORD not in caplog.text
    assert FILTER_CLOUD_ID not in caplog.text


@pytest.mark.parametrize(
    "args,expected_url",
    [
        # No --cloud-id: the id exists only inside the userinfo-bearing gateway url.
        pytest.param(
            ("--url", USERINFO_GATEWAY_URL, "--token", "t", "--cloud-id", ""),
            f"{SA_GATEWAY_URL_PREFIX}{MASKED_CLOUD_ID}",
            id="gateway-userinfo-only",
        ),
        # --cloud-id plus the same url: the id is masked from both sources.
        pytest.param(
            ("--url", USERINFO_GATEWAY_URL, "--token", "t", "--cloud-id", CLOUD_ID),
            f"{SA_GATEWAY_URL_PREFIX}{MASKED_CLOUD_ID}",
            id="gateway-userinfo-with-cloud-id",
        ),
        # A Data Center url resolves no cloud id at all, yet its userinfo still needs the filter.
        pytest.param(
            ("--url", USERINFO_DATACENTER_URL, "--token", "t", "--cloud-id", ""),
            "https://issues.redhat.com",
            id="datacenter-userinfo",
        ),
    ],
)
def test_transport_log_filter_masks_the_userinfo_and_the_cloud_id(mocker, caplog, args, expected_url):
    caplog.set_level(logging.DEBUG)
    # The filter is installed at the CLI boundary, before the client makes its first request.
    _run_jira_command(mocker, *args, "--check-auth")

    # The jira library quotes the request url verbatim, credential and cloud id included.
    logging.getLogger("jira.resilientsession").warning("GET %s/rest/api/2/issue/ABC-1", args[1])

    records = [record for record in caplog.records if record.name == "jira.resilientsession"]
    assert len(records) == 1
    assert records[0].getMessage() == f"GET {expected_url}/rest/api/2/issue/ABC-1"
    assert USERINFO_PASSWORD not in caplog.text
    assert CLOUD_ID not in caplog.text
    # Masked, not dropped: the record keeps its level and its delivery to the handler.
    assert records[0].levelno == logging.WARNING
    assert records[0].levelname == "WARNING"


def test_transport_log_filter_masks_the_bearer_token_in_the_message_and_the_traceback(mocker, caplog):
    caplog.set_level(logging.DEBUG)
    token = "s3cr3t-token-value"
    # A PAT-mode run has no cloud id and no userinfo url, so the token alone has to install the filter.
    _run_jira_command(mocker, "--url", "https://issues.redhat.com", "--token", token, "--cloud-id", "", "--check-auth")

    transport = logging.getLogger("urllib3.connectionpool")
    transport.warning("GET /rest/api/2/issue failed for %s", token)
    try:
        raise RuntimeError(f"connection to /rest/api/2/issue failed for {token}")
    except RuntimeError:
        transport.exception("request failed")

    records = [record for record in caplog.records if record.name == "urllib3.connectionpool"]
    assert len(records) == 2
    assert token not in records[0].getMessage()
    assert "***" in records[0].getMessage()
    # The traceback is rendered into the record, so an exception message cannot walk past the filter either.
    assert records[1].exc_text is not None
    assert token not in records[1].exc_text
    assert token not in caplog.text


def test_log_secret_filter_is_installed_by_the_cli_not_the_resolver(mocker, caplog):
    caplog.set_level(logging.DEBUG)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    factory_before = logging.getLogRecordFactory()

    process_jira_command_line_config_file(
        config_file_path="/path",
        url="",
        token="t",
        issue_pattern="*",
        resolved_statuses=[],
        version_string_not_targeted_jiras="",
        target_versions=[],
        skip_projects=[],
        user="",
        cloud=None,
        cloud_id=RESOLVER_CLOUD_ID,
    )

    # Resolving the config is side-effect free: it must not rewrite process-wide logging.
    assert logging.getLogRecordFactory() is factory_before

    _run_jira_command(mocker, "--cloud-id", RESOLVER_CLOUD_ID, "--token", "t", "--check-auth")

    assert logging.getLogRecordFactory() is not factory_before
    resolver_gateway_url = SA_GATEWAY_URL_TEMPLATE.format(cloud_id=RESOLVER_CLOUD_ID)
    logging.getLogger("jira.resilientsession").warning("GET %s/rest/api/3/project", resolver_gateway_url)
    messages = [record.getMessage() for record in caplog.records if record.name == "jira.resilientsession"]
    assert len(messages) == 1
    assert RESOLVER_CLOUD_ID not in messages[0]
    assert MASKED_CLOUD_ID in messages[0]


def test_check_jira_auth_failure_exits(mocker, caplog):
    caplog.set_level(logging.DEBUG)
    mock_jira = mocker.MagicMock()
    # A cloud id means service-account auth, so both endpoints are probed and both must reject the credential.
    mock_jira.myself.side_effect = JIRAError(status_code=403, text="Forbidden")
    mock_jira.projects.side_effect = JIRAError(status_code=401, text="Unauthorized")

    with pytest.raises(SystemExit) as exc_info:
        _check_jira_auth(
            jira_obj=mock_jira,
            jira_config_dict={"url": "", "cloud": False, "cloud_id": "cloud-id", "token": "s3cr3t-token-value"},
        )

    assert exc_info.value.code == 1
    assert "403" in caplog.text
    # A rejected credential goes through the auth reporter, not the generic failure path.
    assert "Jira authentication failed" in caplog.text
    assert _auth_failure_hint(mode=AUTH_MODE_SERVICE_ACCOUNT) in caplog.text
    assert "s3cr3t-token-value" not in caplog.text


def test_check_jira_auth_non_auth_error_reports_cause(mocker, caplog):
    caplog.set_level(logging.DEBUG)
    mock_jira = mocker.MagicMock()
    mock_jira.myself.side_effect = JIRAError(status_code=404, text="Issue does not exist")

    with pytest.raises(SystemExit) as exc_info:
        _check_jira_auth(
            jira_obj=mock_jira,
            jira_config_dict={"url": "", "cloud": False, "cloud_id": "cloud-id", "token": "s3cr3t-token-value"},
        )

    assert exc_info.value.code == 1
    messages = [record.getMessage() for record in caplog.records]
    failure_messages = [message for message in messages if "Jira credential check failed" in message]
    assert len(failure_messages) == 1
    # A 404 is not a credential problem: name the real cause and its status instead of token advice.
    assert "JIRAError" in failure_messages[0]
    assert "404" in failure_messages[0]
    assert "Issue does not exist" in failure_messages[0]
    assert "Jira authentication failed" not in caplog.text
    assert _hint_logs(messages, AUTH_MODE_SERVICE_ACCOUNT) == []
    assert "s3cr3t-token-value" not in caplog.text


SA_ENV_TOKEN = "atst-env-only-token"


@pytest.mark.parametrize(
    "projects_return,anonymous_status,expected_exit,expected_reason",
    [
        pytest.param([], 401, 1, "no projects are visible", id="empty-list-unverified"),
        pytest.param([{"key": "ABC"}], 401, 0, "", id="non-empty-list-verified"),
        pytest.param([{"key": "ABC"}], 403, 0, "", id="non-empty-list-anonymous-rejected"),
        pytest.param([{"key": "ABC"}], 200, 1, "without an Authorization header", id="non-empty-list-anonymous"),
    ],
)
def test_check_jira_auth_project_list_decides_the_verdict(
    mocker, projects_return, anonymous_status, expected_exit, expected_reason
):
    mock_logger = _patch_logger(mocker)
    result, mock_jira_cls, _ = _run_oauth_command(
        mocker,
        *SCOPED_TOKEN_ARGS,
        "--check-auth",
        myself_error=JIRAError(status_code=401, text="Unauthorized"),
        projects_return=projects_return,
        # The same non-empty list without a bearer is what decides whether the authenticated one proved anything.
        anonymous_status=anonymous_status,
        anonymous_projects=[{"key": "ABC"}],
    )

    assert result.exit_code == expected_exit, _logged(mock_logger, "error")
    mock_jira_cls.return_value.projects.assert_called_once()
    errors = _logged(mock_logger, "error")
    # Neither a rejection hint nor an "authentication failed" line: the endpoint decided, not the /myself 401.
    assert _hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT) == []
    assert [message for message in errors if "Jira authentication failed" in message] == []
    infos = _logged(mock_logger, "info")
    unverified = [message for message in errors if "could not be verified" in message]
    if expected_exit == 0:
        assert [message for message in infos if "project visibility verified" in message]
        assert unverified == []
    else:
        # Neither an empty list nor a list that is also readable anonymously proves anything about the bearer.
        assert len(unverified) == 1
        assert expected_reason in unverified[0]
        assert not [message for message in infos if "Jira authentication OK" in message]


@pytest.mark.parametrize(
    "rest_api_version,expected_version",
    [
        pytest.param(None, "2", id="client-default-rest-api-version"),
        pytest.param("3", "3", id="client-rest-api-version-3"),
    ],
)
def test_check_jira_auth_anonymous_probe_uses_the_client_rest_api_version(mocker, rest_api_version, expected_version):
    _patch_logger(mocker)
    mock_session = _mock_anonymous_probe(mocker, status_code=200, projects=[{"key": "ABC"}])
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    mock_jira_cls.return_value._options = {"rest_api_version": rest_api_version} if rest_api_version else {}
    mock_jira_cls.return_value.myself.side_effect = JIRAError(status_code=401, text="Unauthorized")
    mock_jira_cls.return_value.projects.return_value = [{"key": "ABC"}]
    mock_mint = mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token", return_value="minted-token")

    result = CliRunner().invoke(
        get_jira_mismatch,
        [*SCOPED_TOKEN_ARGS, "--client-id", OAUTH_CLIENT_ID, "--client-secret", OAUTH_CLIENT_SECRET, "--check-auth"],
    )

    assert result.exit_code == 1
    mock_mint.assert_called_once()
    mock_jira_cls.return_value.projects.assert_called_once()
    # The header-less probe must hit the same endpoint the authenticated list went to, or it answers about a
    # different one (a v3 that is not served would report "not anonymous" and invert the guard).
    mock_session.prepare_request.assert_called_once()
    assert mock_session.prepare_request.call_args.args[0].url == f"{GATEWAY_URL}/rest/api/{expected_version}/project"
    # No credential may travel with the probe: requests adds one from the userinfo or the netrc file otherwise.
    assert "Authorization" not in mock_session.send.call_args.kwargs["request"].headers
    # The environment (CA bundle, proxy) is still merged into the send, or the probe would silently be a no-op there.
    merge_args = mock_session.merge_environment_settings.call_args.args
    assert merge_args[0] == f"{GATEWAY_URL}/rest/api/{expected_version}/project"
    # The remaining positionals are (proxies, stream, verify, cert): a `None` proxies argument would make
    # merge_setting() skip resolve_proxies() and silently drop the env proxy and CA bundle altogether.
    assert merge_args[1:] == ({}, None, None, None)


def test_check_jira_auth_is_inconclusive_when_a_dead_bearer_sees_an_anonymous_project_list(mocker):
    mock_logger = _patch_logger(mocker)
    result, mock_jira_cls, _ = _run_oauth_command(
        mocker,
        *SCOPED_TOKEN_ARGS,
        "--check-auth",
        # An expired scoped token: /myself rejects it, and the project endpoint answers as it would with no bearer.
        myself_error=JIRAError(status_code=401, text="Unauthorized"),
        projects_return=[{"key": "ABC"}],
        anonymous_status=200,
        anonymous_projects=[{"key": "ABC"}],
    )

    assert result.exit_code == 1
    assert mock_jira_cls.return_value.projects.call_count == 1
    errors = _logged(mock_logger, "error")
    unverified = [message for message in errors if "could not be verified" in message]
    assert len(unverified) == 1
    assert "without an Authorization header" in unverified[0]
    # A dead bearer must never be reported as a healthy one.
    assert not [message for message in _logged(mock_logger, "info") if "authentication OK" in message]
    assert _hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT) == []


def test_check_jira_auth_projects_outage_reports_the_project_error(mocker):
    mock_logger = _patch_logger(mocker)
    result, _, _ = _run_oauth_command(
        mocker,
        *SCOPED_TOKEN_ARGS,
        "--check-auth",
        myself_error=JIRAError(status_code=401, text="Unauthorized"),
        projects_error=JIRAError(status_code=503, text="Service Unavailable"),
    )

    assert result.exit_code == 1
    errors = _logged(mock_logger, "error")
    # An outage is not a rejection: the project endpoint's status code is what the user needs, not token advice.
    assert [message for message in errors if "Jira authentication failed" in message] == []
    assert _hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT) == []
    check_failed = [message for message in errors if "Jira credential check failed" in message]
    assert len(check_failed) == 1
    assert "status: 503" in check_failed[0]
    assert "Service Unavailable" in check_failed[0]
    assert [message for message in errors if "could not be verified" in message] == []


@pytest.mark.parametrize(
    "myself_status,projects_status",
    [
        pytest.param(401, 401, id="both-401"),
        pytest.param(403, 403, id="both-403"),
        pytest.param(401, 403, id="myself-401-projects-403"),
        pytest.param(403, 401, id="myself-403-projects-401"),
    ],
)
def test_check_jira_auth_rejects_when_both_probe_endpoints_reject(mocker, myself_status, projects_status):
    mock_logger = _patch_logger(mocker)
    result, _, _ = _run_oauth_command(
        mocker,
        *SCOPED_TOKEN_ARGS,
        "--check-auth",
        myself_error=JIRAError(status_code=myself_status, text="Unauthorized"),
        projects_error=JIRAError(status_code=projects_status, text="Forbidden"),
    )

    assert result.exit_code == 1
    errors = _logged(mock_logger, "error")
    auth_messages = [message for message in errors if "Jira authentication failed" in message]
    assert len(auth_messages) == 1
    # The original /myself error is reported, not the projects() one.
    assert f"): {myself_status} " in auth_messages[0]
    assert len(_hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT)) == 1
    assert [message for message in errors if "could not be verified" in message] == []


ANONYMOUS_PROBE_URL = f"{GATEWAY_URL}/rest/api/2/project"


def _probe_response(mocker, ok=True, payload=None, raise_error=None, status_code=None):
    """Mock `requests.Session.send` only, so the probe's own preparation still runs for real."""
    if raise_error is not None:
        return mocker.patch("requests.Session.send", side_effect=raise_error)

    response = mocker.MagicMock()
    response.status_code = status_code if status_code is not None else (200 if ok else 500)
    # requests derives `ok` from the status, and it is True for a 3xx too: that is exactly what the probe must not take
    # for an answer, so the two are set independently.
    response.ok = ok
    response.json.return_value = payload
    response.json.side_effect = None if payload is not None else ValueError("no json")
    return mocker.patch("requests.Session.send", return_value=response)


@pytest.mark.parametrize(
    "server,send_kwargs,expected",
    [
        # No server resolved means no probe at all.
        pytest.param("", {}, False, id="no-server"),
        pytest.param(GATEWAY_URL, {"raise_error": requests.ConnectionError("boom")}, False, id="unreachable"),
        pytest.param(GATEWAY_URL, {"ok": False, "payload": [{"key": "ABC"}]}, False, id="non-ok-status"),
        # A 302 carrying a JSON array: `Response.ok` is True for a 3xx, redirects are not followed, and a redirect body
        # is no evidence of anonymous readability.
        pytest.param(
            GATEWAY_URL,
            {"status_code": 302, "payload": [{"key": "ABC"}]},
            False,
            id="redirect-with-a-list-body",
        ),
        pytest.param(GATEWAY_URL, {"payload": None}, False, id="non-json-body"),
        pytest.param(GATEWAY_URL, {"payload": {"key": "ABC"}}, False, id="json-object"),
        pytest.param(GATEWAY_URL, {"payload": []}, False, id="empty-list"),
        pytest.param(GATEWAY_URL, {"payload": [{"key": "ABC"}]}, True, id="non-empty-list"),
    ],
)
def test_project_endpoint_is_anonymous(mocker, server, send_kwargs, expected):
    mock_send = _probe_response(mocker, **send_kwargs)

    assert _project_endpoint_is_anonymous(server=server) is expected
    # Every branch but the empty-server one is decided by this one request, so it must have been made.
    assert mock_send.called is bool(server)


@pytest.mark.parametrize(
    "server",
    [
        # A userinfo url makes requests send `Authorization: Basic ...` whatever the headers say.
        pytest.param(f"https://user:{USERINFO_PASSWORD}@api.atlassian.com/ex/jira/{CLOUD_ID}", id="userinfo-url"),
        # A ~/.netrc entry for the host is picked up by Session.prepare_request, headers={} or not.
        pytest.param(f"https://{SA_GATEWAY_HOST}/ex/jira/{CLOUD_ID}", id="netrc-entry"),
    ],
)
def test_project_endpoint_is_anonymous_sends_no_authorization_header(mocker, monkeypatch, tmp_path, server):
    netrc_file = tmp_path / "netrc"
    netrc_file.write_text(f"machine {SA_GATEWAY_HOST}\n  login netrc-user\n  password netrc-password\n")
    netrc_file.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc_file))
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_send = _probe_response(mocker, payload=[{"key": "ABC"}])

    assert _project_endpoint_is_anonymous(server=server) is True

    # The control: requests really does turn that netrc entry into a credential, so the probe's own session is what
    # keeps it out.
    control = requests.Session().prepare_request(requests.Request(method="GET", url=ANONYMOUS_PROBE_URL))
    assert "Authorization" in control.headers
    sent = mock_send.call_args.kwargs["request"]
    assert "Authorization" not in sent.headers
    # The userinfo is stripped from the url the probe sends, not just from the header.
    assert sent.url == ANONYMOUS_PROBE_URL
    assert USERINFO_PASSWORD not in sent.url


class _RedirectingAdapter(requests.adapters.HTTPAdapter):
    """Answer every request with a 302 and record what reached it, so a followed redirect is visible."""

    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        response = requests.Response()
        response.status_code = 302
        response.url = request.url
        response.request = request
        response.headers["Location"] = f"https://redirect.target.example/ex/jira/{CLOUD_ID}/rest/api/2/project"
        return response


def test_project_endpoint_is_anonymous_does_not_follow_a_redirect(mocker, monkeypatch, tmp_path):
    netrc_file = tmp_path / "netrc"
    netrc_file.write_text(f"machine {SA_GATEWAY_HOST}\n  login netrc-user\n  password netrc-password\n")
    netrc_file.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc_file))
    monkeypatch.setenv("HOME", str(tmp_path))
    # The real Session (and its prepare_request) runs, mounted on an adapter that answers 302 instead of the network.
    adapter = _RedirectingAdapter()
    session = requests.Session()
    session.mount("https://", adapter)
    mocker.patch("apps.jira_utils.jira_information.requests.Session", return_value=session)

    assert _project_endpoint_is_anonymous(server=GATEWAY_URL) is False

    # The control: requests really does turn that netrc entry into a credential on a plain prepared request.
    control = requests.Session().prepare_request(requests.Request(method="GET", url=ANONYMOUS_PROBE_URL))
    assert "Authorization" in control.headers
    # A 3xx is not a header-less 2xx answer, so the verdict is already False. Following it would run rebuild_auth(),
    # which re-injects the netrc credential - of this host or of the redirect target's - on the next hop.
    assert len(adapter.sent) == 1
    assert "Authorization" not in adapter.sent[0].headers


def test_scan_continues_when_the_projects_probe_is_inconclusive(mocker):
    mock_logger = _patch_logger(mocker)
    result, mock_jira_cls, mock_get_jira_information = _run_scan(
        mocker,
        SCOPED_TOKEN_ARGS,
        issues=["ABC-1", "ABC-2"],
        per_issue_status=401,
        myself_side_effect=JIRAError(status_code=401, text="Unauthorized"),
        projects_side_effect=JIRAError(status_code=503, text="Service Unavailable"),
    )

    assert result.exit_code == 1
    # Inconclusive is not verification, but it is not a rejection either, so the scan is not aborted.
    assert mock_get_jira_information.call_count == 2
    # The first 401 clears the cached verdict of the first issue, so the second one reuses the fresh probe instead of
    # paying for another: at most one probe pair per scan.
    assert mock_jira_cls.return_value.myself.call_count == 1
    assert mock_jira_cls.return_value.projects.call_count == 1
    errors = _logged(mock_logger, "error")
    assert _hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT) == []
    assert [message for message in errors if "Following Jira ids failed" in message]


def test_scan_probes_a_repeated_401_once(mocker):
    mock_logger = _patch_logger(mocker)
    result, mock_jira_cls, mock_get_jira_information = _run_scan(
        mocker,
        SCOPED_TOKEN_ARGS,
        issues=["ABC-1", "ABC-2", "ABC-3"],
        per_issue_status=401,
        # A scoped credential: /myself is out of its scopes, and an empty project list proves nothing but rejects
        # nothing either, so the scan keeps going.
        myself_side_effect=JIRAError(status_code=401, text="Unauthorized"),
        projects_return=[],
    )

    assert result.exit_code == 1
    assert mock_get_jira_information.call_count == 3
    # A 401 clears the cached verdict at most once per scan: the second and third issue reuse the refreshed probe
    # instead of paying for another pair each.
    assert mock_jira_cls.return_value.myself.call_count == 1
    assert mock_jira_cls.return_value.projects.call_count == 1
    errors = _logged(mock_logger, "error")
    assert _hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT) == []
    assert len([message for message in errors if "Following Jira ids failed" in message]) == 1


def test_scan_reprobes_once_when_a_401_follows_a_harmless_403(mocker):
    mock_logger = _patch_logger(mocker)
    result, mock_jira_cls, mock_get_jira_information = _run_scan(
        mocker,
        SCOPED_TOKEN_ARGS,
        issues=["ABC-1", "ABC-2"],
        # A per-issue 403 is a project permission gap: /myself still answers, so the credential is healthy and the
        # verdict is cached as a harmless one. The second issue's 401 (a bearer that expired mid-scan) must clear
        # that stale verdict exactly once.
        per_issue_results=[("file.py", "error 403", 403), ("file.py", "error 401", 401)],
    )

    assert result.exit_code == 1
    assert mock_get_jira_information.call_count == 2
    # One probe for the 403, one re-probe for the 401: neither the stale verdict nor a probe per issue.
    assert mock_jira_cls.return_value.myself.call_count == 2
    errors = _logged(mock_logger, "error")
    assert _hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT) == []
    assert len([message for message in errors if "Following Jira ids failed" in message]) == 1


def test_scan_aborts_when_the_401_after_a_403_rejects_the_credential(mocker):
    mock_logger = _patch_logger(mocker)
    result, mock_jira_cls, mock_get_jira_information = _run_scan(
        mocker,
        SCOPED_TOKEN_ARGS,
        issues=["ABC-1", "ABC-2"],
        # The 403 alone looks like a project permission gap: the first probe is harmless. The 401 after it is what a
        # dead bearer looks like, and the re-probe rejects the credential on both endpoints, so the scan must abort
        # instead of reporting every remaining issue as a permission gap. The first probe keeps the suite green if the
        # abort block is deleted, which only the second probe can catch.
        per_issue_results=[("file.py", "error 403", 403), ("file.py", "error 401", 401)],
        myself_side_effect=[{"accountId": "account-id"}, JIRAError(status_code=401, text="Unauthorized")],
        projects_side_effect=JIRAError(status_code=401, text="Unauthorized"),
    )

    assert result.exit_code == 1
    # Every remaining issue would fail identically, so the scan stops at the second one.
    assert mock_get_jira_information.call_count == 2
    # One probe for the 403, one re-probe for the 401.
    assert mock_jira_cls.return_value.myself.call_count == 2
    assert mock_jira_cls.return_value.projects.call_count == 1
    errors = _logged(mock_logger, "error")
    assert len(_hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT)) == 1
    # The abort replaces the per-issue aggregate: the failures are one dead credential, not N permission gaps.
    assert [message for message in errors if "Following Jira ids failed" in message] == []


def _run_env_only(mocker, monkeypatch, *args):
    """Emulate a process started with JIRA_CLOUD_ID/JIRA_TOKEN only (no url, user or config file)."""
    monkeypatch.setenv("JIRA_CLOUD_ID", CLOUD_ID)
    monkeypatch.setenv("JIRA_TOKEN", SA_ENV_TOKEN)
    for env_var in ("JIRA_URL", "JIRA_SERVER_URL", "JIRA_USER"):
        monkeypatch.delenv(env_var, raising=False)
    # The os.getenv() option defaults were frozen at import time, before this test env existed, so re-evaluate them.
    defaults = {
        "url": os.getenv("JIRA_SERVER_URL") or os.getenv("JIRA_URL"),
        "token": os.getenv("JIRA_TOKEN"),
        "user": os.getenv("JIRA_USER"),
    }
    _patch_option_defaults(monkeypatch, **defaults)
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    mock_jira_cls.return_value.myself.return_value = {"displayName": "display-name"}
    mock_get_jiras = mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})
    return CliRunner().invoke(get_jira_mismatch, list(args)), mock_jira_cls, mock_get_jiras


def test_get_jira_mismatch_env_only_service_account_builds_gateway_client(mocker, monkeypatch):
    # README's primary service-account invocation: cloud id plus token from the env, no url, no config file.
    mock_logger = _patch_logger(mocker)
    result, mock_jira_cls, mock_get_jiras = _run_env_only(mocker, monkeypatch, "--check-auth")

    assert result.exit_code == 0, _logged(mock_logger, "error")
    # A pre-flight rejection would leave the guard message behind instead of building the client.
    reported = _logged(mock_logger, "error") + [result.output]
    assert not [message for message in reported if "Config file or token" in message]
    mock_jira_cls.assert_called_once_with(server=GATEWAY_URL, token_auth=SA_ENV_TOKEN)
    mock_jira_cls.return_value.myself.assert_called_once()
    # --check-auth only verifies the credential, it must not scan the repo.
    mock_get_jiras.assert_not_called()


def test_get_jira_mismatch_env_only_oauth_pair_reaches_the_cloud_id_message(mocker, monkeypatch):
    mock_logger = _patch_logger(mocker)
    # The pair has no os.getenv() option default, so it is resolved inside the config resolver: the pre-flight guard
    # must still see it, or it reports an empty invocation and the documented message becomes unreachable.
    monkeypatch.setenv("JIRA_SA_CLIENT_ID", OAUTH_CLIENT_ID)
    monkeypatch.setenv("JIRA_SA_CLIENT_SECRET", OAUTH_CLIENT_SECRET)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mock_mint = mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token")
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")

    result = CliRunner().invoke(get_jira_mismatch, ["--check-auth"])

    assert result.exit_code == 1
    errors = "\n".join(_logged(mock_logger, "error"))
    assert "No Jira configuration supplied" not in errors
    assert "A Jira cloud id is required for OAuth service-account authentication" in errors
    # Nothing can be attempted without a cloud id, so no token is minted and no client is built.
    mock_mint.assert_not_called()
    mock_jira_cls.assert_not_called()


def test_get_jira_mismatch_env_cloud_id_opt_out_requires_url(mocker, monkeypatch):
    mock_logger = _patch_logger(mocker)
    # An explicit empty --cloud-id must beat JIRA_CLOUD_ID, which is the only thing left to talk to here.
    result, mock_jira_cls, mock_get_jiras = _run_env_only(mocker, monkeypatch, "--cloud-id", "", "--check-auth")

    assert result.exit_code == 1
    errors = _logged(mock_logger, "error")
    # The env token survives the opt-out, so the pre-flight guard does not fire: the url requirement is reported by
    # the config processor, with its own message.
    assert errors == ["Jira url and token are required."]
    mock_jira_cls.assert_not_called()
    mock_get_jiras.assert_not_called()


def test_get_jira_mismatch_config_cloud_id_opt_out_requires_url(mocker, monkeypatch, tmp_path):
    mock_logger = _patch_logger(mocker)
    config_file = tmp_path / "jira_cfg.yaml"
    config_file.write_text(f'pyutils-jira:\n  cloud_id: "{CLOUD_ID}"\n  token: "{SA_ENV_TOKEN}"\n')
    _clear_jira_env(monkeypatch)
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")

    # The opt-out clears the config-file cloud_id too, so no url is left and the run must not build a client.
    result = CliRunner().invoke(
        get_jira_mismatch, ["--config-file-path", str(config_file), "--cloud-id", "", "--check-auth"]
    )

    assert result.exit_code == 1
    assert "Jira url and token are required." in "\n".join(_logged(mock_logger, "error"))
    mock_jira_cls.assert_not_called()


def test_get_jira_mismatch_null_config_token_still_mints(mocker, monkeypatch, tmp_path):
    mock_logger = _patch_logger(mocker)
    config_file = tmp_path / "jira_cfg.yaml"
    # A config file with an OAuth pair may leave the pre-minted token out (`token:` parses to None): the run must mint
    # instead of dying in the log-secret filter before it can.
    config_file.write_text(
        "pyutils-jira:\n"
        f'  cloud_id: "{CLOUD_ID}"\n'
        "  token:\n"
        f'  client_id: "{OAUTH_CLIENT_ID}"\n'
        f'  client_secret: "{OAUTH_CLIENT_SECRET}"\n'
    )
    _clear_jira_env(monkeypatch)
    _mock_anonymous_probe(mocker)
    mock_mint = mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token", return_value="minted-token")
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})

    result = CliRunner().invoke(get_jira_mismatch, ["--config-file-path", str(config_file), "--check-auth"])

    assert result.exit_code == 0, _logged(mock_logger, "error")
    mock_mint.assert_called_once_with(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET)
    # The minted access token is what the client gets, exactly as for a run with a pre-minted one.
    mock_jira_cls.assert_called_once_with(server=GATEWAY_URL, token_auth="minted-token")


def test_get_jira_mismatch_cloud_id_builds_gateway_client(mocker):
    result, mock_jira_cls, mock_get_jiras = _run_jira_command(
        mocker, "--cloud-id", CLOUD_ID, "--token", "t", "--check-auth"
    )

    assert result.exit_code == 0
    mock_jira_cls.assert_called_once_with(server=GATEWAY_URL, token_auth="t")
    # --check-auth only verifies the credential, it must not scan the repo.
    mock_get_jiras.assert_not_called()


def test_get_jira_mismatch_cloud_id_discards_site_url(mocker):
    mock_logger = _patch_logger(mocker)
    result, mock_jira_cls, _ = _run_jira_command(
        mocker,
        "--url",
        "https://example.atlassian.net",
        "--cloud-id",
        CLOUD_ID,
        "--token",
        "t",
        "--check-auth",
    )

    assert result.exit_code == 0
    mock_jira_cls.assert_called_once_with(server=GATEWAY_URL, token_auth="t")
    # Logged from config processing only: resolve_jira_server runs several times per run and must not repeat it.
    # Dropping a url the run does not use is informational, so a healthy run does not warn about it.
    notices = [message for message in _logged(mock_logger, "info") if "discarding supplied Jira url" in message]
    assert len(notices) == 1
    assert _logged(mock_logger, "warning") == []


def test_get_jira_mismatch_mint_failure_reports_once(mocker, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _clear_jira_env(monkeypatch)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mocker.patch(
        "apps.jira_utils.jira_information.mint_atlassian_token",
        side_effect=JiraTokenMintError("could not mint an Atlassian access token (HTTP 401)"),
    )
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})

    result = CliRunner().invoke(get_jira_mismatch, [*OAUTH_ARGS, "--check-auth"])

    assert result.exit_code == 1
    records = [record for record in caplog.records if "could not mint" in record.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert "Jira authentication failed" in records[0].getMessage()
    # The mint error is actionable on its own: a traceback would only re-render the failing request.
    assert records[0].exc_info is None
    assert "Traceback" not in caplog.text
    # The secret is part of the request body, so it must never reach the log through the error path.
    assert OAUTH_CLIENT_SECRET not in caplog.text
    mock_jira_cls.assert_not_called()


@pytest.mark.parametrize("status_code", [pytest.param(401, id="401"), pytest.param(403, id="403")])
def test_get_jira_mismatch_check_auth_scope_fallback_succeeds(mocker, monkeypatch, status_code):
    _clear_jira_env(monkeypatch)
    mock_logger = _patch_logger(mocker)
    result, mock_jira_cls, mock_mint = _run_oauth_command(
        mocker,
        *OAUTH_ARGS,
        "--check-auth",
        myself_error=JIRAError(status_code=status_code, text="Unauthorized"),
    )

    assert result.exit_code == 0
    # /myself is not part of read:jira-work, so a rejected call there is probed against the project endpoint.
    mock_jira_cls.return_value.projects.assert_called_once()
    scope_lines = [message for message in _logged(mock_logger, "info") if "scopes" in message]
    assert len(scope_lines) == 1
    mock_mint.assert_called_once_with(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET)
    mock_jira_cls.assert_called_once_with(server=GATEWAY_URL, token_auth="minted-token")
    # A healthy credential produces no error and no credential hint.
    assert _logged(mock_logger, "error") == []


@pytest.mark.parametrize("projects_status", [pytest.param(401, id="401"), pytest.param(403, id="403")])
def test_get_jira_mismatch_check_auth_scope_fallback_reports_original_error(mocker, monkeypatch, projects_status):
    _clear_jira_env(monkeypatch)
    mock_logger = _patch_logger(mocker)
    result, mock_jira_cls, _ = _run_oauth_command(
        mocker,
        *OAUTH_ARGS,
        "--check-auth",
        myself_error=JIRAError(status_code=401, text="Unauthorized"),
        projects_error=JIRAError(status_code=projects_status, text="Forbidden"),
    )

    assert result.exit_code == 1
    mock_jira_cls.return_value.projects.assert_called_once()
    errors = _logged(mock_logger, "error")
    auth_messages = [message for message in errors if "Jira authentication failed" in message]
    assert len(auth_messages) == 1
    # The original /myself error is what the user needs, not the status of the projects() probe.
    assert "Unauthorized" in auth_messages[0]
    assert "Forbidden" not in auth_messages[0]
    # An OAuth credential is minted on demand, so it gets the OAuth hint, never the re-mint one.
    assert len(_hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT, oauth_service_account=True)) == 1
    assert _hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT) == []


def test_get_jira_mismatch_aborts_scan_with_oauth_hint(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mock_logger = _patch_logger(mocker)
    mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token", return_value="minted-token")
    mocker.patch("apps.jira_utils.jira_information._credential_rejected", return_value=True)
    result, _, mock_get_jira_information = _run_scan(
        mocker, OAUTH_ARGS, issues={"ABC-1", "ABC-2"}, per_issue_status=401
    )

    assert result.exit_code == 1
    mock_get_jira_information.assert_called_once()
    errors = _logged(mock_logger, "error")
    # A minted token gets the OAuth hint, never the re-mint advice meant for a static token.
    assert len(_hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT, oauth_service_account=True)) == 1
    assert _hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT) == []


def test_get_jira_mismatch_oauth_env_aliases(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    monkeypatch.setenv("ATLASSIAN_OAUTH_CLIENT_ID", OAUTH_CLIENT_ID)
    monkeypatch.setenv("JIRA_OAUTH_CLIENT_SECRET", OAUTH_CLIENT_SECRET)
    monkeypatch.setenv("ATLASSIAN_OAUTH_CLOUD_ID", CLOUD_ID)
    mock_logger = _patch_logger(mocker)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    # The os.getenv() option defaults were frozen at import time, before this test env existed, so re-evaluate them.
    # The hermetic fixture drops the primary JIRA_SA_* names, so the defaults can only come from the aliases.
    defaults = _source_option_defaults()
    _patch_option_defaults(
        monkeypatch,
        url=None,
        token=None,
        user=None,
        client_id=defaults["client_id"],
        client_secret=defaults["client_secret"],
    )
    mock_mint = mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token", return_value="minted-token")
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})

    # No --cloud-id, --client-id or --client-secret: every value comes from an env alias.
    result = CliRunner().invoke(get_jira_mismatch, ["--check-auth"])

    assert result.exit_code == 0, _logged(mock_logger, "error")
    mock_mint.assert_called_once_with(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET)
    mock_jira_cls.assert_called_once_with(server=GATEWAY_URL, token_auth="minted-token")


@pytest.mark.parametrize(
    "with_alias", [pytest.param(False, id="primary-only"), pytest.param(True, id="primary-and-alias")]
)
def test_get_jira_mismatch_oauth_primary_env_names_reach_the_client(mocker, monkeypatch, with_alias):
    _clear_jira_env(monkeypatch)
    monkeypatch.setenv("JIRA_SA_CLIENT_ID", OAUTH_CLIENT_ID)
    monkeypatch.setenv("JIRA_SA_CLIENT_SECRET", OAUTH_CLIENT_SECRET)
    monkeypatch.setenv("ATLASSIAN_OAUTH_CLOUD_ID", CLOUD_ID)
    if with_alias:
        # Both spellings set: the primary pair must win, so the mint never sees an alias value.
        monkeypatch.setenv("ATLASSIAN_OAUTH_CLIENT_ID", "alias-client-id")
        monkeypatch.setenv("JIRA_OAUTH_CLIENT_SECRET", "alias-client-secret")  # pragma: allowlist secret
    mock_logger = _patch_logger(mocker)
    # The os.getenv() defaults were frozen at import, so apply the ones the current env produces from the source.
    defaults = _source_option_defaults()
    _patch_option_defaults(
        monkeypatch,
        url=None,
        token=None,
        user=None,
        client_id=defaults["client_id"],
        client_secret=defaults["client_secret"],
    )

    # No --cloud-id, --client-id or --client-secret: every value comes from the env.
    result, mock_jira_cls, mock_mint = _run_oauth_command(mocker, "--check-auth")

    assert result.exit_code == 0, _logged(mock_logger, "error")
    mock_mint.assert_called_once_with(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET)
    mock_jira_cls.assert_called_once_with(server=GATEWAY_URL, token_auth="minted-token")


def test_get_jira_mismatch_ignores_lone_oauth_value_with_another_credential(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mock_logger = _patch_logger(mocker)
    mock_mint = mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token")

    # Half of the OAuth pair next to a complete Data Center credential: the run must not be stopped by it.
    result, mock_jira_cls, _ = _run_jira_command(
        mocker,
        "--url",
        "https://issues.redhat.com",
        "--token",
        "dc-token",
        "--cloud-id",
        "",
        "--client-id",
        OAUTH_CLIENT_ID,
        "--check-auth",
    )

    assert result.exit_code == 0
    warnings = _logged(mock_logger, "warning")
    # Dropping the stray half switches the identity, so it is reported at WARNING and names the credential in use.
    assert len(warnings) == 1
    assert "Incomplete OAuth 2.0 service-account credential" in warnings[0]
    assert "ignoring it" in warnings[0]
    assert "the Data Center personal access token (PAT)" in warnings[0]
    assert OAUTH_CLIENT_ID not in warnings[0]
    assert _logged(mock_logger, "error") == []
    mock_mint.assert_not_called()
    mock_jira_cls.assert_called_once_with(token_auth="dc-token", options={"server": "https://issues.redhat.com"})


@pytest.mark.parametrize(
    "args,expected",
    [
        pytest.param((), "No Jira configuration supplied", id="nothing-supplied"),
        pytest.param(("--cloud-id", CLOUD_ID), "Jira token is required.", id="cloud-id-without-credential"),
        pytest.param(
            ("--url", "https://issues.redhat.com"), "Jira url and token are required.", id="url-without-token"
        ),
        pytest.param(
            ("--client-id", OAUTH_CLIENT_ID),
            "Both --client-id and --client-secret are required for the OAuth 2.0 service-account credential.",
            id="lone-client-id",
        ),
    ],
)
def test_get_jira_mismatch_guard_only_rejects_an_empty_invocation(mocker, args, expected):
    mock_logger = _patch_logger(mocker)
    result, mock_jira_cls, _ = _run_jira_command(mocker, *args, "--check-auth")

    assert result.exit_code == 1
    errors = "\n".join(_logged(mock_logger, "error"))
    assert expected in errors
    # The pre-flight guard only rejects an empty invocation, so it never shadows the dedicated credential message.
    assert ("No Jira configuration supplied" in errors) == (args == ())
    mock_jira_cls.assert_not_called()


@pytest.mark.parametrize(
    "credential_args,expected,expected_client_kwargs",
    [
        pytest.param(
            ("--url", "https://issues.redhat.com", "--token", "dc-token", "--cloud-id", ""),
            "the Data Center personal access token (PAT)",
            {"token_auth": "dc-token", "options": {"server": "https://issues.redhat.com"}},
            id="datacenter-pat",
        ),
        pytest.param(
            ("--url", "https://example.atlassian.net", "--token", "t", "--user", "a@b.c", "--cloud"),
            "cloud basic auth (user email plus API token)",
            {"server": "https://example.atlassian.net", "basic_auth": ("a@b.c", "t")},
            id="cloud-basic",
        ),
    ],
)
def test_get_jira_mismatch_ignores_oauth_pair_without_cloud_id(
    mocker, monkeypatch, credential_args, expected, expected_client_kwargs
):
    _clear_jira_env(monkeypatch)
    mock_logger = _patch_logger(mocker)
    mock_mint = mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token")

    # An env-exported OAuth pair is a documented convention, so it must not break a run on another credential.
    result, mock_jira_cls, _ = _run_jira_command(
        mocker, *credential_args, "--client-id", OAUTH_CLIENT_ID, "--client-secret", OAUTH_CLIENT_SECRET, "--check-auth"
    )

    assert result.exit_code == 0
    warnings = _logged(mock_logger, "warning")
    assert len(warnings) == 1
    assert "OAuth 2.0 service-account credential ignored for lack of a cloud id" in warnings[0]
    # The message names the credential the run actually authenticates with.
    assert expected in warnings[0]
    assert OAUTH_CLIENT_SECRET not in warnings[0]
    assert _logged(mock_logger, "error") == []
    mock_mint.assert_not_called()
    # A site url cannot satisfy the cloud-id requirement, so the pair is dropped and the site credential is used.
    mock_jira_cls.assert_called_once_with(**expected_client_kwargs)


def test_get_jira_mismatch_gateway_url_satisfies_oauth_cloud_id_requirement(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mock_logger = _patch_logger(mocker)

    # A gateway url already encodes the cloud id, so the pair needs no --cloud-id and the minted token is usable.
    result, mock_jira_cls, mock_mint = _run_oauth_command(
        mocker,
        "--url",
        GATEWAY_URL,
        "--client-id",
        OAUTH_CLIENT_ID,
        "--client-secret",
        OAUTH_CLIENT_SECRET,
        "--check-auth",
    )

    assert result.exit_code == 0, _logged(mock_logger, "error")
    mock_mint.assert_called_once_with(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET)
    mock_jira_cls.assert_called_once_with(server=GATEWAY_URL, token_auth="minted-token")


def test_get_jira_mismatch_gateway_url_keeps_oauth_pair_alongside_token(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mock_logger = _patch_logger(mocker)

    # The gateway url keeps the pair complete, so it is not dropped the way the site-url case above drops it.
    result, mock_jira_cls, mock_mint = _run_oauth_command(
        mocker,
        "--url",
        GATEWAY_URL,
        "--token",
        "stale-token",
        "--client-id",
        OAUTH_CLIENT_ID,
        "--client-secret",
        OAUTH_CLIENT_SECRET,
        "--check-auth",
    )

    assert result.exit_code == 0, _logged(mock_logger, "error")
    assert _logged(mock_logger, "error") == []
    mock_mint.assert_called_once_with(client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET)
    # The OAuth flavour wins: the minted token replaces the stale one instead of being ignored.
    mock_jira_cls.assert_called_once_with(server=GATEWAY_URL, token_auth="minted-token")
    assert [message for message in _logged(mock_logger, "info") if "OAuth 2.0 client credentials" in message]


def test_get_jira_mismatch_lone_oauth_value_names_the_pre_minted_credential(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mock_logger = _patch_logger(mocker)
    mock_mint = mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token")

    # In service-account mode the credential left in use is the pre-minted token, and the message must say so.
    result, mock_jira_cls, _ = _run_jira_command(
        mocker, "--cloud-id", CLOUD_ID, "--token", "t", "--client-secret", OAUTH_CLIENT_SECRET, "--check-auth"
    )

    assert result.exit_code == 0
    warnings = _logged(mock_logger, "warning")
    assert len(warnings) == 1
    assert "the pre-minted scoped API token" in warnings[0]
    assert OAUTH_CLIENT_SECRET not in warnings[0]
    assert _logged(mock_logger, "error") == []
    mock_mint.assert_not_called()
    mock_jira_cls.assert_called_once_with(server=GATEWAY_URL, token_auth="t")


def test_get_jira_mismatch_healthy_pre_minted_run_logs_one_line(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mock_logger = _patch_logger(mocker)
    mock_mint = mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token")

    # --cloud/--user alongside a service-account credential is the case that used to add an "Ignoring cloud/user"
    # line, so a run that is healthy in every respect must log nothing but the resolved auth mode.
    result, mock_jira_cls, _ = _run_jira_command(
        mocker, "--cloud-id", CLOUD_ID, "--token", "t", "--cloud", "--user", "a@b.c"
    )

    assert result.exit_code == 0
    records = _all_logged(mock_logger)
    assert len(records) == 1
    level, message = records[0]
    assert level == "info"
    assert "Using Jira service-account (pre-minted scoped API token) authentication" in message
    assert _logged(mock_logger, "error") == []
    assert _logged(mock_logger, "warning") == []
    assert not [message for _, message in records if "Ignoring cloud/user" in message]
    mock_mint.assert_not_called()
    mock_jira_cls.assert_called_once_with(server=GATEWAY_URL, token_auth="t")


def test_get_jira_mismatch_oauth_pair_without_cloud_id_names_the_opt_out(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mock_logger = _patch_logger(mocker)
    mock_mint = mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token")

    # A complete pair as the only credential cannot fall back to anything, so it must name the documented opt-out.
    result, mock_jira_cls, mock_get_jiras = _run_jira_command(
        mocker, "--client-id", OAUTH_CLIENT_ID, "--client-secret", OAUTH_CLIENT_SECRET, "--check-auth"
    )

    assert result.exit_code == 1
    errors = "\n".join(_logged(mock_logger, "error"))
    assert "A Jira cloud id is required for OAuth service-account authentication" in errors
    assert '--client-id "" --client-secret ""' in errors
    mock_mint.assert_not_called()
    mock_jira_cls.assert_not_called()
    mock_get_jiras.assert_not_called()


def test_autouse_guard_blocks_a_real_atlassian_mint(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})

    # No mint mock here on purpose: the autouse safety net must stop the run before a real HTTPS POST leaves the
    # process, so a future test can never mint with the developer's own client secret.
    result = CliRunner().invoke(get_jira_mismatch, [*OAUTH_ARGS, "--check-auth"])

    assert isinstance(result.exception, AssertionError)
    assert "mock mint_atlassian_token/requests.post" in str(result.exception)
    mock_jira_cls.assert_not_called()


def test_resolve_jira_server_logs_nothing(mocker):
    mock_logger = _patch_logger(mocker)
    resolve_jira_server(url="https://example.atlassian.net", cloud_id=CLOUD_ID)
    assert mock_logger.mock_calls == []


def test_get_jira_mismatch_gateway_url_scans_without_url_prefix(mocker):
    # --cloud-id "" overrides the JIRA_CLOUD_ID env default so the run stays env independent.
    result, _, mock_get_jiras = _run_jira_command(mocker, "--url", GATEWAY_URL, "--token", "t", "--cloud-id", "")

    assert result.exit_code == 0
    # The gateway url never appears in source code, so the browse-link match must be host-agnostic ("" prefix).
    mock_get_jiras.assert_called_once_with(issue_pattern="([A-Z]+-[0-9]+)", jira_url="")


@pytest.mark.parametrize(
    "args,expected_jira_url",
    [
        # The bare gateway root (no trailing slash) is still a gateway url: using it as the prefix matches no link,
        # because a source-code link never points at the API gateway.
        pytest.param(
            ("--cloud-id", CLOUD_ID, "--url", SA_GATEWAY_URL_PREFIX.rstrip("/"), "--token", "t"),
            "",
            id="bare-gateway-root",
        ),
        # A site url is exactly what a source-code link holds, so it is the prefix, unchanged.
        pytest.param(DATACENTER_ARGS, "https://issues.redhat.com", id="site-url-unchanged"),
    ],
)
def test_scan_matches_browse_links_against_the_resolved_url(mocker, args, expected_jira_url):
    mock_logger = _patch_logger(mocker)

    result, _, mock_get_jiras = _run_jira_command(mocker, *args)

    assert result.exit_code == 0
    mock_get_jiras.assert_called_once_with(issue_pattern="([A-Z]+-[0-9]+)", jira_url=expected_jira_url)
    # Neither run supplied a url to discard, the gateway root included: a gateway url is not a site url.
    assert not [message for message in _logged(mock_logger, "info") if "discarding" in message]


@pytest.mark.parametrize(
    "url,expected_exit,expected_server",
    [
        pytest.param(SA_GATEWAY_URL_PREFIX, 1, None, id="no-cloud-id-segment"),
        pytest.param(f"{SA_GATEWAY_URL_PREFIX}/", 1, None, id="empty-cloud-id-segment"),
        # The bare url without its trailing slash is the likeliest typo, so it must fail here rather than be taken
        # for a Data Center url and silently switch the run to PAT mode against the gateway root.
        pytest.param(SA_GATEWAY_URL_PREFIX.rstrip("/"), 1, None, id="bare-gateway-url-no-trailing-slash"),
        # The bare gateway host carries no id either, with or without its trailing slash.
        pytest.param(f"https://{SA_GATEWAY_HOST}", 1, None, id="bare-gateway-host"),
        pytest.param(f"https://{SA_GATEWAY_HOST}/", 1, None, id="bare-gateway-host-trailing-slash"),
        pytest.param(GATEWAY_URL, 0, GATEWAY_URL, id="valid-gateway-url"),
        # A deeper path below the cloud id is dropped rather than taken for the server: python-jira appends its own
        # `/rest/api/2/issue/<KEY>`, so keeping it would fetch every issue from the doubled path.
        pytest.param(f"{GATEWAY_URL}/rest/api/2", 0, GATEWAY_URL, id="valid-gateway-url-deeper-path"),
    ],
)
def test_get_jira_mismatch_gateway_url_without_a_cloud_id_segment(mocker, url, expected_exit, expected_server):
    mock_logger = _patch_logger(mocker)
    mock_mint = mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token")

    result, mock_jira_cls, mock_get_jiras = _run_jira_command(mocker, "--url", url, "--token", "t", "--cloud-id", "")

    assert result.exit_code == expected_exit
    if not expected_exit:
        # A gateway url already encodes the cloud id, so no --cloud-id is needed and the scan proceeds against the
        # canonical server: it carries no path of its own, so the issue request is
        # `/ex/jira/<id>/rest/api/2/issue/<KEY>`.
        mock_jira_cls.assert_called_once_with(server=expected_server, token_auth="t")
        assert expected_server == GATEWAY_URL
        mock_get_jiras.assert_called_once()
        return

    message = "\n".join(_logged(mock_logger, "error"))
    assert "missing its cloud id" in message
    # The message names the expected shape, so the fix is obvious without reading the source.
    assert f"expected {SA_GATEWAY_URL_PREFIX}<cloud-id>" in message
    # Failing here rather than at the gateway root means no client is built and no request is made.
    mock_jira_cls.assert_not_called()
    mock_get_jiras.assert_not_called()
    mock_mint.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(f"{SA_GATEWAY_URL_PREFIX}abc123", id="id-segment-too-short"),
        # An Atlassian account id (digits:uuid) is a user identity, not the cloud id of the site.
        pytest.param(f"{SA_GATEWAY_URL_PREFIX}712020:f0a60b32-1234", id="account-id-segment"),
    ],
)
def test_get_jira_mismatch_gateway_url_with_an_invalid_id_segment(mocker, url):
    mock_logger = _patch_logger(mocker)
    result, mock_jira_cls, mock_get_jiras = _run_jira_command(mocker, "--url", url, "--token", "t", "--cloud-id", "")

    assert result.exit_code == 1
    message = "\n".join(_logged(mock_logger, "error"))
    # An id that is present but the wrong shape is not "missing": telling the user to append it sends them looking
    # for the wrong thing.
    assert "Invalid Jira cloud id in the supplied gateway url" in message
    assert "missing its cloud id" not in message
    mock_jira_cls.assert_not_called()
    mock_get_jiras.assert_not_called()


def test_get_jira_mismatch_malformed_url_reports_one_line(mocker):
    mock_logger = _patch_logger(mocker)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mock_get_jiras = mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})

    # An unbracketed IPv6 host makes urlsplit raise. The url is validated once before the client is built, so the
    # documented single-line error - not a traceback, and not an outage blamed on the server - is what comes out.
    result = CliRunner().invoke(
        get_jira_mismatch, ["--url", "https://[2001:db8::1/ex/jira/x", "--token", "t", "--cloud-id", ""]
    )

    assert result.exit_code == 1
    assert _logged(mock_logger, "exception") == []
    errors = _logged(mock_logger, "error")
    assert len(errors) == 1
    assert "Invalid Jira server url (unparseable" in errors[0]
    mock_get_jiras.assert_not_called()


def test_get_jira_mismatch_invalid_cloud_id_exits_without_client(mocker):
    mock_logger = _patch_logger(mocker)
    result, mock_jira_cls, mock_get_jiras = _run_jira_command(mocker, "--cloud-id", "not a cloud id!", "--token", "t")

    assert result.exit_code == 1
    message = "\n".join(_logged(mock_logger, "error"))
    assert "Invalid Jira cloud id" in message
    # The message names both the config source and the escape hatch so the user can act on it.
    assert "JIRA_CLOUD_ID" in message
    assert "ATLASSIAN_OAUTH_CLOUD_ID" in message
    assert "--cloud-id" in message
    assert "config 'cloud_id'" in message
    assert 'Pass --cloud-id "" to ignore it.' in message
    mock_jira_cls.assert_not_called()
    mock_get_jiras.assert_not_called()


def test_get_jira_mismatch_aborts_scan_when_credential_rejected(mocker):
    mock_logger = _patch_logger(mocker)
    mock_credential_rejected = mocker.patch("apps.jira_utils.jira_information._credential_rejected", return_value=True)
    result, mock_jira_cls, mock_get_jira_information = _run_scan(
        mocker,
        ("--cloud-id", CLOUD_ID, "--token", "t"),
        issues={"ABC-1", "ABC-2"},
        per_issue_status=401,
    )

    assert result.exit_code == 1
    # A dead credential breaks every issue identically: stop at the first one instead of scanning the repo.
    mock_get_jira_information.assert_called_once()
    # The abort is decided by the probe, not by the per-issue status code alone; a cloud id is service-account auth.
    mock_credential_rejected.assert_called_once_with(
        jira_obj=mock_jira_cls.return_value, service_account=True, token="t", cloud_id=CLOUD_ID
    )
    error_messages = _logged(mock_logger, "error")
    assert len(_hint_logs(error_messages, AUTH_MODE_SERVICE_ACCOUNT)) == 1
    assert not [message for message in error_messages if "Following Jira ids failed" in message]


@pytest.mark.parametrize(
    "scan_args,mode",
    [
        pytest.param(("--cloud-id", CLOUD_ID, "--token", "t"), AUTH_MODE_SERVICE_ACCOUNT, id="service-account"),
        pytest.param(DATACENTER_ARGS, AUTH_MODE_DATACENTER_PAT, id="datacenter-pat"),
    ],
)
def test_get_jira_mismatch_continues_scan_when_only_issue_is_forbidden(mocker, scan_args, mode):
    mock_logger = _patch_logger(mocker)
    result, mock_jira_cls, mock_get_jira_information = _run_scan(
        mocker, scan_args, issues={"ABC-1", "ABC-2"}, per_issue_status=403
    )

    assert result.exit_code == 1
    # A per-issue 403 is a permission gap, not a dead credential, so every issue is still checked.
    assert mock_get_jira_information.call_count == 2
    mock_jira_cls.return_value.myself.assert_called_once()
    error_messages = _logged(mock_logger, "error")
    assert _hint_logs(error_messages, mode) == []
    assert [message for message in error_messages if "Following Jira ids failed" in message]


CREDENTIAL_REJECTED_PROBE_TEXT = (
    "HTTPSConnectionPool(host='api.atlassian.com', port=443): Max retries exceeded with url: "
    "/ex/jira/{cloud_id}/rest/api/2/myself (Caused by NewConnectionError())"
)


def test_credential_probe_outage_never_logs_the_cloud_id(mocker, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    mock_jira = mocker.MagicMock()
    # A transport error message quotes the request path, which encodes the cloud id.
    mock_jira.myself.side_effect = requests.ConnectionError(CREDENTIAL_REJECTED_PROBE_TEXT.format(cloud_id=CLOUD_ID))
    _credential_rejected.cache_clear()

    try:
        assert _credential_rejected(jira_obj=mock_jira, token="t", cloud_id=CLOUD_ID) is False
    finally:
        _credential_rejected.cache_clear()

    records = [record for record in caplog.records if "Jira credential probe failed" in record.getMessage()]
    assert len(records) == 1
    # A degraded probe is visible, not a DEBUG detail, but the url it quotes is never echoed.
    assert records[0].levelno == logging.WARNING
    assert CLOUD_ID not in caplog.text
    # The placeholder, not just the absence of the id: a truncated message would look the same and prove nothing.
    assert MASKED_CLOUD_ID in records[0].getMessage()
    assert CLOUD_ID not in capsys.readouterr().out


def test_credential_probe_outage_masks_the_cloud_id_before_truncating(mocker, caplog):
    caplog.set_level(logging.DEBUG)
    mock_jira = mocker.MagicMock()
    # The id starts at character 170, so its placeholder ends inside the 200-character cap while the id itself does
    # not: a truncation running before the substitution would drop the placeholder and leave a slice of the id behind.
    probe_text = CREDENTIAL_REJECTED_PROBE_TEXT.format(cloud_id=CLOUD_ID)
    padding = 170 - len(probe_text.split(CLOUD_ID)[0])
    mock_jira.myself.side_effect = requests.ConnectionError("x" * padding + probe_text)
    _credential_rejected.cache_clear()

    try:
        assert _credential_rejected(jira_obj=mock_jira, token="t", cloud_id=CLOUD_ID) is False
    finally:
        _credential_rejected.cache_clear()

    message = next(
        record.getMessage() for record in caplog.records if "Jira credential probe failed" in record.getMessage()
    )
    assert MASKED_CLOUD_ID in message
    assert CLOUD_ID not in message


def test_anonymous_probe_outage_never_logs_the_cloud_id(mocker, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    _probe_response(
        mocker, raise_error=requests.ConnectionError(CREDENTIAL_REJECTED_PROBE_TEXT.format(cloud_id=CLOUD_ID))
    )

    assert _project_endpoint_is_anonymous(server=GATEWAY_URL, cloud_id=CLOUD_ID) is False

    records = [record for record in caplog.records if "Anonymous project probe" in record.getMessage()]
    assert len(records) == 1
    # A guard that could not run is a degraded guard, so it is logged at WARNING rather than at DEBUG.
    assert records[0].levelno == logging.WARNING
    assert CLOUD_ID not in caplog.text
    # Masked, not truncated away: only the placeholder can prove the redaction ran before the 200-character cap.
    assert MASKED_CLOUD_ID in records[0].getMessage()
    assert CLOUD_ID not in capsys.readouterr().out


def test_log_secret_filter_installation_does_not_nest():
    _install_log_secret_filter(cloud_id=FILTER_CLOUD_ID)
    factory = logging.getLogRecordFactory()

    # The CLI installs twice (the configured token, then the minted one); a second install may only widen the secrets,
    # or masking would silently stack one factory per install.
    _install_log_secret_filter(token="s3cr3t-token-value")
    assert logging.getLogRecordFactory() is factory


def test_reset_log_secret_filter_leaves_a_later_factory_in_place():
    original_factory = logging.getLogRecordFactory()
    _install_log_secret_filter(cloud_id=FILTER_CLOUD_ID)
    installed_factory = logging.getLogRecordFactory()

    def third_party_factory(*args, **kwargs):
        return installed_factory(*args, **kwargs)

    logging.setLogRecordFactory(third_party_factory)
    try:
        _reset_log_secret_filter()
        # A factory installed after ours must survive the reset, or its records are silently left unprocessed; ours
        # then runs secretless, because the reset cleared the secrets it masks.
        assert logging.getLogRecordFactory() is third_party_factory
    finally:
        logging.setLogRecordFactory(original_factory)


def test_project_endpoint_is_anonymous_honours_the_environment_ca_bundle(mocker, monkeypatch, tmp_path):
    ca_bundle = tmp_path / "ca.pem"
    ca_bundle.write_text("")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_bundle))
    mock_send = _probe_response(mocker, payload=[{"key": "ABC"}])

    assert _project_endpoint_is_anonymous(server=GATEWAY_URL) is True

    sent = mock_send.call_args.kwargs
    # The CA bundle the site is only reachable through must survive, or the guard is a silent no-op here...
    assert sent["verify"] == str(ca_bundle)
    # ...while the netrc-injected header is still dropped.
    assert "Authorization" not in sent["request"].headers


def test_credential_rejected_is_cached(mocker):
    getattr(_credential_rejected, "cache_clear", lambda: None)()
    mock_jira = mocker.MagicMock()
    assert _credential_rejected(jira_obj=mock_jira) is False
    assert _credential_rejected(jira_obj=mock_jira) is False
    # Once per scan: myself() is a network call and its verdict cannot change mid-run for one client.
    mock_jira.myself.assert_called_once()


def test_credential_probe_outage_is_logged_once_per_probe(mocker, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    # simple_logger's DuplicateFilter would fold the repeated identical line into one, hiding the very duplication
    # under test: the raw records are what count here.
    monkeypatch.setattr("apps.jira_utils.jira_information.LOGGER.filters", [])
    mock_jira = mocker.MagicMock()
    mock_jira.myself.side_effect = requests.ConnectionError("Connection refused")
    _credential_rejected.cache_clear()

    def outage_lines() -> list[logging.LogRecord]:
        return [record for record in caplog.records if "Jira credential probe failed" in record.getMessage()]

    try:
        # Called once per failing issue inside the scan loop: a cached outage verdict must not repeat the line.
        assert _credential_rejected(jira_obj=mock_jira) is False
        assert _credential_rejected(jira_obj=mock_jira) is False
        assert len(outage_lines()) == 1
        mock_jira.myself.assert_called_once()

        # The caller clears the cache on a later 401, which re-arms the message for the fresh probe only.
        _credential_rejected.cache_clear()
        assert _credential_rejected(jira_obj=mock_jira) is False
        assert len(outage_lines()) == 2
        assert mock_jira.myself.call_count == 2
    finally:
        _credential_rejected.cache_clear()


@pytest.mark.parametrize(
    "status_code,expected",
    [
        pytest.param(401, True, id="401_rejected"),
        pytest.param(403, True, id="403_rejected"),
        pytest.param(404, False, id="404_not_rejected"),
        pytest.param(500, False, id="500_not_rejected"),
    ],
)
def test_credential_rejected_truth_table(mocker, status_code, expected):
    # The verdict is cached per client, so a fresh mock needs a fresh cache.
    _credential_rejected.cache_clear()
    mock_jira = mocker.MagicMock()
    mock_jira.myself.side_effect = JIRAError(status_code=status_code)

    try:
        assert _credential_rejected(jira_obj=mock_jira) is expected
    finally:
        _credential_rejected.cache_clear()
    mock_jira.myself.assert_called_once()


def test_credential_rejected_when_myself_succeeds(mocker):
    _credential_rejected.cache_clear()
    mock_jira = mocker.MagicMock()
    mock_jira.myself.return_value = {"accountId": "account-id"}

    try:
        assert _credential_rejected(jira_obj=mock_jira) is False
    finally:
        _credential_rejected.cache_clear()


@pytest.mark.parametrize("status_code", [pytest.param(401, id="401"), pytest.param(403, id="403")])
def test_credential_rejected_service_account_probes_projects(mocker, status_code):
    mock_jira = mocker.MagicMock()
    # read:jira-work does not cover /myself, so a scope-limited credential is rejected there while healthy.
    mock_jira.myself.side_effect = JIRAError(status_code=status_code)

    assert _credential_rejected(jira_obj=mock_jira, service_account=True) is False
    mock_jira.projects.assert_called_once()


@pytest.mark.parametrize("status_code", [pytest.param(401, id="401"), pytest.param(403, id="403")])
def test_credential_rejected_service_account_when_projects_also_fails(mocker, status_code):
    mock_jira = mocker.MagicMock()
    mock_jira.myself.side_effect = JIRAError(status_code=status_code)
    mock_jira.projects.side_effect = JIRAError(status_code=403)

    # Both endpoints reject the credential, so the verdict stays "rejected" for the service-account flavour.
    assert _credential_rejected(jira_obj=mock_jira, service_account=True) is True


def test_credential_rejected_data_center_flavour_does_not_probe_projects(mocker):
    mock_jira = mocker.MagicMock()
    mock_jira.myself.side_effect = JIRAError(status_code=403)

    # The projects() fallback is service-account only: a static PAT that fails /myself is rejected outright.
    assert _credential_rejected(jira_obj=mock_jira) is True
    mock_jira.projects.assert_not_called()


def test_get_jira_mismatch_oauth_scan_continues_when_projects_succeed(mocker, monkeypatch):
    _clear_jira_env(monkeypatch)
    mock_logger = _patch_logger(mocker)
    mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token", return_value="minted-token")
    # _credential_rejected is not mocked: the real probe must see through the /myself 403 via projects().
    result, mock_jira_cls, mock_get_jira_information = _run_scan(
        mocker,
        OAUTH_ARGS,
        issues={"ABC-1", "ABC-2"},
        per_issue_status=403,
        myself_side_effect=JIRAError(status_code=403, text="Forbidden"),
    )

    assert result.exit_code == 1
    # A scope-limited credential is healthy, so every issue is still checked and the failures aggregate.
    assert mock_get_jira_information.call_count == 2
    mock_jira_cls.return_value.projects.assert_called_once()
    errors = _logged(mock_logger, "error")
    assert _hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT, oauth_service_account=True) == []
    assert [message for message in errors if "Following Jira ids failed" in message]


def test_get_jira_mismatch_check_auth_accepts_scoped_token_when_projects_succeed(mocker):
    mock_logger = _patch_logger(mocker)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    # A pre-minted scoped token carries read:jira-work only, so /myself is 401 while the token itself is healthy.
    mock_jira_cls.return_value.myself.side_effect = JIRAError(status_code=401, text="Unauthorized")
    mock_jira_cls.return_value.projects.return_value = [{"key": "ABC"}]
    mock_get_jiras = mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})
    # The project-visibility verdict cross-checks the endpoint without a header, so that call is mocked too.
    _mock_anonymous_probe(mocker)

    result = CliRunner().invoke(get_jira_mismatch, [*SCOPED_TOKEN_ARGS, "--check-auth"])

    assert result.exit_code == 0, _logged(mock_logger, "error")
    # The project endpoint, not /myself, decides whether a scope-limited credential is alive.
    mock_jira_cls.return_value.projects.assert_called_once()
    assert [message for message in _logged(mock_logger, "info") if "project visibility verified" in message]
    mock_get_jiras.assert_not_called()


def test_get_jira_mismatch_scoped_token_scan_continues_when_myself_is_denied(mocker):
    mock_logger = _patch_logger(mocker)
    # _credential_rejected is not mocked: the real probe must see through the /myself 401 via projects().
    result, mock_jira_cls, mock_get_jira_information = _run_scan(
        mocker,
        SCOPED_TOKEN_ARGS,
        issues={"ABC-1", "ABC-2"},
        per_issue_status=403,
        myself_side_effect=JIRAError(status_code=401, text="Unauthorized"),
    )

    assert result.exit_code == 1
    # The credential is healthy, so a per-issue 403 aggregates instead of aborting after the first issue.
    assert mock_get_jira_information.call_count == 2
    mock_jira_cls.return_value.projects.assert_called_once()
    errors = _logged(mock_logger, "error")
    assert _hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT) == []
    assert [message for message in errors if "Following Jira ids failed" in message]


@pytest.mark.parametrize(
    "statuses,expected_probes",
    [
        pytest.param((403, 403), 1, id="repeated-403-uses-the-cached-verdict"),
        pytest.param((403, 401), 2, id="later-401-reprobes"),
    ],
)
def test_scan_reprobes_the_credential_only_after_a_401(mocker, statuses, expected_probes):
    mock_logger = _patch_logger(mocker)
    per_issue_results = [
        ("file.py", f"ABC-{index} error {status_code}", status_code)
        for index, status_code in enumerate(statuses, start=1)
    ]

    result, mock_jira_cls, mock_get_jira_information = _run_scan(
        mocker,
        SCOPED_TOKEN_ARGS,
        issues=["ABC-1", "ABC-2"],
        per_issue_results=per_issue_results,
    )

    assert result.exit_code == 1
    # Neither issue aborts the scan: the probe answers, so the failures aggregate.
    assert mock_get_jira_information.call_count == 2
    # myself() is the probe's network call: a 401 can be a bearer that expired after an earlier 403 was judged
    # harmless, so that verdict must not be reused, while a repeated 403 may.
    assert mock_jira_cls.return_value.myself.call_count == expected_probes
    assert [message for message in _logged(mock_logger, "error") if "Following Jira ids failed" in message]


def test_two_failing_issues_in_one_file_both_reach_the_aggregate(mocker):
    mock_logger = _patch_logger(mocker)
    result, _, mock_get_jira_information = _run_scan(
        mocker,
        SCOPED_TOKEN_ARGS,
        issues=["ABC-1", "ABC-2"],
        per_issue_results=[
            ("file.py", "ABC-1 current status: closed is resolved.", None),
            ("file.py", "ABC-2 current status: closed is resolved.", None),
        ],
    )

    assert result.exit_code == 1
    assert mock_get_jira_information.call_count == 2
    aggregate = [message for message in _logged(mock_logger, "error") if "Following Jira ids failed" in message]
    assert len(aggregate) == 1
    # One file can hold many failures; the report must not keep only the last one.
    assert "ABC-1 current status: closed is resolved." in aggregate[0]
    assert "ABC-2 current status: closed is resolved." in aggregate[0]


@pytest.mark.parametrize(
    "exp,expected",
    [
        pytest.param(JIRAError(status_code=400), False, id="400_not_retryable"),
        pytest.param(JIRAError(status_code=401), False, id="401_not_retryable"),
        pytest.param(JIRAError(status_code=403), False, id="403_not_retryable"),
        pytest.param(JIRAError(status_code=404), False, id="404_not_retryable"),
        pytest.param(JIRAError(status_code=429), False, id="429_not_retryable_python_jira_retries_it"),
        pytest.param(JIRAError(status_code=500), True, id="500_retryable"),
        pytest.param(JIRAError(status_code=503), True, id="503_retryable"),
        pytest.param(JIRAError(status_code=None), True, id="none_status_retryable"),
        pytest.param(RuntimeError("boom"), False, id="non_jira_error_not_retryable"),
    ],
)
def test_retryable_jira_error(exp, expected):
    assert _retryable_jira_error(exp) is expected


@pytest.mark.parametrize("status_code", [pytest.param(401, id="401"), pytest.param(404, id="404")])
def test_get_issue_does_not_retry_permanent_error(mocker, status_code):
    mocker.patch.object(get_issue.retry, "sleep")
    mock_jira = mocker.MagicMock()
    mock_jira.issue.side_effect = JIRAError(status_code=status_code, text="permanent")

    with pytest.raises(JIRAError):
        get_issue(jira=mock_jira, jira_id=f"ABC-{status_code}")

    # A 4xx fails identically on every attempt, it can never succeed.
    assert mock_jira.issue.call_count == 1


def test_get_issue_retries_server_error(mocker):
    mocker.patch.object(get_issue.retry, "sleep")
    mock_jira = mocker.MagicMock()
    mock_jira.issue.side_effect = JIRAError(status_code=500, text="Server error")

    with pytest.raises(RetryError):
        get_issue(jira=mock_jira, jira_id="ABC-500")

    assert mock_jira.issue.call_count == 3


def test_get_jira_mismatch_scan_survives_a_connection_error(mocker):
    mock_logger = _patch_logger(mocker)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    # A transport error is not a JIRAError, so tenacity does not retry it: it surfaces raw out of get_issue for the
    # issue that hit it.
    mock_jira_cls.return_value.issue.side_effect = requests.ConnectionError("Connection refused")
    mocker.patch(
        "apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={"file.py": {"ABC-1", "ABC-2"}}
    )

    result = CliRunner().invoke(get_jira_mismatch, list(SCOPED_TOKEN_ARGS))

    # A RequestException left unwrapped escapes the worker future as a traceback instead of the normal exit path.
    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    assert _logged(mock_logger, "exception") == []
    aggregate = [message for message in _logged(mock_logger, "error") if "Following Jira ids failed" in message]
    assert len(aggregate) == 1
    # Reported per issue in the documented shape, with no status code to show, so the scan keeps accumulating.
    assert aggregate[0].count("Jira connection failed (ConnectionError): Connection refused.") == 2
    assert "status code" not in aggregate[0]


def test_get_jira_mismatch_scan_survives_exhausted_retry(mocker):
    mock_logger = _patch_logger(mocker)
    mocker.patch.object(get_issue.retry, "sleep")
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    mock_jira_cls.return_value.issue.side_effect = JIRAError(status_code=500, text="Server error")
    mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={"file.py": {"ABC-500"}})

    result = CliRunner().invoke(get_jira_mismatch, list(SCOPED_TOKEN_ARGS))

    # A RetryError left unwrapped escapes the worker future as a traceback instead of the normal exit path.
    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    assert _logged(mock_logger, "exception") == []
    aggregate = [message for message in _logged(mock_logger, "error") if "Following Jira ids failed" in message]
    assert len(aggregate) == 1
    assert "ABC-500 JiraError status code: 500, details: Server error." in aggregate[0]


@pytest.mark.parametrize("status_code", [None, 500])
def test_get_jira_mismatch_non_auth_client_error_reports_cause(mocker, status_code):
    mock_logger = _patch_logger(mocker)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mocker.patch(
        "apps.jira_utils.jira_information.build_jira_client",
        side_effect=JIRAError(status_code=status_code, text="connection refused"),
    )
    mock_get_jiras = mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})

    result = CliRunner().invoke(get_jira_mismatch, ["--cloud-id", CLOUD_ID, "--token", "t", "--check-auth"])

    assert result.exit_code == 1
    # Not a credential problem: report the real cause instead of token re-minting advice.
    logged = "\n".join(_logged(mock_logger, "exception") + _logged(mock_logger, "error"))
    assert "likely expired" not in logged
    assert "connection refused" in logged
    mock_get_jiras.assert_not_called()


@pytest.mark.parametrize(
    "exp",
    [
        pytest.param(requests.ConnectionError("Connection refused"), id="connection-error"),
        pytest.param(requests.Timeout("timed out"), id="timeout"),
    ],
)
def test_get_jira_mismatch_request_exception_during_construction_reports_outage(mocker, exp):
    mock_logger = _patch_logger(mocker)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mocker.patch("apps.jira_utils.jira_information.build_jira_client", side_effect=exp)
    mock_get_jiras = mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})

    result = CliRunner().invoke(get_jira_mismatch, ["--cloud-id", CLOUD_ID, "--token", "t", "--check-auth"])

    assert result.exit_code == 1
    # A transport exception is an outage: one line, no traceback, no credential advice.
    assert isinstance(result.exception, SystemExit)
    assert _logged(mock_logger, "exception") == []
    assert "Traceback" not in result.output
    errors = _logged(mock_logger, "error")
    assert len(errors) == 1
    assert errors[0].startswith(f"Jira client construction failed ({type(exp).__name__}, status: None): ")
    assert str(exp) in errors[0]
    assert _hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT) == []
    mock_get_jiras.assert_not_called()


def test_get_jira_mismatch_request_exception_during_the_probe_reports_outage(mocker):
    mock_logger = _patch_logger(mocker)
    result, _, _ = _run_oauth_command(
        mocker,
        *SCOPED_TOKEN_ARGS,
        "--check-auth",
        # A raw requests error escapes _probe_credential, which only translates JIRAError.
        myself_error=requests.ConnectionError("Name or service not known"),
    )

    assert result.exit_code == 1
    assert _logged(mock_logger, "exception") == []
    errors = _logged(mock_logger, "error")
    assert len(errors) == 1
    assert errors[0].startswith("Jira credential check failed (ConnectionError, status: None): ")
    assert "Name or service not known" in errors[0]
    assert _hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT) == []
    assert [message for message in errors if "Jira authentication failed" in message] == []


def test_scan_continues_when_the_probe_is_an_outage(mocker):
    mock_logger = _patch_logger(mocker)
    result, mock_jira_cls, mock_get_jira_information = _run_scan(
        mocker,
        SCOPED_TOKEN_ARGS,
        issues=["ABC-1", "ABC-2"],
        per_issue_status=401,
        # An unreachable API is an outage, not a rejected credential.
        myself_side_effect=requests.ConnectionError("Connection refused"),
    )

    assert result.exit_code == 1
    # The scan is not aborted with a credential hint: the per-issue failures are aggregated instead.
    assert mock_get_jira_information.call_count == 2
    # The first 401 clears the cached verdict once, so the second issue reuses that probe rather than re-running it:
    # one probe per scan, and the outage is still not hidden from the second issue.
    assert mock_jira_cls.return_value.myself.call_count == 1
    errors = _logged(mock_logger, "error")
    assert _hint_logs(errors, AUTH_MODE_SERVICE_ACCOUNT) == []
    assert [message for message in errors if "Following Jira ids failed" in message]


def test_get_jira_mismatch_non_auth_client_error_record_is_bounded(mocker, caplog):
    caplog.set_level(logging.DEBUG)
    body = "connection refused\n" + "y" * 300
    response = mocker.MagicMock()
    response.headers = {"Set-Cookie": "session=secret"}
    response.text = body
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mocker.patch(
        "apps.jira_utils.jira_information.build_jira_client",
        side_effect=JIRAError(status_code=500, text=body, response=response),
    )
    mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})

    result = CliRunner().invoke(get_jira_mismatch, ["--cloud-id", CLOUD_ID, "--token", "t", "--check-auth"])

    assert result.exit_code == 1
    records = [record for record in caplog.records if "client construction failed" in record.getMessage()]
    assert len(records) == 1
    # Rendered as a user would see it: a traceback (exc_info) would re-render JIRAError.__str__, which appends the
    # raw response body and headers and defeats the bound and the redaction.
    rendered = logging.Formatter().format(records[0])
    assert len(rendered) < 500
    assert "response text" not in rendered
    assert "Set-Cookie" not in rendered
    assert "\n" not in rendered


@pytest.mark.parametrize(
    "status_code,body",
    [
        pytest.param(401, "Unauthorized", id="expired-scoped-token"),
        pytest.param(403, "Failed to parse Connect Session Auth Token", id="scoped-token-on-site-url"),
    ],
)
def test_get_jira_mismatch_auth_client_error_reports_hint(mocker, monkeypatch, status_code, body):
    # A scoped token sent to the site url with no cloud id resolves to Data Center PAT, and JIRA.__init__ calls
    # serverInfo eagerly, so the rejected credential raises from the client constructor.
    _clear_jira_env(monkeypatch)
    mock_logger = _patch_logger(mocker)
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mocker.patch(
        "apps.jira_utils.jira_information.JIRA",
        side_effect=JIRAError(status_code=status_code, text=body),
    )
    mock_get_jiras = mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})

    result = CliRunner().invoke(
        get_jira_mismatch,
        ["--url", "https://issues.redhat.com", "--token", "s3cr3t-token-value", "--cloud-id", ""],
    )

    assert result.exit_code == 1
    errors = _logged(mock_logger, "error")
    auth_messages = [message for message in errors if "Jira authentication failed" in message]
    # One actionable line naming the mode and keeping Jira's body, not a bare status.
    assert len(auth_messages) == 1
    assert f"(mode: {AUTH_MODE_DATACENTER_PAT}," in auth_messages[0]
    assert body in auth_messages[0]
    assert len(_hint_logs(errors, AUTH_MODE_DATACENTER_PAT)) == 1
    # The generic construction line would leave the user without the hint, so it must not be used here.
    assert [message for message in errors if "Jira client construction failed" in message] == []
    mock_get_jiras.assert_not_called()


@pytest.mark.parametrize(
    "jira_url,content,expected",
    [
        pytest.param(
            "",
            "JIRA URL is https://redhat.atlassian.net/browse/CNV-1234",
            {"CNV-1234"},
            id="empty_url_matches_site_link",
        ),
        pytest.param("", "see /tmp/browse/ABC-7 for details", set(), id="empty_url_ignores_absolute_path"),
        pytest.param("", "see foo/browse/ABC-7 for details", set(), id="empty_url_ignores_relative_path"),
        pytest.param(
            "https://issues.redhat.com",
            "JIRA URL is https://issuesXredhat.com/browse/CNV-1",
            set(),
            id="explicit_url_is_escaped",
        ),
    ],
)
def test_browse_link_regex_hardening(jira_url, content, expected):
    jira_ids = get_jira_ids_from_file_content(file_content=content, issue_pattern=r"([A-Z]+-[0-9]+)", jira_url=jira_url)
    assert jira_ids == expected
