import os
import shlex
import subprocess

import pytest
from jira.exceptions import JIRAError
from pyhelper_utils.shell import run_command
from simple_logger.logger import get_logger

from apps.jira_utils.jira_information import (
    get_jira_ids_from_file_content,
    get_jira_information,
    process_jira_command_line_config_file,
)

LOGGER = get_logger(name=__name__)
BASE_COMMAND = "uv run apps/jira_utils/jira_information.py --verbose  "


def test_process_jira_command_line_config_file_empty_config_token() -> None:
    config_file = os.path.join(os.path.dirname(__file__), "test_jira_cfg_file.yaml")
    rc, _, err = run_command(
        command=shlex.split(f"{BASE_COMMAND} --config-file-path {config_file}"),
        verify_stderr=False,
        check=False,
        capture_output=False,
        stderr=subprocess.PIPE,
    )
    assert "Jira url and token are required." in err
    assert not rc


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

    assert result == (file_name, expected_jira_error_string)


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

    assert result[1] == "404 JiraError status code: 404, details: Issue not found]."
