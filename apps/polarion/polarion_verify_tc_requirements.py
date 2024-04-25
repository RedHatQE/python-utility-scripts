import re
import logging
from simple_logger.logger import get_logger
import os
import click
from apps.polarion.polarion_utils import (
    git_diff_lines,
    validate_polarion_requirements,
)
from apps.utils import get_util_config

LOGGER = get_logger(name="polarion-verify-tc-requirements")


@click.command()
@click.option(
    "--config-file-path",
    help="Provide absolute path to the config file. Any CLI option(s) would override YAML file",
    type=click.Path(),
    default=os.path.expanduser("~/.config/python-utility-scripts/config.yaml"),
)
@click.option("--project-id", "-p", help="Provide the polarion project id")
@click.option("--verbosity", default=False, is_flag=True)
def has_verify(config_file_path: str, project_id: str, verbosity: bool) -> None:
    if verbosity:
        LOGGER.setLevel(logging.DEBUG)

    polarion_project_id = project_id or get_util_config(
        util_name="pyutils-polarion-verify-tc-requirements",
        config_file_path=config_file_path,
    ).get("project_id")

    if not polarion_project_id:
        LOGGER.error("Polarion project id must be passed via config file or command line")
        raise click.Abort()

    if added_ids := re.findall(
        rf"pytest.mark.polarion.*({polarion_project_id}-[0-9]+)",
        "\n".join(git_diff_lines().get("added", [])),
        re.MULTILINE | re.IGNORECASE,
    ):
        LOGGER.debug(f"Checking following ids: {added_ids}")
        if tests_with_missing_requirements := validate_polarion_requirements(
            polarion_test_ids=added_ids,
            polarion_project_id=polarion_project_id,
        ):
            click.echo(f"TestCases with missing requirement: {tests_with_missing_requirements}")
            raise click.Abort()


if __name__ == "__main__":
    has_verify()
