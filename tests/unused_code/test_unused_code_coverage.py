from __future__ import annotations

import ast
import logging
import os
import subprocess
import textwrap

import pytest
from ast_comments import parse

import apps.unused_code.unused_code
from apps.unused_code.unused_code import (
    _check_fixturenames_insert_pattern,
    _check_getfixturevalue_pattern,
    _find_git_root,
    _git_grep,
    _is_documentation_pattern,
    _is_pytest_mark_usefixtures_call,
    _is_usefixtures_context,
    _iter_functions,
    _resolve_absolute_path,
    get_unused_functions,
    is_fixture_autouse,
    is_ignore_function_list,
    is_pytest_fixture,
    process_file,
)
from tests.utils import get_cli_runner


@pytest.mark.parametrize(
    ("code", "is_fixture"),
    [
        ("@pytest.fixture\ndef my_fixture(): pass", True),
        ("@pytest.fixture()\ndef my_fixture(): pass", True),
        ("def my_function(): pass", False),
        (
            textwrap.dedent(
                """
        import pytest

        @pytest.fixture
        def my_fixture():
            pass
        """
            ),
            True,
        ),
        (
            textwrap.dedent(
                """
                from pytest import fixture

                @fixture
                def my_fixture():
                    pass
                """
            ),
            True,
        ),
        (
            textwrap.dedent(
                """
                import pytest

                @pytest.fixture
                def my_fixture():
                    pass
                """
            ),
            True,
        ),
        (
            textwrap.dedent(
                """
                from pytest import fixture

                @fixture()
                def my_fixture():
                    pass
                """
            ),
            True,
        ),
    ],
)
def test_is_pytest_fixture(code, is_fixture):
    tree = parse(code)
    func = tree.body[0]
    if isinstance(func, ast.Import):
        func = tree.body[1]
    if isinstance(func, ast.ImportFrom):
        func = tree.body[1]

    assert is_pytest_fixture(func) == is_fixture


@pytest.mark.parametrize(
    ("code", "is_autouse"),
    [
        ("@pytest.fixture(autouse=True)\ndef my_fixture(): pass", True),
        ("@pytest.fixture\ndef my_fixture(): pass", False),
    ],
)
def test_is_fixture_autouse(code, is_autouse):
    tree = parse(code)
    func = tree.body[0]
    assert is_fixture_autouse(func) == is_autouse


def test_check_fixturenames_insert_pattern(tmp_path):
    py_file = tmp_path / "tmp_fixture_insert.py"
    py_file.write_text('item.fixturenames.insert(0, "my_fixture")')
    assert _check_fixturenames_insert_pattern("my_fixture", str(py_file))
    assert not _check_fixturenames_insert_pattern("other_fixture", str(py_file))


def test_check_getfixturevalue_pattern(tmp_path):
    py_file = tmp_path / "tmp_getfixturevalue.py"
    py_file.write_text('request.getfixturevalue("my_fixture")')
    assert _check_getfixturevalue_pattern("my_fixture", str(py_file))
    assert not _check_getfixturevalue_pattern("other_fixture", str(py_file))


def test_check_fixturenames_insert_pattern_error(tmp_path):
    py_file = tmp_path / "tmp_fixture_insert_error.py"
    py_file.write_text("invalid python code")
    assert not _check_fixturenames_insert_pattern("my_fixture", str(py_file))


def test_check_getfixturevalue_pattern_error(tmp_path):
    py_file = tmp_path / "tmp_getfixturevalue_error.py"
    py_file.write_text("invalid python code")
    assert not _check_getfixturevalue_pattern("my_fixture", str(py_file))


@pytest.mark.parametrize(
    ("line", "is_doc"),
    [
        ("    my_function (str): description", True),
        ("if my_function(): pass", False),
        ("def my_function(): pass", False),
        ("    # my_function()", True),
    ],
)
def test_is_documentation_pattern(line, is_doc):
    assert _is_documentation_pattern(line, "my_function") == is_doc


def test_git_grep_error(mocker):
    mocker.patch(
        "apps.unused_code.unused_code.subprocess.run",
        side_effect=Exception("git error"),
    )
    with pytest.raises(Exception):
        _git_grep("pattern")


