import shlex
import subprocess

from pyhelper_utils.shell import run_command

BASE_COMMAND = "uv run apps/polarion/polarion_set_automated.py --verbose"


def test_missing_required_params_set_automated():
    rc, _, err = run_command(
        command=shlex.split(BASE_COMMAND),
        verify_stderr=False,
        check=False,
        capture_output=False,
        stderr=subprocess.PIPE,
    )
    assert "Missing option" in err
    assert not rc


def test_missing_project_id_set_automated():
    rc, _, err = run_command(
        command=shlex.split(f"{BASE_COMMAND} --previous-commit commit1 --current-commit commit2"),
        verify_stderr=False,
        check=False,
        capture_output=False,
        stderr=subprocess.PIPE,
    )
    assert "Polarion project id must be passed via config file or command line" in err
    assert not rc
