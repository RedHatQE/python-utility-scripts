import os
import click

from simple_logger.logger import get_logger

from apps.jira_utils.utils import get_jiras_from_python_files, get_closed_jira_ids, get_jira_connection_params, \
    get_jira_version_mismatch, JiraConnector
from apps.utils import ListParamType

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
    help="Provide comma separated list of Jira target version for the bugs.",
    type=ListParamType(),
    required=True
)
def get_closed_jiras(jira_cfg_file, jira_target_versions):
    jira_connector = JiraConnector(cfg_file=jira_cfg_file)
    jira_id_dict = get_jiras_from_python_files()
    if jira_errors:= get_closed_jira_ids(jira_connector=jira_connector,
                                        jira_ids_dict=jira_id_dict):
        click.echo(f"Following jiras are not open or could not be accessed: {jira_errors}")
        raise click.Abort()
    if version_mismatch := get_jira_version_mismatch(jira_connector=jira_connector, jira_id_dict=jira_id_dict,
                                                     jira_expected_versions=jira_target_versions):
        click.echo(f"Following jiras are not matching expected version{jira_target_versions}: {version_mismatch}")
        raise click.Abort()


if __name__ == "__main__":
    get_closed_jiras()
