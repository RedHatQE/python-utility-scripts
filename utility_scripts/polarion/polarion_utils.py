import logging
import re
import shlex
import subprocess

logging.basicConfig(level=logging.INFO)

LOGGER = logging.getLogger(__name__)
APPROVED = "approved"
AUTOMATED = "automated"


def git_diff():
    data = subprocess.check_output(shlex.split("git diff HEAD^-1"))
    data = data.decode("utf-8")
    return data


def get_test_case_ids(polarion_project_name, path="tests"):
    match_ids = set()

    ids = subprocess.check_output(shlex.split(f"grep -r pytest.mark.polarion {path}"))
    for line in ids.splitlines():
        match = re.findall(rf"{polarion_project_name}-[0-9]+", str(line))
        if match:
            match_ids.add(match[0])

    return match_ids


def find_polarion_ids(polarion_project_id, data):
    match_ids = set()
    for item in data:
        match = re.findall(rf"pytest.mark.polarion.*{polarion_project_id}-[0-9]+", item)
        if match:
            match_id = re.findall(rf"{polarion_project_id}-[0-9]+", match[0])
            match_ids.add(match_id[0])

    return match_ids


def set_approve(tc):
    tc.status = APPROVED
    tc.update()
    print(f"{tc.work_item_id} {APPROVED}")


def set_automated_and_approve(tc):
    print(f"{tc.work_item_id}: Not mark as automated, Setting 'automated'")
    tc.caseautomation = AUTOMATED
    set_approve(tc=tc)


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
