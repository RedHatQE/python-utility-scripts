# pyutils-jira
Helper to identify wrong jira card references in a repository files. e.g. closed tickets or tickets with not relevant target versions in a test repository branch would be identified.
It looks for following patterns in files of a pytest repository:
```text
    - jira_id=ABC-12345  # When jira id is present in a function call
    - <jira url>/browse/ABC-12345  # when jira is in a link in comments; see "Browse link matching"
    - pytest.mark.jira_utils(ABC-12345)  # when jira is in a marker
```

## Usage

```bash
pyutils-jira
pyutils-jira --help
JIRA_CLOUD_ID=<cloud-id> JIRA_TOKEN=<token> pyutils-jira --check-auth   # verify the credential before a scan
```

## Authentication
Three authentication modes are supported, selected by the config file, CLI option or environment variable:

| Mode | Selection | Client |
|---|---|---|
| Data Center PAT (default) | url + token | `JIRA(token_auth=token, options={"server": url})` |
| Atlassian Cloud, basic auth | `--cloud` / `cloud: true` + url + user + token (there is no env var for `cloud`) | `JIRA(server=url, basic_auth=(user, token))` |
| Atlassian Cloud service account | `--cloud-id` / `JIRA_CLOUD_ID` / `cloud_id`, or a gateway `url` (see below) | `JIRA(server=https://api.atlassian.com/ex/jira/<cloud_id>, token_auth=token)` |

Service-account (Bearer) mode is selected in two ways: by setting `cloud_id`, or by setting `url` to the API
gateway URL `https://api.atlassian.com/ex/jira/<cloud_id>`. The gateway URL is itself a service-account selector,
so either one is enough and only `token` is required; `cloud` and `user` are then ignored (an INFO message says
so).

`cloud_id` wins over `url`, `cloud` and `user`:

- the client always talks to `https://api.atlassian.com/ex/jira/<cloud_id>`, rebuilt from `cloud_id`;
- it must be the Atlassian cloud id itself - letters, digits and dashes, 8 to 64 characters, not the site host
  (`https://example.atlassian.net` is wrong). An invalid value is rejected before any request is made, so the
  token is never sent anywhere. The run exits `1` with a message that names the source and echoes the value:
  `Invalid Jira cloud id (from --cloud-id/JIRA_CLOUD_ID/config 'cloud_id'): <value>. Pass --cloud-id "" to ignore it.`
  Passing an explicit empty `--cloud-id ""` wins over `JIRA_CLOUD_ID` and the `cloud_id` config key, and the
  utility falls back to `url` + `token`. Service-account auth is only fully off when `url` is not a gateway url
  either - a `https://api.atlassian.com/ex/jira/` `url` still selects Bearer auth on its own;
- a non-gateway `url` supplied together with `cloud_id` is discarded with a warning
  (`cloud_id provided, discarding supplied Jira url ... in favor of the API gateway url`);
- `cloud: true` plus `user` does not switch the mode away from Bearer auth.

A service-account scoped API token (`ATST...`) is sent as `Authorization: Bearer` and only works against the
API gateway URL `https://api.atlassian.com/ex/jira/<cloud_id>`. Against the site URL (for example
`https://redhat.atlassian.net`) it returns HTTP 403 `Failed to parse Connect Session Auth Token`, and it does
not work with basic auth either. Because of that, when `cloud_id` is set the utility builds the gateway URL
itself and discards a site URL supplied via `url`/`--url` for the API calls (it still affects link matching, see
"Browse link matching").

In service-account mode `url` and `user` are optional; only `token` is required.

Environment variables: `JIRA_SERVER_URL` (or `JIRA_URL`), `JIRA_TOKEN`, `JIRA_USER`, `JIRA_CLOUD_ID`. The CLI
options take precedence; an environment variable is used as the default of its CLI option, so it also overrides
the config file entry for the same key. `cloud` has no environment variable, only `--cloud` or `cloud: true`.

### Verify the credential with `--check-auth`

`--check-auth` performs no scan. It builds the client, calls `/myself` and prints the resolved mode, server URL
and identity (`displayName`, `emailAddress`, `accountId`) without revealing the token. It exits `0` on success
and `1` on failure, so it can be used as a pre-flight check before a long scan.

A `401`/`403` here means Jira rejected the credential itself. Common causes:

- an expired token - scoped API tokens expire, so re-mint it;
- a `cloud_id` that does not match the site the token is scoped to;
- a token without the scopes the utility needs to read issues.

Pass the token through the environment rather than on the command line:

```bash
JIRA_CLOUD_ID=<cloud-id> JIRA_TOKEN=<token> pyutils-jira --check-auth
```

Command-line arguments are visible to other users in `ps` output and are appended to your shell history, so use
`--token` only when the environment is not an option.

During a scan a single issue returning `401`/`403` is not taken as proof: the utility probes `/myself` once (the
answer is cached, so it happens at most once per run). If that probe also fails with `401`/`403` the credential is
genuinely rejected - the hint is logged once at `ERROR` and the run exits `1` without a per-issue report. If the
probe succeeds the failure is a per-issue permission problem: the scan continues, the failures are aggregated at
the end and the run still exits `1`.

