"""
Comprehensive test coverage for polarion_verify_tc_requirements.py module.
This file provides extensive test coverage to improve overall test coverage.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from apps.polarion.polarion_verify_tc_requirements import has_verify


class TestHasVerifyCommand:
    """Test the has_verify Click command function"""

    def setup_method(self):
        """Setup test runner"""
        self.runner = CliRunner()

    @patch("apps.polarion.polarion_verify_tc_requirements.get_polarion_project_id")
    @patch("apps.polarion.polarion_verify_tc_requirements.find_polarion_ids")
    @patch("apps.polarion.polarion_verify_tc_requirements.validate_polarion_requirements")
    def test_command_success_no_missing_requirements(self, mock_validate, mock_find, mock_get_project):
        """Test successful command execution with no missing requirements"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = ["TEST-001", "TEST-002"]
        mock_validate.return_value = []  # No missing requirements

        # Act
        result = self.runner.invoke(
            has_verify,
            ["--project-id", "TEST_PROJECT", "--branch", "origin/develop"],
        )

        # Assert
        assert result.exit_code == 0
        mock_find.assert_called_once_with(
            polarion_project_id="TEST_PROJECT",
            string_to_match="added",
            branch="origin/develop",
        )
        mock_validate.assert_called_once_with(
            polarion_test_ids=["TEST-001", "TEST-002"],
            polarion_project_id="TEST_PROJECT",
        )

    @patch("apps.polarion.polarion_verify_tc_requirements.get_polarion_project_id")
    @patch("apps.polarion.polarion_verify_tc_requirements.find_polarion_ids")
    @patch("apps.polarion.polarion_verify_tc_requirements.validate_polarion_requirements")
    @patch("sys.exit")
    def test_command_fails_with_missing_requirements(self, mock_exit, mock_validate, mock_find, mock_get_project):
        """Test command exits with code 1 when there are missing requirements"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = ["TEST-001", "TEST-002"]
        mock_validate.return_value = ["TEST-001"]  # Has missing requirements

        # Act
        result = self.runner.invoke(
            has_verify,
            ["--project-id", "TEST_PROJECT"],
        )

        # Assert
        # sys.exit(1) should be called due to missing requirements
        assert mock_exit.call_count >= 1
        assert 1 in [call.args[0] for call in mock_exit.call_args_list]
        mock_find.assert_called_once_with(
            polarion_project_id="TEST_PROJECT",
            string_to_match="added",
            branch="origin/main",  # Default branch
        )
        mock_validate.assert_called_once_with(
            polarion_test_ids=["TEST-001", "TEST-002"],
            polarion_project_id="TEST_PROJECT",
        )

    @patch("apps.polarion.polarion_verify_tc_requirements.get_polarion_project_id")
    @patch("apps.polarion.polarion_verify_tc_requirements.find_polarion_ids")
    def test_command_no_added_ids_found(self, mock_find, mock_get_project):
        """Test command when no added IDs are found"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = []  # No added IDs found

        with patch("apps.polarion.polarion_verify_tc_requirements.validate_polarion_requirements") as mock_validate:
            # Act
            result = self.runner.invoke(
                has_verify,
                ["--project-id", "TEST_PROJECT"],
            )

            # Assert
            assert result.exit_code == 0
            mock_find.assert_called_once_with(
                polarion_project_id="TEST_PROJECT",
                string_to_match="added",
                branch="origin/main",
            )
            # validate_polarion_requirements should not be called when no added IDs
            mock_validate.assert_not_called()

    @patch("apps.polarion.polarion_verify_tc_requirements.get_polarion_project_id")
    @patch("apps.polarion.polarion_verify_tc_requirements.find_polarion_ids")
    @patch("apps.polarion.polarion_verify_tc_requirements.validate_polarion_requirements")
    def test_command_verbose_logging(self, mock_validate, mock_find, mock_get_project):
        """Test verbose flag enables debug logging"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = []
        mock_validate.return_value = []

        with patch("apps.polarion.polarion_verify_tc_requirements.LOGGER") as mock_logger:
            with patch("logging.getLogger") as mock_get_logger:
                mock_utils_logger = MagicMock()
                mock_get_logger.return_value = mock_utils_logger

                # Act
                result = self.runner.invoke(
                    has_verify,
                    ["--project-id", "TEST_PROJECT", "--verbose"],
                )

                # Assert
                assert result.exit_code == 0
                # Verify logging level was set
                mock_logger.setLevel.assert_called_with(logging.DEBUG)
                mock_get_logger.assert_called_with("apps.polarion.polarion_utils")
                mock_utils_logger.setLevel.assert_called_with(logging.DEBUG)

    @patch("apps.polarion.polarion_verify_tc_requirements.get_polarion_project_id")
    def test_command_uses_config_file_project_id(self, mock_get_project):
        """Test command uses project ID from config file when not provided"""
        # Arrange
        mock_get_project.return_value = "CONFIG_PROJECT"

        with patch("apps.polarion.polarion_verify_tc_requirements.find_polarion_ids") as mock_find:
            mock_find.return_value = []

            # Act
            result = self.runner.invoke(
                has_verify,
                ["--branch", "origin/feature"],
            )

            # Assert
            assert result.exit_code == 0
            # Check that get_polarion_project_id was called with default config path
            args, kwargs = mock_get_project.call_args
            assert kwargs["util_name"] == "pyutils-polarion-verify-tc-requirements"
            assert kwargs["config_file_path"].endswith("/.config/python-utility-scripts/config.yaml")

    @patch("apps.polarion.polarion_verify_tc_requirements.get_polarion_project_id")
    @patch("apps.polarion.polarion_verify_tc_requirements.find_polarion_ids")
    def test_command_custom_config_file_path(self, mock_find, mock_get_project):
        """Test command with custom config file path"""
        # Arrange
        custom_config_path = "/custom/path/config.yaml"
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = []

        # Act
        result = self.runner.invoke(
            has_verify,
            ["--config-file-path", custom_config_path],
        )

        # Assert
        assert result.exit_code == 0
        mock_get_project.assert_called_once_with(
            config_file_path=custom_config_path, util_name="pyutils-polarion-verify-tc-requirements"
        )

    @patch("apps.polarion.polarion_verify_tc_requirements.get_polarion_project_id")
    @patch("apps.polarion.polarion_verify_tc_requirements.find_polarion_ids")
    def test_command_project_id_override_config(self, mock_find, mock_get_project):
        """Test that command line project ID overrides config file"""
        # Arrange
        cli_project_id = "CLI_PROJECT"
        mock_find.return_value = []

        # Act
        result = self.runner.invoke(
            has_verify,
            ["--project-id", cli_project_id],
        )

        # Assert
        assert result.exit_code == 0
        # get_polarion_project_id should not be called when project-id is provided
        mock_get_project.assert_not_called()
        # find_polarion_ids should be called with the CLI project ID
        mock_find.assert_called_once_with(
            polarion_project_id=cli_project_id,
            string_to_match="added",
            branch="origin/main",
        )

    @patch("apps.polarion.polarion_verify_tc_requirements.get_polarion_project_id")
    @patch("apps.polarion.polarion_verify_tc_requirements.find_polarion_ids")
    @patch("apps.polarion.polarion_verify_tc_requirements.validate_polarion_requirements")
    def test_command_default_branch_parameter(self, mock_validate, mock_find, mock_get_project):
        """Test command uses default branch when not specified"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = ["TEST-001"]
        mock_validate.return_value = []

        # Act
        result = self.runner.invoke(
            has_verify,
            ["--project-id", "TEST_PROJECT"],
        )

        # Assert
        assert result.exit_code == 0
        mock_find.assert_called_once_with(
            polarion_project_id="TEST_PROJECT",
            string_to_match="added",
            branch="origin/main",  # Default branch
        )

    @patch("apps.polarion.polarion_verify_tc_requirements.get_polarion_project_id")
    @patch("apps.polarion.polarion_verify_tc_requirements.find_polarion_ids")
    @patch("apps.polarion.polarion_verify_tc_requirements.validate_polarion_requirements")
    def test_command_custom_branch_parameter(self, mock_validate, mock_find, mock_get_project):
        """Test command with custom branch parameter"""
        # Arrange
        custom_branch = "origin/feature-branch"
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = ["TEST-001"]
        mock_validate.return_value = []

        # Act
        result = self.runner.invoke(
            has_verify,
            ["--project-id", "TEST_PROJECT", "--branch", custom_branch],
        )

        # Assert
        assert result.exit_code == 0
        mock_find.assert_called_once_with(
            polarion_project_id="TEST_PROJECT",
            string_to_match="added",
            branch=custom_branch,
        )

    @patch("apps.polarion.polarion_verify_tc_requirements.get_polarion_project_id")
    @patch("apps.polarion.polarion_verify_tc_requirements.find_polarion_ids")
    @patch("apps.polarion.polarion_verify_tc_requirements.validate_polarion_requirements")
    def test_command_short_option_flags(self, mock_validate, mock_find, mock_get_project):
        """Test command with short option flags"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = ["TEST-001"]
        mock_validate.return_value = []

        # Act
        result = self.runner.invoke(
            has_verify,
            ["-p", "TEST_PROJECT", "-b", "origin/develop"],
        )

        # Assert
        assert result.exit_code == 0
        mock_find.assert_called_once_with(
            polarion_project_id="TEST_PROJECT",
            string_to_match="added",
            branch="origin/develop",
        )

    @patch("apps.polarion.polarion_verify_tc_requirements.get_polarion_project_id")
    @patch("apps.polarion.polarion_verify_tc_requirements.find_polarion_ids")
    @patch("apps.polarion.polarion_verify_tc_requirements.validate_polarion_requirements")
    @patch("sys.exit")
    def test_command_with_multiple_missing_requirements(self, mock_exit, mock_validate, mock_find, mock_get_project):
        """Test command with multiple test cases having missing requirements"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = ["TEST-001", "TEST-002", "TEST-003"]
        mock_validate.return_value = ["TEST-001", "TEST-003"]  # Multiple missing requirements

        # Act
        result = self.runner.invoke(
            has_verify,
            ["--project-id", "TEST_PROJECT"],
        )

        # Assert
        # sys.exit(1) should be called due to missing requirements
        assert mock_exit.call_count >= 1
        assert 1 in [call.args[0] for call in mock_exit.call_args_list]
        mock_validate.assert_called_once_with(
            polarion_test_ids=["TEST-001", "TEST-002", "TEST-003"],
            polarion_project_id="TEST_PROJECT",
        )

    @patch("apps.polarion.polarion_verify_tc_requirements.get_polarion_project_id")
    @patch("apps.polarion.polarion_verify_tc_requirements.find_polarion_ids")
    @patch("apps.polarion.polarion_verify_tc_requirements.validate_polarion_requirements")
    def test_command_with_logger_debug_calls(self, mock_validate, mock_find, mock_get_project):
        """Test that debug logging is called when verbose is enabled and IDs are found"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        test_ids = ["TEST-001", "TEST-002"]
        mock_find.return_value = test_ids
        mock_validate.return_value = []

        with patch("apps.polarion.polarion_verify_tc_requirements.LOGGER") as mock_logger:
            # Act
            result = self.runner.invoke(
                has_verify,
                ["--project-id", "TEST_PROJECT", "--verbose"],
            )

            # Assert
            assert result.exit_code == 0
            # Verify debug logging was called with the test IDs
            mock_logger.debug.assert_called_once_with(f"Checking following ids: {test_ids}")

    @patch("apps.polarion.polarion_verify_tc_requirements.get_polarion_project_id")
    @patch("apps.polarion.polarion_verify_tc_requirements.find_polarion_ids")
    @patch("apps.polarion.polarion_verify_tc_requirements.validate_polarion_requirements")
    @patch("sys.exit")
    def test_command_with_logger_error_calls(self, mock_exit, mock_validate, mock_find, mock_get_project):
        """Test that error logging is called when missing requirements are found"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        test_ids = ["TEST-001", "TEST-002"]
        missing_requirements = ["TEST-001"]
        mock_find.return_value = test_ids
        mock_validate.return_value = missing_requirements

        with patch("apps.polarion.polarion_verify_tc_requirements.LOGGER") as mock_logger:
            # Act
            result = self.runner.invoke(
                has_verify,
                ["--project-id", "TEST_PROJECT"],
            )

            # Assert
            # sys.exit(1) should be called due to missing requirements
            assert mock_exit.call_count >= 1
            assert 1 in [call.args[0] for call in mock_exit.call_args_list]
            # Verify error logging was called with the missing requirements
            mock_logger.error.assert_called_once_with(f"TestCases with missing requirement: {missing_requirements}")

    def test_command_missing_required_params_not_applicable(self):
        """Test that the command can run without explicit project-id (uses config)"""
        # This command doesn't have required parameters since project-id can come from config
        # So we test that it attempts to run (but may fail due to missing config)

        # We expect the command to run but potentially fail due to missing config file
        result = self.runner.invoke(has_verify, [])

        # The exit code may vary depending on whether the default config file exists
        # But the command should not fail due to missing required CLI parameters
        assert result.exit_code in [0, 1]  # May succeed or fail due to config, but not parameter validation

    @patch("apps.polarion.polarion_verify_tc_requirements.get_polarion_project_id")
    @patch("apps.polarion.polarion_verify_tc_requirements.find_polarion_ids")
    @patch("apps.polarion.polarion_verify_tc_requirements.validate_polarion_requirements")
    def test_command_mixed_logging_scenarios(self, mock_validate, mock_find, mock_get_project):
        """Test command behavior with mixed logging scenarios (debug and error)"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        test_ids = ["TEST-001", "TEST-002"]
        missing_requirements = ["TEST-002"]
        mock_find.return_value = test_ids
        mock_validate.return_value = missing_requirements

        with patch("apps.polarion.polarion_verify_tc_requirements.LOGGER") as mock_logger:
            with patch("sys.exit") as mock_exit:
                # Act
                result = self.runner.invoke(
                    has_verify,
                    ["--project-id", "TEST_PROJECT", "--verbose"],
                )

                # Assert
                # sys.exit(1) should be called due to missing requirements
                assert mock_exit.call_count >= 1
                assert 1 in [call.args[0] for call in mock_exit.call_args_list]
                # Verify both debug and error logging were called
                mock_logger.debug.assert_called_once_with(f"Checking following ids: {test_ids}")
                mock_logger.error.assert_called_once_with(f"TestCases with missing requirement: {missing_requirements}")
                mock_logger.setLevel.assert_called_with(logging.DEBUG)