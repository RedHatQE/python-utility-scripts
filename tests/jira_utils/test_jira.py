import shlex
import subprocess

from pyhelper_utils.shell import run_command

from simple_logger.logger import get_logger

LOGGER = get_logger(name=__name__)
BASE_COMMAND = "poetry run python apps/jira_utils/get_jira_information.py --verbose"


def test_jira_missing_config_file():
    rc, _, err = run_command(
        command=shlex.split(f"{BASE_COMMAND} --jira-cfg-file invalid-jira.cfg"),
        verify_stderr=False,
        check=False,
        capture_output=False,
        stderr=subprocess.PIPE,
    )
    assert "Jira config file must contain valid url and token" in err
    assert not rc
