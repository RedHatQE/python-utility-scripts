import os
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
    result = get_cli_runner().invoke(
        get_unused_functions,
        '--exclude-files "unused_code_file_for_test.py"',
    )
    LOGGER.info(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 0
    assert "Is not used anywhere in the code" not in result.output


def test_unused_code_function_list_exclude_all():
    result = get_cli_runner().invoke(
        get_unused_functions,
        [
            "--exclude-function-prefixes",
            "unused_code_",
            "--exclude-files",
            "manifests/unused_code_file_for_test.py",
        ],
    )
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
    def _mock_grep(pattern: str, **kwargs):
        # The usage pattern will now match the fixture name in the function signature
        if pattern == r"\bsample_fixture\b":
            return [f"{py_file.as_posix()}:1:def test_something(sample_fixture): pass"]
        return []

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
            self.stdout = ""
            self.stderr = "fatal: not a git repository"

    mocker.patch("apps.unused_code.unused_code.subprocess.run", return_value=FakeCompleted())
    with pytest.raises(RuntimeError):
        _git_grep(pattern="anything")


def test_commented_usage_is_ignored(mocker, tmp_path):
    # Create a temporary python file with a simple function
    py_file = tmp_path / "tmp_commented_usage.py"
    py_file.write_text(
        textwrap.dedent(
            """
def only_here():
    return 0
"""
        )
    )

    # Simulate git grep finding only a commented reference
    mocker.patch(
        "apps.unused_code.unused_code._git_grep",
        return_value=["some/other/file.py:12:# only_here() is not really used"],
    )

    # Should still be reported as unused because usage is commented out
    result = process_file(py_file=str(py_file), func_ignore_prefix=[], file_ignore_list=[])
    assert "Is not used anywhere in the code." in result


def test_git_grep_parsing_handles_windows_paths_with_colons(mocker, tmp_path):
    # Create a temporary python file with a simple function
    py_file = tmp_path / "tmp_windows_path.py"
    py_file.write_text(
        textwrap.dedent(
            """
def my_function():
    return 42
"""
        )
    )

    # Simulate git grep output with Windows-style paths containing drive letters and colons
    # Format: path:line-number:line-content
    # Windows paths like C:\path\to\file.py:123:content would break with split(":", 2)
    # but should work correctly with rsplit(":", 2)
    mocker.patch(
        "apps.unused_code.unused_code._git_grep",
        return_value=[
            "C:\\Users\\test\\project\\file.py:25:result = my_function()",
            "/some/unix/path/with:colon/file.py:30:my_function() # usage found",
            "D:\\Another\\Windows\\Path\\test.py:15:    my_function()  # another usage",
        ],
    )

    # Should detect the function as used (not report as unused)
    result = process_file(py_file=str(py_file), func_ignore_prefix=[], file_ignore_list=[])
    assert result == ""  # Empty string means function is used, not unused


def test_git_grep_parsing_handles_malformed_output_gracefully(mocker, tmp_path):
    # Create a temporary python file with a simple function
    py_file = tmp_path / "tmp_malformed.py"
    py_file.write_text(
        textwrap.dedent(
            """
def my_function():
    return 42
"""
        )
    )

    # Simulate git grep output with malformed entries (missing parts)
    # The parsing should skip malformed entries and continue processing
    mocker.patch(
        "apps.unused_code.unused_code._git_grep",
        return_value=[
            "malformed_line_without_colons",  # Should be skipped
            "only:one:colon",  # Should be skipped (only 2 parts after rsplit)
            "C:\\valid\\path\\file.py:25:result = my_function()",  # Valid - should be processed
        ],
    )

    # Should detect the function as used despite malformed entries
    result = process_file(py_file=str(py_file), func_ignore_prefix=[], file_ignore_list=[])
    assert result == ""  # Empty string means function is used, not unused


def test_function_as_argument_is_used():
    result = process_file(
        py_file="tests/unused_code/manifests/functions_as_args.py",
        func_ignore_prefix=[],
        file_ignore_list=[],
    )
    assert result == ""


def test_unused_code_with_file_path_no_unused():
    result = get_cli_runner().invoke(
        get_unused_functions,
        ["--file-path", "tests/unused_code/manifests/functions_as_args.py"],
    )
    assert result.exit_code == 0
    assert "Is not used anywhere in the code" not in result.output


def test_unused_code_with_file_path_with_unused():
    result = get_cli_runner().invoke(
        get_unused_functions,
        ["--file-path", "tests/unused_code/manifests/unused_code_file_for_test.py"],
    )
    assert result.exit_code == 1
    assert "Is not used anywhere in the code" in result.output


def test_unused_code_with_directory():
    result = get_cli_runner().invoke(get_unused_functions, ["--directory", "tests/unused_code/manifests/"])
    assert result.exit_code == 1
    assert "Is not used anywhere in the code" in result.output


def test_unused_code_with_config_file():
    result = get_cli_runner().invoke(
        get_unused_functions,
        [
            "--config-file-path",
            "tests/unused_code/manifests/test_config.yaml",
            "--directory",
            "tests/unused_code/manifests/",
        ],
    )
    assert result.exit_code == 0
    assert "Is not used anywhere in the code" not in result.output


def test_unused_code_with_file_path_as_dir():
    result = get_cli_runner().invoke(get_unused_functions, ["--file-path", "tests/unused_code/"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)


def test_unused_code_with_directory_as_file():
    result = get_cli_runner().invoke(
        get_unused_functions,
        ["--directory", "tests/unused_code/test_unused_code.py"],
    )
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)


def test_unused_code_not_a_git_repo(tmp_path):
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        # tmp_path is not a git repo, so this should fail
        result = get_cli_runner().invoke(get_unused_functions)
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
    finally:
        os.chdir(original_cwd)
