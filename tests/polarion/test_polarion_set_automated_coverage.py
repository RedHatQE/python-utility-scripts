"""
Comprehensive test coverage for polarion_set_automated.py module.
This file provides extensive test coverage to improve overall test coverage.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from apps.polarion.polarion_set_automated import (
    approve_tests,
    polarion_approve_automate,
    remove_approved_tests,
)


class TestApproveTests:
    """Test the approve_tests function"""

    @patch("apps.polarion.polarion_set_automated.update_polarion_ids")
    def test_approve_tests_success(self, mock_update):
        """Test approve_tests with successful update"""
        # Arrange
        project_id = "TEST_PROJECT"
        test_ids = ["TEST-001", "TEST-002"]
        expected_result = {"updated": test_ids, "failed": []}
        mock_update.return_value = expected_result

        # Act
        result = approve_tests(project_id, test_ids)

        # Assert
        mock_update.assert_called_once_with(
            polarion_ids=test_ids, project_id=project_id, is_automated=True, is_approved=True
        )
        assert result == expected_result

    @patch("apps.polarion.polarion_set_automated.update_polarion_ids")
    def test_approve_tests_with_failures(self, mock_update):
        """Test approve_tests when some updates fail"""
        # Arrange
        project_id = "TEST_PROJECT"
        test_ids = ["TEST-001", "TEST-002"]
        expected_result = {"updated": ["TEST-001"], "failed": ["TEST-002"]}
        mock_update.return_value = expected_result

        # Act
        result = approve_tests(project_id, test_ids)

        # Assert
        assert result == expected_result
        mock_update.assert_called_once_with(
            polarion_ids=test_ids, project_id=project_id, is_automated=True, is_approved=True
        )

    @patch("apps.polarion.polarion_set_automated.update_polarion_ids")
    def test_approve_tests_empty_ids(self, mock_update):
        """Test approve_tests with empty ID list"""
        # Arrange
        project_id = "TEST_PROJECT"
        test_ids = []
        expected_result = {"updated": [], "failed": []}
        mock_update.return_value = expected_result

        # Act
        result = approve_tests(project_id, test_ids)

        # Assert
        mock_update.assert_called_once_with(polarion_ids=[], project_id=project_id, is_automated=True, is_approved=True)
        assert result == expected_result


class TestRemoveApprovedTests:
    """Test the remove_approved_tests function"""

    @patch("apps.polarion.polarion_set_automated.find_polarion_ids")
    @patch("apps.polarion.polarion_set_automated.update_polarion_ids")
    def test_remove_approved_tests_with_removals(self, mock_update, mock_find):
        """Test remove_approved_tests when IDs are found for removal"""
        # Arrange
        project_id = "TEST_PROJECT"
        branch = "main"
        prev_commit = "abc123"
        curr_commit = "def456"
        added_ids = ["TEST-001"]
        found_removed_ids = ["TEST-002", "TEST-003", "TEST-001"]  # Include overlap

        mock_find.return_value = found_removed_ids
        mock_update.return_value = {"updated": ["TEST-002", "TEST-003"], "failed": []}

        # Act
        result = remove_approved_tests(project_id, branch, prev_commit, curr_commit, added_ids)

        # Assert
        mock_find.assert_called_once_with(
            polarion_project_id=project_id,
            string_to_match="removed",
            branch=branch,
            previous_commit=prev_commit,
            current_commit=curr_commit,
        )
        # Verify the call was made with the correct IDs (order-independent)
        _, kwargs = mock_update.call_args
        assert set(kwargs["polarion_ids"]) == {"TEST-002", "TEST-003"}  # Should exclude added_ids overlap
        assert kwargs["project_id"] == project_id
        assert kwargs["is_automated"] is False

        assert result == {"updated": ["TEST-002", "TEST-003"], "failed": []}

    @patch("apps.polarion.polarion_set_automated.find_polarion_ids")
    def test_remove_approved_tests_no_removals(self, mock_find):
        """Test remove_approved_tests when no IDs found for removal"""
        # Arrange
        project_id = "TEST_PROJECT"
        mock_find.return_value = []

        # Act
        result = remove_approved_tests(project_id)

        # Assert
        assert result == {}
        mock_find.assert_called_once_with(
            polarion_project_id=project_id,
            string_to_match="removed",
            branch=None,
            previous_commit=None,
            current_commit=None,
        )

    @patch("apps.polarion.polarion_set_automated.find_polarion_ids")
    def test_remove_approved_tests_all_overlap_with_added(self, mock_find):
        """Test when all removed IDs overlap with added IDs"""
        # Arrange
        project_id = "TEST_PROJECT"
        added_ids = ["TEST-001", "TEST-002"]
        mock_find.return_value = added_ids  # All found IDs are in added_ids

        # Act
        result = remove_approved_tests(project_id, added_ids=added_ids)

        # Assert
        assert result == {}  # Nothing should be removed

    @patch("apps.polarion.polarion_set_automated.find_polarion_ids")
    def test_remove_approved_tests_none_added_ids(self, mock_find):
        """Test remove_approved_tests with None added_ids parameter"""
        # This tests the added_ids = added_ids or [] line
        project_id = "TEST_PROJECT"
        mock_find.return_value = []

        # Act
        result = remove_approved_tests(project_id, added_ids=None)

        # Assert
        assert result == {}

    @patch("apps.polarion.polarion_set_automated.find_polarion_ids")
    @patch("apps.polarion.polarion_set_automated.update_polarion_ids")
    def test_remove_approved_tests_with_failures(self, mock_update, mock_find):
        """Test remove_approved_tests when update operation fails"""
        # Arrange
        project_id = "TEST_PROJECT"
        found_ids = ["TEST-001", "TEST-002"]
        mock_find.return_value = found_ids
        mock_update.return_value = {"updated": ["TEST-001"], "failed": ["TEST-002"]}

        # Act
        result = remove_approved_tests(project_id)

        # Assert
        assert result == {"updated": ["TEST-001"], "failed": ["TEST-002"]}


class TestPolarionApproveAutomateCommand:
    """Test the Click command function"""

    def setup_method(self):
        """Setup test runner"""
        self.runner = CliRunner()

    @patch("apps.polarion.polarion_set_automated.get_polarion_project_id")
    @patch("apps.polarion.polarion_set_automated.find_polarion_ids")
    @patch("apps.polarion.polarion_set_automated.approve_tests")
    @patch("apps.polarion.polarion_set_automated.remove_approved_tests")
    def test_command_success_flow(self, mock_remove, mock_approve, mock_find, mock_get_project):
        """Test successful command execution"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = ["TEST-001", "TEST-002"]
        mock_approve.return_value = {"updated": ["TEST-001", "TEST-002"], "failed": []}
        mock_remove.return_value = {"updated": [], "failed": []}

        # Act
        result = self.runner.invoke(
            polarion_approve_automate,
            ["--previous-commit", "abc123", "--current-commit", "def456", "--project-id", "TEST_PROJECT"],
        )

        # Assert
        assert result.exit_code == 0
        mock_find.assert_called_once_with(
            polarion_project_id="TEST_PROJECT",
            string_to_match="added",
            branch=None,
            previous_commit="abc123",
            current_commit="def456",
        )
        mock_approve.assert_called_once_with(polarion_project_id="TEST_PROJECT", added_ids=["TEST-001", "TEST-002"])
        mock_remove.assert_called_once()

    @patch("apps.polarion.polarion_set_automated.get_polarion_project_id")
    @patch("apps.polarion.polarion_set_automated.find_polarion_ids")
    @patch("apps.polarion.polarion_set_automated.approve_tests")
    @patch("apps.polarion.polarion_set_automated.remove_approved_tests")
    def test_command_with_added_failures_exits_1(self, mock_remove, mock_approve, mock_find, mock_get_project):
        """Test command exits with code 1 when there are added failures"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = ["TEST-001"]
        mock_approve.return_value = {"updated": [], "failed": ["TEST-001"]}
        mock_remove.return_value = {"updated": [], "failed": []}

        # Act
        result = self.runner.invoke(
            polarion_approve_automate,
            ["--previous-commit", "abc123", "--current-commit", "def456", "--project-id", "TEST_PROJECT"],
        )

        # Assert
        assert result.exit_code == 1

    @patch("apps.polarion.polarion_set_automated.get_polarion_project_id")
    @patch("apps.polarion.polarion_set_automated.find_polarion_ids")
    @patch("apps.polarion.polarion_set_automated.approve_tests")
    @patch("apps.polarion.polarion_set_automated.remove_approved_tests")
    def test_command_with_removed_failures_exits_1(self, mock_remove, mock_approve, mock_find, mock_get_project):
        """Test command exits with code 1 when there are removed failures"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = ["TEST-001"]
        mock_approve.return_value = {"updated": ["TEST-001"], "failed": []}
        mock_remove.return_value = {"updated": [], "failed": ["TEST-002"]}

        # Act
        result = self.runner.invoke(
            polarion_approve_automate,
            ["--previous-commit", "abc123", "--current-commit", "def456", "--project-id", "TEST_PROJECT"],
        )

        # Assert
        assert result.exit_code == 1

    def test_command_missing_required_params(self):
        """Test command fails with missing required parameters"""
        result = self.runner.invoke(polarion_approve_automate, [])
        assert result.exit_code != 0
        assert "Missing option" in result.output

    def test_command_missing_previous_commit(self):
        """Test command fails when missing previous-commit"""
        result = self.runner.invoke(polarion_approve_automate, ["--current-commit", "def456"])
        assert result.exit_code != 0
        assert "Missing option" in result.output

    def test_command_missing_current_commit(self):
        """Test command fails when missing current-commit"""
        result = self.runner.invoke(polarion_approve_automate, ["--previous-commit", "abc123"])
        assert result.exit_code != 0
        assert "Missing option" in result.output

    @patch("apps.polarion.polarion_set_automated.get_polarion_project_id")
    @patch("apps.polarion.polarion_set_automated.find_polarion_ids")
    @patch("apps.polarion.polarion_set_automated.remove_approved_tests")
    def test_command_no_added_ids(self, mock_remove, mock_find, mock_get_project):
        """Test command when no IDs are found to add"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = []  # No added IDs found
        mock_remove.return_value = {"updated": [], "failed": []}

        with patch("apps.polarion.polarion_set_automated.approve_tests") as mock_approve:
            # Act
            result = self.runner.invoke(
                polarion_approve_automate,
                ["--previous-commit", "abc123", "--current-commit", "def456", "--project-id", "TEST_PROJECT"],
            )

            # Assert
            assert result.exit_code == 0
            # approve_tests should not be called when no added IDs
            mock_approve.assert_not_called()

    @patch("apps.polarion.polarion_set_automated.get_polarion_project_id")
    @patch("apps.polarion.polarion_set_automated.find_polarion_ids")
    @patch("apps.polarion.polarion_set_automated.remove_approved_tests")
    def test_command_verbose_logging(self, mock_remove, mock_find, mock_get_project):
        """Test verbose flag enables debug logging"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = []
        mock_remove.return_value = {"updated": [], "failed": []}

        with patch("apps.polarion.polarion_set_automated.LOGGER") as mock_logger:
            with patch("logging.getLogger") as mock_get_logger:
                mock_utils_logger = MagicMock()
                mock_get_logger.return_value = mock_utils_logger

                # Act
                result = self.runner.invoke(
                    polarion_approve_automate,
                    [
                        "--previous-commit",
                        "abc123",
                        "--current-commit",
                        "def456",
                        "--project-id",
                        "TEST_PROJECT",
                        "--verbose",
                    ],
                )

                # Assert
                assert result.exit_code == 0
                # Verify logging level was set
                mock_logger.setLevel.assert_called_with(logging.DEBUG)
                mock_get_logger.assert_called_with("apps.polarion.polarion_utils")
                mock_utils_logger.setLevel.assert_called_with(logging.DEBUG)

    @patch("apps.polarion.polarion_set_automated.get_polarion_project_id")
    def test_command_uses_config_file_project_id(self, mock_get_project):
        """Test command uses project ID from config file when not provided"""
        # Arrange
        mock_get_project.return_value = "CONFIG_PROJECT"

        with patch("apps.polarion.polarion_set_automated.find_polarion_ids") as mock_find:
            mock_find.return_value = []
            with patch("apps.polarion.polarion_set_automated.remove_approved_tests") as mock_remove:
                mock_remove.return_value = {"updated": [], "failed": []}

                # Act
                result = self.runner.invoke(
                    polarion_approve_automate,
                    [
                        "--previous-commit",
                        "abc123",
                        "--current-commit",
                        "def456",
                        # No --project-id provided
                    ],
                )

                # Assert
                assert result.exit_code == 0
                # Check that get_polarion_project_id was called with default config path
                _, kwargs = mock_get_project.call_args
                assert kwargs["util_name"] == "pyutils-polarion-set-automated"
                assert kwargs["config_file_path"].endswith("/.config/python-utility-scripts/config.yaml")

    @patch("apps.polarion.polarion_set_automated.get_polarion_project_id")
    @patch("apps.polarion.polarion_set_automated.find_polarion_ids")
    @patch("apps.polarion.polarion_set_automated.remove_approved_tests")
    def test_command_both_add_and_remove_failures(self, mock_remove, mock_find, mock_get_project):
        """Test command when both add and remove operations have failures"""
        # Arrange
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = ["TEST-001"]
        mock_remove.return_value = {"updated": [], "failed": ["TEST-002"]}

        with patch("apps.polarion.polarion_set_automated.approve_tests") as mock_approve:
            mock_approve.return_value = {"updated": [], "failed": ["TEST-001"]}

            # Act
            result = self.runner.invoke(
                polarion_approve_automate,
                ["--previous-commit", "abc123", "--current-commit", "def456", "--project-id", "TEST_PROJECT"],
            )

            # Assert
            assert result.exit_code == 1

    @patch("apps.polarion.polarion_set_automated.get_polarion_project_id")
    @patch("apps.polarion.polarion_set_automated.find_polarion_ids")
    @patch("apps.polarion.polarion_set_automated.approve_tests")
    @patch("apps.polarion.polarion_set_automated.remove_approved_tests")
    def test_command_custom_config_file_path(self, mock_remove, _mock_approve, mock_find, mock_get_project):
        """Test command with custom config file path"""
        # Arrange
        custom_config_path = "/custom/path/config.yaml"
        mock_get_project.return_value = "TEST_PROJECT"
        mock_find.return_value = []
        mock_remove.return_value = {"updated": [], "failed": []}

        # Act
        result = self.runner.invoke(
            polarion_approve_automate,
            ["--config-file-path", custom_config_path, "--previous-commit", "abc123", "--current-commit", "def456"],
        )

        # Assert
        assert result.exit_code == 0
        mock_get_project.assert_called_once_with(
            config_file_path=custom_config_path, util_name="pyutils-polarion-set-automated"
        )

    @patch("apps.polarion.polarion_set_automated.get_polarion_project_id")
    @patch("apps.polarion.polarion_set_automated.find_polarion_ids")
    @patch("apps.polarion.polarion_set_automated.approve_tests")
    @patch("apps.polarion.polarion_set_automated.remove_approved_tests")
    def test_command_project_id_override_config(self, mock_remove, _mock_approve, mock_find, mock_get_project):
        """Test that command line project ID overrides config file"""
        # Arrange
        cli_project_id = "CLI_PROJECT"
        mock_find.return_value = []
        mock_remove.return_value = {"updated": [], "failed": []}

        # Act
        result = self.runner.invoke(
            polarion_approve_automate,
            ["--project-id", cli_project_id, "--previous-commit", "abc123", "--current-commit", "def456"],
        )

        # Assert
        assert result.exit_code == 0
        # get_polarion_project_id should not be called when project-id is provided
        mock_get_project.assert_not_called()
        # find_polarion_ids should be called with the CLI project ID
        mock_find.assert_called_once_with(
            polarion_project_id=cli_project_id,
            string_to_match="added",
            branch=None,
            previous_commit="abc123",
            current_commit="def456",
        )
