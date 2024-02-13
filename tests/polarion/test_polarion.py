from simple_logger.logger import get_logger
from apps.polarion.polarion_verify_tc_requirements import has_verify
from tests.utils import get_cli_runner

LOGGER = get_logger(name=__name__)


def test_polarion_requirement_no_args():
    result = get_cli_runner().invoke(has_verify)
    LOGGER.info(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 1
    assert "Polarion project id must be passed via config file or command line" in result.output


def test_polarion_requirement():
    result = get_cli_runner().invoke(has_verify, "--project-id ABC")
    LOGGER.info(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 0


def test_polarion_requirement_config():
    result = get_cli_runner().invoke(has_verify, "--config-file-path config.example.yaml")
    LOGGER.info(f"Result output: {result.output}, exit code: {result.exit_code}, exceptions: {result.exception}")
    assert result.exit_code == 0
