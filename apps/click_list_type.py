# Reference: https://gist.github.com/welel/9cf860dd3f4d3e09f9b4305878b3a04e
import json
import click


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

    def convert(self, cli_value, param, ctx):
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