@pytest.mark.parametrize(
    ("prefixes", "func_name", "is_ignored"),
    [
        (["_"], "_my_function", True),
        (["test_"], "test_my_function", True),
        ([], "my_function", False),
    ],
)
def test_is_ignore_function_list(prefixes, func_name, is_ignored):
    tree = parse(f"def {func_name}(): pass")
    func = tree.body[0]
    assert is_ignore_function_list(prefixes, func) == is_ignored


def test_is_usefixtures_context(tmp_path):
    py_file = tmp_path / "tmp_usefixtures.py"
    py_file.write_text(
        textwrap.dedent(
            """
    import pytest

    @pytest.mark.usefixtures("my_fixture")
    def test_something():
        pass
    """
        )
    )
    assert _is_usefixtures_context(str(py_file), "4", "my_fixture")


def test_is_usefixtures_context_in_list(tmp_path):
    py_file = tmp_path / "tmp_usefixtures_list.py"
    py_file.write_text(
        textwrap.dedent(
            """
    import pytest

    pytestmark = [pytest.mark.usefixtures("my_fixture")]

    def test_something():
        pass
    """
        )
    )
    assert _is_usefixtures_context(str(py_file), "4", "my_fixture")


def test_is_usefixtures_context_in_assignment(tmp_path):
    py_file = tmp_path / "tmp_usefixtures_assignment.py"
    py_file.write_text(
        textwrap.dedent(
            """
    import pytest

    my_marks = pytest.mark.usefixtures("my_fixture")

    @my_marks
    def test_something():
        pass
    """
        )
    )
    assert _is_usefixtures_context(str(py_file), "4", "my_fixture")


def test_is_usefixtures_context_class(tmp_path):
    py_file = tmp_path / "tmp_usefixtures_class.py"
    py_file.write_text(
        textwrap.dedent(
            """
    import pytest

    @pytest.mark.usefixtures("my_fixture")
    class TestSomething:
        def test_one(self):
            pass
    """
        )
    )
    assert _is_usefixtures_context(str(py_file), "4", "my_fixture")


def test_detect_supported_grep_flag_fallback(mocker):
    apps.unused_code.unused_code._detect_supported_grep_flag.cache_clear()
    mocker.patch(
        "apps.unused_code.unused_code.subprocess.run",
        side_effect=[
            subprocess.CalledProcessError(1, "git"),
            subprocess.CalledProcessError(1, "git"),
        ],
    )
    with pytest.raises(RuntimeError):
        apps.unused_code.unused_code._detect_supported_grep_flag()


def test_process_file_usefixtures(tmp_path):
    py_file = tmp_path / "tmp_usefixtures.py"
    py_file.write_text(
        textwrap.dedent(
            """
    import pytest

    @pytest.fixture
    def my_fixture():
        pass

    @pytest.mark.usefixtures("my_fixture")
    def test_something():
        pass
    """
        )
    )
    assert "my_fixture" not in process_file(str(py_file), [], [])


def test_process_file_fixturenames_insert(tmp_path):
    py_file = tmp_path / "tmp_fixturenames_insert.py"
    py_file.write_text(
        textwrap.dedent(
            """
    import pytest

    @pytest.fixture
    def my_fixture():
        pass

    def pytest_runtest_setup(item):
        item.fixturenames.insert(0, "my_fixture")
    """
        )
    )
    assert "my_fixture" not in process_file(str(py_file), [], [])


def test_process_file_getfixturevalue(tmp_path):
    py_file = tmp_path / "tmp_getfixturevalue.py"
    py_file.write_text(
        textwrap.dedent(
            """
    import pytest

    @pytest.fixture
    def my_fixture():
        pass

    def test_something(request):
        request.getfixturevalue("my_fixture")
    """
        )
    )
    assert "my_fixture" not in process_file(str(py_file), [], [])


def test_process_file_lambda(tmp_path):
    py_file = tmp_path / "tmp_lambda.py"
    py_file.write_text(
        textwrap.dedent(
            """
    def my_function():
        pass

    x = lambda: my_function()
    """
        )
    )
    assert "my_function" not in process_file(str(py_file), [], [])


def test_process_file_list_comprehension(tmp_path):
    py_file = tmp_path / "tmp_list_comprehension.py"
    py_file.write_text(
        textwrap.dedent(
            """
    def my_function():
        pass

    x = [my_function() for _ in range(5)]
    """
        )
    )
    assert "my_function" not in process_file(str(py_file), [], [])


