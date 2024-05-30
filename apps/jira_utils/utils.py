import os
import re
import click
from configparser import ConfigParser
from packaging.version import InvalidVersion, Version
from simple_logger.logger import get_logger
from jira import JIRA, JIRAError

from apps.utils import all_python_files

LOGGER = get_logger(name=__name__)


def get_jira_connection_params(conf_file_name):
    if os.path.exists(conf_file_name):
        parser = ConfigParser()
        parser.read(conf_file_name, encoding="utf-8")
        params_dict = {}
        for params in parser.items("DEFAULT"):
            params_dict[params[0]] = params[1]
        return params_dict
    click.echo("Jira config file is required.")
    raise click.Abort()


class JiraConnector:
    def __init__(self, cfg_file):
        self.cfg_file = cfg_file
        config_dict = get_jira_connection_params(conf_file_name=self.cfg_file)
        self.token = config_dict["token"]
        self.server = config_dict["url"]
        if not (self.token and self.server):
            raise ValueError("Jira config file must contain token and server information.")

        self.resolved_statuses = config_dict.get('resolved_statuses')
        self.project_ids = config_dict.get('project_ids')
        self.jira = None
        self.authenticate_to_jira_server()

    def authenticate_to_jira_server(self):
        try:
            self.jira = JIRA(token_auth=self.token, options={"server": self.server})
            LOGGER.info("Connected to Jira")
        except JIRAError as e:
            LOGGER.error("Failed to connect to Jira: %s", e)
            raise

    def get_issue_metadata(self, jira_id, max_retry=3):
        retries = 0
        while True:
            try:
                return self.jira.issue(id=jira_id, fields="status, issuetype, fixVersions").fields
            except JIRAError as e:
                # Check for inactivity error (adjust based on your library)
                if "401 Unauthorized" in str(e) or "Session timed out" in str(e):
                    retries += 1
                    LOGGER.warning("Failed to get issue due to inactivity, retrying (%d/%d)", retries, max_retry)
                    if retries < max_retry:
                        self.authenticate_to_jira_server()  # Attempt reconnection
                    else:
                        raise  # Re-raise the error after exceeding retries
                else:
                    raise


def get_all_jiras_from_file(file_content):
    """
    Try to find all jira_utils tickets in the file.
    Looking for the following patterns:
    - jira_id=CNV-12345  # call in is_jira_open
    - https://issues.redhat.com/browse/CNV-12345  # when jira_utils is in a link in comments
    - pytest.mark.jira_utils(CNV-12345)  # when jira_utils is in a marker

    Args:
        file_content (str): The content of the file.

    Returns:
        list: A list of jira_utils tickets.
    """
    issue_pattern = r"([A-Z]+-[0-9]+)"
    _pytest_jira_marker_bugs = re.findall(
        rf"pytest.mark.jira.*?{issue_pattern}.*", file_content, re.DOTALL
    )
    _is_jira_open = re.findall(rf"jira_id\s*=[\s*\"\']*{issue_pattern}.*", file_content)
    _jira_url_jiras = re.findall(
        rf"https://issues.redhat.com/browse/{issue_pattern}(?! <skip-jira_utils-check>)",
        file_content,
    )
    return list(set(_pytest_jira_marker_bugs + _is_jira_open + _jira_url_jiras))


def get_jiras_from_python_files():
    jira_found = {}
    for filename in all_python_files():
        with open(filename) as fd:
            if unique_jiras := get_all_jiras_from_file(file_content=fd.read()):
                jira_found[filename] = unique_jiras
                LOGGER.warning(f"File: {filename}, {unique_jiras}")
    return jira_found


def get_closed_jira_ids(jira_connector, jira_ids_dict):
    jira_errors = {}
    for file_name in jira_ids_dict:
        for jira_id in jira_ids_dict[file_name]:
            try:
                current_jira_status = jira_connector.get_issue_metadata(jira_id=jira_id).status.name.lower()
                if current_jira_status in jira_connector.resolved_statuses:
                    jira_errors.setdefault(file_name, []).append(
                        f"{jira_id} [{current_jira_status}]"
                    )
            except JIRAError as exp:
                jira_errors.setdefault(file_name, []).append(
                    f"{jira_id} [{exp.text}]"
                )
                continue
    return jira_errors


def get_jira_version_mismatch(jira_connector, jira_id_dict, jira_expected_versions=None):
    jira_mismatch = {}
    for file_name in jira_id_dict:
        unique_jira_ids = jira_id_dict[file_name]
        if jira_connector.project_ids:
            unique_jira_ids = [jira_id for jira_id in unique_jira_ids
                               if jira_id.startswith(tuple(jira_connector.project_ids))]
        for jira_id in unique_jira_ids:
            jira_issue = jira_connector.get_issue_metadata(jira_id=jira_id)
            fix_version = re.search(r"([\d.]+)", jira_issue.fixVersions[0].name) if jira_issue.fixVersions else None
            jira_target_release_version = fix_version.group(1) if fix_version else "vfuture"
            LOGGER.info(f"issue: {jira_id}, version: {jira_target_release_version}, {jira_expected_versions}")
            if not jira_target_release_version.startswith(tuple(jira_expected_versions)):
                jira_mismatch.setdefault(file_name, []).append(
                    f"{jira_id} [{jira_target_release_version}]"
                )

    return jira_mismatch