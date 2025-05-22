import shlex
import subprocess

from pyhelper_utils.shell import run_command

BASE_COMMAND = "uv run python apps/polarion/polarion_verify_tc_requirements.py"


def test_missing_project_id():
    rc, _, err = run_command(
        command=shlex.split(f"{BASE_COMMAND} --verbose"),
        verify_stderr=False,
        check=False,
        capture_output=False,
        stderr=subprocess.PIPE,
    )
    assert "Polarion project id must be passed via config file or command line" in err
    assert not rc


def test_project_id_from_config():
    command = f"{BASE_COMMAND} --config-file-path=config.example.yaml"
    rc, _, err = run_command(
        command=shlex.split(command),
        verify_stderr=False,
        check=False,
        capture_output=False,
        stderr=subprocess.PIPE,
    )
    assert not err
    assert rc
