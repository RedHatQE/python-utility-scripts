from __future__ import annotations

import logging
import os
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest
from ast_comments import parse
from click.testing import Result
from simple_logger.logger import get_logger

from apps.unused_constants.unused_constants import (
    _file_matches_include_list,
    _has_skip_unused_code_comment,
    _is_constant_definition_line,
    _is_name_excluded,
    _is_uppercase_constant,
    _iter_module_level_constants,
    _usage_search_pattern,
    collect_file_constants,
    find_used_constants,
    process_file_constants,
)

LOGGER = get_logger(name=__name__)

CONSTANTS_MODULE = """
    USED_CONSTANT = "used"
    UNUSED_CONSTANT = "unused"
"""

CONSUMER_MODULE = """
    from constants import USED_CONSTANT


    def consume() -> str:
        return USED_CONSTANT
"""


def _reported_names(result: Result) -> set[str]:
    """Constant names from `path:NAME:line:col No usage found.` output lines."""
    return {line.split(":")[1] for line in result.output.splitlines() if "No usage found." in line}


# --------------------------------------------------------------------------- #
# Detection, against a real repository
# --------------------------------------------------------------------------- #


def test_reports_only_the_unused_constant(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    git_repository({"constants.py": CONSTANTS_MODULE, "consumer.py": CONSUMER_MODULE})

    result = run_unused_constants()

    assert result.exit_code == 1
    assert _reported_names(result=result) == {"UNUSED_CONSTANT"}


def test_clean_repository_exits_zero(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    git_repository({"constants.py": 'USED_CONSTANT = "used"\n', "consumer.py": CONSUMER_MODULE})

    result = run_unused_constants()

    assert result.exit_code == 0
    assert not result.output.strip()


def test_reports_unpacking_targets(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    """`A, B = 1, 2` binds two constants; both are module level and unused."""
    git_repository({"constants.py": "FIRST, SECOND = 1, 2\n"})

    result = run_unused_constants()

    assert result.exit_code == 1
    assert _reported_names(result=result) == {"FIRST", "SECOND"}


def test_reports_nested_and_starred_unpacking_targets(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    git_repository({"constants.py": "OUTER, (LEFT, RIGHT) = 1, (2, 3)\nHEAD, *TAIL = [1, 2, 3]\n"})

    result = run_unused_constants()

    assert _reported_names(result=result) == {"OUTER", "LEFT", "RIGHT", "HEAD", "TAIL"}


def test_reports_constants_defined_in_module_level_blocks(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    """Constants are routinely bound inside module-level if/try/with."""
    git_repository({
        "constants.py": """
            import os

            if os.environ.get("FLAG"):
                INSIDE_IF = 1
            else:
                INSIDE_ELSE = 2

            try:
                INSIDE_TRY = 3
            except Exception:
                pass
            finally:
                INSIDE_FINALLY = 4
        """
    })

    result = run_unused_constants()

    assert _reported_names(result=result) == {"INSIDE_IF", "INSIDE_ELSE", "INSIDE_TRY", "INSIDE_FINALLY"}


def test_repeated_binding_reported_once_at_first_line(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    """The try/except ImportError pattern binds one name twice."""
    git_repository({
        "constants.py": """
            try:
                TIMEOUT = 1
            except ImportError:
                TIMEOUT = 2
        """
    })

    result = run_unused_constants()

    findings = [line for line in result.output.splitlines() if "No usage found." in line]
    assert len(findings) == 1
    assert findings[0].startswith("constants.py:TIMEOUT:2:")


def test_skip_unused_code_marker_suppresses_a_constant(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    git_repository({"constants.py": 'KEPT = "value"  # skip-unused-code\nREPORTED = "value"\n'})

    result = run_unused_constants()

    assert _reported_names(result=result) == {"REPORTED"}


# --------------------------------------------------------------------------- #
# Name selection
# --------------------------------------------------------------------------- #


def test_uppercase_only_is_the_default(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    git_repository({"constants.py": "UPPER = 1\nlower = 2\n"})

    assert _reported_names(result=run_unused_constants()) == {"UPPER"}


def test_all_names_includes_lowercase(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    git_repository({"constants.py": "UPPER = 1\nlower = 2\n"})

    assert _reported_names(result=run_unused_constants("--all-names")) == {"UPPER", "lower"}


def test_config_uppercase_only_applies_when_no_flag_is_given(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    repository = git_repository({
        "constants.py": "UPPER = 1\nlower = 2\n",
        "config.yaml": "pyutils-unusedconstants:\n  uppercase_only: false\n",
    })

    result = run_unused_constants("--config-file-path", str(repository / "config.yaml"))

    assert _reported_names(result=result) == {"UPPER", "lower"}


def test_cli_flag_overrides_config_uppercase_only(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    """--config-file-path's help text promises the CLI wins."""
    repository = git_repository({
        "constants.py": "UPPER = 1\nlower = 2\n",
        "config.yaml": "pyutils-unusedconstants:\n  uppercase_only: true\n",
    })

    result = run_unused_constants("--all-names", "--config-file-path", str(repository / "config.yaml"))

    assert _reported_names(result=result) == {"UPPER", "lower"}


def test_exclude_names(git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]) -> None:
    git_repository({"constants.py": "KEPT = 1\nREPORTED = 2\n"})

    assert _reported_names(result=run_unused_constants("--exclude-names", "KEPT")) == {"REPORTED"}


# --------------------------------------------------------------------------- #
# File selection
# --------------------------------------------------------------------------- #


def test_exclude_files_skips_only_that_file(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    """Excluding one file must not hide findings in the others."""
    git_repository({"skipped.py": "IN_SKIPPED = 1\n", "scanned.py": "IN_SCANNED = 2\n"})

    result = run_unused_constants("--exclude-files", "skipped.py")

    assert result.exit_code == 1
    assert _reported_names(result=result) == {"IN_SCANNED"}


def test_include_files_accepts_a_glob(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    git_repository({"utilities/constants.py": "IN_UTILITIES = 1\n", "other.py": "IN_OTHER = 2\n"})

    result = run_unused_constants("--include-files", "utilities/*.py")

    assert _reported_names(result=result) == {"IN_UTILITIES"}


def test_include_files_path_does_not_match_a_basename_elsewhere(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    """`constants.py` means the file at the root, not every constants.py in the tree."""
    git_repository({"constants.py": "AT_ROOT = 1\n", "nested/constants.py": "NESTED = 2\n"})

    result = run_unused_constants("--include-files", "constants.py")

    assert _reported_names(result=result) == {"AT_ROOT"}


def test_empty_selection_exits_one_not_a_finding(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result], caplog: pytest.LogCaptureFixture
) -> None:
    git_repository({"constants.py": "UNUSED = 1\n"})

    with caplog.at_level(logging.ERROR):
        result = run_unused_constants("--exclude-files", "constants.py")

    assert result.exit_code == 1
    assert "No Python files selected" in caplog.text


# --------------------------------------------------------------------------- #
# Repository resolution and paths
# --------------------------------------------------------------------------- #


def test_directory_option_analyses_a_repository_elsewhere(
    git_repository: Callable[..., Path],
    run_unused_constants: Callable[..., Result],
    outside_any_repository: Path,
) -> None:
    """The README states --directory works against any repository.

    Run from a directory in no repository at all, not merely a directory that
    is not the repository root.
    """
    repository = git_repository({"constants.py": "UNUSED = 1\n"}, chdir=False)

    result = run_unused_constants("--directory", str(repository))

    assert result.exit_code == 1
    assert _reported_names(result=result) == {"UNUSED"}


def test_file_path_option_analyses_a_repository_elsewhere(
    git_repository: Callable[..., Path],
    run_unused_constants: Callable[..., Result],
    outside_any_repository: Path,
) -> None:
    repository = git_repository({"constants.py": "UNUSED = 1\n"}, chdir=False)

    result = run_unused_constants("--file-path", str(repository / "constants.py"))

    assert result.exit_code == 1
    assert _reported_names(result=result) == {"UNUSED"}


def test_symlinked_directory_still_skips_definition_lines(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result], tmp_path: Path
) -> None:
    repository = git_repository(
        {"constants.py": 'USED_CONSTANT = "used"\nUNUSED_CONSTANT = "unused"\n', "consumer.py": CONSUMER_MODULE},
        chdir=False,
    )
    checkout = tmp_path / "checkout"
    checkout.symlink_to(repository, target_is_directory=True)

    result = run_unused_constants("--directory", str(checkout))

    assert result.exit_code == 1
    assert _reported_names(result=result) == {"UNUSED_CONSTANT"}
    assert "constants.py:UNUSED_CONSTANT:" in result.output
    assert ".." not in result.output


def test_symlinked_directory_include_files_stays_repository_relative(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result], tmp_path: Path
) -> None:
    repository = git_repository(
        {"constants.py": "AT_ROOT = 1\n", "nested/constants.py": "NESTED = 2\n"},
        chdir=False,
    )
    checkout = tmp_path / "checkout"
    checkout.symlink_to(repository, target_is_directory=True)

    result = run_unused_constants("--directory", str(checkout), "--include-files", "constants.py")

    assert _reported_names(result=result) == {"AT_ROOT"}
    assert "constants.py:AT_ROOT:" in result.output


def test_reported_paths_are_repository_relative(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--include-files is git-root relative, so the output uses the same base."""
    repository = git_repository({"utilities/constants.py": "UNUSED = 1\n"})
    monkeypatch.chdir(repository / "utilities")

    result = run_unused_constants("--directory", str(repository))

    assert "utilities/constants.py:UNUSED:1:0 No usage found." in result.output


def test_process_file_constants_works_from_outside_any_repository(
    git_repository: Callable[..., Path], outside_any_repository: Path
) -> None:
    """A library caller's working directory need not be in the repository.

    The CLI resolves the root and moves there; a direct caller gets whatever
    working directory it happens to have. Without the root being resolved from
    the file, the grep-flag probe fails and reports that the platform's git
    lacks PCRE support, which is not the problem.
    """
    repository = git_repository({"utilities/constants.py": "UNUSED = 1\n"}, chdir=False)

    output = process_file_constants(
        py_file=str(repository / "utilities" / "constants.py"),
        exclude_names=[],
        uppercase_only=True,
        file_ignore_list=[],
    )

    assert output.startswith("utilities/constants.py:UNUSED:1:0")


def test_working_directory_is_restored(
    git_repository: Callable[..., Path],
    run_unused_constants: Callable[..., Result],
    outside_any_repository: Path,
) -> None:
    repository = git_repository({"constants.py": "UNUSED = 1\n"}, chdir=False)

    run_unused_constants("--directory", str(repository))

    assert Path(os.getcwd()).resolve() == outside_any_repository.resolve()


def test_not_a_git_repository_exits_one(
    run_unused_constants: Callable[..., Result],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.ERROR):
        result = run_unused_constants()

    assert result.exit_code == 1
    assert "git repository" in caplog.text


@pytest.mark.parametrize(
    ("option", "target", "expected_message"),
    [
        ("--file-path", "a_directory", "must be a file"),
        ("--directory", "a_file.py", "must be a directory"),
    ],
)
def test_mismatched_target_type_exits_one(
    run_unused_constants: Callable[..., Result],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    option: str,
    target: str,
    expected_message: str,
) -> None:
    (tmp_path / "a_directory").mkdir()
    (tmp_path / "a_file.py").write_text("X = 1\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        result = run_unused_constants(option, str(tmp_path / target))

    assert result.exit_code == 1
    assert expected_message in caplog.text


# --------------------------------------------------------------------------- #
# Unparseable sources
# --------------------------------------------------------------------------- #


def test_unparseable_file_is_skipped_without_failing_the_run(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    """Generated and vendored sources must not decide the exit code."""
    git_repository({"constants.py": "UNUSED = 1\n", "broken.py": "def broken(:\n"})

    result = run_unused_constants()

    assert result.exit_code == 1
    assert _reported_names(result=result) == {"UNUSED"}
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_unparseable_single_file_exits_clean(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result]
) -> None:
    repository = git_repository({"broken.py": "def broken(:\n"})

    result = run_unused_constants("--file-path", str(repository / "broken.py"))

    assert result.exit_code == 0
    assert not isinstance(result.exception, SyntaxError)


def test_process_file_constants_returns_empty_for_unparseable_source(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def broken(:\n", encoding="utf-8")

    assert (
        process_file_constants(
            py_file=str(broken),
            exclude_names=[],
            uppercase_only=True,
            file_ignore_list=[],
        )
        == ""
    )


def test_process_file_constants_honours_the_file_ignore_list(tmp_path: Path) -> None:
    source = tmp_path / "constants.py"
    source.write_text("UNUSED = 1\n", encoding="utf-8")

    assert (
        process_file_constants(
            py_file=str(source),
            exclude_names=[],
            uppercase_only=True,
            file_ignore_list=["constants.py"],
        )
        == ""
    )


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_iter_module_level_constants() -> None:
    tree = parse(
        source=textwrap.dedent(
            """
            FOO = 1
            BAR: str = "x"

            def func():
                LOCAL = 2
            """
        )
    )
    assert [name for name, _, _ in _iter_module_level_constants(tree=tree)] == ["FOO", "BAR"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("A, B = 1, 2\n", ["A", "B"], id="tuple-unpacking"),
        pytest.param("[A, B] = [1, 2]\n", ["A", "B"], id="list-unpacking"),
        pytest.param("A, (B, C) = 1, (2, 3)\n", ["A", "B", "C"], id="nested-unpacking"),
        pytest.param("A, *REST = [1, 2, 3]\n", ["A", "REST"], id="starred-unpacking"),
        pytest.param("A = B = 1\n", ["A", "B"], id="chained-assignment"),
        pytest.param("if True:\n    A = 1\n", ["A"], id="module-level-if"),
        pytest.param("if False:\n    A = 1\nelse:\n    B = 2\n", ["A", "B"], id="module-level-if-else"),
        pytest.param(
            "try:\n    A = 1\nexcept ImportError:\n    B = 2\nfinally:\n    C = 3\n",
            ["A", "B", "C"],
            id="module-level-try",
        ),
        pytest.param("with open('f') as fd:\n    A = fd\n", ["A"], id="module-level-with"),
        pytest.param("class Holder:\n    A = 1\n", [], id="class-body-is-not-module-level"),
        pytest.param("def func():\n    A = 1\n", [], id="function-body-is-not-module-level"),
    ],
)
def test_iter_module_level_constants_binding_forms(source: str, expected: list[str]) -> None:
    assert [name for name, _, _ in _iter_module_level_constants(tree=parse(source=source))] == expected


def test_iter_module_level_constants_reports_each_target_column() -> None:
    tree = parse(source="ALPHA, BETA = 1, 2\n")
    assert list(_iter_module_level_constants(tree=tree)) == [("ALPHA", 1, 0), ("BETA", 1, 7)]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("USED_CONSTANT", True),
        ("used_constant", False),
        ("UsedConstant", False),
    ],
)
def test_is_uppercase_constant(name: str, expected: bool) -> None:
    assert _is_uppercase_constant(name=name) is expected


@pytest.mark.parametrize(
    ("line", "name", "expected"),
    [
        ("FOO = 1", "FOO", True),
        ("FOO: str = 'a'", "FOO", True),
        ("FOOBAR = FOO", "FOO", False),
        pytest.param("FOO, BAR = 1, 2", "FOO", True, id="first-unpacking-target"),
        pytest.param("FOO, BAR = 1, 2", "BAR", True, id="second-unpacking-target"),
        pytest.param("    FOO = 1", "FOO", True, id="indented-definition"),
        pytest.param("FOO = 1  # trailing comment", "FOO", True, id="inline-comment-stripped"),
        pytest.param("if FOO == BAR:", "FOO", False, id="equality-comparison"),
        pytest.param("if FOO != BAR:", "FOO", False, id="inequality-comparison"),
        pytest.param("FOO += 1", "FOO", False, id="augmented-assignment-is-a-read"),
        pytest.param("assert FOO >= BAR", "FOO", False, id="greater-equal-comparison"),
        pytest.param("call(FOO, BAR)", "FOO", False, id="call-argument"),
        pytest.param("BAZ = FOO, BAR", "FOO", False, id="right-hand-side-tuple"),
        pytest.param("HEAD, *TAIL = values", "TAIL", True, id="starred-unpacking-target"),
        pytest.param("[FIRST, SECOND] = pair", "SECOND", True, id="list-unpacking-target"),
        pytest.param(
            "VMI_SOURCE_POD_STR: vm.vmi.get_virt_launcher_pod(privileged_client=admin_client),",
            "VMI_SOURCE_POD_STR",
            False,
            id="dict-key-with-kwargs-is-a-usage",
        ),
        pytest.param("FOO: Optional[List[int]] = []", "FOO", True, id="annotated-assign-with-subscript"),
    ],
)
def test_is_constant_definition_line(line: str, name: str, expected: bool) -> None:
    assert _is_constant_definition_line(line_content=line, constant_name=name) is expected


def test_has_skip_unused_code_comment() -> None:
    lines = ["FOO = 1  # skip-unused-code", "BAR = 2"]
    assert _has_skip_unused_code_comment(source_lines=lines, lineno=1)
    assert not _has_skip_unused_code_comment(source_lines=lines, lineno=2)


@pytest.mark.parametrize("lineno", [0, -1, 99])
def test_has_skip_unused_code_comment_out_of_range(lineno: int) -> None:
    assert not _has_skip_unused_code_comment(source_lines=["FOO = 1"], lineno=lineno)


def test_is_name_excluded() -> None:
    assert _is_name_excluded(name="__all__", exclude_names=["__all__"])
    assert not _is_name_excluded(name="FOO", exclude_names=["__all__"])


def test_file_matches_include_list(git_repository: Callable[..., Path]) -> None:
    repository = git_repository({"utilities/constants.py": "A = 1\n"})
    py_file = str(repository / "utilities" / "constants.py")

    assert _file_matches_include_list(py_file=py_file, include_files=[])
    assert _file_matches_include_list(py_file=py_file, include_files=["utilities/constants.py"])
    assert _file_matches_include_list(py_file=py_file, include_files=["./utilities/constants.py"])
    assert _file_matches_include_list(py_file=py_file, include_files=["utilities/*.py"])
    assert not _file_matches_include_list(py_file=py_file, include_files=["other.py"])
    assert not _file_matches_include_list(py_file=py_file, include_files=["constants.py"])


def test_find_used_constants_single_scan_sees_usage_in_any_file_type(
    git_repository: Callable[..., Path],
) -> None:
    """Usage in a non-Python file counts, as it did with the per-constant search."""
    repository = git_repository({
        "constants.py": "IN_YAML = 1\nIN_MARKDOWN = 2\nNOWHERE = 3\n",
        "config.yaml": "key: IN_YAML\n",
        "docs.md": "Mentions IN_MARKDOWN in prose.\n",
    })
    constants_file = str(repository / "constants.py")

    used = find_used_constants(constants_by_file={constants_file: {"IN_YAML", "IN_MARKDOWN", "NOWHERE"}})

    assert (constants_file, "IN_YAML") in used
    assert (constants_file, "IN_MARKDOWN") in used
    assert (constants_file, "NOWHERE") not in used


def test_find_used_constants_handles_no_candidates() -> None:
    assert find_used_constants(constants_by_file={}) == set()


def test_collect_file_constants_applies_every_exclusion(
    git_repository: Callable[..., Path],
) -> None:
    repository = git_repository({"constants.py": "KEPT = 1\nEXCLUDED = 2\nMARKED = 3  # skip-unused-code\nlower = 4\n"})

    names = [
        name
        for name, _, _ in collect_file_constants(
            py_file=str(repository / "constants.py"),
            exclude_names=["EXCLUDED"],
            uppercase_only=True,
            file_ignore_list=[],
        )
    ]

    assert names == ["KEPT"]


@pytest.mark.parametrize(
    ("names", "expected_body"),
    [
        pytest.param({"UPPER_ONE", "UPPER_TWO"}, "[A-Z][A-Z0-9_]*", id="all-uppercase-uses-narrow-class"),
        pytest.param({"UPPER", "lower"}, r"\w+", id="any-lowercase-widens-the-class"),
    ],
)
def test_usage_search_pattern_narrows_when_it_can(names: set[str], expected_body: str) -> None:
    assert expected_body in _usage_search_pattern(candidate_names=names)


# --------------------------------------------------------------------------- #
# git grep engine and failure handling
# --------------------------------------------------------------------------- #


def test_basic_regex_fallback_still_finds_usage(
    git_repository: Callable[..., Path], run_unused_constants: Callable[..., Result], mocker
) -> None:
    r"""The -G pattern must be valid POSIX basic regex.

    `+` is a literal in BRE, so a `\\w+` body matches nothing and every
    candidate would be reported unused -- a false positive, which is the
    dangerous direction for a tool whose output is "delete this".
    """
    mocker.patch(
        "apps.unused_constants.unused_constants._detect_supported_grep_flag",
        return_value="-G",
    )
    git_repository({
        "constants.py": "used_lower = 1\nunused_lower = 2\n",
        "consumer.py": "from constants import used_lower\n\n\ndef consume():\n    return used_lower\n",
    })

    result = run_unused_constants("--all-names")

    assert _reported_names(result=result) == {"unused_lower"}


@pytest.mark.parametrize("flag", ["-P", "-G"])
def test_usage_search_pattern_is_valid_for_its_engine(git_repository: Callable[..., Path], mocker, flag: str) -> None:
    """Run the generated pattern through git grep and require it to match."""
    mocker.patch(
        "apps.unused_constants.unused_constants._detect_supported_grep_flag",
        return_value=flag,
    )
    repository = git_repository({"constants.py": "mixed_case = 1\n"})
    pattern = _usage_search_pattern(candidate_names={"mixed_case"})

    completed = subprocess.run(
        ["git", "grep", "-n", "--no-color", "-I", flag, "-e", pattern],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, f"pattern {pattern!r} matched nothing under {flag}"
    assert "mixed_case" in completed.stdout


def test_git_grep_failure_exits_one(
    git_repository: Callable[..., Path],
    run_unused_constants: Callable[..., Result],
    mocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same as pyutils-unusedcode: grep / engine errors exit 1, not 0."""
    mocker.patch(
        "apps.unused_constants.unused_constants._detect_supported_grep_flag",
        return_value="--not-a-real-git-grep-flag",
    )
    git_repository({"constants.py": "UNUSED = 1\n"})

    with caplog.at_level(logging.ERROR):
        result = run_unused_constants()

    assert result.exit_code == 1
    assert "git grep" in caplog.text.lower()


def test_find_used_constants_outside_a_repository_names_the_real_cause(
    tmp_path: Path, outside_any_repository: Path
) -> None:
    """Not "your git lacks PCRE", which is what the grep-flag probe would say."""
    stray = tmp_path / "stray.py"
    stray.write_text("UNUSED = 1\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not inside a git repository"):
        find_used_constants(constants_by_file={str(stray): {"UNUSED"}})
