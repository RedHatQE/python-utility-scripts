# pyutils-jira
Helper to identify wrong jira card references in a repository files. e.g. closed tickets or tickets with not relevant target versions in a test repository branch would be identified.
It looks for following patterns in files of a pytest repository:
```bash
    - jira_id=ABC-12345  # When jira id is present in a function call
    - https://issues.redhat.com/browse/ABC-12345  # when jira is in a link in comments
    - pytest.mark.jira_utils(ABC-12345)  # when jira is in a marker
```

## Usage

```bash
pyutils-jira
pyutils-jira --help
```

## Config file
A config file with the jira connection parameters like url, token, resolved_statuses, skip_project_ids, jira_target_versions should be added to
`~/.config/python-utility-scripts/jira_utils/config.cfg`

### Example:

```yaml
pyutils-jira:
  url: <jira_url>
  token: mytoken
  resolved_statuses:
  - verified
  - release pending
  - closed
  skip_project_ids:
    - ABC
    - DEF
  jira_target_versions:
  - 1.0.0
  - 2.0.1
  issue_pattern: "([A-Z]+-[0-9]+)"
```
This would skip version checks on any jira ids associated with project ABC and DEF
This would also check if the current repository branch is pointing to any jira card that is not targeted for 1.0.0 or 2.0.1 version

To run from CLI with `--jira-target-versions`

```bash
pyutils-unusedcode --jira-target-versions '1.0.0,2.0.1'
```
#### Note:
To mark to skip a jira from these checks, one can add `<skip-jira-utils-check>` as a comment to the same line containing the specific jira

Example:
```bash
 #https://issues.redhat.com/browse/OSD-5716 <skip-jira_utils-check>
 ```
