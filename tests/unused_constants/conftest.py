from __future__ import annotations

import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import Result

from apps.unused_constants.unused_constants import get_unused_constants
from tests.utils import get_cli_runner


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Path]:
    """Build a real, committed git repository and move the working directory into it.

    The usage oracle is `git grep` over a real repository, so the tests drive a
    real repository. Mocking _git_grep would assert the mock's return value
    rather than the tool's behaviour.
    """

    def _build(files: dict[str, str], chdir: bool = True) -> Path:
        # A subdirectory, not tmp_path itself, so that tmp_path/"outside" is
        # genuinely outside the repository rather than a directory within it.
        root = tmp_path / "repository"
        root.mkdir()

        for relative_path, content in files.items():
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

        _git("init", "-q", ".", cwd=root)
        _git("config", "user.email", "test@example.com", cwd=root)
        _git("config", "user.name", "Test", cwd=root)
        _git("add", "-A", cwd=root)
        _git("commit", "-qm", "fixture", cwd=root)

        if chdir:
            monkeypatch.chdir(root)
        return root

    return _build


@pytest.fixture
def outside_any_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Move the working directory to a path that is in no git repository.

    `git rev-parse` walks upwards, so this needs its own repository-free root
    rather than a subdirectory of a fixture repository.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    return outside


@pytest.fixture
def run_unused_constants(tmp_path: Path) -> Callable[..., Result]:
    """Invoke the CLI with config isolation.

    --config-file-path defaults to ~/.config/python-utility-scripts/config.yaml,
    so without this a developer's own config would change the result.
    """

    def _run(*arguments: str) -> Result:
        if "--config-file-path" not in arguments:
            arguments = (*arguments, "--config-file-path", str(tmp_path / "absent-config.yaml"))
        return get_cli_runner().invoke(get_unused_constants, list(arguments))

    return _run
