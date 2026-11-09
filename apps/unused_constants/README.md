# pyutils-unusedconstants

Helper to identify unused module-level constants in a pytest repository. It should be run from inside the test repository using this tool.

## Usage

### Basic Usage

```bash
# Analyze Python files in the current directory (must be a git repository)
pyutils-unusedconstants

# Show help with all available options
pyutils-unusedconstants --help
```

### Analyze specific files or directories

```bash
# Analyze a single Python file
pyutils-unusedconstants --file-path /path/to/your/file.py
pyutils-unusedconstants -f /path/to/your/file.py

# Analyze all Python files in a specific directory recursively
pyutils-unusedconstants --directory /path/to/your/project
pyutils-unusedconstants -d /path/to/your/project

# Limit analysis to specific paths (relative to git root)
pyutils-unusedconstants --include-files "utilities/constants.py,tests/conftest.py"
```

**Note:** When using `--file-path` or `--directory`, the tool analyzes files from any git repository, not just the current working directory; the repository root is resolved with `git rev-parse --show-toplevel`.

Exit codes match `pyutils-unusedcode`: `0` when nothing is reported, `1` when
unused constants are found or when the run cannot continue (for example not in
a git repository, bad `--file-path`/`--directory`, or `git grep` failure).

Reported paths are relative to the git root, matching `--include-files`.

Unparseable files are skipped with a warning rather than failing the run.

## Command-Line Options

| Option | Short | Description |
|--------|-------|-------------|
| `--file-path` | `-f` | Analyze a single Python file for unused module-level constants. |
| `--directory` | `-d` | Analyze all Python files in a directory recursively. |
| `--include-files` | | Comma-separated list of paths or globs to include, relative to the git root (e.g. `utilities/constants.py`, `tests/*/consts.py`). A bare `constants.py` matches only the file at the repository root, not every `constants.py` in the tree. |
| `--exclude-files` | | Comma-separated list of file basenames to exclude from analysis. |
| `--exclude-names` | | Comma-separated list of constant names to exclude. |
| `--uppercase-only` / `--all-names` | | When `--uppercase-only` (default), only check `ALL_CAPS` names. |
| `--config-file-path` | | Path to custom config file (default: `~/.config/python-utility-scripts/config.yaml`). |
| `--verbose` | `-v` | Enable verbose logging for debugging. |
| `--help` | | Show help message with all available options. |

## What counts as a usage

A constant is reported when no occurrence of its name is found anywhere in the
repository, other than a line in its own file that binds it. The search is
textual, over tracked and untracked files of every type, so the following count
as usages and the constant is **not** reported:

- the name inside a string literal or a docstring
- the name in a non-Python file: Markdown, YAML, JSON, a test parameter id
- the same name defined in another module, if that other one is used. Two
  parallel constants modules both defining `TIMEOUT` mask each other: a single
  use of `tests/network/constants.py`'s `TIMEOUT` marks
  `tests/storage/constants.py`'s `TIMEOUT` used as well, so duplicated names
  across sibling modules are exactly the case this will not report
- the name referenced only by other dead code

A clean run therefore means "no unused constant was proved", not "there are
none". Findings are reliable; their absence is not. The uppercase-only default
also means lowercase module-level names are skipped unless `--all-names` is
given, and constants bound in a class body are never module level.

## Library use

`process_file_constants` and `find_used_constants` are importable. They resolve
the repository root from the files they are given, so the caller's working
directory does not matter and need not be inside the repository.

`find_used_constants` raises `RuntimeError` when the paths are not in a git
repository or when `git grep` fails (the CLI logs the error and exits `1`).

All paths passed to `find_used_constants` must live in one git repository; the
root is taken from the first file when `repository_root` is omitted.

Usage is resolved with one `git grep` per call (character-class pattern, then
tokenising in Python), not one search per constant.

Each `process_file_constants` call runs its own repository scan. That is fine
for a single file, but looping it over many files runs one scan per file; use
`collect_file_constants` for each file and then one `find_used_constants` over
the union, which is what the CLI does.

## Config file

To skip unused constant checks on specific files or names, add a config section to
`~/.config/python-utility-scripts/config.yaml`

### Example

```yaml
pyutils-unusedconstants:
  include_files:
    - "utilities/constants.py"
    - "tests/conftest.py"
  exclude_files:
    - "my_exclude_file.py"
  exclude_names:
    - "__all__"
  uppercase_only: true
```

To run from CLI with `--exclude-names`

```bash
pyutils-unusedconstants --exclude-names '__all__,MY_LEGACY_CONSTANT'
```

### Skip a single constant

Add `# skip-unused-code` on the same line as the assignment:

```python
MY_CONSTANT = "value"  # skip-unused-code
```
