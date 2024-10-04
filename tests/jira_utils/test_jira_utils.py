import pytest
from unittest.mock import MagicMock, patch
from apps.jira_utils.jira_information import get_jira_information, process_jira_command_line_config_file
from simple_logger.logger import get_logger

LOGGER = get_logger(name=__name__)


def test_process_jira_command_line_config_file_empty_config_token():
    config_file_path = None
    url = "https://example.com"
    token = ""
    issue_pattern = "*"
    resolved_statuses = ["RESOLVED"]
    version_string_not_targeted_jiras = "v1.*"
    target_versions = ["v2", "v3"]
    skip_projects = [1, 2]
    with patch("apps.jira_utils.jira_information.get_util_config") as mock_get_util_config_mock:
        mock_get_util_config_mock.return_value = {}
        with pytest.raises(SystemExit) as pytest_wrapped_e:
            process_jira_command_line_config_file(
                config_file_path,
                url,
                token,
                issue_pattern,
                resolved_statuses,
                version_string_not_targeted_jiras,
                target_versions,
                skip_projects,
            )
        assert pytest_wrapped_e.value.code == 1


def test_process_jira_command_line_config_file_valid_config():
    config_file_path = "/path/to/config"
    url = "https://example.com"
    token = "1234567890"
    issue_pattern = "*"
    resolved_statuses = ["RESOLVED"]
    version_string_not_targeted_jiras = "v1.*"
    target_versions = ["v2", "v3"]
    skip_projects = [1, 2]
    with patch("apps.jira_utils.jira_information.get_util_config") as mock_get_util_config_mock:
        mock_get_util_config_mock.return_value = {
            "url": url,
            "token": token,
            "issue_pattern": "*",
            "resolved_statuses": ["RESOLVED"],
            "version_string_not_targeted_jiras": version_string_not_targeted_jiras,
            "target_versions": target_versions,
            "skip_project_ids": skip_projects,
        }
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


@pytest.mark.parametrize(
    "jira_id, resolved_status, jira_target_versions, target_version_str, file_name, expected_jira_error_string, "
    "test_jira_version",
    [
        ("issue1", ["resolved"], [], "1.0", "file1.txt", "", "1.0"),
        ("issue2", ["open"], [], "1.0", "file2.txt", "issue2 current status: open is resolved.", "1.0"),
        ("issue3", [], [], "", "file3.txt", "", "1.1"),
        ("issue4", ["resolved"], ["1.0"], "1.0", "file4.txt", "", "1.0"),
        (
            "issue5",
            ["resolved"],
            ["1.1"],
            "1.0",
            "file4.txt",
            "issue5 target version: 1.0, does not match expected " "version ['1.1'].",
            "1.0",
        ),
    ],
)
def test_get_issue(
    jira_id,
    resolved_status,
    jira_target_versions,
    target_version_str,
    file_name,
    expected_jira_error_string,
    test_jira_version,
):
    with patch("apps.jira_utils.jira_information.get_issue") as get_patched_issue:
        mock_jira = MagicMock()
        mock_jira.fields.status.name = "open"
        jira_version = MagicMock()
        jira_version.name = test_jira_version
        mock_jira.fixVersions = [jira_version]
        get_patched_issue.return_value = mock_jira
        if jira_target_versions:
            with patch("apps.jira_utils.jira_information.re.search") as mock_search:
                mock_search.return_value = MagicMock(group=lambda x: test_jira_version)
                result = get_jira_information(
                    jira_object=mock_jira,
                    jira_id=jira_id,
                    resolved_status=resolved_status,
                    target_version_str="1.0",
                    skip_project_ids=[],
                    jira_target_versions=jira_target_versions,
                    file_name=file_name,
                )
                assert result == (file_name, expected_jira_error_string)
        else:
            result = get_jira_information(
                jira_object=mock_jira,
                jira_id=jira_id,
                resolved_status=resolved_status,
                target_version_str="1.0",
                skip_project_ids=[],
                jira_target_versions=jira_target_versions,
                file_name=file_name,
            )
        assert result == (file_name, expected_jira_error_string)
