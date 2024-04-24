import logging
import os
from simple_logger.logger import get_logger

import click

from apps.polarion.polarion_utils import get_polarion_project_id, find_polarion_ids, update_polarion_ids

LOGGER = get_logger(name=__name__)


@click.command()
@click.option(
    "--config-file-path",
    help="Provide absolute path to the config file. Any CLI option(s) would override YAML file",
    type=click.Path(),
    default=os.path.expanduser("~/.config/python-utility-scripts/config.yaml"),
)
@click.option("--project-id", "-p", help="Provide the polarion project id")
@click.option("--verbosity", default=False, is_flag=True)
def polarion_approve_automate(config_file_path, project_id, verbosity):
    if not verbose:
        logging.disable(logging.CRITICAL)
    removed_polarions = {}
    added_polarions = {}
    polarion_project_id = get_polarion_project_id(
        project_id=project_id, config_file_path=config_file_path, util_name="pyutils-polarion-set-automated"
    )
    added_ids = find_polarion_ids(polarion_project_id=polarion_project_id, string_to_match="added")
    LOGGER.debug(f"Following polarion ids were added: {added_ids}")
    if removed_ids := set(find_polarion_ids(polarion_project_id=polarion_project_id, string_to_match="removed")) - set(
        added_ids
    ):
        LOGGER.debug(f"Following polarion ids were removed: {removed_ids}")
        removed_polarions = update_polarion_ids(
            polarion_ids=removed_ids, project_id=polarion_project_id, is_automated=False
        )
        LOGGER.error(f"Following polarion ids marked not automated: {removed_polarions.get('updated')}")

    if added_polarions := update_polarion_ids(
        polarion_ids=added_ids, project_id=polarion_project_id, is_automated=True, is_approved=True
    ):
        LOGGER.debug(f"Following polarion ids were marked automated and approved: {added_polarions.get('updated')}")
    if removed_polarions.get("failed") or added_polarions.get("failed"):
        LOGGER.error(
            f"Following polarion ids updates failed. Removed ids: {removed_polarions.get('failed')}"
            f"Added ids:: {added_polarions.get('failed')}"
        )
        raise click.Abort()


if __name__ == "__main__":
    polarion_approve_automate()
