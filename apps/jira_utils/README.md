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
Three authentication modes are supported (the service-account mode has two credential flavours, described
below), selected by the config file, CLI option or environment variable:

| Mode | Selection | Client |
|---|---|---|
| Data Center PAT (default) | url + token | `JIRA(token_auth=token, options={"server": url})` |
| Atlassian Cloud, basic auth | `--cloud` / `cloud: true` + url + user + token (there is no env var for `cloud`) | `JIRA(server=url, basic_auth=(user, token))` |
| Atlassian Cloud service account, pre-minted token | `--cloud-id` / `JIRA_CLOUD_ID` / `cloud_id`, or a gateway `url` (see below) | `JIRA(server=https://api.atlassian.com/ex/jira/<cloud_id>, token_auth=token)` |
| Atlassian Cloud service account, OAuth 2.0 client credentials | `--cloud-id` (required) + `--client-id` + `--client-secret` | `JIRA(server=https://api.atlassian.com/ex/jira/<cloud_id>, token_auth=<minted token>)` |

Service-account (Bearer) mode is selected in two ways: by setting `cloud_id`, or by setting `url` to the API
gateway URL `https://api.atlassian.com/ex/jira/<cloud_id>`. The gateway URL is itself a service-account selector,
so either one is enough and `cloud` and `user` are then ignored (an INFO message says so). A credential is still
required: `token`, or the OAuth 2.0 `client_id` + `client_secret` pair.

`cloud_id` wins over `url`, `cloud` and `user`:

- the client always talks to `https://api.atlassian.com/ex/jira/<cloud_id>`, rebuilt from `cloud_id`;
- it must be the Atlassian cloud id itself - letters, digits and dashes, 8 to 64 characters, not the site host
  (`https://example.atlassian.net` is wrong). An invalid value is rejected before any request is made, so the
  token is never sent anywhere. The run exits `1` with a message that names the source and echoes the value:
  `Invalid Jira cloud id (from --cloud-id/JIRA_CLOUD_ID/config 'cloud_id'): <value>. Pass --cloud-id "" to ignore it.`
  Passing an explicit empty `--cloud-id ""` wins over `JIRA_CLOUD_ID`/`ATLASSIAN_OAUTH_CLOUD_ID` and the `cloud_id`
  config key, and the utility falls back to `url` + `token`. Service-account auth is only fully off when `url` is
  not a gateway url either - a `https://api.atlassian.com/ex/jira/` `url` still selects Bearer auth on its own;
- a non-gateway `url` supplied together with `cloud_id` is discarded with a warning
  (`cloud_id provided, discarding supplied Jira url ... in favor of the API gateway url`);
- `cloud: true` plus `user` does not switch the mode away from Bearer auth.

A service-account scoped API token (`ATST...`) is sent as `Authorization: Bearer` and only works against the
API gateway URL `https://api.atlassian.com/ex/jira/<cloud_id>`. Against the site URL (for example
`https://redhat.atlassian.net`) it returns HTTP 403 `Failed to parse Connect Session Auth Token`, and it does
not work with basic auth either. Because of that, when `cloud_id` is set the utility builds the gateway URL
itself and discards a site URL supplied via `url`/`--url` for the API calls (it still affects link matching, see
"Browse link matching").

In service-account mode `url` and `user` are optional; only `token`, or the OAuth 2.0 `client_id` + `client_secret`
pair, is required.

Environment variables: `JIRA_SERVER_URL` (or `JIRA_URL`), `JIRA_TOKEN`, `JIRA_USER`, `JIRA_CLOUD_ID` (or
`ATLASSIAN_OAUTH_CLOUD_ID`), `ATLASSIAN_OAUTH_CLIENT_ID`, `JIRA_OAUTH_CLIENT_SECRET`. The CLI options take
precedence; an environment variable is used as the default of its CLI option, so it also overrides the config file
entry for the same key. `cloud` has no environment variable, only `--cloud` or `cloud: true`. The pair mixes prefixes
on purpose: `ATLASSIAN_OAUTH_CLOUD_ID` and `ATLASSIAN_OAUTH_CLIENT_ID` are kept for compatibility with the existing
rootcoz/tooling convention, while the `JIRA_*` names follow this repo's scheme.

