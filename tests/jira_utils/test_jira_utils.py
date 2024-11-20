import pytest
from pytest_mock import MockerFixture
from apps.jira_utils.jira_information import (
    get_jira_information,
    process_jira_command_line_config_file,
    get_jira_ids_from_file_content,
)
from simple_logger.logger import get_logger
from pyhelper_utils.shell import run_command
import shlex
import subprocess
import os

LOGGER = get_logger(name=__name__)
BASE_COMMAND = "poetry run python apps/jira_utils/jira_information.py --verbose  "


def test_process_jira_command_line_config_file_empty_config_token(mocker: MockerFixture) -> None:
    config_file = os.path.join(os.path.dirname(__file__), "test_jira_cfg_file.yaml")
    rc, _, err = run_command(
        command=shlex.split(f"{BASE_COMMAND} --config-file-path {config_file}"),
        verify_stderr=False,
        check=False,
        capture_output=False,
        stderr=subprocess.PIPE,
    )
    assert "Jira url and token are required." in err
    assert rc != 0


def test_process_jira_command_line_config_file_valid_config(mocker):
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
    )
    assert result == {
        "url": url,
        "token": token,
        "issue_pattern": issue_pattern,
        "resolved_status": resolved_statuses,
        "not_targeted_version_str": version_string_not_targeted_jiras,
        "target_versions": target_versions,
        "skip_project_ids": skip_projects,
    }
    mock_get_util_config.assert_called_once()


@pytest.mark.parametrize(
    "jira_id, resolved_status, jira_target_versions, target_version_str, file_name, "
    "skip_project_ids, expected_jira_error_string, "
    "test_jira_version",
    [
        # Test case 1: Issue with no jira target versions and not resolved status
        ("issue1", ["resolved"], [], "1.0", "file1.txt", [], "", "1.0"),
        # Test case 2: Issue with no jira target versions, but resolved status
        ("issue2", ["open"], [], "1.0", "file2.txt", [], "issue2 current status: open is resolved.", "1.0"),
        # Test case 3: Issue with no jira target versions, default resolved status
        ("issue3", [], [], "", "file3.txt", [], "", "1.1"),
        # Test case 4: Issue with not resolved state, but matching jira target version
        ("issue4", ["resolved"], ["1.0"], "1.0", "file4.txt", [], "", "1.0"),
        # Test case 5: Issue with not resolved state, and not matching jira target version
        (
            "issue5",
            ["resolved"],
            ["1.1"],
            "1.0",
            "file5.txt",
            [],
            "issue5 target version: 1.0, does not match expected version ['1.1'].",
            "1.0",
        ),
        # Test case 6: Issue that would be skipped for version check because of skip
        ("issue6", ["resolved"], ["1.0"], "1.0", "file6.txt", ["issue"], "", "1.1"),
        # Test case 7: Issue that would be skipped for version check but fail resolved check
        ("issue7", ["open"], ["1.0"], "1.0", "file6.txt", ["issue"], "issue7 current status: open is resolved.", "1.1"),
    ],
    ids=[
        "test_no_jira_versions_no_resolved_status",
        "test_no_jira_versions_resolved_status",
        "test_no_jira_versions_default_resolved_status",
        "test_matching_target_versions",
        "test_no_target_versions_not_resolved_state",
        "test_skip_version_check",
        "test_skip_version_check_fail_status_check",
    ],
)
def test_get_jira_information(
    mocker,
    jira_id,
    resolved_status,
    jira_target_versions,
    target_version_str,
    file_name,
    skip_project_ids,
    expected_jira_error_string,
    test_jira_version,
):
    mock_jira = mocker.MagicMock()
    mock_jira.fields.status.name = "open"
    jira_version = mocker.MagicMock()
    jira_version.name = test_jira_version
    mock_jira.fixVersions = [jira_version]
    mocker.patch("apps.jira_utils.jira_information.get_issue", return_value=mock_jira)

    if jira_target_versions:
        mocker.patch(
            "apps.jira_utils.jira_information.re.search",
            return_value=mocker.MagicMock(group=lambda x: test_jira_version),
        )
        result = get_jira_information(
            jira_object=mock_jira,
            jira_id=jira_id,
            resolved_status=resolved_status,
            target_version_str="1.0",
            skip_project_ids=skip_project_ids,
            jira_target_versions=jira_target_versions,
            file_name=file_name,
        )
    else:
        result = get_jira_information(
            jira_object=mock_jira,
            jira_id=jira_id,
            resolved_status=resolved_status,
            target_version_str="1.0",
            skip_project_ids=skip_project_ids,
            jira_target_versions=jira_target_versions,
            file_name=file_name,
        )

    assert result == (file_name, expected_jira_error_string)


@pytest.mark.parametrize(
    "content_and_expected",
    [
        pytest.param({"content": "pytest.mark.jira(ABC-1111)", "expected": {"ABC-1111"}}, id="pytest_mark_jira"),
        pytest.param({"content": "JIRA ID is jira_id=ABC-1111", "expected": {"ABC-1111"}}, id="jira_id="),
        pytest.param(
            {"content": "JIRA URL is https://example.com/browse/ABC-1111", "expected": {"ABC-1111"}}, id="jira_url="
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
        file_content=content_and_expected["content"], issue_pattern=r"([A-Z]+-[0-9]+)", jira_url="https://example.com"
    )
    assert jira_ids == content_and_expected["expected"]
