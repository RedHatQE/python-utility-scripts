# pyutils-jira
Helper to identify wrong jira card references in a repository files. e.g. closed tickets or tickets with not relevant target versions in a test repository branch would be identified.
It looks for following patterns in files of a pytest repository:
```text
    - jira_id=ABC-12345  # When jira id is present in a function call
    - <jira url>/browse/ABC-12345  # when jira is in a link in comments; see "Browse link matching"
    - pytest.mark.jira_utils(ABC-12345)  # when jira is in a marker
```

The scan skips a directory by its **name**, never by a path substring, so a checkout living under e.g.
`/tmp/build-tools/` is still scanned. Excluded names: `.git`, `.tox`, `.venv`, `venv`, `.pytest_cache`,
`site-packages`, `.mypy_cache`, `.worktrees`, `.pi`, `node_modules`, `build`, `dist`.

## Usage

```bash
pyutils-jira
pyutils-jira --help

# verify the credential before a scan; quote the values, unquoted <...> is a shell redirection
export JIRA_CLOUD_ID="<cloud-id>"
export JIRA_TOKEN="<token>"
pyutils-jira --check-auth
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
so either one is enough and `cloud` and `user` are then ignored. A credential is still required: `token`, or the
OAuth 2.0 `client_id` + `client_secret` pair.

`cloud_id` wins over `url`, `cloud` and `user`:

- the client always talks to `https://api.atlassian.com/ex/jira/<cloud_id>`, rebuilt from `cloud_id`;
- it must be the Atlassian cloud id itself - letters, digits and dashes, 8 to 64 characters, not the site host
  (`https://example.atlassian.net` is wrong). An invalid value is rejected before any request is made, so the
  token is never sent anywhere. The run exits `1` with a message that names the source and masks the value (the
  placeholder is printed, not the rejected id):
  `Invalid Jira cloud id (from --cloud-id/JIRA_CLOUD_ID/ATLASSIAN_OAUTH_CLOUD_ID/config 'cloud_id'): <cloud-id>. Pass --cloud-id "" to ignore it.`
  Passing an explicit empty `--cloud-id ""` wins over `JIRA_CLOUD_ID`/`ATLASSIAN_OAUTH_CLOUD_ID` and the `cloud_id`
  config key, and the utility falls back to `url` + `token`. Service-account auth is only fully off when `url` is
  not a gateway url either;
- a gateway `url` selects Bearer auth because it carries the cloud id (`https://api.atlassian.com/ex/jira/<cloud_id>`).
  A bare gateway url (`https://api.atlassian.com/ex/jira/`, no id) - and the bare gateway host
  `https://api.atlassian.com` itself, with or without its trailing slash, which is recognised as a gateway url too -
  is rejected with no cloud id resolved: the run
  exits `1` before any request is made, with
  `The supplied Jira API gateway url is missing its cloud id: append it, or pass --cloud-id (expected https://api.atlassian.com/ex/jira/<cloud-id>).`
  An id segment that is present but the wrong shape (`/ex/jira/abc123`, an account id) gets its own message instead:
  `Invalid Jira cloud id in the supplied gateway url: expected letters, digits and dashes, 8-64 characters.`
  A path below the cloud id (`.../ex/jira/<cloud-id>/rest/api/2`) is ignored: the server is rebuilt as the canonical
  gateway url, so the path python-jira appends itself is never doubled.
  With a `cloud_id` set the bare prefix costs nothing for the API calls - `cloud_id` rebuilds the full gateway url
  anyway - but it still decides browse-link matching: a bare gateway prefix, with or without its trailing slash, is
  recognised as a gateway url, so links are then matched host-agnostically as `/browse/<KEY>` (see
  "Browse link matching");
- a non-gateway `url` supplied together with `cloud_id` is discarded and the discard is logged at `INFO`
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
`ATLASSIAN_OAUTH_CLOUD_ID`), `JIRA_SA_CLIENT_ID`, `JIRA_SA_CLIENT_SECRET`. `url`, `token` and `user` are
`os.getenv()` defaults of their CLI option, so their environment variable also overrides the config file entry for
the same key. `cloud_id` and the OAuth 2.0 pair are resolved per key instead, where the environment beats the config
file but an explicitly empty option beats both (see "Precedence is per key"). `cloud` has no environment variable,
only `--cloud` or `cloud: true`. The two OAuth 2.0 credential variables
also accept the older aliases `ATLASSIAN_OAUTH_CLIENT_ID` and `JIRA_OAUTH_CLIENT_SECRET`; the `JIRA_SA_*` pair wins
when both are set. The aliases keep the older mixed prefixes for compatibility with the existing rootcoz/tooling
convention, while the primary `JIRA_SA_*` names follow this repo's scheme.

