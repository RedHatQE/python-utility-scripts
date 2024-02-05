import ast
import os
import subprocess
import click
import yaml
from simple_logger.logger import get_logger
import urllib3

from apps.click_list_type import ListParamType
from apps.utils import all_python_files

urllib3.disable_warnings()
LOGGER = get_logger(name=__name__)


def is_fixture_autouse(func):
    if func.decorator_list:
        for deco in func.decorator_list:
            if not hasattr(deco, "func"):
                continue
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
    if config_file_path and not os.path.exists(config_file_path):
        LOGGER.error(f"{config_file_path} file does not exist.")
        raise click.Abort()
    skip_config_file = config_file_path or os.path.join(
        os.path.expanduser("~"), ".config", "python-utility-scripts", "config.yaml"
    )
    if os.path.exists(skip_config_file):
        with open(skip_config_file) as _file:
            return yaml.safe_load(_file)


@click.command()
@click.option(
    "--config-file-path",
    help="Provide absolute path to the config file. Any CLI option(s) would override YAML file",
    type=click.Path(),
)
@click.option("--exclude-files", help="Provide a comma-separated string of files to exclude", type=ListParamType())
@click.option(
    "--exclude-function-prefixes",
    help="Provide a comma-separated string of function prefixes to exclude",
    type=ListParamType(),
)
def get_unused_functions(config_file_path, exclude_file_list, exclude_function_prefixes):
    _unused_functions = []
    config_yaml = read_config_file(config_file_path=config_file_path)
    unused_code = config_yaml.get("pyappsutils-unusedcode", {})
    func_ignore_prefix = exclude_function_prefixes or unused_code.get("exclude_function_prefix", [])
    file_ignore_list = exclude_file_list or unused_code.get("exclude_files") or []
    for py_file in all_python_files():
        LOGGER.info(f"Checking file: {py_file}")

        if file_ignore_list and os.path.basename(py_file) in file_ignore_list:
            LOGGER.warning(f"File {py_file} is being skipped.")
            continue

        with open(py_file) as fd:
            tree = ast.parse(source=fd.read())

        for func in _iter_functions(tree=tree):
            ignore_functions = (
                [func.name for ignore_prefix in func_ignore_prefix if func.name.startswith(ignore_prefix)]
                if func_ignore_prefix
                else []
            )
            if ignore_functions:
                LOGGER.warning(f"functions {ignore_functions} are being skipped.")
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
        LOGGER.error("\n".join(_unused_functions))
        raise click.Abort()


if __name__ == "__main__":
    get_unused_functions()
