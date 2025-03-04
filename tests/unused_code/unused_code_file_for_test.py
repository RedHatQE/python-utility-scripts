from typing import Any


def unused_code_check_fail():
    pass


def unused_code_check_file():
    pass


def foo():  # skip-unused-code
    pass


def bar(x: Any, y: Any, z: Any) -> None:  # skip-unused-code
    pass


def check_me():
    # skip-unused code
    pass


# skip-unused-code
def check_me_too():
    pass