### The two service-account flavours

Both flavours select the same mode, send `Authorization: Bearer` to `https://api.atlassian.com/ex/jira/<cloud_id>`
and ignore `cloud`/`user`. They differ only in where the bearer value comes from:

| | A - pre-minted scoped token | B - OAuth 2.0 client credentials |
|---|---|---|
| Credential | `--token` (`ATST...`) minted by an Atlassian admin | `--client-id` + `--client-secret` (`ATOA...`) created by an Atlassian admin for the service account |
| Environment | `JIRA_TOKEN` | `JIRA_SA_CLIENT_ID` / `JIRA_SA_CLIENT_SECRET` (aliases: `ATLASSIAN_OAUTH_CLIENT_ID` / `JIRA_OAUTH_CLIENT_SECRET`) |
| Config keys | `token` | `client_id` / `client_secret` |
| Lifetime | expires, so it must be re-minted by hand | ~1 hour access token, minted once per run and reused for the whole run, so no manual re-minting |
| Log line | `Using Jira service-account (pre-minted scoped API token) authentication against ...` | `Using Jira service-account (OAuth 2.0 client credentials) authentication against ...` |

In service-account mode a healthy scan logs exactly one `INFO` line naming the resolved flavour and the server - the
`Log line` above. The cloud id is masked in every log line and message: only the resolved flavour and the masked
gateway url `https://api.atlassian.com/ex/jira/<cloud-id>` are printed, never the id itself - the real id and url are
still what the utility uses to talk to Jira. A `user:password@` component is stripped the same way, with one split:
a url passed on its own (`--url`, a server named in a message, the probe url) always loses its userinfo - parsed
whole, or, when the authority does not parse at all (an unmatched bracket), matched over the authority alone - while
inside free-form text (a log line, an error body) a url ends at whitespace or at a quote/bracket, so a credential
containing one of those cannot be delimited there and is not stripped from that text. The `jira`/`urllib3`
transport logs - which quote the request url verbatim - are filtered too, in
message and traceback alike. The configured (or minted) bearer token is redacted there as `***`, and only when it is
at least 8 characters long so a dummy value cannot corrupt words. The filter is installed once at start-up, when any
of the three is present: a resolved cloud id, a real token, or a url carrying a userinfo.
Flavour B adds a second `INFO` line when it mints (`Minted an Atlassian OAuth 2.0 access token, valid for <n> seconds`).
A site `url` discarded because `cloud_id` is set is also logged at `INFO`. An OAuth 2.0 credential that is ignored
(incomplete pair, or a complete pair with no resolvable cloud id) yields one `WARNING`, naming both what was ignored
and the credential actually used; none of these notices is an error.

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
  `cloud_id`) is logged at `WARNING` and the run continues on that credential; the message names the credential
  actually used (`the pre-minted scoped API token`, `cloud basic auth (user email plus API token)` or
  `the Data Center personal access token (PAT)`):
  `Incomplete OAuth 2.0 service-account credential (both --client-id and --client-secret are required): ignoring it and authenticating with <credential> instead.`
- a lone pair member as the **only** credential exits `1`:
  `Both --client-id and --client-secret are required for the OAuth 2.0 service-account credential.`
