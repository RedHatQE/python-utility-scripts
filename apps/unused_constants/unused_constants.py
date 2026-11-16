from __future__ import annotations

import ast
import fnmatch
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Generator, Iterable
from contextlib import contextmanager

import click
from ast_comments import parse
from simple_logger.logger import get_logger

from apps.git_grep_utils import (
    detect_supported_grep_flag as _detect_supported_grep_flag,
)
from apps.git_grep_utils import (
    find_git_root as _find_git_root,
)
from apps.git_grep_utils import (
    resolve_absolute_path as _resolve_absolute_path,
)
from apps.git_grep_utils import wrap_grep_word
from apps.utils import ListParamType, all_python_files, get_util_config

LOGGER = get_logger(name=__name__)

UPPERCASE_CONSTANT_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SKIP_UNUSED_CODE_MARKER = "# skip-unused-code"


def _iter_target_names(target: ast.expr) -> Iterable[ast.Name]:
    """Yield every Name bound by an assignment target, including unpacking."""
    if isinstance(target, ast.Name):
        yield target
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _iter_target_names(target=element)
    elif isinstance(target, ast.Starred):
        yield from _iter_target_names(target=target.value)


def _iter_module_level_constants(tree: ast.Module) -> Iterable[tuple[str, int, int]]:
    """Yield (name, lineno, col_offset) for module-level bindings.

    Descends into module-level if/try/with; not into class or function bodies.
    """
    constants: list[tuple[str, int, int]] = []
    pending: list[ast.stmt] = list(tree.body)

    while pending:
        element = pending.pop()

        if isinstance(element, (ast.If, ast.Try, ast.With)):
            for block in ("body", "orelse", "finalbody"):
                pending.extend(getattr(element, block, None) or [])
            for handler in getattr(element, "handlers", None) or []:
                pending.extend(handler.body)
            continue

        if isinstance(element, ast.Assign):
            for target in element.targets:
                for name in _iter_target_names(target=target):
                    constants.append((name.id, element.lineno, name.col_offset))
        elif isinstance(element, ast.AnnAssign) and isinstance(element.target, ast.Name):
            constants.append((element.target.id, element.lineno, element.target.col_offset))

    # Nested blocks are visited out of order; sort so callers see source order and
    # the first binding of a repeated name is the earliest one in the file.
    yield from sorted(constants, key=lambda constant: (constant[1], constant[2]))


def _has_skip_unused_code_comment(source_lines: list[str], lineno: int) -> bool:
    if lineno < 1 or lineno > len(source_lines):
        return False
    return SKIP_UNUSED_CODE_MARKER in source_lines[lineno - 1]


def _is_uppercase_constant(name: str) -> bool:
    return bool(UPPERCASE_CONSTANT_RE.match(name))


COMPARISON_OPERATOR_TAIL_RE = re.compile(r"[=!<>+\-*/%&|^~]$|//$|\*\*$|>>$|<<$")


def _is_constant_definition_line(line_content: str, constant_name: str) -> bool:
    """Return True when the line binds `constant_name` rather than reading it."""
    code_part = line_content.split("#", 1)[0].strip()

    name_colon = re.match(rf"^{re.escape(constant_name)}\s*:", code_part)
    if name_colon:
        rest = code_part[name_colon.end() :]
        equals_index = rest.find("=")
        if equals_index != -1 and "(" not in rest[:equals_index]:
            # `TIMEOUT: int = 300`, not dict keys like `KEY: func(kw=val)`.
            return True

    if re.match(rf"^{re.escape(constant_name)}\s*=", code_part):
        return True

    left_hand_side, separator, right_hand_side = code_part.partition("=")
    if not separator or not right_hand_side:
        return False

    # Reject comparisons and augmented assignment: `A == B`, `A != B`, `A += 1`.
    if COMPARISON_OPERATOR_TAIL_RE.search(left_hand_side.rstrip()) or right_hand_side.startswith("="):
        return False

    targets = {target.strip().strip("()[]").lstrip("*").strip() for target in left_hand_side.split(",")}
    return constant_name in targets


def _relative_path(py_file: str) -> str:
    resolved_file = os.path.realpath(py_file)
    return os.path.relpath(resolved_file, _find_git_root(resolved_file))


