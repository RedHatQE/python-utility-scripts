import logging
import os
import re
import shlex
import subprocess

from app.utils import get_repo_dir, delete_temp_dir

logging.basicConfig(level=logging.INFO)

LOGGER = logging.getLogger(__name__)
APPROVED = "approved"
AUTOMATED = "automated"


def get_test_case_ids(polarion_project_name, path="tests"):
    match_ids = set()
    os.chdir(path=get_repo_dir())

    ids = subprocess.check_output(
        shlex.split(f"grep -r pytest.mark.polarion {path}")
    )
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


def git_diff(repo_ssh_link):
    file_path = get_repo_dir(repo_ssh_link=repo_ssh_link)
    os.chdir(path=file_path)
    data = subprocess.check_output(shlex.split("git diff HEAD^-1"))
    data = data.decode("utf-8")
    delete_temp_dir(dirpath=file_path)
    return data


def set_approve(tc):
    tc.status = APPROVED
    tc.update()
    print(f"{tc.work_item_id} {APPROVED}")


def set_automated_and_approve(tc):
    print(f"{tc.work_item_id}: Not mark as automated, Setting 'automated'")
    tc.caseautomation = AUTOMATED
    set_approve(tc=tc)


def git_diff_added_removed_lines(repo_ssh_link):
    diff = {}
    for line in git_diff(repo_ssh_link=repo_ssh_link).splitlines():
        LOGGER.info(line)
        if line.startswith("+"):
            diff.setdefault("added", []).append(line)

        if line.startswith("-"):
            diff.setdefault("removed", []).append(line)

    return diff


def get_polarion_ids_from_diff(diff, polarion_project_id):
    added_ids = find_polarion_ids(data=diff.get("added", []),polarion_project_id=polarion_project_id)
    removed_ids = find_polarion_ids(data=diff.get("removed", []), polarion_project_id=polarion_project_id)
    return added_ids, removed_ids
