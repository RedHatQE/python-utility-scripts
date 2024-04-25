from simple_logger.logger import get_logger
import shlex
import subprocess
from pylero.exceptions import PyleroLibException
from typing import Dict, List

LOGGER = get_logger(name=__name__)


def git_diff() -> str:
    data = subprocess.check_output(shlex.split("git diff HEAD^-1"))
    return data.decode()


def git_diff_lines() -> Dict[str, List[str]]:
    diff: Dict[str, List[str]] = {}
    for line in git_diff().splitlines():
        LOGGER.debug(line)
        if line.startswith("+"):
            diff.setdefault("added", []).append(line)
    return diff


def validate_polarion_requirements(
    polarion_test_ids: List[str],
    polarion_project_id: str,
) -> List[str]:
    from pylero.work_item import TestCase, Requirement

    tests_with_missing_requirements: List[str] = []

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
