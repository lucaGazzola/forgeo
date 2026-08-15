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
| <span style="white-space: nowrap">`backlog`</span> | `backlog.json` | The task backlog: the path of a JSON file (keep it outside the repo if you can), or an `http(s)` URL serving the same document — see [a backlog over HTTP](backlog.md#a-backlog-over-http). |
| <span style="white-space: nowrap">`state_dir`</span> | — | Directory for Forgeo's runtime files (locks, run history, daemon state). Only meaningful with a backlog URL, where it defaults to the directory of `forgeo.yaml`. |
| <span style="white-space: nowrap">`backlog_auth`</span> | — | OAuth2 client credentials for a backlog URL that requires them (see [below](#backlog_auth)). |
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
| <span style="white-space: nowrap">`log_file`</span> | `forgeo.log` | Where the daemon writes its log. |
| <span style="white-space: nowrap">`run_history_keep`</span> | `2000` | How many finished runs `runs.jsonl` keeps (oldest trimmed atomically on append). `0` disables retention (file grows forever). |
| <span style="white-space: nowrap">`run_output_lines`</span> | `200` | How many agent output lines each run record keeps in `runs.jsonl` (the bounded tail of the agent's stdout/stderr). `0` disables persisting agent output. |
| <span style="white-space: nowrap">`failed_retry_max`</span> | `0` | How many times a `FAILED` task is retried automatically. `0` (default) = a `FAILED` task stays `FAILED` until a human reopens it, exactly as before. A task may override this budget per-task with `retries_left`. |
| <span style="white-space: nowrap">`failed_retry_wait_cycles`</span> | `1` | How many cycles a retry-eligible `FAILED` task waits (backoff) before it is moved back to `OPEN`. |
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
the backlog file, or — when the backlog is a URL — in `state_dir`, which
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
