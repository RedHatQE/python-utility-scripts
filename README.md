# python-utility-scripts
Repository for various utility scripts

# Installation:
```bash
poetry add python-utility-scripts
```
# Scripts:
## unusedcode
Helper to identify unused code in a pytest repository. It should be run from inside the test repository using this tool.

### Usage
```bash
poetry run unusedcode
```
### Config file
To skip unused code check on specific files of a repository, a config file with the list of names of such files separated by `\n` should be added to
`<repo_path>/.config/unusedcode/config`

e.g to skip running unsedcode check on file pytest_matrix_utils.py, following can be added to `<repo_path>/.config/unusedcode/config`
```bash
pytest_matrix_utils.py
```


## polarion_tc_requirements
Helper to see polarion test cases of a pytest repository has associated polarion requirements

### Usage
```bash
poetry run polarion_tc_requirements -p <polarion_project_id>
```
