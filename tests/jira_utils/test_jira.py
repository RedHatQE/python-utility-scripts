from apps.jira_utils.get_jira_information import get_closed_jiras
from tests.utils import get_cli_runner
from simple_logger.logger import get_logger
from unittest import mock

LOGGER = get_logger(name=__name__)


def test_jira_missing_target_version():
    result = get_cli_runner().invoke(get_closed_jiras, "--jira-cfg-file example.jira.cfg")
    LOGGER.info(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code != 0
    assert "Missing option '--jira-target-versions'" in result.output


def test_jira_missing_cfg_file():
    result = get_cli_runner().invoke(get_closed_jiras, '--jira-cfg-file jira.cfg --jira-target-versions "ABC"')
    LOGGER.info(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 1
    assert "Jira config file is required." in result.output


def test_jira_with_closed_bugs():
    with mock.patch("apps.jira_utils.get_jira_information.JiraConnector") as jira_connector:
        a_instance = jira_connector.return_value
        a_instance.get_issue_metadata.return_value = {'status':{'name':'closed'}}
        with mock.patch("apps.jira_utils.get_jira_information.get_jiras_from_python_files") as jiras:
            jiras.return_value = {"file1": ['ABC-1234']}
            with mock.patch("apps.jira_utils.get_jira_information.JiraConnector.get_closed_jira_ids") as closed_ids:
                closed_ids.return_value = "{'file1': ['ABC-1234' ['closed']]}"
                result = get_cli_runner().invoke(get_closed_jiras, '--jira-target-versions "ABC"')
                assert result.exit_code == 1
                #assert f"Following jiras are not open or could not be accessed:" in result.output
            LOGGER.info(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