def test_process_file_dict_comprehension(tmp_path):
    py_file = tmp_path / "tmp_dict_comprehension.py"
    py_file.write_text(
        textwrap.dedent(
            """
    def my_function():
        pass

    x = {i: my_function() for i in range(5)}
    """
        )
    )
    assert "my_function" not in process_file(str(py_file), [], [])


def test_process_file_generator_expression(tmp_path):
    py_file = tmp_path / "tmp_generator_expression.py"
    py_file.write_text(
        textwrap.dedent(
            """
    def my_function():
        pass

    x = (my_function() for _ in range(5))
    """
        )
    )
    assert "my_function" not in process_file(str(py_file), [], [])


def test_process_file_yield_from(tmp_path):
    py_file = tmp_path / "tmp_yield_from.py"
    py_file.write_text(
        textwrap.dedent(
            """
    def my_function():
        yield from range(5)

    def my_generator():
        yield from my_function()
    """
        )
    )
    assert "my_function" not in process_file(str(py_file), [], [])


def test_process_file_with_statement(tmp_path):
    py_file = tmp_path / "tmp_with_statement.py"
    py_file.write_text(
        textwrap.dedent(
            """
    from contextlib import contextmanager

    @contextmanager
    def my_function():
        yield

    with my_function():
        pass
    """
        )
    )
    assert "my_function" not in process_file(str(py_file), [], [])


def test_process_file_for_loop(tmp_path):
    py_file = tmp_path / "tmp_for_loop.py"
    py_file.write_text(
        textwrap.dedent(
            """
    def my_function():
        return range(5)

    for i in my_function():
        pass
    """
        )
    )
    assert "my_function" not in process_file(str(py_file), [], [])


def test_process_file_while_loop(tmp_path):
    py_file = tmp_path / "tmp_while_loop.py"
    py_file.write_text(
        textwrap.dedent(
            """
    def my_function():
        return False

    while my_function():
        pass
    """
        )
    )
    assert "my_function" not in process_file(str(py_file), [], [])


def test_process_file_if_statement(tmp_path):
    py_file = tmp_path / "tmp_if_statement.py"
    py_file.write_text(
        textwrap.dedent(
            """
    def my_function():
        return True

    if my_function():
        pass
    """
        )
    )
    assert "my_function" not in process_file(str(py_file), [], [])


def test_find_git_root(tmp_path):
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        assert str(tmp_path) == _find_git_root(str(tmp_path))
    finally:
        os.chdir(original_cwd)


def test_resolve_absolute_path(tmp_path):
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        file_path = tmp_path / "my_file.py"
        file_path.touch()
        assert str(file_path) == _resolve_absolute_path("my_file.py", str(tmp_path))
    finally:
        os.chdir(original_cwd)


def test_is_pytest_mark_usefixtures_call():
    tree = parse(
        textwrap.dedent(
            """
    import pytest

    @pytest.mark.usefixtures("my_fixture")
    def test_something():
        pass
    """
        )
    )
    decorator = tree.body[1].decorator_list[0]
    assert _is_pytest_mark_usefixtures_call(decorator)


def test_iter_functions():
    tree = parse(
        textwrap.dedent(
            """
    def my_function():
        pass

    def test_my_function():
        pass
    """
        )
    )
    functions = list(_iter_functions(tree))
    assert len(functions) == 1
    assert functions[0].name == "my_function"


def test_process_file_skip_comment(tmp_path):
    py_file = tmp_path / "tmp_skip_comment.py"
    py_file.write_text(
        textwrap.dedent(
            """
    def my_function():
        # skip-unused-code
        pass
    """
        )
    )
    assert "" == process_file(str(py_file), [], [])


def test_process_file_ignore_file(tmp_path):
    py_file = tmp_path / "tmp_ignore_file.py"
    py_file.write_text("def my_function(): pass")
    assert "" == process_file(str(py_file), [], ["tmp_ignore_file.py"])


def test_get_unused_functions_processing_error(mocker):
    mocker.patch(
        "apps.unused_code.unused_code.process_file",
        side_effect=Exception("processing error"),
    )
    result = get_cli_runner().invoke(get_unused_functions)
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)


def test_get_unused_functions_verbose(mocker):
    mocker.patch("apps.unused_code.unused_code.LOGGER.setLevel")
    get_cli_runner().invoke(get_unused_functions, ["--verbose"])
    apps.unused_code.unused_code.LOGGER.setLevel.assert_called_once_with(logging.DEBUG)
