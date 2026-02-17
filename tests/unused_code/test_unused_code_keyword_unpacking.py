import textwrap

from simple_logger.logger import get_logger

from apps.unused_code.unused_code import get_unused_functions, process_file
from tests.utils import get_cli_runner

LOGGER = get_logger(name=__name__)


def test_keyword_unpacking_usage_is_detected(mocker, tmp_path):
    """Test that functions used in **function_name() patterns are detected as used."""
    # Create a temporary python file with a function that would be unused without keyword unpacking
    py_file = tmp_path / "tmp_keyword_unpacking.py"
    py_file.write_text(
        textwrap.dedent(
            """
def get_config():
    return {"key": "value"}
"""
        )
    )

    # Mock git grep to simulate the detection
    def _mock_grep(pattern: str, **kwargs):
        # Regular usage pattern - no matches found
        if pattern == r"\bget_config\b":
            return []
        # Keyword unpacking pattern - match found
        elif pattern == r"\*\*get_config\s*\(":
            return [f"{py_file.as_posix()}:10:    result = some_function(**get_config())"]
        return []

    mocker.patch("apps.unused_code.unused_code._git_grep", side_effect=_mock_grep)

    result = process_file(py_file=str(py_file), func_ignore_prefix=[], file_ignore_list=[])
    # Should not report the function as unused since it's used in keyword unpacking
    assert result == ""


def test_keyword_unpacking_in_function_definition_is_detected(mocker, tmp_path):
    """Test that functions used in keyword unpacking within function definitions are detected as used."""
    py_file = tmp_path / "tmp_keyword_unpacking_def.py"
    py_file.write_text(
        textwrap.dedent(
            """
def helper_function():
    return {"key": "value"}
"""
        )
    )

    def _mock_grep(pattern: str, **kwargs):
        if pattern == r"\bhelper_function\b":
            return []
        elif pattern == r"\*\*helper_function\s*\(":
            # Return a function definition that USES helper_function - this should be detected
            return [f"{py_file.as_posix()}:5:def target_function(**helper_function()):"]
        return []

    mocker.patch("apps.unused_code.unused_code._git_grep", side_effect=_mock_grep)

    result = process_file(py_file=str(py_file), func_ignore_prefix=[], file_ignore_list=[])
    # Should NOT report as unused because helper_function() is being called in the definition
    assert result == ""


def test_keyword_unpacking_ignores_comments(mocker, tmp_path):
    """Test that commented keyword unpacking usage is properly ignored."""
    py_file = tmp_path / "tmp_keyword_unpacking_comment.py"
    py_file.write_text(
        textwrap.dedent(
            """
def config_helper():
    return {"setting": "value"}
"""
        )
    )

    def _mock_grep(pattern: str, **kwargs):
        if pattern == r"\bconfig_helper\b":
            return []
        elif pattern == r"\*\*config_helper\s*\(":
            # Return a commented usage which should be ignored
            return [f"{py_file.as_posix()}:6:    # result = setup(**config_helper())"]
        return []

    mocker.patch("apps.unused_code.unused_code._git_grep", side_effect=_mock_grep)

    result = process_file(py_file=str(py_file), func_ignore_prefix=[], file_ignore_list=[])
    # Should report as unused because usage is commented out
    assert "Is not used anywhere in the code." in result


def test_keyword_unpacking_ignores_documentation_patterns(mocker, tmp_path):
    """Test that documentation patterns with keyword unpacking are properly ignored."""
    py_file = tmp_path / "tmp_keyword_unpacking_doc.py"
    py_file.write_text(
        textwrap.dedent(
            '''
def doc_function():
    """
    Example: setup(**doc_function())
    This function returns configuration data.
    """
    return {"config": "value"}
'''
        )
    )

    def _mock_grep(pattern: str, **kwargs):
        if pattern == r"\bdoc_function\b":
            return []
        elif pattern == r"\*\*doc_function\s*\(":
            # Return documentation pattern which should be ignored
            return [f"{py_file.as_posix()}:4:    Example: setup(**doc_function())"]
        return []

    mocker.patch("apps.unused_code.unused_code._git_grep", side_effect=_mock_grep)

    result = process_file(py_file=str(py_file), func_ignore_prefix=[], file_ignore_list=[])
    # Should report as unused because usage is in documentation
    assert "Is not used anywhere in the code." in result


def test_keyword_unpacking_with_cli(tmp_path):
    """Test keyword unpacking detection through the CLI interface."""
    py_file = tmp_path / "test_keyword_unpacking.py"
    py_file.write_text(
        textwrap.dedent(
            """
def get_config():
    return {"database_url": "localhost", "debug": True}

def get_defaults():
    return {"timeout": 30, "retries": 3}

def unused_function():
    return "This should be flagged as unused"

def main():
    config = {
        **get_config(),
        "extra": "value"
    }
    result = setup(**get_defaults())
    return result
"""
        )
    )

    result = get_cli_runner().invoke(
        get_unused_functions,
        ["--file-path", str(py_file)],
    )
    assert result.exit_code == 1
    # Should not report get_config or get_defaults as unused
    assert "get_config" not in result.output
    assert "get_defaults" not in result.output
    assert "Is not used anywhere in the code." in result.output
