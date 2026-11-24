from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any

import click
import yaml
from simple_logger.logger import get_logger

LOGGER = get_logger(name=__name__)


def get_util_config(util_name: str, config_file_path: str | None = None) -> dict[str, Any]:
    if config_file_path and os.path.exists(config_file_path):
        with open(config_file_path) as _file:
            return yaml.safe_load(_file).get(util_name, {})
    return {}


# Reference: https://gist.github.com/welel/9cf860dd3f4d3e09f9b4305878b3a04e
class ListParamType(click.ParamType):
    """Represents the list type of a CLI parameter.

    Validates and converts values from the command line string or Python into
    a Python list.

    Usage:
        >>> @click.option("--param", default=None, type=ListParamType())
        ... def command(param):
        ...     ...

        CLI: command --param param_name 'a, b, c,'

    Example:

        >>> param_value = '1, 2, three,'
        >>> ListParamType().convert(param_value, None, None)
        'param_value' = [1, 2, 'three']`

    """

    name = "list"

    def convert(self, cli_value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        """Converts CLI value to the list structure.

        Args:
            cli_value (Any): The value to convert.
            param (click.Parameter | None): The parameter that is using this
                type to convert its value.
            ctx (click.Context | None): The current context that arrived
                at this value.

        Returns:
            list: The validated and converted list.

        Raises:
            click.BadParameter: If the validation is failed.
        """
        try:
            value = json.loads(cli_value)
            if isinstance(value, list):
                return value
        except json.JSONDecodeError:
            try:
                return [item.strip() for item in cli_value.rstrip(",").split(",")]
            except ValueError:
                self.fail(
                    f"List value items must be separated by one comma {cli_value}.",
                    param,
                    ctx,
                )


def all_python_files(directory: click.Path | None = None) -> Iterable[str]:
    """
    Get all python files from current directory and subdirectories.

    Directories whose `name` is in the exclusion list are pruned from the walk, so the same sources are not scanned
    again from other checkouts, virtualenvs or build outputs. Only the directory name is compared, never the path, so
    a checkout living under e.g. /tmp/build-tools still gets scanned.
    """
    exclude_dirs = {
        ".tox",
        "venv",
        ".venv",
        ".pytest_cache",
        "site-packages",
        ".git",
        # Other checkouts of the same repo (.worktrees), agent scratch trees (.pi/tmp) and build outputs hold the
        # same sources again, so scanning them reports every finding once per copy.
        ".worktrees",
        ".pi",
        ".mypy_cache",
        "node_modules",
        "build",
        "dist",
    }
    target = str(directory) if directory else os.path.abspath(os.curdir)

    for root, dirnames, files in os.walk(target):
        dirnames[:] = [dirname for dirname in dirnames if dirname not in exclude_dirs]
        for filename in files:
            if filename.endswith(".py"):
                yield os.path.join(root, filename)
