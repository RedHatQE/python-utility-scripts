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
    SA_GATEWAY_URL_TEMPLATE,
    TOKEN_REFRESH_MARGIN_SECONDS,
    JiraTokenMintError,
    _auth_failure_hint,
    _check_jira_auth,
    _credential_rejected,
    _jira_browse_url,
    _report_jira_auth_error,
    _retryable_jira_error,
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
# --cloud-id "" overrides the JIRA_CLOUD_ID env default so the run stays env independent.
DATACENTER_ARGS = ("--url", "https://issues.redhat.com", "--token", "t", "--cloud-id", "")
OAUTH_CLIENT_ID = "oauth-client-id"
OAUTH_CLIENT_SECRET = "s3cr3t-client-secret"
# --cloud-id is explicit so the OAuth runs never depend on JIRA_CLOUD_ID / ATLASSIAN_OAUTH_CLOUD_ID.
OAUTH_ARGS = ("--cloud-id", CLOUD_ID, "--client-id", OAUTH_CLIENT_ID, "--client-secret", OAUTH_CLIENT_SECRET)
JIRA_SOURCE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "apps", "jira_utils", "jira_information.py")
)


def _run_jira_command(mocker, *args):
    """Invoke the click command with the Jira client and scan path mocked out."""
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    mock_get_jiras = mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})
    result = CliRunner().invoke(get_jira_mismatch, list(args))
    return result, mock_jira_cls, mock_get_jiras


def _run_oauth_command(mocker, *args, myself_error=None, projects_error=None):
    """Invoke the click command with the token mint and the Jira client mocked out (offline)."""
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mock_mint = mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token", return_value="minted-token")
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    if myself_error is not None:
        mock_jira_cls.return_value.myself.side_effect = myself_error
    if projects_error is not None:
        mock_jira_cls.return_value.projects.side_effect = projects_error
    mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})
    result = CliRunner().invoke(get_jira_mismatch, list(args))
    return result, mock_jira_cls, mock_mint


def _run_scan(mocker, scan_args, issues, per_issue_status, myself_side_effect=None):
    """Run a scan over `issues`, where every issue fails with `per_issue_status`."""
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    if myself_side_effect is not None:
        mock_jira_cls.return_value.myself.side_effect = myself_side_effect
    mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={"file.py": issues})
    mock_get_jira_information = mocker.patch(
        "apps.jira_utils.jira_information.get_jira_information",
        return_value=("file.py", f"error {per_issue_status}", per_issue_status),
    )
    result = CliRunner().invoke(get_jira_mismatch, list(scan_args))
    return result, mock_jira_cls, mock_get_jira_information


def _patch_logger(mocker):
    """Return a mock of the module logger; it does not propagate, so CLI logs are asserted on its calls."""
    return mocker.patch("apps.jira_utils.jira_information.LOGGER")


def _logged(mock_logger, level):
    """Return the messages logged at `level` through `mock_logger`."""
    return [call.args[0] for call in getattr(mock_logger, level).call_args_list]


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

    `cloud_id` is resolved from the env at call time and the other values arrive as os.getenv() click defaults that
    were frozen at import, so both must be neutralised for a test to be independent of the developer's own Jira
    credential. A test that wants an env-provided value sets it itself, which makes the dependency explicit.
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
            client_secret="client-secret",
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
    assert result["client_secret"] == "config-client-secret"
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
    assert result["client_secret"] == "config-client-secret"


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
    assert set(_ATLASSIAN_TOKEN_CACHE) == {_token_cache_key(client_secret="rotated-secret")}


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
    "env_var,option_name,expected",
    [
        pytest.param("ATLASSIAN_OAUTH_CLIENT_ID", "client_id", OAUTH_CLIENT_ID, id="client-id"),
        pytest.param("JIRA_OAUTH_CLIENT_SECRET", "client_secret", OAUTH_CLIENT_SECRET, id="client-secret"),
    ],
)
def test_oauth_credential_env_aliases_are_option_defaults(monkeypatch, env_var, option_name, expected):
    monkeypatch.setenv(env_var, expected)
    # The click defaults are evaluated at import time, so only re-executing the module body can prove the env read.
    spec = importlib.util.spec_from_file_location("jira_information_env_defaults", JIRA_SOURCE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    defaults = {option.name: option.default for option in module.get_jira_mismatch.params}

    assert defaults[option_name] == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        pytest.param(SA_GATEWAY_URL_TEMPLATE.format(cloud_id="cloud-id"), "", id="gateway_url_dropped"),
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

    assert result == ("", "404 JiraError status code: 404, details: Issue not found].", 404)


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

    details = result[1].removeprefix(f"ABC-1 JiraError status code: {status_code}, details: ").removesuffix("].")
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
    assert result[1] == "ABC-1 JiraError status code: 403, details: You do not have permission to see this issue]."
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
    assert SA_GATEWAY_URL_TEMPLATE.format(cloud_id="cloud-id") in caplog.text
    assert "display-name" in caplog.text
    assert "s3cr3t-token-value" not in caplog.text


def test_check_jira_auth_failure_exits(mocker, caplog):
    caplog.set_level(logging.DEBUG)
    mock_jira = mocker.MagicMock()
    mock_jira.myself.side_effect = JIRAError(status_code=403, text="Forbidden")

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
    warnings = [message for message in _logged(mock_logger, "warning") if "discarding supplied Jira url" in message]
    assert len(warnings) == 1


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
    _patch_option_defaults(
        monkeypatch,
        url=None,
        token=None,
        user=None,
        client_id=os.getenv("ATLASSIAN_OAUTH_CLIENT_ID"),
        client_secret=os.getenv("JIRA_OAUTH_CLIENT_SECRET"),
    )
    mock_mint = mocker.patch("apps.jira_utils.jira_information.mint_atlassian_token", return_value="minted-token")
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})

    # No --cloud-id, --client-id or --client-secret: every value comes from an env alias.
    result = CliRunner().invoke(get_jira_mismatch, ["--check-auth"])

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
    errors = _logged(mock_logger, "error")
    # Dropping the stray half switches the identity, so it is reported at ERROR and names the credential in use.
    assert len(errors) == 1
    assert "Incomplete OAuth 2.0 service-account credential" in errors[0]
    assert "ignoring it" in errors[0]
    assert "the Data Center personal access token (PAT)" in errors[0]
    assert OAUTH_CLIENT_ID not in errors[0]
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
    errors = _logged(mock_logger, "error")
    assert len(errors) == 1
    assert "OAuth 2.0 service-account credential ignored for lack of a cloud id" in errors[0]
    # The message names the credential the run actually authenticates with.
    assert expected in errors[0]
    assert OAUTH_CLIENT_SECRET not in errors[0]
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
    errors = _logged(mock_logger, "error")
    assert len(errors) == 1
    assert "the pre-minted scoped API token" in errors[0]
    assert OAUTH_CLIENT_SECRET not in errors[0]
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
    # The abort is decided by the myself() probe, not by the per-issue status code alone.
    mock_credential_rejected.assert_called_once_with(jira_obj=mock_jira_cls.return_value, oauth_service_account=False)
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


