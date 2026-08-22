# Configuration reference

Forgeo reads `forgeo.yaml` from the current directory (pass
`--config <file>` to any command to use a different one). The file is loaded
and validated on every invocation; relative paths resolve against the config
file's own directory, so a config can live anywhere and still point at sibling
directories.

The daemon watches `forgeo.yaml` and re-reads it on the next cycle boundary
when the file changes (or on `SIGHUP`): a valid change is applied from the
next cycle, an invalid one is logged and the last valid config stays in use.
Relocating the `repo`, `backlog`, `blocker_file` or `log_file` paths is
deferred to a restart (the daemon's lock files stay pinned to its startup
paths), so `forgeo restart` is still used for those.

## Keys

| Key | Default | Meaning |
| --- | --- | --- |
| <span style="white-space: nowrap">`name`</span> | `forgeo` | Display name (logs, commit messages, Telegram notifications). |
| <span style="white-space: nowrap">`repo`</span> | `.` | The git repository Forgeo works on. |
| <span style="white-space: nowrap">`interval_minutes`</span> | `60` | How often Forgeo runs (≥ 1). |
| <span style="white-space: nowrap">`branch`</span> | `main` | The single branch everything is committed to. |
| <span style="white-space: nowrap">`remote`</span> | — | Remote to push to (e.g. `origin`); omit to only commit locally. |
| <span style="white-space: nowrap">`backlog`</span> | `backlog.json` | The task backlog: the path of a JSON file, an HTTP endpoint serving the same document, or a base URL for `jira`/`github`/`gitlab` providers. |
| <span style="white-space: nowrap">`backlog_provider`</span> | `auto` | `auto` infers file/HTTP from `backlog`, or `jira`/`github`/`gitlab` when the corresponding block is present; explicitly choose `file`, `http`, `jira`, `github`, or `gitlab`. |
| <span style="white-space: nowrap">`state_dir`</span> | — | Directory for Forgeo's runtime files (locks, run history, daemon state). Remote backlogs default this to the directory of `forgeo.yaml`. |
| <span style="white-space: nowrap">`backlog_auth`</span> | — | OAuth2 client credentials for a backlog URL that requires them (see [below](#backlog_auth)). Only for `http` provider. |
| <span style="white-space: nowrap">`jira`</span> | — | Jira REST, workflow, authentication, and custom-field settings. Required when `backlog_provider: jira`. |
| <span style="white-space: nowrap">`github`</span> | — | GitHub REST settings. Required when `backlog_provider: github`. |
| <span style="white-space: nowrap">`gitlab`</span> | — | GitLab REST settings. Required when `backlog_provider: gitlab`. |
| <span style="white-space: nowrap">`blocker_file`</span> | `BLOCKER.md` | Where `BLOCKER.md` is written. Keep it outside the repo so it is never committed. |
| <span style="white-space: nowrap">`agent_command`</span> | — | The coding agent: any shell command (string) or argv list. **Required.** |
| <span style="white-space: nowrap">`agent_timeout_seconds`</span> | — | Optional: kill the agent after this many seconds (`null` = never). |
| <span style="white-space: nowrap">`agent_env`</span> | `{}` | Extra environment variables for the agent process. |
| <span style="white-space: nowrap">`agent_sandbox`</span> | `none` | Agent isolation: `none` (runs on the host) or `docker` (runs in a container). |
| <span style="white-space: nowrap">`agent_sandbox_image`</span> | — | Container image, **required when `agent_sandbox: docker`**; must contain the agent CLI and a shell. |
| <span style="white-space: nowrap">`agent_sandbox_network`</span> | `none` | Docker `--network` for the sandboxed agent (default `none` = networking disabled). |
| <span style="white-space: nowrap">`agent_sandbox_mounts`</span> | `[]` | Host paths mounted read-only into the sandboxed container (agent credentials/config). |
| <span style="white-space: nowrap">`blocked_exit_code`</span> | `2` | Exit code meaning "needs human input". |
| <span style="white-space: nowrap">`no_changes_exit_code`</span> | `3` | Exit code meaning "this task needs no code change". Exiting `0` with an unchanged tree fails the task instead. |
| <span style="white-space: nowrap">`refactor_prompt`</span> | default refactor prompt | Instruction used when the backlog is empty. |
| <span style="white-space: nowrap">`task_context`</span> | — | Optional path to a file (e.g. `CONTEXT.md`) whose contents are prepended to every agent instruction. |
| <span style="white-space: nowrap">`log_file`</span> | `forgeo.log` | Where the daemon writes its log. |
| <span style="white-space: nowrap">`run_history_keep`</span> | `2000` | How many finished runs `runs.jsonl` keeps (oldest trimmed atomically on append). `0` disables retention (file grows forever). |
| <span style="white-space: nowrap">`run_output_lines`</span> | `200` | How many agent output lines each run record keeps in `runs.jsonl` (the bounded tail of the agent's stdout/stderr). `0` disables persisting agent output. |
| <span style="white-space: nowrap">`agent_response_lines`</span> | — (unbounded) | How many agent output lines the task's `agent_response` keeps on a status transition (the bounded tail of the agent's stdout/stderr, shown in the task modal / available to a backlog consumer). Omit = unbounded; `0` disables persisting agent output on the task. |
| <span style="white-space: nowrap">`failed_retry_max`</span> | `0` | How many times a `FAILED` task is retried automatically. `0` (default) = a `FAILED` task stays `FAILED` until a human reopens it, exactly as before. A task may override this budget per-task with `retries_left`. |
| <span style="white-space: nowrap">`failed_retry_wait_cycles`</span> | `1` | How many cycles a retry-eligible `FAILED` task waits (backoff) before it is moved back to `OPEN`. |
| <span style="white-space: nowrap">`no_changes_retry_max`</span> | `0` | How many times a task whose agent exits `0` without producing any code changes is re-run immediately, in the same cycle, before the task is marked `BLOCKED` for human review. `0` (default) = a silent no-change SUCCESS is marked `BLOCKED` on the first attempt. |
| <span style="white-space: nowrap">`git_timeout_seconds`</span> | `120` | Kill a git subprocess after this many seconds. |
| <span style="white-space: nowrap">`telegram_bot_token`</span> | — | Telegram bot token for blocked-run notifications (disabled unless `telegram_chat_id` is also set). |
| <span style="white-space: nowrap">`telegram_chat_id`</span> | — | Chat ID that receives blocked-run notifications (disabled unless `telegram_bot_token` is also set). |
| <span style="white-space: nowrap">`notify_webhook_url`</span> | — | Vendor-neutral webhook URL that receives a JSON POST for run outcomes (Slack, Discord, ntfy, ...). Disabled when unset. |
| <span style="white-space: nowrap">`notify_webhook_events`</span> | `["blocked"]` | Which outcomes to POST to `notify_webhook_url`; a subset of `blocked`, `completed`, `failed`. |

## Minimal example

```yaml
name: my-project
repo: .
interval_minutes: 30
branch: main

backlog: .forgeo/backlog.json
blocker_file: .forgeo/BLOCKER.md

agent_command: "claude -p \"$FORGEO_TASK\""
refactor_prompt: >
  Review the codebase for improvement opportunities that do not change
  behavior, run the test suite, and apply safe changes.
```

## Jira backlog

Forgeo can read and update Jira issues directly. Set `backlog_provider: jira`
and make `backlog` the Jira base URL. Jira issue keys become task ids. The JQL
should include every lifecycle state that should be visible to Forgeo; do not
filter it to only `To Do`, or completed and blocked issues will disappear from
the dashboard and dependency checks.

```yaml
backlog_provider: jira
backlog: https://jira.example.com
state_dir: .forgeo

jira:
  jql: 'project = APP AND labels = forgeo'
  project_key: APP                 # Needed for task creation from the web UI.
  issue_type: Task
  auth:
    scheme: basic                  # Jira Cloud; use bearer for a Server/DC PAT.
    username_env: JIRA_USER
    token_env: JIRA_TOKEN
  workflow:
    open_statuses: ["10000", "10001"]
    open_status: "10000"
    running_status: "3"
    completed_status: "10002"
    blocked_status: null            # Optional Jira workflow transition.
    failed_status: null             # Failed is represented by a Forgeo label.
  fields:
    acceptance_criteria: customfield_10042
    dependencies: customfield_10043
```

Status values may be names, but stable Jira status ids are preferred. Forgeo
adds `forgeo-running`, `forgeo-blocked`, and `forgeo-failed` labels as needed.
Engine-managed details (`blocker_reason`, failure details, retry counters and
the bounded agent response) are stored in a Jira issue property, whose key is
`forgeo` by default. A Jira workflow does not need a custom `FAILED` status.

Before starting the daemon, set the configured environment variables and run:

```bash
export JIRA_USER='automation@example.com'
export JIRA_TOKEN='...'
forgeo validate
```

Forgeo transitions an issue to `running_status` before invoking the agent and
releases stale claims after `claim_timeout_seconds` (one day by default). The
web console can create, edit, reopen, and delete Jira issues when the required
project and custom-field mappings are configured; Jira remains the source of
truth for human changes.

### Jira settings

| Key | Default | Meaning |
| --- | --- | --- |
| `jira.jql` | — | Required JQL scope. Include all lifecycle states that Forgeo must see. |
| `jira.auth` | — | Required credentials. Use `basic` with a username and API-token environment variable, or `bearer` with a token environment variable. |
| `jira.project_key` | — | Jira project key used when the web console creates issues. |
| `jira.issue_type` | `Task` | Jira issue type name used for creation. |
| `jira.api_version` | `3` | API version. v3 uses Jira Cloud's cursor-based `/search/jql`; v2 uses offset-based search. |
| `jira.page_size` | `50` | Issues requested per search page, from 1 to 100. |
| `jira.max_issues` | `1000` | Maximum issues read from one JQL search. |
| `jira.timeout_seconds` | `30` | Timeout for each Jira REST request. |
| `jira.claim_timeout_seconds` | `86400` | Age after which an abandoned running claim is released. |
| `jira.label_prefix` | `forgeo` | Prefix for the running, blocked, and failed labels. |
| `jira.property_key` | `forgeo` | Jira issue-property key holding Forgeo engine state. |
| `jira.workflow` | defaults | Status ids or names for open, running, blocked, completed, and failed transitions. |
| `jira.fields` | — | Optional custom-field ids for task attributes such as acceptance criteria and dependencies. |

### GitHub backlog

Forgeo can read and update GitHub issues directly. Set `backlog_provider: github` and make `backlog` the GitHub API base URL. Issue numbers become task ids. Labels and a hidden JSON block in the issue body hold Forgeo's engine state.

```yaml
backlog_provider: github
backlog: https://api.github.com
github:
  repo: owner/repo
  token_env: GITHUB_TOKEN
  label_prefix: forgeo
```

| Key | Default | Meaning |
| --- | --- | --- |
| `github.repo` | — | Required owner/repo. |
| `github.auth` | — | Required PAT env var `token_env`. |
| `github.label_prefix` | `forgeo` | Prefix for running/blocked/failed labels. |
| `github.property_key` | `forgeo` | Marker key for hidden body block (symmetry). |
| `github.page_size` | `30` | Issues per page. |
| `github.max_issues` | `1000` | Max issues read. |
| `github.timeout_seconds` | `30` | HTTP timeout. |
| `github.claim_timeout_seconds` | `86400` | Stale claim timeout. |
| `github.workflow` | defaults | State/label mapping. |
| `github.fields` | — | Optional field mappings. |

### GitLab backlog

```yaml
backlog_provider: gitlab
backlog: https://gitlab.example.com
gitlab:
  repo: group/project
  token_env: GITLAB_TOKEN
```

| Key | Default | Meaning |
| --- | --- | --- |
| `gitlab.repo` | — | Required project path or numeric id. |
| `gitlab.auth` | — | Required PAT env var `token_env`. |
| `gitlab.label_prefix` | `forgeo` | Prefix for labels. |
| `gitlab.property_key` | `forgeo` | Marker key. |
| `gitlab.page_size` | `30` | Issues per page. |
| `gitlab.max_issues` | `1000` | Max issues. |
| `gitlab.timeout_seconds` | `30` | HTTP timeout. |
| `gitlab.claim_timeout_seconds` | `86400` | Stale claim timeout. |
| `gitlab.workflow` | defaults | State mapping. |
| `gitlab.fields` | — | Optional field mappings. |

## Key details

### `backlog_auth`

Credentials for a backlog URL behind an identity provider. Forgeo requests an
access token with the OAuth2 **client-credentials grant** and sends it as a
bearer on every backlog request, so it authenticates as a service rather than
as a person:

```yaml
backlog: https://api.example.com/api/forgeo/backlog
backlog_auth:
  token_url: https://keycloak.example.com/realms/dev/protocol/openid-connect/token
  client_id: forgeo
  client_secret_env: FORGEO_BACKLOG_CLIENT_SECRET
  scope: forgeo-backlog       # optional
  timeout_seconds: 10         # optional
```

| Key | Meaning |
| --- | --- |
| <span style="white-space: nowrap">`token_url`</span> | The provider's token endpoint. |
| <span style="white-space: nowrap">`client_id`</span> | The confidential client requesting the token. |
| <span style="white-space: nowrap">`client_secret_env`</span> | **Name of the environment variable** holding that client's secret. |
| <span style="white-space: nowrap">`scope`</span> | Optional scope requested with the token. |
| <span style="white-space: nowrap">`timeout_seconds`</span> | Timeout for the token request (default `10`). |

The secret itself is never a config value: `client_secret_env` names the
environment variable the daemon reads it from, so the secret stays out of
`forgeo.yaml` (which the web console serves to your browser) and out of any
copy or backup of it. A missing variable fails the cycle with a message naming
the variable.

Tokens are cached in memory and renewed shortly before they expire; nothing is
written to disk. If the endpoint rejects a token Forgeo believed to be valid
(HTTP 401/403), it requests a fresh one and retries the request once, so a key
rotation does not cost a cycle.

With Keycloak, this means a client with *Client authentication* on and
*Service accounts roles* enabled: the resulting `service-account-<client-id>`
user is what your backend authorizes.

`backlog_auth` is rejected when `backlog` is a file — that combination is
almost always a typo in the backlog value.

### `agent_command`

Any shell command (string) or argv list. It is run with the repository as its
working directory and the task delivered via the `FORGEO_TASK` environment
variable. A string is executed with `sh -c`; an argv list is executed directly
without a shell. See [Agent contract](agent-contract.md).

```yaml
agent_command: "claude -p \"$FORGEO_TASK\""
# or, as an argv list (no shell involved):
agent_command: ["aider", "--message", "$FORGEO_TASK"]
```

### `agent_timeout_seconds`

When set, the agent process is killed after this many seconds and the task
fails. When `null` (the default) the agent runs to completion. A run that
overruns `interval_minutes` never kills anything — the next iteration simply
skips while the previous run is still active.

### `agent_env`

Extra environment variables merged into the agent process environment. They
are merged *over* the process environment but *under* the `FORGEO_*`
variables (which are set unconditionally).

```yaml
agent_env:
  OPENAI_API_KEY: sk-...
  MODEL: claude-sonnet-4
```

### `blocked_exit_code`

The exit code the agent uses to signal "I need a human decision" — see
[Agent contract](agent-contract.md) for what happens on that exit code.
Default `2`.

### `no_changes_exit_code`

The exit code the agent uses to signal "this task needs no code change" — a
legitimate no-op that completes the task without a commit. Default `3`.

Exiting `0` while leaving the working tree unchanged is **not** accepted as a
no-op: it fails the task, because the engine cannot tell a deliberate no-op
from an agent that simply did nothing. See [Agent
contract](agent-contract.md) for the full contract.

### `agent_sandbox`

Opt-in isolation for the agent process. Default `none` runs the command
directly on the host with the user's full privileges. Set `docker` to run it
inside `docker run --rm`:

- the repository is bind-mounted into the container at the same absolute path
  (edits land on the host checkout);
- `FORGEO_TASK`, the other `FORGEO_*` variables, and every `agent_env` key
  are passed through as container environment variables;
- networking is disabled by default (`--network none`); set
  `agent_sandbox_network` to e.g. `bridge` or `host` to re-enable it;
- nothing is mounted unless listed in `agent_sandbox_mounts` (host paths such
  as agent credentials/config, mounted read-only at the same path).

`agent_sandbox_image` is required in this mode and must already contain the
agent CLI used by `agent_command` plus a POSIX shell (`sh`) — nothing is
installed at run time. The exit-code contract (0 / `blocked_exit_code` /
other) is unchanged. Forgeo needs a working `docker` binary; a missing
binary makes `forgeo start` / `forgeo once` fail fast with a clear error.

```yaml
agent_sandbox: docker
agent_sandbox_image: forgeo-agent
agent_sandbox_network: none
agent_sandbox_mounts:
  - ~/.claude
  - ~/.config/claude
```

### `blocker_file`

Where the blocker file is written. Keep it **outside the repository** (the
forgeo pauses while this file exists, and it should not be committed).
Relative paths resolve against the config file's directory.

### `remote`

When set, successful commits are pushed to `<remote> <branch>`. When omitted,
Forgeo only commits locally. A push failure never discards the commit —
the work stays committed locally and the error is logged.

### `run_history_keep`

Every finished cycle appends one line to `runs.jsonl` (next to the backlog).
On a busy Forgeo that file would grow forever and is read fully by `forgeo
status` and the web console, so Forgeo trims it on append: when the file
holds `run_history_keep` lines or more, the oldest lines are dropped before
the new record is written. Trimming is atomic (temp file + rename), so a
reader never sees a half-trimmed file, and a failed trim is logged and
skipped — it can never change the outcome of a cycle.

Set `0` to disable retention entirely, keeping the original grow-forever
behavior.

### `run_output_lines`

Every run record in `runs.jsonl` can carry the tail of what the agent printed
(stdout and stderr), so a failed or blocked run is fully explainable later.
To keep the file bounded, each record stores at most `run_output_lines` lines
(the last ones) — a chatty agent can never blow up a run record. The web
console's **History** tab shows this tail in a read-only, collapsible view.

Set `0` to stop persisting agent output entirely (run records stay small and
the History tab shows nothing for them). Old run records written before this
field existed simply have no output.

### `agent_response_lines`

Alongside the per-run record above, Forgeo persists the agent's output on the
task itself (`agent_response`, shown in the task modal and available to a
backlog consumer served over HTTP). Unlike `run_output_lines` it is **unbounded
by default**: the whole stdout/stderr is stored, overwritten on each status
transition (a transition that carries no output never wipes a previously
stored response).

Set a positive value to keep only the last that many lines; set `0` to stop
persisting agent output on the task entirely.

### `failed_retry_max` / `failed_retry_wait_cycles`

Some failures are transient — a network blip, a flaky test, a dependency
version hiccup — and a retry would succeed without a human. By default
(`failed_retry_max: 0`) a `FAILED` task stays `FAILED` forever and needs a
human to reopen it, exactly as before. Set a positive `failed_retry_max` and
Forgeo retries a failed task automatically:

- while a task is `FAILED` and retry-eligible, each cycle bumps its internal
  wait counter; once `failed_retry_wait_cycles` cycles have passed it is
  moved back to `OPEN` and Forgeo picks it up again (so with the default
  `1`, the retry happens on the next cycle);
- a task that keeps failing exhausts its budget and stays `FAILED` with its
  original `failure_reason` preserved — a human reopens it as before;
- the retry count is visible in `runs.jsonl` (the run record that finally
  succeeds carries it) and in the web console (task card, task modal, and a
  **retry** column in the History tab);
- a `BLOCKED` task is **never** auto-retried — blocking still needs a human.

A single task can override the global budget with its own `retries_left`
field in the backlog (see [Backlog format](backlog.md)): set it to a number
to cap or allow retries for just that task, or `0` to opt the task out of
retries even when the config would retry it.

```yaml
failed_retry_max: 3           # retry each FAILED task up to 3 times
failed_retry_wait_cycles: 2   # back off 2 cycles before each retry
```

### `no_changes_retry_max`

An agent that exits `0` while leaving the working tree unchanged has not
completed the task — the engine cannot tell "deliberately did nothing" from
"did nothing". By default (`no_changes_retry_max: 0`) that run is marked
`BLOCKED` on the first attempt: the only acceptable outcome for a no-change
run is a blocked task awaiting human review, never a silent completion and
never a `FAILED` task.

Sometimes the agent needs a second chance (a flaky model, a transient context
issue). Set a positive `no_changes_retry_max` and Forgeo re-runs the agent
immediately, back-to-back in the **same cycle**, that many extra times; a run
that finally produces changes completes the task, and one that still produces
nothing after the budget is spent is marked `BLOCKED`.

```yaml
no_changes_retry_max: 2       # re-run the agent up to 2 extra times on a silent no-change
```

`BLOCKED` tasks are never auto-retried — a human decides whether to reopen,
split, or drop the task (see [Backlog](backlog.md)).

### Telegram notifications

Both `telegram_bot_token` **and** `telegram_chat_id` must be set for blocked
run notifications. A notification failure never changes the outcome of a
cycle — it is logged as a warning.

### Webhook notifications

For integrations that are not Telegram (Slack, Discord, ntfy, ...), set
`notify_webhook_url` to any HTTPS endpoint. Forgeo POSTs a small JSON payload
with the forgeo name, the run outcome, the task id and title, and the reason:

```json
{
  "forgeo": "my-forgeo",
  "outcome": "blocked",
  "task_id": "TASK-001",
  "task_title": "Do the thing",
  "reason": "Which retry policy should I use?"
}
```

`outcome` is one of `blocked`, `completed` or `failed`. Blocked-run
notifications are on whenever the URL is set; to also be notified on
completed or failed runs, add them to `notify_webhook_events`:

```yaml
notify_webhook_url: "https://hooks.example.com/forgeo"
notify_webhook_events:
  - blocked
  - completed
  - failed
```

The payload is the JSON body of a `POST` with `Content-Type:
application/json`; Forgeo considers any non-200 response a failure. Uses the
stdlib only, with a 5-second timeout. A failing or unreachable webhook is
logged as a warning and never changes the outcome of a cycle.

### `task_context`

Optional path to a file whose contents are prepended to **every** agent
instruction — tasks and refactoring runs alike — before the task description:

```yaml
task_context: CONTEXT.md
```

The agent receives the file's contents as the first part of `FORGEO_TASK`,
with the task appended after a `# Task` heading, so it always has the
high-level project overview instead of only the isolated task description.
The file is re-read on every run, so updates made by an earlier agent (e.g.
the agent keeping `CONTEXT.md` accurate) are picked up on the next cycle.

A missing or unreadable file never fails a cycle: it is logged as a warning,
the run proceeds with the bare task instruction, and `forgeo validate`
surfaces it as a warning too.

## Instance registry

Several factories can run side by side — one config per repository, each a
separate daemon. The **instance registry** maps a stable instance name to the
absolute path of that instance's `forgeo.yaml`, so the CLI can resolve a
config by name (`--name`) and a single command can enumerate every forgeo on
the host (`forgeo list` / `forgeo instance list`).

- **Location**: the file at `$FORGEO_REGISTRY`, or
  `~/.config/forgeo/instances.yaml` when the variable is unset.
- **Format**: a YAML mapping of instance name → absolute path of that
  instance's `forgeo.yaml`:

  ```yaml
  site-a: /home/me/projects/site-a/forgeo.yaml
  site-b: /home/me/projects/site-b/forgeo.yaml
  ```

- The file is created on the first registration — a `forgeo instance add`,
  or a `forgeo start`/`forgeo stop` whose config is not registered yet (it
  is registered under the config's `name`); a missing file reads as an empty
  registry. Writes are atomic (temp file + rename), so a crash mid-write
  never corrupts it.
- Names must match `^[a-zA-Z0-9._-]+$`; duplicates and unknown names are
  rejected with a clear error.
- `forgeo instance rm NAME` unregisters without touching the config file or
  the repository.

Manage instances with `forgeo instance add|rm|list` and `forgeo list` — see
[CLI reference](cli-reference.md).

## Per-instance isolation

Each registered instance is fully independent: every instance owns its own
**backlog**, **logs** (`log_file`), **run history** (`runs.jsonl`), **locks**
(`backlog.lock` and the per-iteration `backlog.run`), and a
**`backlog.state.json`** with its live state. Those runtime files sit next to
the backlog file, or — when the backlog is remote — in `state_dir`, which
defaults to the directory of that instance's `forgeo.yaml`. Because relative
paths resolve against each config file's own directory, two configs in
different directories can never share state.

The daemons bind no ports. The central dashboard (`forgeo web`, default port
`8790`) reads every instance's data straight from its files, so it works
whether or not each daemon is running — see
[Web console & HTTP API](web-console-api.md).

The dashboard's own settings live next to its lock, in
`~/.config/forgeo/web.toml` (or `$FORGEO_CONFIG_DIR/web.toml`): a `token`
key turns on bearer auth for every `/api/*` route. `forgeo web --token`
generates one, prints it once, and saves it here; with no `web.toml` the
dashboard stays open (no auth).

## Default refactor prompt

When `refactor_prompt` is omitted, Forgeo uses:

> Review the codebase for improvement opportunities that do not change
> behavior: dead code, duplication, overly complex functions, missing tests,
> outdated comments. Apply the safe improvements you find and run the test
> suite to verify nothing broke. If nothing needs refactoring, make no
> changes.
