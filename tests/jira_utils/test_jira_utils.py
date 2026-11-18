import logging
import os
import shlex
import subprocess

import pytest
from click.testing import CliRunner
from jira.exceptions import JIRAError
from pyhelper_utils.shell import run_command
from simple_logger.logger import get_logger
from tenacity import RetryError

from apps.jira_utils.jira_information import (
    AUTH_MODE_CLOUD_BASIC,
    AUTH_MODE_DATACENTER_PAT,
    AUTH_MODE_SERVICE_ACCOUNT,
    SA_GATEWAY_URL_TEMPLATE,
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
    process_jira_command_line_config_file,
    resolve_jira_server,
)

LOGGER = get_logger(name=__name__)
BASE_COMMAND = "uv run apps/jira_utils/jira_information.py --verbose  "

CLOUD_ID = "a1b2c3d4-0000-1111-2222-333344445555"
GATEWAY_URL = SA_GATEWAY_URL_TEMPLATE.format(cloud_id=CLOUD_ID)
# --cloud-id "" overrides the JIRA_CLOUD_ID env default so the run stays env independent.
DATACENTER_ARGS = ("--url", "https://issues.redhat.com", "--token", "t", "--cloud-id", "")


def _run_jira_command(mocker, *args):
    """Invoke the click command with the Jira client and scan path mocked out."""
    mocker.patch("apps.jira_utils.jira_information.get_util_config", return_value={})
    mock_jira_cls = mocker.patch("apps.jira_utils.jira_information.JIRA")
    mock_get_jiras = mocker.patch("apps.jira_utils.jira_information.get_jiras_from_python_files", return_value={})
    result = CliRunner().invoke(get_jira_mismatch, list(args))
    return result, mock_jira_cls, mock_get_jiras


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


def _hint_logs(messages, mode):
    """Return the credential hints for `mode` among the given log messages."""
    return [message for message in messages if message == _auth_failure_hint(mode=mode)]


JIRA_ENV_VARS = ("JIRA_CLOUD_ID", "JIRA_URL", "JIRA_SERVER_URL", "JIRA_TOKEN", "JIRA_USER")


def _clear_jira_env(monkeypatch):
    """Drop inherited Jira env defaults so config tests assert on their own arguments only."""
    for env_var in JIRA_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)


def test_process_jira_command_line_config_file_empty_config_token() -> None:
    config_file = os.path.join(os.path.dirname(__file__), "test_jira_cfg_file.yaml")
    # Strip Jira env defaults so the assertion reflects the config file only.
    env = {key: value for key, value in os.environ.items() if not key.startswith("JIRA_")}
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
    for option in get_jira_mismatch.params:
        if option.name in defaults:
            monkeypatch.setattr(option, "default", defaults[option.name])
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
    errors = "\n".join(_logged(mock_logger, "error"))
    assert "Config file or token plus url (or cloud id) are required." in errors
    mock_jira_cls.assert_not_called()
    mock_get_jiras.assert_not_called()


def test_get_jira_mismatch_config_cloud_id_opt_out_requires_url(mocker, monkeypatch, tmp_path):
    mock_logger = _patch_logger(mocker)
    config_file = tmp_path / "jira_cfg.yaml"
    config_file.write_text(f'pyutils-jira:\n  cloud_id: "{CLOUD_ID}"\n  token: "{SA_ENV_TOKEN}"\n')
    monkeypatch.delenv("JIRA_CLOUD_ID", raising=False)
    for env_var in ("JIRA_URL", "JIRA_SERVER_URL", "JIRA_USER"):
        monkeypatch.delenv(env_var, raising=False)
    for option in get_jira_mismatch.params:
        if option.name in ("url", "token", "user"):
            monkeypatch.setattr(option, "default", None)
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
    mock_credential_rejected.assert_called_once_with(jira_obj=mock_jira_cls.return_value)
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
