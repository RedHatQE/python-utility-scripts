# pyutils-unusedcode

Helper to identify unused code in a pytest repository. It should be run from inside the test repository using this tool.

## Usage

### Basic Usage

```bash
# Analyze all Python files in the current directory (must be a git repository)
pyutils-unusedcode

# Show help with all available options
pyutils-unusedcode --help
```

### Analyze Specific Files or Directories

```bash
# Analyze a single Python file
pyutils-unusedcode --file-path /path/to/your/file.py
pyutils-unusedcode -f /path/to/your/file.py

# Analyze all Python files in a specific directory recursively
pyutils-unusedcode --directory /path/to/your/project
pyutils-unusedcode -d /path/to/your/project
```

**Note:** When using `--file-path` or `--directory`, the tool will analyze files from any git repository, not just the current working directory.

## Command-Line Options

| Option | Short | Description |
|--------|-------|-------------|
| `--file-path` | `-f` | Analyze a single Python file for unused functions. Must be an existing .py file. |
| `--directory` | `-d` | Analyze all Python files in a directory recursively for unused functions. Must be an existing directory. |
| `--exclude-files` | | Comma-separated list of files to exclude from analysis. |
| `--exclude-function-prefixes` | | Comma-separated list of function prefixes to exclude from analysis. |
| `--config-file-path` | | Path to custom config file (default: `~/.config/python-utility-scripts/config.yaml`). |
| `--verbose` | `-v` | Enable verbose logging for debugging. |
| `--help` | | Show help message with all available options. |

## Config file

To skip unused code check on specific files or functions of a repository, a config file with the list of names of such files and function prefixes should be added to
`~/.config/python-utility-scripts/config.yaml`

### Example

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

### Skip single function in file

Add `# skip-unused-code` comment in the function name list to skip it from check.

```python
def my_function(): # skip-unused-code
    pass
```
