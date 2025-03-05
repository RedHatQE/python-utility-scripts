# pyutils-unusedcode
Helper to identify unused code in a pytest repository. It should be run from inside the test repository using this tool.

## Usage

```bash
pyutils-unusedcode
pyutils-unusedcode --help
```

## Config file
To skip unused code check on specific files or functions of a repository, a config file with the list of names of such files and function prefixes should be added to
`~/.config/python-utility-scripts/config.yaml`

### Example:

```yaml
pyutils-unusedcode:
  exclude_files:
  - "my_exclude_file.py"
  exclude_function_prefix:
  - "my_exclude_function_prefix"
```
This would exclude any functions with prefix my_exclude_function_prefix and file my_exclude_file.py from unused code check

To run from CLI with `--exclude-function-prefixes`

```bash
pyutils-unusedcode --exclude-function-prefixes 'my_exclude_function1,my_exclude_function2'
```

To run from CLI with `--exclude-files`

```bash
pyutils-unusedcode --exclude-files 'my_exclude_file1.py,my_exclude_file2.py'
```

## Excluding single functions in your code
To skip single functions in your target repository you can add an inline comment to the function definition. The comment should match `# skip-unused-code`

### Example:

Given a target file main.py
```python
def tmp1():  # skip-unused-code
  pass

def tmp2(
  x,
  y,
  z
):  # skip-unused-code
```
Running this tool would exclude both functions `tmp1` and `tmp2`
```bash
pyutils-unusedcode -v
2025-01-01T00:00:00.1 apps.unused_code.unused_code DEBUG Skipping function due to comment: tmp1
2025-01-01T00:00:00.2 apps.unused_code.unused_code DEBUG Skipping function due to comment: tmp2
```
