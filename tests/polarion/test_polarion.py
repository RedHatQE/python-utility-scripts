from simple_logger.logger import get_logger
from unittest import mock

from apps.polarion.polarion_verify_tc_requirements import has_verify
from tests.utils import get_cli_runner

LOGGER = get_logger(name=__name__)
ERROR_MESSAGE = "{exit_code} does not match expected value."


def test_polarion_requirement_no_args():
    result = get_cli_runner().invoke(has_verify)
    LOGGER.debug(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 1, ERROR_MESSAGE.format(exit_code=result.exit_code)
    assert "Polarion project id must be passed via config file or command line" in result.output


def test_polarion_requirement():
    result = get_cli_runner().invoke(has_verify, "--project-id ABC")
    LOGGER.debug(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 0, ERROR_MESSAGE.format(exit_code=result.exit_code)


def test_polarion_requirement_config():
    result = get_cli_runner().invoke(has_verify, "--config-file-path config.example.yaml")
    LOGGER.debug(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 0, ERROR_MESSAGE.format(exit_code=result.exit_code)


def test_polarion_with_no_requirement():
    with mock.patch("apps.polarion.polarion_verify_tc_requirements.git_diff_lines") as get_diff_lines:
        get_diff_lines.return_value = {
            "added": ['+ @pytest.mark.polarion("ABC-1212")', '+ @pytest.mark.polarion("ABC-1213")']
        }
        with mock.patch("apps.polarion.polarion_verify_tc_requirements.validate_polarion_requirements") as validate_req:
            validate_req.return_value = ["ABC-1212", "ABC-1213"]
            result = get_cli_runner().invoke(has_verify, "--project-id ABC")
            assert result.exit_code == 1, ERROR_MESSAGE.format(exit_code=result.exit_code)
            assert f"TestCases with missing requirement: {validate_req.return_value}" in result.output
    LOGGER.debug(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")


def test_polarion_with_requirement():
    with mock.patch("apps.polarion.polarion_verify_tc_requirements.git_diff_lines") as get_diff_lines:
        get_diff_lines.return_value = {
            "added": ['+ @pytest.mark.polarion("ABC-1212")', '+ @pytest.mark.polarion("ABC-1213")']
        }
        with mock.patch("apps.polarion.polarion_verify_tc_requirements.validate_polarion_requirements") as validate_req:
            validate_req.return_value = []
            result = get_cli_runner().invoke(has_verify, "--project-id ABC")
            assert result.exit_code == 0, ERROR_MESSAGE.format(exit_code=result.exit_code)
            assert f"TestCases with missing requirement: {validate_req.return_value}" not in result.output
    LOGGER.debug(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