### The two service-account flavours

Both flavours select the same mode, send `Authorization: Bearer` to `https://api.atlassian.com/ex/jira/<cloud_id>`
and ignore `cloud`/`user`. They differ only in where the bearer value comes from:

| | A - pre-minted scoped token | B - OAuth 2.0 client credentials |
|---|---|---|
| Credential | `--token` (`ATST...`) minted by an Atlassian admin | `--client-id` + `--client-secret` (`ATOA...`) created by an Atlassian admin for the service account |
| Environment | `JIRA_TOKEN` | `ATLASSIAN_OAUTH_CLIENT_ID` / `JIRA_OAUTH_CLIENT_SECRET` |
| Config keys | `token` | `client_id` / `client_secret` |
| Lifetime | expires, so it must be re-minted by hand | ~1 hour access token, minted once per run and reused for the whole run, so no manual re-minting |
| Log line | `Using Jira service-account (pre-minted scoped API token) authentication against ...` | `Using Jira service-account (OAuth 2.0 client credentials) authentication against ...` |

With flavour B the utility exchanges the pair at `https://auth.atlassian.com/oauth/token`
(`grant_type=client_credentials`), keeps the resulting access token in-process for the whole run and never logs the
client secret. `token` is not needed then; if it is set as well, the minted access token replaces it.

The pair is validated before any request is made. Only a completely empty invocation (no config file, no
credential, no cloud id) is rejected earlier, by the pre-flight guard:

```text
No Jira configuration supplied: pass --config-file-path, or a credential (--token with --url or --cloud-id, or --client-id plus --client-secret with --cloud-id).
```

Every credential combination then gets its own message:

- a lone `client_id` or `client_secret` **together with another complete credential** (`token` plus `url` or
  `cloud_id`) is logged at `ERROR` and the run continues on that credential; the message names the credential
  actually used (`the pre-minted scoped API token`, `cloud basic auth (user email plus API token)` or
  `the Data Center personal access token (PAT)`):
  `Incomplete OAuth 2.0 service-account credential (both --client-id and --client-secret are required): ignoring it and authenticating with <credential> instead.`
- a lone pair member as the **only** credential exits `1`:
  `Both --client-id and --client-secret are required for the OAuth 2.0 service-account credential.`
- a complete pair without a resolvable cloud id, with `token` and `url` set, is logged at `ERROR` and the run
  continues on that credential, again naming it:
  `OAuth 2.0 service-account credential ignored for lack of a cloud id (pass --cloud-id or set JIRA_CLOUD_ID): authenticating with <credential> instead.`
- a complete pair without a cloud id as the **only** credential exits `1`, naming the opt-out:
  `A Jira cloud id is required for OAuth service-account authentication: pass --cloud-id (or set JIRA_CLOUD_ID), or opt out of the OAuth credential with --client-id "" --client-secret "".`

An invalid or disabled credential never reaches Jira: the exchange at `https://auth.atlassian.com/oauth/token`
rejects it first, so the mint failure exits `1` before any Jira API call and `/myself` is never called. The message
carries whatever status the token endpoint answered with, for example
`Jira authentication failed: could not mint an Atlassian access token from the OAuth 2.0 credential (HTTP 401 - check the client id and secret)`.
The same prefix covers the other mint failures: a network error appends the `requests` exception text, a non-JSON
body renders `... (HTTP <status> - malformed response payload)`, and a payload without a usable `access_token`
renders `... (HTTP <status> - the response payload has no access_token)`.

> **Known limitation:** the access token is minted once per run and reused for the whole run, so a scan that runs
> longer than the token lifetime (~1 hour) is not refreshed mid-run — it simply starts failing with `401`. Re-run
> the scan, or split it into shorter runs.
>
> The 120 second margin in `mint_atlassian_token` bounds only its in-process token cache (a second mint inside the
> margin re-mints instead of reusing). The CLI mints once, before it builds the client, so that margin never acts
> at run time.

#### Where the cloud id comes from

```bash
curl https://<site>.atlassian.net/_edge/tenant_info   # -> {"cloudId": "4a4f8f8f-1234-5678-9abc-def012345678", ...}
```

That `cloudId` is a plain UUID. `ATLASSIAN_OAUTH_CLOUD_ID` is accepted as an alternative to `JIRA_CLOUD_ID` for
this value.