### Example: Data Center PAT (default)

```yaml
pyutils-jira:
  url: https://jira.example.com
  token: mytoken
```

### Example: Atlassian Cloud, basic auth

```yaml
pyutils-jira:
  cloud: true
  url: https://example.atlassian.net
  user: myuser@example.com
  token: mytoken
```

### Example: Atlassian Cloud service account

```yaml
pyutils-jira:
  cloud_id: 4a4f8f8f-1234-5678-9abc-def012345678
  token: ATST...
```

Equivalent from the CLI:

```bash
JIRA_CLOUD_ID=4a4f8f8f-1234-5678-9abc-def012345678 JIRA_TOKEN="$JIRA_TOKEN" pyutils-jira --check-auth
```

## Browse link matching
`<jira url>/browse/<key>` links are matched differently per mode, because the API gateway URL never appears in
source code:

| Mode | Matched prefix |
|---|---|
| Data Center PAT, Cloud basic auth | the configured `url` must prefix the link (a trailing `/` is ignored) |
| Service account (`cloud_id` or gateway `url`) | `/browse/<KEY>` on any `http(s)` host, e.g. `https://example.atlassian.net/browse/KEY` |

Relative links such as `/browse/KEY` are never matched, and outside service-account mode a link to a host other
than the configured `url` is not matched either.

In service-account mode the match is host-agnostic only while no site `url` is supplied: when `cloud_id` and a
site `url` are both set, that `url` is still used as the link prefix - it is discarded only for the API calls.

## Config file
A config file with the jira connection parameters like url, token, resolved_statuses, skip_project_ids,  
target_versions can be passed to the command line option `--config-file-path`.

`--config-file-path` is optional: `pyutils-jira` also runs on `token` plus `url` or `cloud_id` alone, which is
why the environment-variable usage above needs no config file. Unlike `pyutils-unusedcode` and the Polarion
utilities it defines no `~/.config/python-utility-scripts/config.yaml` default, so without the option nothing is
read from disk.

Precedence is per key:

- `url`, `token`, `user`, `cloud_id`, `target_versions` and `skip_project_ids` fall back to the config file only
  when the CLI option is not set (`target_versions` is read from the file when `--target-versions` is not passed;
  `skip_project_ids` resolves as `skip_projects or config_dict.get("skip_project_ids", [])` with a click default of
  `None`: an unset `--skip-projects` falls back to the config key, but an explicitly empty value becomes `[""]`
  (truthy, and its empty prefix matches every issue), so it silently disables the target-version check - omit it);
- `cloud` resolves as `cloud if cloud is not None else config_dict.get("cloud", False)`. `--cloud` is a flag whose
  default is `None` and it has no environment variable, so it can enable cloud basic auth but cannot disable a
  `cloud: true` coming from the config file;
- `version_string_not_targeted_jiras` is the exception - it resolves as `config_dict.get(key, cli_value)`, so a
  value in the config file wins even over an explicitly passed `--version-string-not-targeted-jiras` flag;
- the target version key is `target_versions`; `jira_target_versions` is not read;
- `resolved_statuses` and `issue_pattern` are accepted but currently overridden by the non-empty CLI defaults
  (see `config.example.yaml`).

### Example:

```yaml
pyutils-jira:
  # Data Center PAT: url + token. Cloud basic auth: also set cloud: true and fill user.
  # Service-account auth: set cloud_id and token, then remove url and user or leave them empty.
  url: <jira_url>
  token: mytoken
  user: <jira_user_email>
  cloud: false
  cloud_id: ""
  resolved_statuses:
  - verified
  - release pending
  - closed
  skip_project_ids:
    - ABC
    - DEF
  # target_versions is read from this file when --target-versions is not passed
  target_versions:
  - 1.0.0
  - 2.0.1
  - 3.0.z
  issue_pattern: "([A-Z]+-[0-9]+)"
  version_string_not_targeted_jiras: "vfuture"
```
This would skip version checks on any jira ids associated with project ABC and DEF
This would also check if the current repository branch is pointing to any jira card that is not targeted for 1.0.0 or 2.0.1 version

To run from CLI with `--target-versions`

```bash
pyutils-jira --target-versions '1.0.0,2.0.1'
```
#### Note:
To mark to skip a jira from these checks, one can add `<skip-jira-utils-check>` as a comment to the same line containing the specific jira

Example:
```text
 #https://issues.redhat.com/browse/OSD-5716 <skip-jira-utils-check>
 ```
The effective default for resolved statuses is the `--resolved-statuses` CLI default, because that option is
always non-empty and therefore shadows the `resolved_statuses` config key:

```text
verified, release pending, closed, resolved
```

The config key has no effect until that CLI default is changed to `None`.

Default value for `version_string_not_targeted_jiras` is `vfuture`; a value in the config file wins over both
this default and an explicitly passed flag.