def test_credential_rejected_is_cached(mocker):
    getattr(_credential_rejected, "cache_clear", lambda: None)()
    mock_jira = mocker.MagicMock()
    assert _credential_rejected(jira_obj=mock_jira) is False
    assert _credential_rejected(jira_obj=mock_jira) is False
    # Once per scan: myself() is a network call and its verdict cannot change mid-run for one client.
    mock_jira.myself.assert_called_once()


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
def test_credential_rejected_oauth_service_account_probes_projects(mocker, status_code):
    mock_jira = mocker.MagicMock()
    # read:jira-work does not cover /myself, so a scope-limited OAuth credential is rejected there while healthy.
    mock_jira.myself.side_effect = JIRAError(status_code=status_code)

    assert _credential_rejected(jira_obj=mock_jira, oauth_service_account=True) is False
    mock_jira.projects.assert_called_once()


@pytest.mark.parametrize("status_code", [pytest.param(401, id="401"), pytest.param(403, id="403")])
def test_credential_rejected_oauth_service_account_when_projects_also_fails(mocker, status_code):
    mock_jira = mocker.MagicMock()
    mock_jira.myself.side_effect = JIRAError(status_code=status_code)
    mock_jira.projects.side_effect = JIRAError(status_code=403)

    # Both endpoints reject the credential, so the verdict stays "rejected" even for the OAuth flavour.
    assert _credential_rejected(jira_obj=mock_jira, oauth_service_account=True) is True


def test_credential_rejected_data_center_flavour_does_not_probe_projects(mocker):
    mock_jira = mocker.MagicMock()
    mock_jira.myself.side_effect = JIRAError(status_code=403)

    # The projects() fallback is OAuth-only: a static PAT that fails /myself is rejected outright.
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


@pytest.mark.parametrize(
    "exp,expected",
    [
        pytest.param(JIRAError(status_code=401), False, id="401_not_retryable"),
        pytest.param(JIRAError(status_code=403), False, id="403_not_retryable"),
        pytest.param(JIRAError(status_code=404), True, id="404_retryable"),
        pytest.param(JIRAError(status_code=500), True, id="500_retryable"),
        pytest.param(JIRAError(status_code=None), True, id="none_status_retryable"),
        pytest.param(RuntimeError("boom"), False, id="non_jira_error_not_retryable"),
    ],
)
def test_retryable_jira_error(exp, expected):
    assert _retryable_jira_error(exp) is expected


def test_get_issue_does_not_retry_auth_error(mocker):
    mocker.patch.object(get_issue.retry, "sleep")
    mock_jira = mocker.MagicMock()
    mock_jira.issue.side_effect = JIRAError(status_code=401, text="Unauthorized")

    with pytest.raises(JIRAError):
        get_issue(jira=mock_jira, jira_id="ABC-401")

    # Retrying a rejected credential only delays the failure, it can never succeed.
    assert mock_jira.issue.call_count == 1


def test_get_issue_retries_non_auth_error(mocker):
    mocker.patch.object(get_issue.retry, "sleep")
    mock_jira = mocker.MagicMock()
    mock_jira.issue.side_effect = JIRAError(status_code=500, text="Server error")

    with pytest.raises(RetryError):
        get_issue(jira=mock_jira, jira_id="ABC-500")

    assert mock_jira.issue.call_count == 3


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
