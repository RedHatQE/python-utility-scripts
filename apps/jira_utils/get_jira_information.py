import logging
import os
import concurrent.futures

import click
from jira import JIRA

from simple_logger.logger import get_logger

from apps.jira_utils.jira_utils import (
    get_jiras_from_python_files,
    JiraValidationError,
    get_jira_information,
    process_jira_command_line_config_file,
)

from apps.utils import ListParamType
from typing import Dict, List


LOGGER = get_logger(name=__name__)


@click.command()
@click.option(
    "--cfg-file",
    help="Provide absolute path to the jira_utils config file. ",
    type=click.Path(exists=True),
)
@click.option(
    "--target-versions",
    help="Provide comma separated list of Jira target version, for version validation against a repo branch.",
    type=ListParamType(),
    required=False,
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
    cfg_file: str,
    target_versions: List[str],
    url: str,
    token: str,
    skip_projects: List[str],
    resolved_statuses: List[str],
    issue_pattern: str,
    version_string_not_targeted_jiras: str,
    verbose: bool,
) -> None:
    if verbose:
        LOGGER.setLevel(logging.DEBUG)
    else:
        logging.disable(logging.CRITICAL)
    # Process all the arguments passed from command line or config file or environment variable
    jira_config_dict = process_jira_command_line_config_file(
        cfg_file=cfg_file,
        url=url,
        token=token,
        resolved_statuses=resolved_statuses,
        issue_pattern=issue_pattern,
        skip_projects=skip_projects,
        version_string_not_targeted_jiras=version_string_not_targeted_jiras,
        target_versions=target_versions,
    )

    jira_obj = JIRA(token_auth=jira_config_dict["token"], options={"server": jira_config_dict["url"]})
    jira_error: Dict[str, str] = {}

    if jira_id_dict := get_jiras_from_python_files(
        issue_pattern=jira_config_dict["issue_pattern"], jira_url=jira_config_dict["url"]
    ):
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_to_jiras = {
                executor.submit(
                    get_jira_information,
                    jira_object=jira_obj,
                    jira_id=jira_id,
                    skip_project_ids=jira_config_dict["skip_project_ids"],
                    resolved_status=jira_config_dict["resolved_status"],
                    jira_target_versions=jira_config_dict["target_versions"],
                    target_version_str=jira_config_dict["not_targeted_version_str"],
                ): jira_id
                for jira_id in set.union(*jira_id_dict.values())
            }

            for future in concurrent.futures.as_completed(future_to_jiras):
                jira_id = future_to_jiras[future]
                file_names = [file_name for file_name, jiras in jira_id_dict.items() if jira_id in jiras]
                jira_error_string = future.result()
                if jira_error_string:
                    jira_error[jira_id] = f" {file_names}: {jira_error_string}"

    if jira_error:
        error = f"Following Jira ids failed jira check: {jira_error}\n"
        LOGGER.error(error)
        raise JiraValidationError(error)
    LOGGER.info("Successfully completed Jira validations")


if __name__ == "__main__":
    get_jira_mismatch()