def _resolve_repository_root(target: str) -> str | None:
    """Return the git repository root containing `target`, or None."""
    directory = target if os.path.isdir(target) else os.path.dirname(os.path.abspath(target))
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            cwd=directory or ".",
        )
    except (OSError, subprocess.SubprocessError) as error:
        LOGGER.debug(f"git rev-parse failed for {directory}: {error}")
        return None

    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _file_matches_include_list(py_file: str, include_files: list[str]) -> bool:
    """Match a file against git-root-relative --include-files paths or globs."""
    if not include_files:
        return True

    relative_path = _relative_path(py_file=py_file)
    for include_path in include_files:
        normalized = include_path.strip().lstrip("./")
        if relative_path == normalized or fnmatch.fnmatch(relative_path, normalized):
            return True
    return False


def _is_name_excluded(name: str, exclude_names: list[str]) -> bool:
    return name in exclude_names


def collect_file_constants(
    py_file: str,
    exclude_names: list[str],
    uppercase_only: bool,
    file_ignore_list: list[str],
) -> list[tuple[str, int, int]]:
    """Return (name, lineno, col_offset) module-level constant candidates for one file."""
    if os.path.basename(py_file) in file_ignore_list:
        LOGGER.debug(f"Skipping file: {py_file}")
        return []

    # A file that cannot be parsed is skipped with a warning rather than failing
    # the run: real repositories contain generated, vendored and templated
    # sources, and one of them must not decide the exit code for everything else.
    try:
        with open(py_file, encoding="utf-8") as file_descriptor:
            source = file_descriptor.read()
        tree = parse(source=source)
    except (SyntaxError, UnicodeDecodeError, ValueError) as error:
        LOGGER.warning(f"Skipping {py_file}: cannot parse ({error})")
        return []

    source_lines = source.splitlines()
    candidates: list[tuple[str, int, int]] = []
    seen_names: set[str] = set()

    for constant_name, lineno, col_offset in _iter_module_level_constants(tree=tree):
        # A name bound more than once at module level is one constant; report the
        # first binding only.
        if constant_name in seen_names:
            continue
        seen_names.add(constant_name)

        if _is_name_excluded(name=constant_name, exclude_names=exclude_names):
            LOGGER.debug(f"Skipping excluded constant: {constant_name}")
            continue

        if uppercase_only and not _is_uppercase_constant(name=constant_name):
            LOGGER.debug(f"Skipping non-uppercase constant: {constant_name}")
            continue

        if _has_skip_unused_code_comment(source_lines=source_lines, lineno=lineno):
            LOGGER.debug(f"Skipping constant {constant_name}: found `{SKIP_UNUSED_CODE_MARKER}`")
            continue

        candidates.append((constant_name, lineno, col_offset))

    return candidates


def _usage_search_pattern(candidate_names: Iterable[str]) -> str:
    r"""Build a git grep pattern that may match any candidate name.

    Uses a single character class per run (tokenise hits in Python). For -G,
    any-identifier uses \w\+ because + is literal in POSIX basic regex.
    """
    all_uppercase = all(_is_uppercase_constant(name=name) for name in candidate_names)
    flag = _detect_supported_grep_flag()
    if all_uppercase:
        body = "[A-Z][A-Z0-9_]*"
    elif flag == "-P":
        body = r"\w+"
    else:
        # -G basic regex: + is literal, so the quantifier is \+
        body = r"\w\+"
    return wrap_grep_word(body, flag=flag)


def find_used_constants(
    constants_by_file: dict[str, set[str]],
    repository_root: str | None = None,
) -> set[tuple[str, str]]:
    """Return (absolute file, constant name) pairs that have a usage.

    One streamed git grep for all candidates. Binding lines in the defining file
    are not usages. ``repository_root`` defaults from the given files; the
    caller's working directory need not be inside the repository.
    """
    names_to_files: dict[str, list[str]] = defaultdict(list)
    for py_file_absolute, names in constants_by_file.items():
        for name in names:
            names_to_files[name].append(py_file_absolute)

    if not names_to_files:
        return set()

    if repository_root is None:
        first_file = next(iter(constants_by_file))
        repository_root = _resolve_repository_root(target=first_file)
        if repository_root is None:
            raise RuntimeError(f"{first_file} is not inside a git repository.")

    if os.path.abspath(os.getcwd()) == os.path.abspath(repository_root):
        return _scan_for_usage(names_to_files=names_to_files)

    with _working_directory(path=repository_root):
        return _scan_for_usage(names_to_files=names_to_files)


