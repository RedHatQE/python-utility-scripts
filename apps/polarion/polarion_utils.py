import re

import click
from simple_logger.logger import get_logger
import shlex
import subprocess

from apps.utils import get_util_config

LOGGER = get_logger(name=__name__)
AUTOMATED = "automated"
NOT_AUTOMATED = "notautomated"
APPROVED = "approved"


def git_diff():
    data = subprocess.check_output(shlex.split("git diff HEAD^-1"))
    data = data.decode("utf-8")
    return data


def git_diff_lines():
    diff = {}
    for line in git_diff().splitlines():
        LOGGER.debug(line)
        if line.startswith("+"):
            diff.setdefault("added", []).append(line)
        if line.startswith("-"):
            diff.setdefault("removed", []).append(line)
    return diff


def validate_polarion_requirements(
    polarion_project_id,
    polarion_test_ids=None,
):
    tests_with_missing_requirements = []
    if polarion_test_ids:
        from pylero.work_item import TestCase, Requirement
        from pylero.exceptions import PyleroLibException

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


def find_polarion_ids(polarion_project_id, string_to_match):
    return re.findall(
        rf"pytest.mark.polarion.*({polarion_project_id}-[0-9]+)",
        "\n".join(git_diff_lines().get(string_to_match, [])),
        re.MULTILINE | re.IGNORECASE,
    )


def get_polarion_project_id(project_id, config_file_path, util_name):
    polarion_project_id = project_id or get_util_config(util_name=util_name, config_file_path=config_file_path).get(
        "project_id"
    )
    if not polarion_project_id:
        LOGGER.error("Polarion project id must be passed via config file or command line")
        raise click.Abort()
    return polarion_project_id


def update_polarion_ids(project_id, is_automated, polarion_ids=None, is_approved=False):
    updated_ids = {}
    if polarion_ids:
        automation_status = AUTOMATED if is_automated else NOT_AUTOMATED

        from pylero.work_item import TestCase
        from pylero.exceptions import PyleroLibException

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
                LOGGER.warning(f"{id}: {polarion_exception}")
                updated_ids.setdefault("failed", []).append(id)
    return updated_ids
