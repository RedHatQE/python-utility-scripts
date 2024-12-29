from __future__ import annotations

import re
import shlex
import subprocess
import sys

from simple_logger.logger import get_logger

from apps.utils import get_util_config

LOGGER = get_logger(name=__name__)
AUTOMATED = "automated"
NOT_AUTOMATED = "notautomated"
APPROVED = "approved"


def git_diff(branch: str) -> str:
    data = subprocess.check_output(shlex.split(f"git diff {branch} HEAD"))
    return data.decode()


def git_diff_lines(branch: str) -> dict[str, list[str]]:
    diff: dict[str, list[str]] = {}
    for line in git_diff(branch=branch).splitlines():
        LOGGER.debug(line)
        if line.startswith("+"):
            diff.setdefault("added", []).append(line)
        if line.startswith("-"):
            diff.setdefault("removed", []).append(line)
    return diff


def validate_polarion_requirements(
    polarion_test_ids: list[str],
    polarion_project_id: str,
) -> list[str]:
    tests_with_missing_requirements: list[str] = []
    if polarion_test_ids:
        from pylero.exceptions import PyleroLibException
        from pylero.work_item import Requirement, TestCase

        for _id in polarion_test_ids:
            has_req = False
            LOGGER.debug(f"Checking if {_id} verifies any requirement")
            tc = TestCase(project_id=polarion_project_id, work_item_id=_id)
            for link in tc.linked_work_items:
                try:
                    Requirement(project_id=polarion_project_id, work_item_id=link.work_item_id)
                    has_req = True
                    break
                except PyleroLibException:
                    continue

            if not has_req:
                LOGGER.error(f"{_id}: Is missing requirement")
                tests_with_missing_requirements.append(_id)
    return tests_with_missing_requirements


def find_polarion_ids(polarion_project_id: str, string_to_match: str, branch: str) -> list[str]:
    return re.findall(
        rf"pytest.mark.polarion.*({polarion_project_id}-[0-9]+)",
        "\n".join(git_diff_lines(branch=branch).get(string_to_match, [])),
        re.MULTILINE | re.IGNORECASE,
    )


def get_polarion_project_id(util_name: str, config_file_path: str) -> str:
    polarion_project_id = get_util_config(util_name=util_name, config_file_path=config_file_path).get("project_id")
    if not polarion_project_id:
        LOGGER.error("Polarion project id must be passed via config file or command line")
        sys.exit(1)
    return polarion_project_id


def update_polarion_ids(
    project_id: str, is_automated: bool, polarion_ids: list[str], is_approved: bool = False
) -> dict[str, list[str]]:
    updated_ids: dict[str, list[str]] = {}
    if polarion_ids:
        automation_status = AUTOMATED if is_automated else NOT_AUTOMATED

        from pylero.exceptions import PyleroLibException
        from pylero.work_item import TestCase

        for id in polarion_ids:
            try:
                tc = TestCase(project_id=project_id, work_item_id=id)
                tc.caseautomation = automation_status
                if is_approved:
                    tc.status = APPROVED
                tc.update()
                LOGGER.debug(f"Polarion {id}: marked as: {automation_status}, approved status set: {is_approved}")
                updated_ids.setdefault("updated", []).append(id)
            except PyleroLibException as polarion_exception:
                error = f"{id}: {polarion_exception}"
                LOGGER.error(error)
                updated_ids.setdefault("failed", []).append(error)
    return updated_ids
