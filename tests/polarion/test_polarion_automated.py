import shlex
import subprocess

from pyhelper_utils.shell import run_command

BASE_COMMAND = "poetry run python apps/polarion/polarion_set_automated.py --verbose"


def test_missing_project_id_set_automated():
    rc, _, err = run_command(
        command=shlex.split(BASE_COMMAND),
        verify_stderr=False,
        check=False,
        capture_output=False,
        stderr=subprocess.PIPE,
    )
    assert "Previous and current commit" in err
    assert not rc
