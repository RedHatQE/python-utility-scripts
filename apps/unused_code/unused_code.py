from __future__ import annotations

import ast
import logging
import os
import subprocess
import sys
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, Iterable

import click
from ast_comments import parse
from simple_logger.logger import get_logger

from apps.utils import ListParamType, all_python_files, get_util_config

LOGGER = get_logger(name=__name__)


def is_fixture_autouse(func: ast.FunctionDef) -> bool:
    deco_list: list[Any] = func.decorator_list
    for deco in deco_list or []:
        if not hasattr(deco, "func"):
            continue

        if getattr(deco.func, "attr", None) and getattr(deco.func, "value", None):
            if deco.func.attr == "fixture" and deco.func.value.id == "pytest":
                for _key in deco.keywords:
                    if _key.arg == "autouse":
                        return True
    return False


def is_pytest_fixture(func: ast.FunctionDef) -> bool:
    """Return True if the function is decorated with @pytest.fixture.

    Detects any pytest fixture regardless of parameters (scope, autouse, etc.).
    """
    decorators: list[Any] = func.decorator_list
    for decorator in decorators or []:
        # Case 1: @pytest.fixture(...)
        if hasattr(decorator, "func"):
            if getattr(decorator.func, "attr", None) and getattr(decorator.func, "value", None):
                if decorator.func.attr == "fixture" and getattr(decorator.func.value, "id", None) == "pytest":
                    return True
        # Case 2: @pytest.fixture (no parentheses)
        else:
            if getattr(decorator, "attr", None) == "fixture" and getattr(decorator, "value", None):
                if getattr(decorator.value, "id", None) == "pytest":
                    return True
    return False


def _git_grep(pattern: str) -> list[str]:
    """Run git grep with a pattern and return matching lines.

    - Includes untracked files so local changes are considered.
    - Treats exit code 1 (no matches) as an empty result.
    - Any other non-zero exit code is logged at debug level and treated as empty.
    """
    cmd = [
        "git",
        "grep",
        "-n",  # include line numbers
        "--no-color",
        "--untracked",
        "-wE",
        pattern,
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode == 0:
        return [line for line in result.stdout.splitlines() if line]
    if result.returncode == 1:
        # no matches
        LOGGER.debug(f"git grep returned no matches for pattern: {pattern}")
        return []

    # Unexpected error: propagate to caller so the CLI can exit with a non-zero code cleanly
    error_message = result.stderr.strip() or "Unknown git grep error"
    raise RuntimeError(f"git grep failed (rc={result.returncode}) for pattern {pattern!r}: {error_message}")


def _iter_functions(tree: ast.Module) -> Iterable[ast.FunctionDef]:
    """
    Get all function from python file
    """
    for elm in tree.body:
        if isinstance(elm, ast.FunctionDef):
            if elm.name.startswith("test_"):
                continue

            yield elm


def is_ignore_function_list(ignore_prefix_list: list[str], function: ast.FunctionDef) -> bool:
    ignore_function_lists = [
        function.name for ignore_prefix in ignore_prefix_list if function.name.startswith(ignore_prefix)
    ]
    if ignore_function_lists:
        return True

    return False


def process_file(py_file: str, func_ignore_prefix: list[str], file_ignore_list: list[str]) -> str:
    if os.path.basename(py_file) in file_ignore_list:
        LOGGER.debug(f"Skipping file: {py_file}")
        return ""

    with open(py_file) as fd:
        tree = parse(source=fd.read())

    unused_messages: list[str] = []

    for func in _iter_functions(tree=tree):
        if func_ignore_prefix and is_ignore_function_list(ignore_prefix_list=func_ignore_prefix, function=func):
            LOGGER.debug(f"Skipping function: {func.name}")
            continue

        if is_fixture_autouse(func=func):
            LOGGER.debug(f"Skipping `autouse` fixture function: {func.name}")
            continue

        if any(getattr(item, "value", None) == "# skip-unused-code" for item in func.body):
            LOGGER.debug(f"Skipping function {func.name}: found `# skip-unused-code`")
            continue

        used = False

        # First, look for call sites: function_name(...)
        for entry in _git_grep(pattern=f"{func.name}(.*)"):
            _, _line = entry.split(":", 1)

            # ignore its own definition
            if f"def {func.name}" in _line:
                continue

            # ignore commented lines
            if _line.strip().startswith("#"):
                continue

            if func.name in _line:
                used = True
                break

        # If not found and it's a pytest fixture, also search for parameter usage in function definitions
        if not used and is_pytest_fixture(func=func):
            param_pattern = rf"def\s+\w+\s*\([^)]*\b{func.name}\b"
            for entry in _git_grep(pattern=param_pattern):
                _, _line = entry.split(":", 1)

                # ignore commented lines
                if _line.strip().startswith("#"):
                    continue

                used = True
                break

        if not used:
            unused_messages.append(
                f"{os.path.relpath(py_file)}:{func.name}:{func.lineno}:{func.col_offset} Is not used anywhere in the code."
            )

    return "\n".join(unused_messages)


@click.command()
@click.option(
    "--config-file-path",
    help="Provide absolute path to the config file. Any CLI option(s) would override YAML file",
    type=click.Path(),
    default=os.path.expanduser("~/.config/python-utility-scripts/config.yaml"),
)
@click.option(
    "--exclude-files",
    help="Provide a comma-separated string or list of files to exclude",
    type=ListParamType(),
)
@click.option(
    "--exclude-function-prefixes",
    help="Provide a comma-separated string or list of function prefixes to exclude",
    type=ListParamType(),
)
@click.option("--verbose", "-v", default=False, is_flag=True)
def get_unused_functions(
    config_file_path: str, exclude_files: list[str], exclude_function_prefixes: list[str], verbose: bool
) -> None:
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)

    unused_functions: list[str] = []
    unused_code_config = get_util_config(util_name="pyutils-unusedcode", config_file_path=config_file_path)
    func_ignore_prefix = exclude_function_prefixes or unused_code_config.get("exclude_function_prefix", [])
    file_ignore_list = exclude_files or unused_code_config.get("exclude_files", [])

    jobs: dict[Future, str] = {}
    if not os.path.exists(".git"):
        LOGGER.error("Must be run from a git repository")
        sys.exit(1)

    with ThreadPoolExecutor() as executor:
        for py_file in all_python_files():
            future = executor.submit(
                process_file,
                py_file=py_file,
                func_ignore_prefix=func_ignore_prefix,
                file_ignore_list=file_ignore_list,
            )
            jobs[future] = py_file

        for future in as_completed(jobs):
            try:
                if unused_func := future.result():
                    unused_functions.append(unused_func)
            except Exception as exc:
                LOGGER.error(f"Failed to process file {jobs[future]}: {exc}")
                sys.exit(2)

    if unused_functions:
        click.echo("\n".join(unused_functions))
        sys.exit(1)


if __name__ == "__main__":
    get_unused_functions()
