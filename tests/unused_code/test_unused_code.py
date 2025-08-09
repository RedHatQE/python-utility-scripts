import textwrap

import pytest
from simple_logger.logger import get_logger

from apps.unused_code.unused_code import _git_grep, get_unused_functions, process_file
from tests.utils import get_cli_runner

LOGGER = get_logger(name=__name__)


def test_unused_code():
    result = get_cli_runner().invoke(get_unused_functions)
    LOGGER.info(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 1
    assert "Is not used anywhere in the code" in result.output


def test_unused_code_file_list():
    result = get_cli_runner().invoke(get_unused_functions, '--exclude-files "unused_code_file_for_test.py"')
    LOGGER.info(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 0
    assert "Is not used anywhere in the code" not in result.output


def test_unused_code_function_list_exclude_all():
    result = get_cli_runner().invoke(get_unused_functions, '--exclude-function-prefixes "unused_code_"')
    LOGGER.info(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 0
    assert "Is not used anywhere in the code" not in result.output


def test_unused_code_function_list_exclude():
    result = get_cli_runner().invoke(get_unused_functions, '--exclude-function-prefixes "unused_code_check_function"')
    LOGGER.info(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 1
    assert "Is not used anywhere in the code" in result.output


def test_unused_code_check_skip_with_comment():
    result = get_cli_runner().invoke(get_unused_functions)
    LOGGER.info(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 1
    assert "skip_with_comment" not in result.output


def test_unused_code_handles_pytest_fixture_parameter_usage(mocker, tmp_path):
    # Create a temporary python file with a pytest fixture and a test using it as a parameter
    py_file = tmp_path / "tmp_fixture_usage.py"
    py_file.write_text(
        textwrap.dedent(
            """
import pytest

@pytest.fixture
def sample_fixture():
    return 1

def test_something(sample_fixture):
    assert sample_fixture == 1
"""
        )
    )

    # Mock grep to simulate: no direct call matches, but parameter usage is detected
    def _mock_grep(pattern: str):
        if pattern.endswith("(.*)"):
            return []  # simulate no function call usage
        # simulate finding a function definition that includes the fixture as a parameter
        return [f"{py_file}:1:def test_something(sample_fixture): pass"]

    mocker.patch("apps.unused_code.unused_code._git_grep", side_effect=_mock_grep)

    result = process_file(py_file=str(py_file), func_ignore_prefix=[], file_ignore_list=[])
    assert result == ""  # should not report fixture as unused


def test_unused_code_handles_no_matches_without_crashing(mocker, tmp_path):
    # Create a temporary python file with a simple function
    py_file = tmp_path / "tmp_simple.py"
    py_file.write_text(
        textwrap.dedent(
            """
def my_helper():
    return 42
"""
        )
    )

    # Mock grep to simulate no matches anywhere
    mocker.patch("apps.unused_code.unused_code._git_grep", return_value=[])

    # Should return an "unused" message and not crash
    result = process_file(py_file=str(py_file), func_ignore_prefix=[], file_ignore_list=[])
    assert "Is not used anywhere in the code." in result


def test_unused_code_skips_autouse_fixture(tmp_path):
    py_file = tmp_path / "tmp_autouse_fixture.py"
    py_file.write_text(
        textwrap.dedent(
            """
import pytest

@pytest.fixture(autouse=True)
def auto_fixture():
    return 1
"""
        )
    )

    result = process_file(py_file=str(py_file), func_ignore_prefix=[], file_ignore_list=[])
    # should skip autouse fixture and not report unused
    assert result == ""


def test_git_grep_raises_on_unexpected_error(mocker):
    class FakeCompleted:
        def __init__(self):
            self.returncode = 2
            self.stdout = b""
            self.stderr = b"fatal: not a git repository"

    mocker.patch("apps.unused_code.unused_code.subprocess.run", return_value=FakeCompleted())
    with pytest.raises(RuntimeError):
        _git_grep(pattern="anything")
