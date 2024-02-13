import logging
import re
import shlex
import subprocess

from pylero.exceptions import PyleroLibException
from pylero.work_item import Requirement, TestCase

logging.basicConfig(level=logging.INFO)

LOGGER = logging.getLogger(__name__)
APPROVED = "approved"
AUTOMATED = "automated"


def git_diff():
    data = subprocess.check_output(shlex.split("git diff HEAD^-1"))
    data = data.decode("utf-8")
    return data


def find_polarion_ids(polarion_project_id, data):
    match_ids = set()
    for item in data:
        match = re.findall(rf"pytest.mark.polarion.*{polarion_project_id}-[0-9]+", item)
        if match:
            match_id = re.findall(rf"{polarion_project_id}-[0-9]+", match[0])
            match_ids.add(match_id[0])

    return match_ids


def git_diff_added_removed_lines():
    diff = {}
    for line in git_diff().splitlines():
        LOGGER.info(line)
        if line.startswith("+"):
            diff.setdefault("added", []).append(line)

        if line.startswith("-"):
            diff.setdefault("removed", []).append(line)

    return diff


def get_polarion_ids_from_diff(diff, polarion_project_id):
    added_ids = find_polarion_ids(data=diff.get("added", []), polarion_project_id=polarion_project_id)
    removed_ids = find_polarion_ids(data=diff.get("removed", []), polarion_project_id=polarion_project_id)
    return added_ids, removed_ids


def validate_polarion_requirements(polarion_test_ids, polarion_project_id):
    tests_with_missing_requirements = []
    for _id in polarion_test_ids:
        has_req = False
        LOGGER.info(f"Checking if {_id} verifies any requirement")
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
