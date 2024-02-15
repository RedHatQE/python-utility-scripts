from simple_logger.logger import get_logger
import os
import click

from apps.polarion.polarion_utils import (
    get_polarion_ids_from_diff,
    git_diff_lines,
    validate_polarion_requirements,
)
from apps.utils import get_util_config

LOGGER = get_logger(name=__name__)


@click.command()
@click.option(
    "--config-file-path",
    help="Provide absolute path to the config file. Any CLI option(s) would override YAML file",
    type=click.Path(),
    default=os.path.expanduser("~/.config/python-utility-scripts/config.yaml"),
)
@click.option("--project-id", "-p", help="Provide the polarion project id")
def has_verify(config_file_path, project_id):
    tests_with_missing_requirements = []
    polarion_project_id = project_id or get_util_config(
        util_name="pyappsutils-polarion_tc_requirements", config_file_path=config_file_path
    ).get("project_id")
    if not polarion_project_id:
        click.echo("Polarion project id must be passed via config file or command line")
        raise click.Abort()
    added_ids = get_polarion_ids_from_diff(diff=git_diff_lines(), polarion_project_id=polarion_project_id)
    LOGGER.info(f"Checking following ids: {added_ids}")
    if added_ids:
        tests_with_missing_requirements = validate_polarion_requirements(
            polarion_test_ids=added_ids, polarion_project_id=polarion_project_id
        )
    if tests_with_missing_requirements:
        click.echo(f"TestCases with missing requirement: {tests_with_missing_requirements}")
        raise click.Abort()


if __name__ == "__main__":
    has_verify()
