import shlex
import subprocess
from pyhelper_utils.shell import run_command

BASE_COMMAND = "poetry run python apps/polarion/polarion_set_automated.py"


def test_missing_project_id_set_automated():
    rc, _, err = run_command(
        command=shlex.split(BASE_COMMAND),
        verify_stderr=False,
        check=False,
        capture_output=False,
        stderr=subprocess.PIPE,
    )
    assert "Polarion project id must be passed via config file or command line" in err
    assert not rc
