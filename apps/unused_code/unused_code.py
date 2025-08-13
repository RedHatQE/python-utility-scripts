from __future__ import annotations

import ast
import logging
import os
import re
import subprocess
import sys
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from functools import lru_cache
from typing import Any, Iterable

import click
from ast_comments import parse
from simple_logger.logger import get_logger

from apps.utils import ListParamType, all_python_files, get_util_config

LOGGER = get_logger(name=__name__)


@lru_cache(maxsize=1)
def _detect_supported_grep_flag() -> str:
    """Detect and cache a supported regex engine flag for git grep.

    Prefer PCRE ("-P") for proper \b handling; fall back to basic regex ("-G").
    Run a harmless grep to verify support and cache the first working flag.
    Uses lru_cache for thread-safe caching that runs only once per process.
    """
    candidate_flags = ["-P", "-G"]
    for flag in candidate_flags:
        try:
            # Use a trivial pattern to minimize output. We only care that the flag is accepted.
            # Discard stdout/stderr to prevent capturing large data in big repositories.
            probe_cmd = [
                "git",
                "grep",
                "-n",
                "--no-color",
                "--untracked",
                "-I",
                flag,
                "^$",  # match empty lines; success (rc=0) or no matches (rc=1) are both fine
            ]
            result = subprocess.run(probe_cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode in (0, 1):
                return flag
        except Exception:
            # Try next candidate
            pass

    raise RuntimeError(
        "git grep does not support '-P' (PCRE) or '-G' (basic regex) on this platform. "
        "Please ensure a compatible git/grep is installed."
    )


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
            # e.g. @pytest.fixture(...)
            if getattr(decorator.func, "attr", None) and getattr(decorator.func, "value", None):
                if decorator.func.attr == "fixture" and getattr(decorator.func.value, "id", None) == "pytest":
                    return True
            # e.g. from pytest import fixture; @fixture(...)
            if isinstance(decorator.func, ast.Name) and decorator.func.id == "fixture":
                return True
        # Case 2: @pytest.fixture (no parentheses)
        else:
            # e.g. @pytest.fixture
            if getattr(decorator, "attr", None) == "fixture" and getattr(decorator, "value", None):
                if getattr(decorator.value, "id", None) == "pytest":
                    return True
            # e.g. from pytest import fixture; @fixture
            if isinstance(decorator, ast.Name) and decorator.id == "fixture":
                return True
    return False


def _build_call_pattern(function_name: str) -> str:
    r"""Build a portable regex to match function call sites.

    Uses word boundary semantics based on the detected grep engine.
    The pattern is designed to match actual function calls while minimizing
    false positives from documentation patterns.
    - PCRE (-P):    \bname\s*[(]
    - Basic (-G):   \<name\s*[(]
    """
    flag = _detect_supported_grep_flag()
    if flag == "-P":
        return rf"\b{function_name}\s*[(]"
    # -G basic regex: use word-start token \< and literal '('
    return rf"\<{function_name}[[:space:]]*[(]"


def _build_fixture_param_pattern(function_name: str) -> str:
    r"""Build a portable regex to find a parameter named `function_name` in a def signature."""
    flag = _detect_supported_grep_flag()
    if flag == "-P":
        return rf"def\s+\w+\s*[(][^)]*\b{function_name}\b"
    # For -G (basic regex), avoid PCRE tokens; use POSIX classes and literals
    # def[space+][ident][ident*][space*]([^)]*\<name\>)
    return rf"def[[:space:]]+[[:alnum:]_][[:alnum:]_]*[[:space:]]*[(][^)]*\<{function_name}\>"


def _is_documentation_pattern(line: str, function_name: str) -> bool:
    """Check if a line contains a documentation pattern rather than a function call.

    Filters out common documentation patterns that include function names with parentheses:
    - Parameter descriptions: 'param_name (type): description'
    - Type annotations in docstrings
    - Inline documentation patterns
    - Lines within triple-quoted strings that aren't actual code

    Args:
        line: The line of code to check
        function_name: The function name we're searching for

    Returns:
        True if this appears to be documentation, False if it might be a function call
    """
    stripped_line = line.strip()

    # First, exclude obvious code patterns that should never be considered documentation
    # Skip control flow statements and common code patterns
    code_prefixes = ["if ", "elif ", "while ", "for ", "with ", "assert ", "return ", "yield ", "raise "]
    if any(stripped_line.startswith(prefix) for prefix in code_prefixes):
        return False

    # Skip function definitions (these are already filtered elsewhere but be extra safe)
    if stripped_line.startswith("def "):
        return False

    # Pattern 1: Parameter description format "name (type): description"
    # But be more specific - require either indentation or specific doc context
    # This catches patterns like "    namespace (str): The namespace of the pod."
    if re.search(rf"^\s+{re.escape(function_name)}\s*\([^)]*\)\s*:\s+\w", stripped_line):
        return True

    # Pattern 2: Lines that look like type annotations in docstrings
    # Must have descriptive text after the colon, not just code
    type_annotation_pattern = rf"\b{re.escape(function_name)}\s*\([^)]*\)\s*:\s+[A-Z][a-z]"
    if re.search(type_annotation_pattern, stripped_line):
        return True

    # Pattern 3: Lines that contain common documentation keywords near the function name
    doc_keywords = ["Args:", "Arguments:", "Parameters:", "Returns:", "Return:", "Raises:", "Note:", "Example:"]
    for keyword in doc_keywords:
        if keyword in stripped_line:
            # If we find doc keywords and function name with parens, likely documentation
            if f"{function_name}(" in stripped_line:
                return True
            # Also catch cases where the keyword line itself contains the function name
            if keyword.rstrip(":").lower() == function_name.lower():
                return True

    # Pattern 4: Check for common docstring patterns
    # Lines that start with common documentation patterns
    doc_starters = ['"""', "'''", "# ", "## ", "### ", "*", "-", "•"]
    if any(stripped_line.startswith(starter) for starter in doc_starters):
        if f"{function_name}(" in stripped_line:
            return True

    return False


def _git_grep(pattern: str) -> list[str]:
    """Run git grep with a pattern and return matching lines.

    - Uses dynamically detected regex engine (prefers PCRE ``-P``, falls back to basic ``-G``).
    - Includes untracked files so local changes are considered.
    - Return an empty list when no matches are found (rc=1).
    - Raise on other non-zero exit codes.
    """
    cmd = [
        "git",
        "grep",
        "-n",  # include line numbers
        "--no-color",
        "--untracked",
        "-I",  # ignore binary files
        _detect_supported_grep_flag(),
        "-e",  # safely handle patterns starting with dash
        pattern,
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode == 0:
        return [line for line in result.stdout.splitlines() if line]
    # rc=1 means no matches were found
    if result.returncode == 1:
        return []

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
        for entry in _git_grep(pattern=_build_call_pattern(function_name=func.name)):
            # git grep -n output format: path:line-number:line-content
            # Use split to properly handle git grep format: first colon separates path from line number,
            # second colon separates line number from content
            parts = entry.split(":", 2)
            if len(parts) != 3:
                continue
            _, _, _line = parts

            # ignore its own definition
            if f"def {func.name}" in _line:
                continue

            # ignore commented lines
            if _line.strip().startswith("#"):
                continue

            # Filter out documentation patterns that aren't actual function calls
            # This prevents false positives from docstrings, parameter descriptions, etc.
            if _is_documentation_pattern(_line, func.name):
                continue

            if func.name in _line:
                used = True
                break

        # If not found and it's a pytest fixture, also search for parameter usage in function definitions
        if not used and is_pytest_fixture(func=func):
            param_pattern = _build_fixture_param_pattern(function_name=func.name)
            for entry in _git_grep(pattern=param_pattern):
                # git grep -n output format: path:line-number:line-content
                # Use split to properly handle git grep format: first colon separates path from line number,
                # second colon separates line number from content
                parts = entry.split(":", 2)
                if len(parts) != 3:
                    continue
                _path, _lineno, _line = parts

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

    # Pre-flight grep flag detection to fail fast with clear error if unsupported
    try:
        detected_flag = _detect_supported_grep_flag()
        LOGGER.debug(f"Using git grep flag: {detected_flag}")
    except RuntimeError as e:
        LOGGER.error(str(e))
        sys.exit(1)

    # res = process_file(
    #     py_file="tests/unused_code/unused_code_file_for_test.py",
    #     func_ignore_prefix=func_ignore_prefix,
    #     file_ignore_list=file_ignore_list,
    # )
    # __import__("ipdb").set_trace()
    # return
    with ThreadPoolExecutor() as executor:
        for py_file in all_python_files():
            future = executor.submit(
                process_file,
                py_file=py_file,
                func_ignore_prefix=func_ignore_prefix,
                file_ignore_list=file_ignore_list,
            )
            jobs[future] = py_file

        processing_errors: list[str] = []
        for future in as_completed(jobs):
            try:
                if unused_func := future.result():
                    unused_functions.append(unused_func)
            except Exception as exc:
                processing_errors.append(f"{jobs[future]}: {exc}")

        if processing_errors:
            joined = "\n".join(processing_errors)
            LOGGER.error(f"One or more files failed to process:\n{joined}")
            sys.exit(2)

    if unused_functions:
        # Sort output for deterministic CI logs
        sorted_output = sorted(unused_functions)
        click.echo("\n".join(sorted_output))
        sys.exit(1)


if __name__ == "__main__":
    get_unused_functions()