- a complete pair without a resolvable cloud id, with `token` and `url` set, is logged at `WARNING` and the run
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
curl "https://<site>.atlassian.net/_edge/tenant_info"   # -> {"cloudId": "4a4f8f8f-1234-5678-9abc-def012345678", ...}
```

That `cloudId` is a plain UUID. `ATLASSIAN_OAUTH_CLOUD_ID` is accepted as an alternative to `JIRA_CLOUD_ID` for
this value.

> **Not a cloud id:** an Atlassian *account id* (shape `digits:uuid`, for example `712020:f0a60b32-...`, as seen
> in profile URLs) is a user identity. As `--cloud-id` it fails the validation above (no colons allowed), and
> behind a gateway `url` the run exits `1` before any request is made with
> `Invalid Jira cloud id in the supplied gateway url: expected letters, digits and dashes, 8-64 characters.` - the
> id segment of a gateway url must itself be a valid cloud id. `JIRA_SA_CLIENT_ID` likewise must be
> the OAuth client id of the service account, not an account id.

### Verify the credential with `--check-auth`

`--check-auth` performs no scan. It builds the client, calls `/myself` and prints the resolved mode, server URL
and identity (`displayName`, `emailAddress`, `accountId`) without revealing the token, so it can be used as a
pre-flight check before a long scan.

| Exit | Meaning | Line it prints |
|---|---|---|
| `0` | The credential is verified: `/myself` answered, or the project endpoint returned a non-empty list that the same request without an `Authorization` header does not also return | `Jira authentication OK - ...` |
| `1` | The credential is rejected: a `401`/`403` from `/myself` (Data Center PAT, cloud basic auth), or from **both** probe endpoints (Bearer) | `Jira authentication failed (mode: ..., server: ...): <status> <detail>` plus the credential hint |
| `1` | The probe could not decide: an outage, an unreachable API, an empty project list, or a project list that is also readable anonymously | `Jira credential check failed (<ErrorType>, status: <status>): <detail>` or `Jira credential could not be verified: ...` |

Service-account credentials - the pre-minted scoped API token as much as the OAuth 2.0 pair - usually carry only
`read:jira-work`/`write:jira-work`, and `/myself` is not part of those scopes. When `/myself` answers `401`/`403`
the check falls back to the project endpoint, for either flavour and without needing a client secret. A non-empty
project list only verifies the credential when the same request without a credential does not return it as well, in
which case the success log reports the project visibility instead of an identity:

```text
Jira authentication OK - mode: service-account, server: https://api.atlassian.com/ex/jira/<cloud-id>, service account: project visibility verified; the current-user endpoint (/myself) is unavailable for this credential's scopes
```

The fallback is a second opinion rather than a certainty, so it can be inconclusive in two ways.

In service-account mode a non-empty project list is only evidence when the same request without the credential does
not return it as well, so the check repeats the call once with no `Authorization` header, against the same project
path and API version the authenticated call used. Only a genuinely different answer counts as evidence: an empty
list, a non-OK status or a non-JSON body all mean "not anonymously readable", so the authenticated non-empty list
stands and the check exits `0`. If that header-less call returns a non-empty project list instead, the outcome is
inconclusive - a dead bearer would look exactly as healthy - and the check exits `1`:

```text
Jira credential could not be verified: the project endpoint returns the same non-empty project list without an Authorization header, so a non-empty list does not prove this credential is honoured.
```

That cross-check is a tripwire rather than the general verdict: in service-account mode the server is always the
canonical API gateway url, so the header-less probe can only ever ask the gateway's project path - it cannot fire
against Atlassian with the current server resolution, and the tests reach the branch by mocking the premise. Every
scope-limited `--check-auth` still pays one extra header-less request for it, which a proxy or a
TLS-intercepting environment may turn into a `WARNING` (a probe that could not run counts as no evidence).

In service-account mode an empty project list verifies nothing either (the project endpoint is also readable
anonymously), so it exits `1` without claiming success:

```text
Jira credential could not be verified: the current-user endpoint (/myself) is out of this credential's scopes and no projects are visible, so neither endpoint proves the credential is valid.
```

When the probe fails for a non-auth reason - any status that is not `401`/`403` (a `5xx`, a `404`, a throttled
`429`) or an unreachable API (a connection error or a timeout, which surfaces as a `requests` exception rather than
a `JIRAError`) - the outage is reported as an outage, not as a rejected credential, and the check exits `1` without
the credential-failure hint:

```text
Jira credential check failed (<ErrorType>, status: <status>): <detail>
```

`<ErrorType>` is the exception class name (`JIRAError`, `ConnectionError`, `Timeout`, ...) and `<status>` is the HTTP
status of the answer, or `None` when the request never got one (a connection error, a timeout). The client
construction path reports an unreachable server the same way, under `Jira client construction failed (...)`. A server
url that cannot be parsed at all is rejected before that, on one line
(`Invalid Jira server url (unparseable, e.g. an unbracketed IPv6 host): <url>`), so a genuine `ValueError` from inside
the client keeps its traceback instead of being reported as an outage.

An unreachable anonymous probe counts as no evidence of anonymous readability, so it does not undo a
project-visibility verdict, and a probe that could not run at all is logged at `WARNING` (a degraded guard is not a
detail). The header-less probe itself is built to stay header-less: the url it sends has its `user:password@`
component stripped, the `Authorization` header requests adds from a `~/.netrc` entry for the host is dropped once the
request is prepared, and a redirect is not followed (a redirect hop runs `rebuild_auth()`, which would re-inject that
credential). The request still merges the environment, so `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE` and the proxy
variables are honoured: discarding them would silently turn the guard into a no-op behind a mandatory proxy or an
env CA bundle.

In service-account (Bearer) mode - the only mode with two probe endpoints - a `401`/`403` from **both** endpoints
means Jira rejected the credential itself, and only then is the credential-failure hint printed. Data Center PAT and
cloud basic auth have no project-endpoint fallback, so a `401`/`403` from `/myself` is the verdict there. Common
causes:

- a `cloud_id` that does not match the site the credential is scoped to;
- a token or credential without the scopes the utility needs to read issues;
- an expired token - **pre-minted scoped API token only**: scoped API tokens expire, so re-mint it. An OAuth 2.0
  credential is exchanged for a fresh access token on every run, so it never presents an expired access token.

An invalid or disabled OAuth 2.0 client id/secret is not in this list: it fails at the token endpoint before any
Jira call (see the mint-failure paragraph above), so it never produces a `/myself` response.

Pass the token through the environment rather than on the command line:

```bash
export JIRA_CLOUD_ID="<cloud-id>"
export JIRA_TOKEN="<token>"
pyutils-jira --check-auth
```

Command-line arguments are visible to other users in `ps` output and are appended to your shell history, so use
`--token` only when the environment is not an option.

During a scan a single issue returning `401`/`403` is not taken as proof: the utility runs the same credential
probe (the verdict is cached per client and flavour, so repeated failures reuse it instead of re-probing). The
first per-issue `401` clears that cache, once per scan: the scan re-probes at most once, which is enough to catch a
bearer that expired mid-scan without probing again for every failing issue, and the fresh verdict is reused for the
rest of the scan.

A per-issue transport failure (a connection error, a timeout, an exhausted retry) does not abort the scan either: it
is rendered as one finding of its own, `"<KEY> Jira connection failed (<ErrorType>): <detail>"`, and every such
finding reaches the aggregate report. A scan retries a `5xx` and a `JIRAError` that carries no status code at all (the
shape python-jira wraps a connection error in); a raw `requests.Timeout`/`ConnectionError` that reaches the scan
without that wrapper is not retried by this layer and is reported per issue straight away. A `429` is left to
python-jira's `ResilientSession`, which retries a `429` (and a `503` carrying `Retry-After`) internally with its own
backoff and jitter, honouring `Retry-After` best-effort - how long it sleeps depends on the installed python-jira
version, so no fixed formula and no minimum wait is promised here. That is also why there is no second layer: it
would add tens of seconds to minutes per throttled issue, serially. A `503` carrying `Retry-After` is still retried by
both layers here; a `503` without one is a `5xx`, so it is retried by this layer.

In service-account (Bearer) mode - the only mode with two probe endpoints - a `401`/`403` from **both** of them means
the credential is genuinely rejected. A Data Center PAT or cloud basic auth has no project-endpoint fallback: its
probe is called without the service-account path, so a single `401`/`403` from `/myself` is already the verdict.
Either way the hint is logged once at `ERROR` and the run exits `1` without a per-issue report.

Otherwise the failure is treated as a per-issue permission problem: the scan continues, the failures are
aggregated at the end and the run still exits `1`. That covers a probe that succeeds and an inconclusive one alike
(a project endpoint outage or an empty project list): inconclusive is not verification, but it does not abort a
scan either. The scan runs the same two-endpoint probe `--check-auth` uses, including the project-endpoint fallback
for a scope-limited service-account credential (either flavour) but not its extra anonymous cross-check, so a
credential that passes `--check-auth` does not abort a scan. An unreachable API during that probe is an outage rather
than a rejection (it is logged at `WARNING`), so it likewise does not abort the scan with a credential hint.

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
export JIRA_CLOUD_ID="4a4f8f8f-1234-5678-9abc-def012345678"
export JIRA_TOKEN="<ATST-token>"
pyutils-jira --check-auth
```

