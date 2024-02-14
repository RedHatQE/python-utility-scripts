# scripts to interact with Polarion instance

## pyappsutils-polarion_tc_requirements
Helper to check if test cases in a pytest repository has associated Polarion requirements.

## Usage
This script uses [pylero](https://github.com/RedHatQE/pylero) and expects .pylero config file to be present in current directory or user's home directory.
```bash
pyappsutils-polarion_tc_requirements --help
pyappsutils-polarion_tc_requirements --project_id <project_id>
```

## Config file
To specify polation project id for polarion scripts, it can be added to the config file:
`~/.config/python-utility-scripts/config.yaml`


### Example:

```yaml
pyappsutils-polarion_tc_requirements:
  project_id: "<project_id>"
```
This would run the polarion requirement check against Polarion project <project_id>

To run from CLI with `--project-id`

```bash
pyappsutils-polarion_tc_requirements --project-id 'my_project_id'
```