> **Not a cloud id:** an Atlassian *account id* (shape `digits:uuid`, for example `712020:f0a60b32-...`, as seen
> in profile URLs) is a user identity. As `--cloud-id` it fails the validation above (no colons allowed), and
> behind a gateway `url` the gateway answers with a confusing `404`. `ATLASSIAN_OAUTH_CLIENT_ID` likewise must be
> the OAuth client id of the service account, not an account id.

### Verify the credential with `--check-auth`

`--check-auth` performs no scan. It builds the client, calls `/myself` and prints the resolved mode, server URL
and identity (`displayName`, `emailAddress`, `accountId`) without revealing the token. It exits `0` on success
and `1` on failure, so it can be used as a pre-flight check before a long scan.

OAuth 2.0 service-account credentials usually carry only `read:jira-work`/`write:jira-work`, and `/myself` is not
part of those scopes. When `/myself` answers `401`/`403` and a client secret is in play, the check falls back to
the project endpoint; if that succeeds the run exits `0` and the success log reports the project visibility instead
of an identity:

```text
Jira authentication OK - mode: service-account, server: <gateway>, service account: project visibility verified; the current-user endpoint (/myself) is unavailable for this credential's scopes
```

A `401`/`403` from the Jira API means Jira rejected the credential itself - or, for a scope-limited OAuth 2.0
credential, that both `/myself` and the project fallback were rejected. Common causes:

- a `cloud_id` that does not match the site the credential is scoped to;
- a token or credential without the scopes the utility needs to read issues;
- an expired token - **pre-minted scoped API token only**: scoped API tokens expire, so re-mint it. An OAuth 2.0
  credential is exchanged for a fresh access token on every run, so it never presents an expired access token.

An invalid or disabled OAuth 2.0 client id/secret is not in this list: it fails at the token endpoint before any
Jira call (see the mint-failure paragraph above), so it never produces a `/myself` response.

Pass the token through the environment rather than on the command line:

```bash
JIRA_CLOUD_ID=<cloud-id> JIRA_TOKEN=<token> pyutils-jira --check-auth
```

Command-line arguments are visible to other users in `ps` output and are appended to your shell history, so use
`--token` only when the environment is not an option.

During a scan a single issue returning `401`/`403` is not taken as proof: the utility runs the same credential
probe once (the verdict is cached, so it happens at most once per run). If that probe also fails with `401`/`403`
the credential is genuinely rejected - the hint is logged once at `ERROR` and the run exits `1` without a per-issue
report. If the probe succeeds the failure is a per-issue permission problem: the scan continues, the failures are
aggregated at the end and the run still exits `1`. The probe is the same one `--check-auth` uses, including the
project-endpoint fallback for a scope-limited OAuth 2.0 credential, so a credential that passes `--check-auth` does
not abort a scan.

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

### Example: Atlassian Cloud service account, OAuth 2.0 client credentials

```yaml
pyutils-jira:
  cloud_id: 4a4f8f8f-1234-5678-9abc-def012345678
  client_id: <oauth client id>
  client_secret: ATOA...
```

Equivalent from the CLI, with the secret kept out of `ps` and the shell history:

```bash
ATLASSIAN_OAUTH_CLOUD_ID=4a4f8f8f-1234-5678-9abc-def012345678 \
ATLASSIAN_OAUTH_CLIENT_ID=<oauth client id> JIRA_OAUTH_CLIENT_SECRET="$JIRA_OAUTH_CLIENT_SECRET" \
pyutils-jira --check-auth
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
- `client_id` and `client_secret` resolve as `cli_value or config_dict.get(key, "")`; their environment variables
  (`ATLASSIAN_OAUTH_CLIENT_ID`, `JIRA_OAUTH_CLIENT_SECRET`) arrive as the CLI option defaults, exactly like
  `JIRA_TOKEN`, so the order stays CLI/env then config file;
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
  # Service-account auth: set cloud_id and token, or cloud_id with client_id + client_secret for OAuth 2.0.
  # url and user may then be removed or left empty: both are optional, cloud and user are ignored in this mode,
  # and a leftover site url is only used for browse-link matching (never for the API calls).
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
