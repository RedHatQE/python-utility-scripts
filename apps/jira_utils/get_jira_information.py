import logging
import os
import re

import click
from jira import JIRAError

from simple_logger.logger import get_logger

from apps.jira_utils.exceptions import JiraValidationError, JiraInvalidConfigFileError
from apps.jira_utils.jira_utils import get_jiras_from_python_files, JiraConnector
from apps.utils import ListParamType, get_util_config
from typing import Any, Dict

DEFAULT_RESOLVED_STATUS = ["verified", "release pending", "closed", "resolved"]

LOGGER = get_logger(name=__name__)


@click.command()
@click.option(
    "--jira-cfg-file",
    help="Provide absolute path to the jira_utils config file. ",
    type=click.Path(),
    default=os.path.expanduser("~/.config/python-utility-scripts/jira_utils/config.cfg"),
)
@click.option(
    "--jira-target-versions",
    help="Provide comma separated list of Jira target version, for version validation against a repo branch.",
    type=ListParamType(),
    required=False,
)
@click.option("--verbose", default=False, is_flag=True)
def get_jira_mismatch(jira_cfg_file: Any, jira_target_versions: Any, verbose: bool) -> Any:
    if verbose:
        LOGGER.setLevel(logging.DEBUG)
    else:
        logging.disable(logging.CRITICAL)
    config_dict = get_util_config(util_name="pyutils-jira", config_file_path=jira_cfg_file)
    if not (config_dict.get("url") and config_dict.get("token")):
        raise JiraInvalidConfigFileError("Jira config file must contain valid url and token.")
    jira_connector = JiraConnector(token=config_dict["token"], url=config_dict["url"])
    jira_error: Dict[str, Dict[str, Any]] = {"status_mismatch": {}, "version_mismatch": {}, "connection_error": {}}
    resolved_status = config_dict.get("resolved_statuses", DEFAULT_RESOLVED_STATUS) 
    jira_target_versions = jira_target_versions or config_dict.get("jira_target_versions", [])
    skip_project_ids = config_dict.get("skip_project_ids", [])
    for file_name in (jira_id_dict := get_jiras_from_python_files()):
        for jira_id in jira_id_dict[file_name]:
            try:
                # check closed status:
                jira_issue_metadata = jira_connector.get_issue(jira_id=jira_id).fields
                current_jira_status = jira_issue_metadata.status.name.lower()
                if current_jira_status in resolved_status:
                    jira_error["status_mismatch"].setdefault(file_name, []).append(
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
                    current_target_version = fix_version.group(1) if fix_version else "vfuture"
                    if not any([current_target_version == version for version in jira_target_versions]):
                        jira_error["version_mismatch"].setdefault(file_name, []).append(
                            f"{jira_id}: target version: {current_target_version}]"
                        )

            except JIRAError as exp:
                jira_error["connection_error"].setdefault(file_name, []).append(
                    f"{jira_id}: status code: {exp.status_code}, details: {exp.text}]."
                )
    jira_error = {key: value for key, value in jira_error.items() if value}
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
