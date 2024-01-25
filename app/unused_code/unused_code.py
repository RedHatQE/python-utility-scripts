import ast
import os
import subprocess
import sys

from simple_logger.logger import get_logger
import urllib3
from app.utils import all_python_files

urllib3.disable_warnings()
LOGGER = get_logger(name=__name__)


def is_fixture_autouse(func):
    if func.decorator_list:
        for deco in func.decorator_list:
            if not hasattr(deco, "func"):
                continue
            if deco.func.attr == "fixture" and deco.func.value.id == "pytest":
                for _key in deco.keywords:
                    if _key.arg == "autouse":
                        return _key.value.s


def _iter_functions(tree):
    """
    Get all function from python file
    """

    def is_func(_elm):
        return isinstance(_elm, ast.FunctionDef)

    def is_test(_elm):
        return _elm.name.startswith("test_")

    for elm in tree.body:
        if is_func(_elm=elm):
            if is_test(_elm=elm):
                continue

            yield elm


def get_unused_functions():
    _unused_functions = []
    func_ignore_prefix = ["pytest_"]
    for py_file in all_python_files("."):
        LOGGER.info(f"Looking at: {py_file}")
        if os.path.basename(py_file) == "pytest_matrix_utils.py":
            continue

        with open(py_file) as fd:
            tree = ast.parse(source=fd.read())

        for func in _iter_functions(tree=tree):
            if [func.name for ignore_prefix in func_ignore_prefix if func.name.startswith(ignore_prefix)]:
                continue

            if is_fixture_autouse(func=func):
                continue

            _used = subprocess.check_output(
                f"git grep -w '{func.name}' | wc -l",
                shell=True,
            )
            used = int(_used.strip())
            if used < 2:
                _unused_functions.append(
                    f"{os.path.relpath(py_file)}:{func.name}:{func.lineno}:{func.col_offset} Is"
                    " not used anywhere in the code.",
                )
    if _unused_functions:
        LOGGER.error("\n".join(_unused_functions))
        sys.exit(1)


if __name__ == "__main__":
    get_unused_functions()