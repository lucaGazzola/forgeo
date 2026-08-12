# Configuration reference

Forgeo reads `forgeo.yaml` from the current directory (pass
`--config <file>` to any command to use a different one). The file is loaded
and validated on every invocation; relative paths resolve against the config
file's own directory, so a config can live anywhere and still point at sibling
directories.

The daemon reads `forgeo.yaml` **only at startup** — after editing the config
use `forgeo restart` so it re-reads the file.

## Keys

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | `forgeo` | Display name (logs, commit messages, Telegram notifications). |
| `repo` | `.` | The git repository Forgeo works on. |
| `interval_minutes` | `60` | How often Forgeo runs (≥ 1). |
| `branch` | `main` | The single branch everything is committed to. |
| `remote` | — | Remote to push to (e.g. `origin`); omit to only commit locally. |
| `backlog` | `backlog.json` | The task backlog JSON. Keep it outside the repo if you can. |
| `blocker_file` | `BLOCKER.md` | Where `BLOCKER.md` is written. Keep it outside the repo so it is never committed. |
| `agent_command` | — | The coding agent: any shell command (string) or argv list. **Required.** |
| `agent_timeout_seconds` | — | Optional: kill the agent after this many seconds (`null` = never). |
| `agent_env` | `{}` | Extra environment variables for the agent process. |
| `agent_sandbox` | `none` | Agent isolation: `none` (runs on the host) or `docker` (runs in a container). |
| `agent_sandbox_image` | — | Container image, **required when `agent_sandbox: docker`**; must contain the agent CLI and a shell. |
| `agent_sandbox_network` | `none` | Docker `--network` for the sandboxed agent (default `none` = networking disabled). |
| `agent_sandbox_mounts` | `[]` | Host paths mounted read-only into the sandboxed container (agent credentials/config). |
| `blocked_exit_code` | `2` | Exit code meaning "needs human input". |
| `no_changes_exit_code` | `3` | Exit code meaning "this task needs no code change". Exiting `0` with an unchanged tree fails the task instead. |
| `refactor_prompt` | default refactor prompt | Instruction used when the backlog is empty. |
| `log_file` | `forgeo.log` | Where the daemon writes its log. |
| `run_history_keep` | `2000` | How many finished runs `runs.jsonl` keeps (oldest trimmed atomically on append). `0` disables retention (file grows forever). |
| `git_timeout_seconds` | `120` | Kill a git subprocess after this many seconds. |
| `telegram_bot_token` | — | Telegram bot token for blocked-run notifications (disabled unless `telegram_chat_id` is also set). |
| `telegram_chat_id` | — | Chat ID that receives blocked-run notifications (disabled unless `telegram_bot_token` is also set). |
| `notify_webhook_url` | — | Vendor-neutral webhook URL that receives a JSON POST for run outcomes (Slack, Discord, ntfy, ...). Disabled when unset. |
| `notify_webhook_events` | `["blocked"]` | Which outcomes to POST to `notify_webhook_url`; a subset of `blocked`, `completed`, `failed`. |

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
**backlog** file, **logs** (`log_file`), **run history** (`runs.jsonl` next
to the backlog), **locks** (`backlog.lock` and the per-iteration run lock),
and a **`daemon.state.json`** with its live state. Because relative paths
resolve against each config file's own directory, two configs in different
directories can never share state.

The daemons bind no ports. The central dashboard (`forgeo web`, default port
`8790`) reads every instance's data straight from its files, so it works
whether or not each daemon is running — see
[Web console & HTTP API](web-console-api.md).

## Default refactor prompt

When `refactor_prompt` is omitted, Forgeo uses:

> Review the codebase for improvement opportunities that do not change
> behavior: dead code, duplication, overly complex functions, missing tests,
> outdated comments. Apply the safe improvements you find and run the test
> suite to verify nothing broke. If nothing needs refactoring, make no
> changes.
