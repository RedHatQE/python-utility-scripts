from __future__ import annotations

import logging
import os
import sys

import click
from simple_logger.logger import get_logger

from apps.polarion.polarion_utils import find_polarion_ids, get_polarion_project_id, update_polarion_ids

LOGGER = get_logger(name=__name__)


def approve_tests(polarion_project_id: str, added_ids: list[str]) -> dict[str, list[str]]:
    LOGGER.debug(f"Following polarion ids were added: {added_ids}")
    return update_polarion_ids(
        polarion_ids=list(added_ids), project_id=polarion_project_id, is_automated=True, is_approved=True
    )


def remove_approved_tests(
    polarion_project_id: str,
    branch: str | None = None,
    previous_commit: str | None = None,
    current_commit: str | None = None,
    added_ids: list[str] | None = None,
) -> dict[str, list[str]]:
    removed_polarions = {}
    added_ids = added_ids or []
    if removed_ids := set(
        find_polarion_ids(
            polarion_project_id=polarion_project_id,
            string_to_match="removed",
            branch=branch,
            previous_commit=previous_commit,
            current_commit=current_commit,
        )
    ) - set(added_ids):
        LOGGER.info(f"Following polarion ids were removed: {removed_ids}")
        removed_polarions = update_polarion_ids(
            polarion_ids=list(removed_ids), project_id=polarion_project_id, is_automated=False
        )
        LOGGER.error(f"Following polarion ids marked not automated: {removed_polarions.get('updated')}")
    return removed_polarions


@click.command()
@click.option(
    "--config-file-path",
    help="Provide absolute path to the config file. Any CLI option(s) would override YAML file",
    type=click.Path(),
    default=os.path.expanduser("~/.config/python-utility-scripts/config.yaml"),
)
@click.option("--project-id", "-p", help="Provide the polarion project id")
@click.option("--previous-commit", "-p", help="Provide previous-commit to compare against", required=True)
@click.option("--current-commit", "-c", help="Provide current-commit to compare with", required=True)
@click.option("--verbose", default=False, is_flag=True)
def polarion_approve_automate(
    config_file_path: str, project_id: str, previous_commit: str, current_commit: str, verbose: bool
) -> None:
    if verbose:
        LOGGER.setLevel(logging.DEBUG)
        # since the utilities are in apps.polarion.polarion_utils, we need to change log level
        # for apps.polarion.polarion_utils as well
        logging.getLogger("apps.polarion.polarion_utils").setLevel(logging.DEBUG)

    polarion_project_id = project_id or get_polarion_project_id(
        config_file_path=config_file_path, util_name="pyutils-polarion-set-automated"
    )
    added_polarions = {}
    if added_ids := find_polarion_ids(
        polarion_project_id=polarion_project_id,
        string_to_match="added",
        branch=None,
        previous_commit=previous_commit,
        current_commit=current_commit,
    ):
        added_polarions = approve_tests(polarion_project_id=polarion_project_id, added_ids=added_ids)
        LOGGER.debug(f"Following polarion ids were marked automated and approved: {added_polarions.get('updated')}")

    removed_polarions = remove_approved_tests(
        polarion_project_id=polarion_project_id,
        added_ids=added_ids,
        previous_commit=previous_commit,
        current_commit=current_commit,
    )
    if removed_polarions.get("failed") or added_polarions.get("failed"):
        error = "Following polarion ids updates failed."
        if removed_polarions.get("failed"):
            error += f" Removed ids: {removed_polarions.get('failed')}."
        if added_polarions.get("failed"):
            error += f" Added ids:: {added_polarions.get('failed')}."
        LOGGER.error(error)
        sys.exit(1)


if __name__ == "__main__":
    polarion_approve_automate()
