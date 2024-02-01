# pyappsutils-unusedcode
Helper to identify unused code in a pytest repository. It should be run from inside the test repository using this tool.

## Usage

```bash
pyappsutils-unusedcode
pyappsutils-unusedcode --help
```

## Config file
To skip unused code check on specific files or functions of a repository, a config file with the list of names of such files and function prefixes should be added to
`~/.config/python-utility-scripts/config.yaml`

### Example:

```yaml
pyappsutils-unusedcode:
  exclude_files:
  - "my_exclude_file.py"
  exclude_function_prefix:
  - "my_exclude_function_prefix"
```
This would exclude any functions with prefix my_exclude_function_prefix and file my_exclude_file.py from unused code check
