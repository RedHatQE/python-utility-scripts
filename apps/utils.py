from __future__ import annotations
import os

import yaml
from simple_logger.logger import get_logger
import json
import click
from typing import Any, Dict, Iterable

LOGGER = get_logger(name=__name__)


def get_util_config(util_name: str, config_file_path: str) -> Dict[str, Any]:
    if os.path.exists(config_file_path):
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


def all_python_files() -> Iterable[str]:
    """
    Get all python files from current directory and subdirectories
    """
    exclude_dirs = [".tox", "venv", ".pytest_cache", "site-packages", ".git"]
    for root, _, files in os.walk(os.path.abspath(os.curdir)):
        if [_dir for _dir in exclude_dirs if _dir in root]:
            continue

        for filename in files:
            if filename.endswith(".py") and filename != os.path.split(__file__)[-1]:
                yield os.path.join(root, filename)
