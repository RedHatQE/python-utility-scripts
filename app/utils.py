import os
from simple_logger.logger import get_logger

LOGGER = get_logger(name=__name__)


def all_python_files(repo_location):
    exclude_dirs = [".tox", "venv", ".pytest_cache", "site-packages", ".git"]
    LOGGER.info(repo_location)
    for root, _, files in os.walk(os.path.abspath(repo_location)):
        if [_dir for _dir in exclude_dirs if _dir in root]:
            continue

        for filename in files:
            if filename.endswith(".py") and filename != os.path.split(__file__)[-1]:
                yield os.path.join(root, filename)