def _scan_for_usage(names_to_files: dict[str, list[str]]) -> set[tuple[str, str]]:
    used: set[tuple[str, str]] = set()
    command = [
        "git",
        "grep",
        "-n",
        "--no-color",
        "--untracked",
        "-I",
        _detect_supported_grep_flag(),
        "-e",
        _usage_search_pattern(candidate_names=names_to_files),
    ]

    # stderr goes to a temp file, not a second pipe: draining stdout to
    # completion before reading stderr would deadlock if git ever filled the
    # stderr pipe buffer first.
    with (
        tempfile.TemporaryFile(mode="w+") as stderr_file,
        subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr_file, text=True) as process,
    ):
        assert process.stdout is not None
        for raw_line in process.stdout:
            parts = raw_line.rstrip("\n").split(":", 2)
            if len(parts) != 3:
                continue
            grep_path, _grep_lineno, grep_line = parts

            if grep_line.strip().startswith("#"):
                continue
            code_part = grep_line.split("#", 1)[0]

            hit_absolute: str | None = None
            for token in IDENTIFIER_RE.findall(code_part):
                defining_files = names_to_files.get(token)
                if not defining_files:
                    continue
                if hit_absolute is None:
                    hit_absolute = os.path.realpath(
                        _resolve_absolute_path(git_grep_path=grep_path, reference_file=grep_path)
                    )
                for defining_file in defining_files:
                    if (defining_file, token) in used:
                        continue
                    if hit_absolute == os.path.realpath(defining_file) and _is_constant_definition_line(
                        line_content=code_part, constant_name=token
                    ):
                        continue
                    used.add((defining_file, token))

        process.wait()
        stderr_file.seek(0)
        stderr = stderr_file.read()

    # rc=1 means "no matches", which is a legitimate answer, not a failure.
    if process.returncode not in (0, 1):
        error_message = stderr.strip() or "unknown error"
        raise RuntimeError(f"git grep failed (rc={process.returncode}): {error_message}")

    return used


def process_file_constants(
    py_file: str,
    exclude_names: list[str],
    uppercase_only: bool,
    file_ignore_list: list[str],
) -> str:
    """Report the unused module-level constants of a single file."""
    candidates = collect_file_constants(
        py_file=py_file,
        exclude_names=exclude_names,
        uppercase_only=uppercase_only,
        file_ignore_list=file_ignore_list,
    )
    if not candidates:
        return ""

    py_file_absolute = os.path.abspath(py_file)
    used = find_used_constants(constants_by_file={py_file_absolute: {name for name, _, _ in candidates}})

    return "\n".join(
        _format_findings(py_file=py_file, candidates=candidates, used=used, py_file_absolute=py_file_absolute)
    )


def _format_findings(
    py_file: str,
    candidates: list[tuple[str, int, int]],
    used: set[tuple[str, str]],
    py_file_absolute: str,
) -> list[str]:
    # Repository-root relative, matching --include-files, so one run does not mix
    # path bases depending on where it was invoked from.
    relative_path = _relative_path(py_file=py_file)
    return [
        f"{relative_path}:{constant_name}:{lineno}:{col_offset} No usage found."
        for constant_name, lineno, col_offset in candidates
        if (py_file_absolute, constant_name) not in used
    ]


