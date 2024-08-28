import logging
import os
import re

import click
from jira import JIRAError, JIRA

from simple_logger.logger import get_logger

from apps.jira_utils.jira_utils import (
    get_jiras_from_python_files,
    JiraInvalidConfigFileError,
    JiraValidationError,
    get_issue,
)

from apps.utils import ListParamType, get_util_config
from typing import Any, Dict, List

DEFAULT_RESOLVED_STATUS = ["verified", "release pending", "closed", "resolved"]

LOGGER = get_logger(name=__name__)


@click.command()
@click.option(
    "--jira-cfg-file",
    help="Provide absolute path to the jira_utils config file. ",
    type=click.Path(exists=True),
    default=os.path.expanduser("~/.config/python-utility-scripts/jira_utils/config.cfg"),
)
@click.option(
    "--jira-target-versions",
    help="Provide comma separated list of Jira target version, for version validation against a repo branch.",
    type=ListParamType(),
    required=False,
)
@click.option(
    "--jira-issue-pattern",
    help="Provide the regex for Jira ids",
    type=click.STRING,
    show_default=True,
    default="([A-Z]+-[0-9]+)",
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
    jira_cfg_file: str,
    jira_target_versions: List[str],
    jira_issue_pattern: str,
    version_string_not_targeted_jiras: str,
    verbose: bool,
) -> None:
    if verbose:
        LOGGER.setLevel(logging.DEBUG)
    else:
        logging.disable(logging.CRITICAL)
    config_dict = get_util_config(util_name="pyutils-jira", config_file_path=jira_cfg_file)
    jira_url = config_dict.get("url")
    jira_token = config_dict.get("token")
    jira_issue_pattern = config_dict.get("issue_pattern", jira_issue_pattern)
    if not (jira_url and jira_token):
        raise JiraInvalidConfigFileError("Jira config file must contain valid url or token.")
    jira_connection = JIRA(token_auth=jira_token, options={"server": jira_url})
    jira_error: Dict[str, Dict[str, Any]] = {}
    resolved_status = config_dict.get("resolved_statuses", DEFAULT_RESOLVED_STATUS)
    jira_target_versions = jira_target_versions or config_dict.get("jira_target_versions", [])
    skip_project_ids = config_dict.get("skip_project_ids", [])
    not_targeted_version_str = config_dict.get("version_string_not_targeted_jiras", version_string_not_targeted_jiras)
    for file_name in (jira_id_dict := get_jiras_from_python_files(issue_pattern=jira_issue_pattern, jira_url=jira_url)):
        for jira_id in jira_id_dict[file_name]:
            try:
                # check resolved status:
                jira_issue_metadata = get_issue(jira=jira_connection, jira_id=jira_id).fields
                current_jira_status = jira_issue_metadata.status.name.lower()
                if current_jira_status in resolved_status:
                    jira_error.setdefault("status_mismatch", {}).setdefault(file_name, []).append(
                        f"{jira_id}: current status: {current_jira_status}"
                    )
                # validate correct target version if provided:
                if jira_target_versions:
                    if skip_project_ids and jira_id.startswith(tuple(skip_project_ids)):
                        continue
                    fix_version = (
                        re.search(r"([\d.]+)", jira_issue_metadata.fixVersions[0].name)
                        if (jira_issue_metadata.fixVersions)
                        else None
                    )
                    current_target_version = fix_version.group(1) if fix_version else not_targeted_version_str
                    if not any([current_target_version == version for version in jira_target_versions]):
                        jira_error.setdefault("version_mismatch", {}).setdefault(file_name, []).append(
                            f"{jira_id}: target version: {current_target_version}]"
                        )

            except JIRAError as exp:
                jira_error.setdefault("connection_error", {}).setdefault(file_name, []).append(
                    f"{jira_id}: status code: {exp.status_code}, details: {exp.text}]."
                )
    # https://issues.redhat.com/browse/RHEL-40899
    if jira_error.values():
        error = "Following Jira ids failed jira check:\n"
        if jira_error.get("status_mismatch"):
            error += f" Jira ids in resolved state: {jira_error['status_mismatch']}."
        if jira_error.get("version_mismatch"):
            error += (
                f" Jira expected versions: {jira_target_versions}, "
                f"current versions: {jira_error['version_mismatch']}."
            )
        if jira_error.get("connection_error"):
            error += f" Jira ids with connection error: {jira_error['connection_error']}."
        LOGGER.error(error)
        raise JiraValidationError(error)
    LOGGER.info("Successfully completed Jira validations")


if __name__ == "__main__":
    get_jira_mismatch()
