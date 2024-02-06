from simple_logger.logger import get_logger
from click.testing import CliRunner
from apps.unused_code.unused_code import get_unused_functions

LOGGER = get_logger(name=__name__)


def test_unused_code():
    runner = CliRunner()
    result = runner.invoke(get_unused_functions)
    LOGGER.debug(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 1
    assert "Is not used anywhere in the code" in result.output


def test_unused_code_file_list():
    runner = CliRunner()
    result = runner.invoke(get_unused_functions, '--exclude-files "unused_code_file_for_test.py"')
    LOGGER.debug(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 0
    assert "Is not used anywhere in the code" not in result.output


def test_unused_code_function_list_exclude_all():
    runner = CliRunner()
    result = runner.invoke(get_unused_functions, '--exclude-function-prefixes "unused_code_"')
    LOGGER.debug(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 0
    assert "Is not used anywhere in the code" not in result.output


def test_unused_code_function_list_exclude():
    runner = CliRunner()
    result = runner.invoke(get_unused_functions, '--exclude-function-prefixes "unused_code_check_file"')
    LOGGER.debug(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 1
    assert "Is not used anywhere in the code" in result.output
