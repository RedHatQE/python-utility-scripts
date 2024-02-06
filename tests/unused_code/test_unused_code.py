from simple_logger.logger import get_logger
from click.testing import CliRunner
from apps.unused_code.unused_code import get_unused_functions

LOGGER = get_logger(name=__name__)


def get_cli_runner():
    return CliRunner()


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
