import ast
import os
import subprocess
import click
import yaml
from simple_logger.logger import get_logger

from apps.utils import all_python_files, ListParamType

LOGGER = get_logger(name=__name__)


def is_fixture_autouse(func):
    if func.decorator_list:
        for deco in func.decorator_list:
            if not hasattr(deco, "func"):
                continue

            if getattr(deco.func, "attr", None) and getattr(deco.func, "value", None):
                if deco.func.attr == "fixture" and deco.func.value.id == "pytest":
                    for _key in deco.keywords:
                        if _key.arg == "autouse":
                            return _key.value.s


def _iter_functions(tree):
    """
    Get all function from python file
    """

    def is_func(_elm):
        return isinstance(_elm, ast.FunctionDef)

    def is_test(_elm):
        return _elm.name.startswith("test_")

    for elm in tree.body:
        if is_func(_elm=elm):
            if is_test(_elm=elm):
                continue

            yield elm


def read_config_file(config_file_path):
    if os.path.exists(config_file_path):
        with open(config_file_path) as _file:
            return yaml.safe_load(_file)
    return {}


def is_ignore_function_list(ignore_prefix_list, function):
    ignore_function_lists = [
        function.name for ignore_prefix in ignore_prefix_list if function.name.startswith(ignore_prefix)
    ]
    if ignore_function_lists:
        LOGGER.info(f"Following functions are getting skipped: {ignore_function_lists}")
        return True


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
def get_unused_functions(config_file_path, exclude_files, exclude_function_prefixes):
    _unused_functions = []
    config_yaml = read_config_file(config_file_path=config_file_path)
    unused_code_config = config_yaml.get("pyappsutils-unusedcode", {})
    func_ignore_prefix = exclude_function_prefixes or unused_code_config.get("exclude_function_prefix", [])
    file_ignore_list = exclude_files or unused_code_config.get("exclude_files", [])
    for py_file in all_python_files():
        if os.path.basename(py_file) in file_ignore_list:
            continue
        with open(py_file) as fd:
            tree = ast.parse(source=fd.read())

        for func in _iter_functions(tree=tree):
            if func_ignore_prefix and is_ignore_function_list(ignore_prefix_list=func_ignore_prefix, function=func):
                continue

            if is_fixture_autouse(func=func):
                continue

            _used = subprocess.check_output(
                f"git grep -w '{func.name}' | wc -l",
                shell=True,
            )
            used = int(_used.strip())
            if used < 2:
                _unused_functions.append(
                    f"{os.path.relpath(py_file)}:{func.name}:{func.lineno}:{func.col_offset} Is"
                    " not used anywhere in the code.",
                )
    if _unused_functions:
        click.echo("\n".join(_unused_functions))
        raise click.Abort()


if __name__ == "__main__":
    get_unused_functions()
