import logging
import os
import concurrent.futures

import click
from jira import JIRA

from simple_logger.logger import get_logger

from apps.jira_utils.jira_utils import (
    get_jiras_from_python_files,
    JiraInvalidConfigFileError,
    JiraValidationError,
    get_jira_information,
)

from apps.utils import ListParamType, get_util_config
from typing import Dict, List


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
    "--jira-skip-projects",
    help="Provide comma separated list of Jira Project keys, against which version check should be skipped.",
    type=ListParamType(),
)
@click.option("--jira-url", help="Provide the Jira server URL", type=click.STRING, default=os.getenv("JIRA_SERVER_URL"))
@click.option("--jira-token", help="Provide the Jira token.", type=click.STRING, default=os.getenv("JIRA_TOKEN"))
@click.option(
    "--jira-issue-pattern",
    help="Provide the regex for Jira ids",
    type=click.STRING,
    show_default=True,
    default="([A-Z]+-[0-9]+)",
)
@click.option(
    "--jira-resolved-statuses",
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
    jira_cfg_file: str,
    jira_target_versions: List[str],
    jira_url: str,
    jira_token: str,
    jira_skip_projects: List[str],
    jira_resolved_statuses: List[str],
    jira_issue_pattern: str,
    version_string_not_targeted_jiras: str,
    verbose: bool,
) -> None:
    if verbose:
        LOGGER.setLevel(logging.DEBUG)
    else:
        logging.disable(logging.CRITICAL)
    jira_mismatch: Dict[str, str] = {}
    # Process all the arguments passed from command line or config file or environment variable
    config_dict = get_util_config(util_name="pyutils-jira", config_file_path=jira_cfg_file)
    jira_url = jira_url or config_dict.get("url", "")
    jira_token = jira_token or config_dict.get("token", "")
    if not (jira_url and jira_token):
        raise JiraInvalidConfigFileError("Jira config file must contain valid url or token.")

    jira_issue_pattern = jira_issue_pattern or config_dict.get("issue_pattern", "")
    resolved_status = jira_resolved_statuses or config_dict.get("resolved_statuses", [])
    not_targeted_version_str = config_dict.get("version_string_not_targeted_jiras", version_string_not_targeted_jiras)
    jira_target_versions = jira_target_versions or config_dict.get("jira_target_versions", [])
    skip_project_ids = jira_skip_projects or config_dict.get("skip_project_ids", [])

    jira_obj = JIRA(token_auth=jira_token, options={"server": jira_url})
    jira_error: Dict[str, str] = {}

    if jira_id_dict := get_jiras_from_python_files(issue_pattern=jira_issue_pattern, jira_url=jira_url):
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_jiras = {
                executor.submit(
                    get_jira_information,
                    jira_object=jira_obj,
                    jira_id=jira_id,
                    skip_project_ids=skip_project_ids,
                    resolved_status=resolved_status,
                    jira_target_versions=jira_target_versions,
                    target_version_str=not_targeted_version_str,
                ): jira_id
                for jira_id in set.union(*jira_id_dict.values())
            }

            for future in concurrent.futures.as_completed(future_to_jiras):
                jira_id = future_to_jiras[future]
                jira_error_string = future.result()
                if jira_error_string:
                    jira_error[jira_id] = jira_error_string

        for file_name, jiras in jira_id_dict.items():
            for jira_id in jiras:
                if jira_error.get(jira_id):
                    jira_mismatch[file_name] = jira_error[jira_id]

    if jira_mismatch:
        error = f"Following Jira ids failed jira check: {jira_mismatch}\n"
        LOGGER.error(error)
        raise JiraValidationError(error)
    LOGGER.info("Successfully completed Jira validations")


if __name__ == "__main__":
    get_jira_mismatch()
