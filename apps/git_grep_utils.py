from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

from simple_logger.logger import get_logger

LOGGER = get_logger(name=__name__)


@lru_cache(maxsize=1)
def detect_supported_grep_flag() -> str:
    r"""Detect and cache a supported regex engine flag for git grep.

    Prefer PCRE ("-P") for proper \b handling; fall back to basic regex ("-G").
    Run a harmless grep to verify support and cache the first working flag.
    Uses lru_cache for thread-safe caching that runs only once per process.
    """
    candidate_flags = ["-P", "-G"]
    for flag in candidate_flags:
        try:
            probe_cmd = [
                "git",
                "grep",
                "-n",
                "--no-color",
                "--untracked",
                "-I",
                flag,
                "^$",
            ]
            result = subprocess.run(probe_cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode in (0, 1):
                return flag
        except (OSError, subprocess.SubprocessError):
            LOGGER.debug(f"git grep flag {flag!r} probe failed, trying next candidate")

    raise RuntimeError(
        "git grep does not support '-P' (PCRE) or '-G' (basic regex) on this platform. "
        "Please ensure a compatible git/grep is installed."
    )


def wrap_grep_word(body: str, *, flag: str | None = None) -> str:
    r"""Wrap `body` in word boundaries for a git grep engine.

    - PCRE (-P): \bbody\b
    - Basic (-G): \<body\>
    """
    if (flag or detect_supported_grep_flag()) == "-P":
        return rf"\b{body}\b"
    return rf"\<{body}\>"


def find_git_root(file_path: str) -> str:
    """Return the git repository root containing `file_path`."""
    path = Path(file_path).resolve().parent

    while path != path.parent:
        if (path / ".git").exists():
            return str(path)
        path = path.parent

    return os.getcwd()


def resolve_absolute_path(git_grep_path: str, reference_file: str) -> str:
    """Convert a git grep path to an absolute path if needed."""
    if not os.path.isabs(git_grep_path):
        git_root = find_git_root(reference_file)
        return os.path.join(git_root, git_grep_path)
    return git_grep_path
