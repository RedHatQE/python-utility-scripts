from __future__ import annotations

import ast
import logging
import os
import subprocess
import sys
import tokenize
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from io import StringIO
from typing import Any, Iterable

import click
from simple_logger.logger import get_logger

from apps.utils import ListParamType, all_python_files, get_util_config

LOGGER = get_logger(name=__name__)
SKIP_COMMENT = "# skip-unused-code"


def extract_inline_function_comments(source_code: str) -> dict[str, list[str]]:
    """
    Finds *only* inline comments for function definition that match `SKIP_COMMENT` and returns them
    """
    # Tokenize the source code to find comments
    tokens = tokenize.generate_tokens(StringIO(source_code).readline)

    # To store the comments for each function
    prev_token = None
    comments = {}
    def_tok = False

    # Process the tokens and extract comments
    for token in tokens:
        tok_type, tok_string, _, _, _ = token

        # Detect the start of a new function definition
        if tok_type == tokenize.NAME and tok_string == "def":
            def_tok = True

        elif tok_type == tokenize.NAME and def_tok:
            # First "NAME" token after a "def" will be the function name
            prev_token = token
            def_tok = False

        elif tok_type == tokenize.NEWLINE and prev_token:
            # we found a function name and this is the first logical newline after it
            # if no comment has been found it means that anything that comes after could be within the function
            # or outside of it, which is outside the scope of what we are looking for. we can empty prev_token.
            # note that tokenize.NL would be a different (non-logical) newline, e.g. a multi-line function def
            # which is thus still handled correctly.
            # Not handling this here can cause comments outside the scope of the function to be mishandled, e.g.
            # ------------
            # def foo():
            #      pass
            #
            # # my-comment
            # def bar():
            # ------------
            # would return "# my-comment" as a foo() comment
            prev_token = None

        # If this is the comment we look for, and it comes after a function definition
        elif tok_type == tokenize.COMMENT and prev_token and tok_string == SKIP_COMMENT:
            LOGGER.debug(f"found comment for function def: {prev_token.line.strip()}")
            LOGGER.debug(f"comment is: {tok_string}")
            func_name = prev_token.string
            comments[func_name] = [tok_string]
            prev_token = None

    return comments


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
    if os.path.relpath(py_file) in file_ignore_list:
        LOGGER.debug(f"Skipping file: {py_file}")
        return ""

    with open(py_file) as fd:
        tree = ast.parse(source=fd.read())

    with open(py_file) as fd:
        comments = extract_inline_function_comments(source_code=fd.read())

    found = []
    for func in _iter_functions(tree=tree):
        if func.name in comments.keys():
            LOGGER.debug(f"Skipping function due to comment: {func.name}")
            continue

        if func_ignore_prefix and is_ignore_function_list(ignore_prefix_list=func_ignore_prefix, function=func):
            LOGGER.debug(f"Skipping function: {func.name}")
            continue

        if is_fixture_autouse(func=func):
            LOGGER.debug(f"Skipping `autouse` fixture function: {func.name}")
            continue

        used = False
        _func_grep_found = subprocess.check_output(["git", "grep", "-w", func.name], shell=False)

        for entry in _func_grep_found.decode().splitlines():
            _, _line = entry.split(":", 1)

            if f"def {func.name}" in _line:
                continue

            if _line.strip().startswith("#"):
                continue

            if _line.strip().startswith("assert"):
                # if the function is only called from a test assert statement do not count it
                continue

            if func.name in _line:
                used = True
                break

        if not used:
            # store all unused functions in the file
            found.append(
                f"{os.path.relpath(py_file)}:{func.name}:{func.lineno}:{func.col_offset} Is not used anywhere in the code.\n"
            )

    # return all unused functions if any
    if len(found) > 0:
        return "".join(found)

    return ""


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

    jobs: list[Future] = []
    if not os.path.exists(".git"):
        LOGGER.error("Must be run from a git repository")
        sys.exit(1)

    with ThreadPoolExecutor() as executor:
        for py_file in all_python_files():
            jobs.append(
                executor.submit(
                    process_file,
                    py_file=py_file,
                    func_ignore_prefix=func_ignore_prefix,
                    file_ignore_list=file_ignore_list,
                )
            )

        for result in as_completed(jobs):
            if unused_func := result.result():
                unused_functions.append(unused_func)

    if unused_functions:
        click.echo("\n".join(unused_functions))
        sys.exit(1)


if __name__ == "__main__":
    get_unused_functions()
