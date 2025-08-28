"""
Test file for unused code detection with various documentation patterns.

This file contains functions that are referenced in documentation but not in actual code,
to test the false positive detection capabilities of unused_code.py.
"""


# Original functions for existing tests compatibility
def unused_code_check_fail() -> None:
    """Original function for compatibility with existing tests."""
    pass


def unused_code_check_file() -> None:
    """Original function for compatibility with existing tests."""
    pass


def unused_code_namespace():
    """Create a new namespace.

    Args:
        unused_code_namespace (str): The namespace of the pod.
        unused_code_namespace (str): Kubernetes namespace in which to create the Secret.
        unused_code_namespace (Optional[str]): The namespace to use.
        unused_code_namespace (Union[str, None]): Optional namespace parameter
        unused_code_namespace (List[str]): List of namespaces

    Parameters:
        unused_code_namespace (str): Namespace identifier

    Returns:
        str: The created namespace name.

    Note:
        * unused_code_namespace (string): Pod namespace
        - unused_code_namespace (str, optional): Target namespace
        unused_code_namespace (str, default='default'): The namespace name
    """
    # unused_code_namespace (str): Comment documentation
    ## unused_code_namespace (str): Markdown style documentation
    return "default"


def unused_code_create_secret():
    """Create secrets with various documentation patterns.

    Args:
        unused_code_create_secret (callable): Function to create secrets
        unused_code_create_secret   (   str   )   :   Description with extra spaces
        unused_code_create_secret(str): Documentation without space before paren
        unused_code_create_secret (Callable[[str], Secret]): Complex type annotation
        unused_code_create_secret (Union[str, None]): Union type in docs
        unused_code_create_secret (Dict[str, Any]): Dictionary type parameter

    Returns:
        str: The created secret.
    """
    return "secret-value"


def unused_code_create_namespace():
    """Create a new namespace.

    This function demonstrates documentation false positives.

    Args:
        unused_code_create_namespace (str): The namespace to create.

    Returns:
        str: The created namespace name.
    """
    return "default"


def unused_code_get_pod_status():
    """Get the status of a pod.

    Args:
        unused_code_get_pod_status (callable): Function to get status.

    Returns:
        str: Pod status.
    """
    return "Running"


def unused_code_check_pods():
    """Check pod status.

    This function actually calls get_pod_status(), so get_pod_status should NOT
    be marked as unused.

    Args:
        unused_code_get_pod_status (callable): Function to get status.
    """
    # This is a real function call
    return unused_code_get_pod_status()


def unused_code_validate_namespace():
    """Validate a Kubernetes namespace.

    Args:
        unused_code_validate_namespace (str): The namespace to validate.

    Returns:
        bool: True if namespace is valid.
    """
    return True


def unused_code_deploy_app():
    """Deploy an application with comprehensive documentation.

    Standard Python docstring formats:
        unused_code_deploy_app (str): Application name to deploy
        unused_code_deploy_app (Optional[str]): Optional app name
        unused_code_deploy_app (Union[str, None]): App name or None
        unused_code_deploy_app (List[str]): List of app names
        unused_code_deploy_app (Dict[str, Any]): App configuration
        unused_code_deploy_app (Callable[[str], bool]): Deployment function

    Markdown documentation:
        * unused_code_deploy_app (str): Application name parameter
        - unused_code_deploy_app (string): The app to deploy

    Type hints in comments:
        # unused_code_deploy_app (str): Type annotation comment
        ## unused_code_deploy_app (str): Markdown header documentation

    In docstrings with quotes:
        '''unused_code_deploy_app (str): Parameter description'''

    Returns:
        bool: True if deployment successful.
    """
    # unused_code_deploy_app (str): Type annotation comment
    ## unused_code_deploy_app (str): Markdown header documentation
    return True


def unused_code_some_other_function():
    """Some other function.

    This function contains documentation references to other functions
    but doesn't actually call them.

    Args:
        unused_code_create_namespace (str): The namespace to create.
        unused_code_validate_namespace (str): The namespace to validate.
        unused_code_deploy_app (str): Application name to deploy
        unused_code_namespace (str): The namespace of the pod.

    Returns:
        str: The created namespace name.
    """
    pass


def unused_code_edge_case_function():
    """Function with edge case documentation patterns.

    Various edge cases for documentation pattern detection:
        unused_code_namespace (str): The namespace of the pod.
        unused_code_namespace (str): Kubernetes namespace description
        unused_code_namespace (Optional[str]): Optional namespace
        unused_code_create_secret (callable): Function to create secrets
        unused_code_deploy_app (str): Application name to deploy

    Returns:
        None
    """
    pass


def unused_code_function_with_legitimate_calls():
    """Function that actually uses other functions.

    This function demonstrates real function calls that should be detected
    as legitimate usage, not documentation patterns.
    """
    # These should be detected as real usage
    result = unused_code_namespace()
    unused_code_create_secret()

    if unused_code_validate_namespace():
        deploy_result = unused_code_deploy_app()
        return deploy_result

    return result


def unused_code_unused_function_no_docs():
    """This function has no documentation patterns referencing it.

    It should be detected as unused since there are no references to it
    anywhere in the codebase.
    """
    return "I am unused"


def unused_code_unused_function_with_docs():
    """This function is only referenced in documentation.

    Args:
        unused_code_unused_function_with_docs (callable): This function itself

    It should be detected as unused since the only reference is in
    its own documentation.
    """
    return "I am also unused"


def unused_code_skip_with_comment() -> None:
    """Function that should be skipped due to comment."""
    pass  # skip-unused-code