### Example: Atlassian Cloud service account, OAuth 2.0 client credentials

```yaml
pyutils-jira:
  cloud_id: 4a4f8f8f-1234-5678-9abc-def012345678
  client_id: <oauth client id>
  client_secret: ATOA...
```

Equivalent from the CLI. The secret is exported into the environment rather than passed on the command line, so it
stays out of `ps` output (the `export` line itself still lands in your shell history):

```bash
export JIRA_CLOUD_ID="4a4f8f8f-1234-5678-9abc-def012345678"
export JIRA_SA_CLIENT_ID="<oauth-client-id>"
export JIRA_SA_CLIENT_SECRET="<atoa-client-secret>"
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
- `client_id` and `client_secret` resolve per key, and the option state decides - the same shape as `--cloud-id`:
  an explicitly passed `--client-id`/`--client-secret` wins **even when it is empty** (the empty pair is the
  documented opt-out, and it beats the environment and the config file alike); otherwise the environment variable
  (`JIRA_SA_CLIENT_ID`/`JIRA_SA_CLIENT_SECRET`, or the aliases `ATLASSIAN_OAUTH_CLIENT_ID`/`JIRA_OAUTH_CLIENT_SECRET`,
  the `JIRA_SA_*` name winning when both are set); otherwise the config key. Unlike `token`/`url`, the OAuth pair is
  therefore *not* an `os.getenv()` option default - both defaults are `None`, so "option not passed" stays
  distinguishable from "option passed empty";
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
