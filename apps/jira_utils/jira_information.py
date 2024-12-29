from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import sys
from functools import lru_cache
from typing import Any

import click
from jira import JIRA, Issue, JIRAError
from simple_logger.logger import get_logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from apps.utils import ListParamType, all_python_files, get_util_config

LOGGER = get_logger(name=__name__)


@retry(retry=retry_if_exception_type(JIRAError), stop=stop_after_attempt(3), wait=wait_fixed(2))
@lru_cache
def get_issue(
    jira: JIRA,
    jira_id: str,
) -> Issue:
    LOGGER.debug(f"Retry staistics for {jira_id}: {get_issue.statistics}")
    return jira.issue(id=jira_id, fields="status, issuetype, fixVersions")


def get_jira_ids_from_file_content(file_content: str, issue_pattern: str, jira_url: str) -> set[str]:
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
        set: A set of jira tickets.
    """
    _pytest_jira_marker_bugs = re.findall(rf"pytest.mark.jira.*?{issue_pattern}.*", file_content, re.DOTALL)
    _jira_id_arguments = re.findall(rf"jira_id\s*=[\s*\"\']*{issue_pattern}.*", file_content)
    _jira_url_jiras = re.findall(
        rf"{jira_url}/browse/{issue_pattern}",
        file_content,
    )

    return set(_pytest_jira_marker_bugs + _jira_id_arguments + _jira_url_jiras)


def get_jiras_from_python_files(issue_pattern: str, jira_url: str) -> dict[str, set[str]]:
    """
    Get all python files from the current directory and get list of jira ids from each of them

    Args:
        issue_pattern (str): regex pattern for jira ids
        jira_url (str): jira url that could be used to look for possible presence of jira references in a file

    Returns:
        Dict: A dict of filenames and associated jira tickets.

    Note: any line containing <skip-jira_utils-check> would be not be checked for presence of a jira id
    """
    jira_found: dict[str, set[str]] = {}
    for filename in all_python_files():
        file_content = []
        with open(filename) as fd:
            file_content = fd.readlines()
        # if <skip-jira-utils-check> appears in a line, exclude that line from jira check
        if unique_jiras := get_jira_ids_from_file_content(
            file_content="\n".join([line for line in file_content if "<skip-jira-utils-check>" not in line]),
            issue_pattern=issue_pattern,
            jira_url=jira_url,
        ):
            jira_found[filename] = unique_jiras

    if jira_found:
        _jira_found = "\n\t".join([f"{key}: {val}" for key, val in jira_found.items()])
        LOGGER.debug(f"Following jiras are found: \n\t{_jira_found}")

    return jira_found


def get_jira_information(
    jira_object: JIRA,
    jira_id: str,
    skip_project_ids: list[str],
    resolved_status: list[str],
    jira_target_versions: list[str],
    target_version_str: str,
    file_name: str,
) -> tuple[str, str]:
    jira_error_string = ""
    try:
        # check resolved status:
        jira_issue_metadata = get_issue(jira=jira_object, jira_id=jira_id).fields
        current_jira_status = jira_issue_metadata.status.name.lower()
        LOGGER.debug(f"Jira: {jira_id}, status: {current_jira_status}")
        if current_jira_status in resolved_status:
            jira_error_string += f"{jira_id} current status: {current_jira_status} is resolved."

        # validate correct target version if provided:
        if jira_target_versions:
            if skip_project_ids and jira_id.startswith(tuple(skip_project_ids)):
                return file_name, jira_error_string

            fix_version = (
                re.search(r"([\d.]+)", jira_issue_metadata.fixVersions[0].name)
                if (jira_issue_metadata.fixVersions)
                else None
            )
            current_target_version = fix_version.group(1) if fix_version else target_version_str
            if not any([current_target_version == version for version in jira_target_versions]):
                jira_error_string += (
                    f"{jira_id} target version: {current_target_version}, does not match expected "
                    f"version {jira_target_versions}."
                )
    except JIRAError as exp:
        jira_error_string += f"{jira_id} JiraError status code: {exp.status_code}, details: {exp.text}]."

    return file_name, jira_error_string


def process_jira_command_line_config_file(
    config_file_path: str,
    url: str,
    token: str,
    issue_pattern: str,
    resolved_statuses: list[str],
    version_string_not_targeted_jiras: str,
    target_versions: list[str],
    skip_projects: list[str],
) -> dict[str, Any]:
    # Process all the arguments passed from command line or config file or environment variable
    config_dict = get_util_config(util_name="pyutils-jira", config_file_path=config_file_path)
    url = url or config_dict.get("url", "")
    token = token or config_dict.get("token", "")
    if not (url and token):
        LOGGER.error("Jira url and token are required.")
        sys.exit(1)

    return {
        "url": url,
        "token": token,
        "issue_pattern": issue_pattern or config_dict.get("issue_pattern", ""),
        "resolved_status": resolved_statuses or config_dict.get("resolved_statuses", []),
        "not_targeted_version_str": config_dict.get(
            "version_string_not_targeted_jiras", version_string_not_targeted_jiras
        ),
        "target_versions": target_versions or config_dict.get("target_versions", []),
        "skip_project_ids": skip_projects or config_dict.get("skip_project_ids", []),
    }


@click.command()
@click.option(
    "--config-file-path",
    help="Provide absolute path to the jira_utils config file.",
    type=click.Path(exists=True),
)
@click.option(
    "--target-versions",
    help="Provide comma separated list of Jira target version, for version validation against a repo branch.",
    type=ListParamType(),
)
@click.option(
    "--skip-projects",
    help="Provide comma separated list of Jira Project keys, against which version check should be skipped.",
    type=ListParamType(),
)
@click.option("--url", help="Provide the Jira server URL", type=click.STRING, default=os.getenv("JIRA_SERVER_URL"))
@click.option("--token", help="Provide the Jira token.", type=click.STRING, default=os.getenv("JIRA_TOKEN"))
@click.option(
    "--issue-pattern",
    help="Provide the regex for Jira ids",
    type=click.STRING,
    show_default=True,
    default="([A-Z]+-[0-9]+)",
)
@click.option(
    "--resolved-statuses",
    help="Comma separated list of Jira resolved statuses",
    type=ListParamType(),
    show_default=True,
    default="verified, release pending, closed, resolved",
)
@click.option(
    "--version-string-not-targeted-jiras",
    help="Provide possible version strings for not yet targeted jiras",
    type=click.STRING,
    show_default=True,
    default="vfuture",
)
@click.option("--verbose", default=False, is_flag=True)
def get_jira_mismatch(
    config_file_path: str,
    target_versions: list[str],
    url: str,
    token: str,
    skip_projects: list[str],
    resolved_statuses: list[str],
    issue_pattern: str,
    version_string_not_targeted_jiras: str,
    verbose: bool,
) -> None:
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not (config_file_path or (token and url)):
        LOGGER.error("Config file or token and url are required.")
        sys.exit(1)

    # Process all the arguments passed from command line or config file or environment variable
    jira_config_dict = process_jira_command_line_config_file(
        config_file_path=config_file_path,
        url=url,
        token=token,
        resolved_statuses=resolved_statuses,
        issue_pattern=issue_pattern,
        skip_projects=skip_projects,
        version_string_not_targeted_jiras=version_string_not_targeted_jiras,
        target_versions=target_versions,
    )

    jira_obj = JIRA(token_auth=jira_config_dict["token"], options={"server": jira_config_dict["url"]})
    jira_error: dict[str, str] = {}

    if jira_id_dict := get_jiras_from_python_files(
        issue_pattern=jira_config_dict["issue_pattern"], jira_url=jira_config_dict["url"]
    ):
        with concurrent.futures.ThreadPoolExecutor() as executor:
            for file_name, ids in jira_id_dict.items():
                for jira_id in ids:
                    future_to_jiras = {
                        executor.submit(
                            get_jira_information,
                            jira_object=jira_obj,
                            jira_id=jira_id,
                            skip_project_ids=jira_config_dict["skip_project_ids"],
                            resolved_status=jira_config_dict["resolved_status"],
                            jira_target_versions=jira_config_dict["target_versions"],
                            target_version_str=jira_config_dict["not_targeted_version_str"],
                            file_name=file_name,
                        )
                    }

                    for future in concurrent.futures.as_completed(future_to_jiras):
                        file_name, jira_error_string = future.result()
                        if jira_error_string:
                            jira_error[file_name] = jira_error_string

    if jira_error:
        _jira_error = "\n\t".join([f"{key}: {val}" for key, val in jira_error.items()])
        LOGGER.error(f"Following Jira ids failed jira version/statuscheck: \n\t{_jira_error}\n")
        sys.exit(1)


if __name__ == "__main__":
    get_jira_mismatch()
