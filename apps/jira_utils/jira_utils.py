import re
from simple_logger.logger import get_logger
from jira import JIRA, JIRAError, Issue
from tenacity import retry, retry_if_exception_type, wait_fixed, stop_after_attempt
from apps.utils import all_python_files
from typing import List, Dict, Any

LOGGER = get_logger(name=__name__)


class JiraConnector:
    def __init__(self, token: str, url: str) -> None:
        self.token = token
        self.url = url
        self.jira = JIRA(token_auth=self.token, options={"server": self.url})

    @retry(retry=retry_if_exception_type(JIRAError), stop=stop_after_attempt(3), wait=wait_fixed(2))
    def get_issue(self, jira_id: str) -> Issue:
        LOGGER.info(f"Retry staistics: {self.get_issue.statistics}")
        return self.jira.issue(id=jira_id, fields="status, issuetype, fixVersions")


def get_jira_ids_from_file_content(file_content: str, issue_pattern: str) -> List[str]:
    """
    Try to find all jira_utils tickets in a given file content.
    Looking for the following patterns:
    - jira_id=ABC-12345  # When jira id is present in a function call
    - https://issues.redhat.com/browse/ABC-12345  # when jira is in a link in comments
    - pytest.mark.jira_utils(ABC-12345)  # when jira is in a marker

    Args:
        file_content (str): The content of a given file.
        issue_pattern (str): regex pattern for jira ids

    Returns:
        list: A list of jira tickets.
    """
    _pytest_jira_marker_bugs = re.findall(rf"pytest.mark.jira.*?{issue_pattern}.*", file_content, re.DOTALL)
    _jira_id_arguments = re.findall(rf"jira_id\s*=[\s*\"\']*{issue_pattern}.*", file_content)
    _jira_url_jiras = re.findall(
        rf"https://issues.redhat.com/browse/{issue_pattern}",
        file_content,
    )
    return list(set(_pytest_jira_marker_bugs + _jira_id_arguments + _jira_url_jiras))


def get_jiras_from_python_files(issue_pattern: str) -> Dict[str, Any]:
    """
    Get all python files from the current directory and get list of jira ids from each of them

    Args:
        issue_pattern (str): regex pattern for jira ids

    Returns:
        Dict: A dict of filenames and associated jira tickets.

    Note: any line containing <skip-jira_utils-check> would be not be checked for presence of a jira id
    """
    jira_found: Dict[str, list[str]] = {}
    for filename in all_python_files():
        with open(filename) as fd:
            file_content = []
            for line in fd.readlines():
                # if <skip-jira_utils-check> appears in a line, exclude that line from jira check
                if "<skip-jira_utils-check>" in line:
                    continue
                else:
                    file_content.append(line)
            if unique_jiras := get_jira_ids_from_file_content(
                file_content="\n".join(file_content), issue_pattern=issue_pattern
            ):
                jira_found[filename] = unique_jiras
    LOGGER.info(f"Following jiras are found: {jira_found}")
    return jira_found
