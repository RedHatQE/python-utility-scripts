# scripts to interact with Polarion instance

## pyutils-polarion-verify-tc-requirements
Utility to check if test cases in a pytest repository has associated Polarion requirements.

## Requirements
This script uses [pylero](https://github.com/RedHatQE/pylero) and expects .pylero config file to be present in current directory or user's home directory.

## Usage
```bash
pyutils-polarion-verify-tc-requirements --help
pyutils-polarion-verify-tc-requirements --project_id <project_id> --branch <remote_branch_to_compare_to>
```

## Config file
To specify polation project id for polarion scripts, it can be added to the config file:
`~/.config/python-utility-scripts/config.yaml`


### Example:

```yaml
pyutils-polarion-verify-tc-requirements:
  project_id: "<project_id>"
  branch: "<remote_branch_to_compare_to>"
```
This would run the polarion requirement check against Polarion project <project_id> and remote branch <remote_branch_to_compare_to>

To run from CLI with `--project-id` and `--branch`

```bash
pyutils-polarion-verify-tc-requirements  --project-id 'my_project_id' --branch "origin/main"
```


## pyutils-polarion-set-automated
Utility to mark newly added test cases in a pytest repository as approved automatically.

## Requirements
This script uses [pylero](https://github.com/RedHatQE/pylero) and expects .pylero config file to be present in current directory or user's home directory.

## Usage
```bash
pyutils-polarion-set-automated --help
pyutils-polarion-set-automated --project_id <project_id> --branch <remote_branch_to_compare_to>
```

## Config file
To specify polation project id for polarion scripts, it can be added to the config file:
`~/.config/python-utility-scripts/config.yaml`


### Example:

```yaml
pyutils-polarion-set-automated:
  project_id: "<project_id>"
  branch: "<remote_branch_to_compare_to>"
```
This would run the polarion requirement check against Polarion project <project_id> and remote branch <remote_branch_to_compare_to>

To run from CLI with `--project-id` and `--branch`

```bash
pyutils-polarion-set-automated  --project-id 'my_project_id' --branch "origin/main"
```
