import re
from simple_logger.logger import get_logger
from jira import JIRA, JIRAError, Issue
from tenacity import retry, retry_if_exception_type, wait_fixed, stop_after_attempt
from apps.utils import all_python_files
from typing import List, Dict, Any

LOGGER = get_logger(name=__name__)


@retry(retry=retry_if_exception_type(JIRAError), stop=stop_after_attempt(3), wait=wait_fixed(2))
def get_issue(jira: JIRA, jira_id: str) -> Issue:
    LOGGER.info(f"Retry staistics for {jira_id}: {get_issue.statistics}")
    return jira.issue(id=jira_id, fields="status, issuetype, fixVersions")


def get_jira_ids_from_file_content(file_content: str, issue_pattern: str, jira_url: str) -> List[str]:
    """
    Try to find all Jira tickets in a given file content.
    Looking for the following patterns:
    - jira_id=ABC-12345  # When jira id is present in a function call
    - <jira_url>/browse/ABC-12345  # when jira is in a link in comments
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
        rf"{jira_url}/browse/{issue_pattern}",
        file_content,
    )
    return list(set(_pytest_jira_marker_bugs + _jira_id_arguments + _jira_url_jiras))


def get_jiras_from_python_files(issue_pattern: str, jira_url: str) -> Dict[str, Any]:
    """
    Get all python files from the current directory and get list of jira ids from each of them

    Args:
        issue_pattern (str): regex pattern for jira ids
        jira_url (str): jira url that could be used to look for possible presence of jira references in a file

    Returns:
        Dict: A dict of filenames and associated jira tickets.

    Note: any line containing <skip-jira_utils-check> would be not be checked for presence of a jira id
    """
    jira_found: Dict[str, list[str]] = {}
    for filename in all_python_files():
        with open(filename) as fd:
            # if <skip-jira-utils-check> appears in a line, exclude that line from jira check
            if unique_jiras := get_jira_ids_from_file_content(
                file_content="\n".join([line for line in fd.readlines() if "<skip-jira-utils-check>" not in line]),
                issue_pattern=issue_pattern,
                jira_url=jira_url,
            ):
                jira_found[filename] = unique_jiras
    if jira_found:
        LOGGER.info(f"Following jiras are found: {jira_found}")
    return jira_found


class JiraInvalidConfigFileError(Exception):
    pass


class JiraValidationError(Exception):
    pass