@contextmanager
def _working_directory(path: str) -> Generator[None, None, None]:
    """Temporarily chdir to `path`, then restore the previous working directory."""
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _iter_files_to_process(
    file_path: str | None,
    directory: str | None,
    include_files: list[str],
    file_ignore_list: list[str],
) -> Iterable[str]:
    if file_path:
        yield file_path
        return

    for py_file in all_python_files(directory=directory):
        if os.path.basename(py_file) in file_ignore_list:
            continue
        if not _file_matches_include_list(py_file=py_file, include_files=include_files):
            continue
        yield py_file


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
    "--exclude-names",
    help="Provide a comma-separated string or list of constant names to exclude",
    type=ListParamType(),
)
@click.option(
    "--include-files",
    help="Provide a comma-separated string or list of files to include (paths relative to git root)",
    type=ListParamType(),
)
@click.option(
    "--uppercase-only/--all-names",
    # Three-state: None means "not given on the command line", which is what lets
    # the config value apply while an explicit flag still wins over it.
    default=None,
    help="When enabled, only check ALL_CAPS module-level names (default: enabled)",
)
@click.option("--verbose", "-v", default=False, is_flag=True)
@click.option(
    "--file-path",
    "-f",
    help="Analyze a single Python file for unused module-level constants. Must be an existing .py file.",
    type=click.Path(exists=True),
)
@click.option(
    "--directory",
    "-d",
    help=(
        "Analyze Python files in a directory recursively for unused module-level constants. "
        "Must be an existing directory."
    ),
    type=click.Path(exists=True, dir_okay=True),
)
def get_unused_constants(
    config_file_path: str,
    exclude_files: list[str],
    exclude_names: list[str],
    include_files: list[str],
    uppercase_only: bool | None,
    verbose: bool,
    file_path: click.Path,
    directory: click.Path,
) -> None:
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)

    if file_path and not os.path.isfile(str(file_path)):
        LOGGER.error("File path must be a file, not a directory.")
        sys.exit(1)

    if directory and not os.path.isdir(str(directory)):
        LOGGER.error("Directory must be a directory, not a file.")
        sys.exit(1)

    unused_constants_config = get_util_config(util_name="pyutils-unusedconstants", config_file_path=config_file_path)
    file_ignore_list = exclude_files or unused_constants_config.get("exclude_files", [])
    exclude_name_list = exclude_names or unused_constants_config.get("exclude_names", [])
    include_file_list = include_files or unused_constants_config.get("include_files", [])

    # An explicit --uppercase-only/--all-names wins over the config file, which
    # is what --config-file-path's help text promises.
    if uppercase_only is None:
        uppercase_only = bool(unused_constants_config.get("uppercase_only", True))

    unused_constants: list[str] = []

    analysis_target = os.path.abspath(str(file_path or directory or os.curdir))
    repository_root = _resolve_repository_root(target=analysis_target)
    if repository_root is None:
        LOGGER.error("Must be run from a git repository")
        sys.exit(1)

    # Targets are absolute by now, so they survive the move to the repository root.
    resolved_file_path = analysis_target if file_path else None
    resolved_directory = analysis_target if directory else None

    with _working_directory(path=repository_root):
        try:
            detected_flag = _detect_supported_grep_flag()
            LOGGER.debug(f"Using git grep flag: {detected_flag}")
        except RuntimeError as error:
            LOGGER.error(str(error))
            sys.exit(1)

        files_to_process = list(
            _iter_files_to_process(
                file_path=resolved_file_path,
                directory=resolved_directory,
                include_files=include_file_list,
                file_ignore_list=file_ignore_list,
            )
        )

        if not files_to_process:
            # Not a finding: an empty selection after --exclude-files/--include-files
            # must not look like "unused constants were found".
            LOGGER.error("No Python files selected for analysis.")
            sys.exit(1)

        # Collect every candidate first, then resolve usage for all of them in one
        # repository scan. Parsing is the only per-file work left, so there is
        # nothing for a thread pool to overlap.
        candidates_by_file: dict[str, list[tuple[str, int, int]]] = {}
        for py_file in files_to_process:
            candidates = collect_file_constants(
                py_file=py_file,
                exclude_names=exclude_name_list,
                uppercase_only=uppercase_only,
                file_ignore_list=file_ignore_list,
            )
            if candidates:
                candidates_by_file[py_file] = candidates

        try:
            used = find_used_constants(
                constants_by_file={
                    os.path.abspath(py_file): {name for name, _, _ in candidates}
                    for py_file, candidates in candidates_by_file.items()
                },
                repository_root=repository_root,
            )
        except RuntimeError as error:
            LOGGER.error(str(error))
            sys.exit(1)

        for py_file, candidates in candidates_by_file.items():
            unused_constants.extend(
                _format_findings(
                    py_file=py_file,
                    candidates=candidates,
                    used=used,
                    py_file_absolute=os.path.abspath(py_file),
                )
            )

        if unused_constants:
            click.echo("\n".join(sorted(unused_constants)))
            raise SystemExit(1)


if __name__ == "__main__":
    get_unused_constants()
