# CLI reference

All commands read `forgeo.yaml` from the current directory; use `--config <file>` or `--name <instance>` (registry) — the two are mutually exclusive.

```
forgeo --version
forgeo <command> --help
```

Bare `forgeo` starts the wizard if no config exists, otherwise shows help.

## `forgeo init`

Guided setup — writes `forgeo.yaml`.

| Flag | Description |
| --- | --- |
| `--config <file>` | Where to write config (default `forgeo.yaml`). |
| `--force` | Overwrite existing config. |

Asks for: Forgeo folder, backlog provider, agent command, refactor prompt. Exit codes: `0` written, `2` exists without `--force`, `130` aborted.

## `forgeo start`

Start the daemon. By default **detached in background**; use `-f` for foreground.

| Flag | Description |
| --- | --- |
| `--config <file>` / `--name <name>` | Config file or registry name. |
| `--interval-minutes <n>` | Override `interval_minutes` for this run. |
| `-f`, `--foreground` | Run in foreground (Ctrl-C to stop). |

- Wakes every `interval_minutes`, runs one cycle. When no config exists, offers the wizard.
- Refuses if the lock is held; runs `validate` checks before detaching.
- Wakes early for a due `run_at` schedule.
- With `--config`, auto-registers under `config.name` if not yet in registry.
- Binds no ports — dashboard is `forgeo web`. Logs to `log_file`, state to `daemon.state.json`.

## `forgeo once`

Run **one cycle** and exit; no daemon. Shares the run lock, so never overlaps `start`.

| Flag | Description |
| --- | --- |
| `--config <file>` / `--name <name>` | Config file or registry name. |

Prints `Cycle finished: <outcome>`. Outcomes: `task`, `refactor`, `blocked`, `paused`, `dirty`, `skipped`, `error`.

## `forgeo run`

Run **one specific `OPEN` task** by ID (triage). The id may be passed
positionally (`forgeo run TASK-012`) or with `--task` — never both.

```bash
forgeo run TASK-012
forgeo run --task TASK-012
```

| Flag | Description |
| --- | --- |
| `--task <id>` / `TASK_ID` | Task ID, flag or positional (one required). |
| `--config <file>` / `--name <name>` | Config file or registry name. |

Refuses if task missing, not `OPEN`, or a daemon/once/run holds the lock.

## `forgeo task`

Manage backlog tasks from the terminal — no JSON editing or dashboard needed. Never starts an agent. Works with every provider (`file`, `http`, `github`, `gitlab`, `jira`); on issue backlogs the provider assigns the real id and it is reported back. `task next` explains the scheduler's next pick (dependencies, `run-at`, queue order). `task rm` deletes a task outright (on issue backlogs the provider closes the issue when a hard delete is not permitted). `task complete-review` / `task request-changes` triage `REVIEW` tasks from the terminal (merge the branch first, then complete it).

### `forgeo task add [--title <t> | <t>] [--description <d>]`

Create a new `OPEN` task. The title may be passed positionally
(`forgeo task add "Fix typo"`) or with `--title` — never both.
`--description` defaults to the title, so
one-liners need only a title; pass `--description`/`--description-file`
for anything needing a real spec.

```bash
forgeo task add "Fix typo in README"
forgeo task add --title "Fix typo in README"
forgeo task add --title "Add login page" --description "Build it with tests."
forgeo task add --title "Ship it" --description "Do it." --id TASK-042 \
  --acceptance "pytest passes" --acceptance "ruff check is clean" \
  --depends-on TASK-001
forgeo task add --title "Hotfix" --description "Ship today." --run-at now
forgeo task add --title "After deploy" --description "Migrate." --run-at 2026-10-01T09:00:00Z
# Multiline specs without shell quoting — file or stdin:
forgeo task add --title "Big spec" --description-file spec.md
cat spec.md | forgeo task add --title "Big spec" --description-file -
echo "Do it." | forgeo task add --title "Quick" --description -
```

| Flag | Description |
| --- | --- |
| `--title <t>` / `TITLE` | Task title, flag or positional (one required). |
| `--description <d>` | What the agent should do (default: the title; use `-` to read from stdin; not with `--description-file`). |
| `--description-file <file>` | Read the description from `FILE` (`-` for stdin; not with `--description`). |
| `--id <id>` | Task id (default: next `TASK-###`; must be unique). |
| `--acceptance <c>` | Acceptance criterion (repeatable). |
| `--depends-on <id>` | Id of a task this task waits for (repeatable). |
| `--run-at <time>` | Earliest pick time: ISO-8601, or `now` to run next (due tasks jump ahead of oldest-first order). |
| `--config <file>` / `--name <name>` | Config file or registry name. |

Refuses duplicates and blank titles/descriptions (an explicitly passed blank `--description` is still refused; omitting it files the title as the description). Prints the created id plus the `forgeo run --task <id>` shortcut to try it immediately.

### `forgeo task list [--status <s>] [--limit <n>]`

List backlog tasks and their statuses as a table (`Id`, `Status`, `Title`).

| Flag | Description |
| --- | --- |
| `--status <s>` | Only `open`, `blocked`, `failed`, `completed`, or `review` (default: all). |
| `--limit <n>` | Show at most `N` tasks (default: all). |
| `--config <file>` / `--name <name>` | Config file or registry name. |

An empty backlog hints at `forgeo task add`.

### `forgeo task next`

Show which task the scheduler would pick next and why the rest wait —
the terminal answer to "why isn't my task running?". Read-only; never
starts an agent. Mirrors the cycle's pick: while any task is `BLOCKED`
the next cycle renders `BLOCKER.md` instead of running anything,
otherwise the oldest runnable `OPEN` task wins (overdue `run_at` tasks
first, future `run_at` tasks skipped, tasks waiting on uncompleted
dependencies skipped).

```bash
forgeo task next
```

| Flag | Description |
| --- | --- |
| `--config <file>` / `--name <name>` | Config file or registry name. |

Prints `next: <id> — <title>` plus a `why:` line (oldest runnable, or
due-since for an overdue `run_at`), one `skipped:` line per waiting
`OPEN` task (`waiting on <id> (<status>)`, `scheduled for <time>`, or
`queued behind <id>`), and a directly runnable hint (`forgeo run
--task <id>` / `forgeo task show --task <id>`). When nothing is runnable
it says so (`next: (none)` with per-task `waiting:` reasons and the
earliest scheduled time, or `next: (paused)` while `BLOCKED` tasks hold
the cycle).

### `forgeo task show [TASK_ID]`

Show one task's full detail — description, acceptance criteria,
dependencies, blocker/failure reasons, and agent response. Read-only; never
starts an agent. The id may be passed positionally
(`forgeo task show TASK-003`) or with `--task` — never both. `task list`
only shows `Id`/`Status`/`Title`, so this is the
terminal equivalent of opening the task in the dashboard.

```bash
forgeo task show TASK-003
forgeo task show --task TASK-003
```

| Flag | Description |
| --- | --- |
| `--task <id>` / `TASK_ID` | Task id, flag or positional (one required). |
| `--config <file>` / `--name <name>` | Config file or registry name. |

Prints `Unknown task: <id>` for missing ids, plus a directly runnable hint
(`forgeo run --task <id>` for `OPEN` tasks, `forgeo task edit` +
`forgeo task reopen --task <id>` for `BLOCKED`/`FAILED` tasks,
`forgeo task complete-review` / `forgeo task request-changes` for `REVIEW` tasks).

### `forgeo task edit [TASK_ID] [--title <t>] [--description <d>] ...`

Update a task's fields in place — the terminal equivalent of editing it
in the dashboard, and the missing step between `task show` (see why a
task is `BLOCKED`) and `task reopen` (retry it). The id may be passed
positionally (`forgeo task edit TASK-003 ...`) or with `--task` — never
both. Never starts an agent.

```bash
forgeo task edit TASK-003 --description "Pick blue; see brand guide."
forgeo task edit --task TASK-003 --description-file spec.md
echo "New spec." | forgeo task edit --task TASK-003 --description-file -
forgeo task edit --task TASK-003 --title "New title" --acceptance "pytest passes"
forgeo task edit --task TASK-003 --clear-depends-on
forgeo task edit --task TASK-003 --run-at now   # run it next
forgeo task edit --task TASK-003 --clear-run-at # back to oldest-first order
```

| Flag | Description |
| --- | --- |
| `--task <id>` / `TASK_ID` | Task id, flag or positional (one required). |
| `--title <t>` | New title (must not be blank). |
| `--description <d>` | New description (must not be blank; `-` reads stdin; not with `--description-file`). |
| `--description-file <file>` | Read the new description from `FILE` (`-` for stdin; not with `--description`). |
| `--acceptance <c>` | Acceptance criterion (repeatable; replaces the whole list). |
| `--depends-on <id>` | Dependency task id (repeatable; replaces the whole list). |
| `--files <path>` | File the agent may touch (repeatable; replaces the whole list). |
| `--run-at <time>` | Earliest pick time: ISO-8601, or `now` to run next (not with `--clear-run-at`). |
| `--clear-acceptance` | Clear all acceptance criteria (not with `--acceptance`). |
| `--clear-depends-on` | Clear all dependencies (not with `--depends-on`). |
| `--clear-files` | Clear the files-to-modify list (not with `--files`). |
| `--clear-run-at` | Clear the scheduled run time (not with `--run-at`). |
| `--config <file>` / `--name <name>` | Config file or registry name. |

At least one edit flag is required. Prints `Updated task <id>` on
success, `Unknown task: <id>` for missing ids.

### `forgeo task reopen [TASK_ID]`

Move a `BLOCKED` or `FAILED` task back to `OPEN` (the terminal equivalent of resolving `BLOCKER.md` and reopening from the dashboard). The id may be passed positionally or with `--task`. `FAILED` tasks re-queue through the retry path.

```bash
forgeo task reopen TASK-003
forgeo task reopen --task TASK-003
```

| Flag | Description |
| --- | --- |
| `--task <id>` / `TASK_ID` | `BLOCKED` or `FAILED` task id, flag or positional (one required). |
| `--config <file>` / `--name <name>` | Config file or registry name. |

Refuses unknown ids, tasks that are already `OPEN`, and `REVIEW`/`COMPLETED` tasks (triaged in the review flow instead).

### `forgeo task rm [TASK_ID]`

Delete a task from the backlog — typos, duplicates, or tasks that
will never be done, without hand-editing JSON or opening the
dashboard. The id may be passed positionally or with `--task`.
Never starts an agent.

```bash
forgeo task rm TASK-003
forgeo task rm --task TASK-003
```

| Flag | Description |
| --- | --- |
| `--task <id>` / `TASK_ID` | Task id to delete, flag or positional (one required). |
| `--config <file>` / `--name <name>` | Config file or registry name. |

Any status can be removed. On issue backlogs the provider closes the
issue when a hard delete is not permitted. Prints `Removed task <id>`
on success, `Unknown task: <id>` for missing ids, plus a warning naming
any remaining tasks that still list the removed id in their
dependencies.

### `forgeo task complete-review [TASK_ID]`

Mark a `REVIEW` task `COMPLETED` — the terminal equivalent of the
dashboard's Complete button. Merge the review branch manually first
(PR or `git merge`), then complete it without opening the dashboard.
The id may be passed positionally or with `--task`.
Never starts an agent.

```bash
forgeo task complete-review TASK-003
forgeo task complete-review --task TASK-003
```

| Flag | Description |
| --- | --- |
| `--task <id>` / `TASK_ID` | `REVIEW` task id, flag or positional (one required). |
| `--config <file>` / `--name <name>` | Config file or registry name. |

Refuses unknown ids and tasks that are not `REVIEW`.

### `forgeo task request-changes [TASK_ID]`

Send a `REVIEW` task back to `OPEN` for rework — the terminal equivalent
of the dashboard's Request-changes button. The id may be passed
positionally or with `--task`. Never starts an agent.

```bash
forgeo task request-changes TASK-003
forgeo task request-changes --task TASK-003
```

| Flag | Description |
| --- | --- |
| `--task <id>` / `TASK_ID` | `REVIEW` task id, flag or positional (one required). |
| `--config <file>` / `--name <name>` | Config file or registry name. |

Refuses unknown ids and tasks that are not `REVIEW`.

## `forgeo status`

Read-only summary (never starts agent).

| Flag | Description |
| --- | --- |
| `--config <file>` / `--name <name>` | Config file or registry name. |

```
name: my-forgeo
repo: /path/to/repo
interval: 30 min
branch: main
backlog: OPEN=2 BLOCKED=1 COMPLETED=5 FAILED=0
next: TASK-001 — First open
daemon: not running
last outcome: task
waiting on: TASK-002 (needs COMPLETED: TASK-001 (OPEN))
blocked: TASK-003 — Needs human — first line of the blocker reason
action: resolve BLOCKED tasks above (BLOCKER.md / `forgeo web`), then `forgeo task reopen --task <id>`
```

`waiting on` appears when the oldest `OPEN` task has unmet dependencies. `run_at` due tasks are shown ahead of older ones.

`blocked:` / `failed:` lines (up to 3 each, oldest first, with the reason's
first line) appear when tasks need attention, plus a single `action:` line
with the most useful next step (resolve + `forgeo task reopen --task <id>`,
`forgeo start`, or add tasks via `forgeo task add`).

## `forgeo logs`

Print the tail of the forgeo log file (`log_file` from `forgeo.yaml`) — the
fastest way to see why the last cycle did what it did. Read-only; never
starts an agent.

| Flag | Description |
| --- | --- |
| `--config <file>` / `--name <name>` | Config file or registry name. |
| `-n`, `--lines <N>` | How many trailing lines to print (default `100`, same as the web console). |
| `-f`, `--follow` | Keep printing appended lines like `tail -f` (Ctrl-C to stop). |

```bash
forgeo logs -n 50
forgeo logs --follow
```

When the log file does not exist yet (nothing has run), prints a hint to run
`forgeo start` or `forgeo once` first and exits `0`. `[...]` in agent output
is printed literally, never interpreted as formatting.

## `forgeo validate`

Read-only dry run — never invokes agent or writes.

| Flag | Description |
| --- | --- |
| `--config <file>` / `--name <name>` | Config file or registry name. |

Checks: config schema, repo is a git tree, branch/remote resolve, backlog parses (HTTP/Jira fetched once), agent command non-blank, lock state. Reports all problems at once. Exit `0` healthy, `1` otherwise.

- Missing file backlog → fine (empty on first cycle); missing branch → warning (created on first cycle).
- No commits + clean tree → warning; no commits + dirty tree → error (run `git add -A && git commit`).

## `forgeo check`

Run the contributor quality gates in one step — the single-command version
of the `CONTRIBUTING.md` checklist. Read-only; needs no config file and
never starts an agent.

```bash
forgeo check
```

Runs `pytest`, then `ruff check`, then `mypy src/forgeo` (each via the
current Python environment), prints each tool's output under its own
header, and ends with a summary line such as
`check: PASS (pytest: PASS, ruff: PASS, mypy: PASS)`. Exits `0` only when
every gate passes; exits `1` when any gate fails or a gate tool is not
installed (with a `pip install -e ".[dev]"` hint).

## `forgeo stop` / `forgeo restart`

Graceful shutdown via SIGTERM (cycle in progress finishes first).

| Flag | Description |
| --- | --- |
| `--config <file>` / `--name <name>` | Config file or registry name. |
| `--timeout <seconds>` | Wait for exit (default `600`). |

`stop` exits `1` if not running or timeout elapses; auto-registers with `--config` if missing. `restart` stops then starts detached, re-reading `forgeo.yaml`. Config edits apply on next cycle without restart, except `repo`/`backlog`/`blocker_file`/`log_file` which need `restart`.

`--config` vs `--name` applies to `start`, `once`, `run`, `task add`, `task list`, `task show`, `task edit`, `task reopen`, `task rm`, `task complete-review`, `task request-changes`, `status`, `logs`, `validate`, `stop`, `restart` — passing both is an error; unknown name exits non-zero.

## `forgeo instance`

Registry at `$FORGEO_REGISTRY` or `~/.config/forgeo/instances.yaml` (atomic writes).

### `forgeo instance add <name> --config <file>`

Register a `forgeo.yaml` under a stable name. Names must match `^[a-zA-Z0-9._-]+$`. Validates config before registering; relative paths stored as absolute.

### `forgeo instance rm <name>`

Unregister (never touches config/repo). Errors if unknown.

### `forgeo instance list` / `forgeo list`

Table of all instances: name, daemon state, last outcome (from `runs.jsonl`). Exits `0` with hint if none.

## `forgeo auth`

Browser/OAuth login for `github`/`gitlab`/`jira` (alternative to a PAT in an environment variable). This is separate from `backlog_auth`, which uses OAuth2 client credentials for an HTTP backlog. Tokens are stored `0600` in `~/.config/forgeo/tokens/` and read by `forgeo validate`/`start`/`once`.

Create an OAuth application with the provider before logging in. GitHub defaults to the device flow, GitLab defaults to browser PKCE, and Jira Cloud supports browser PKCE only. Browser flows listen on `127.0.0.1`; the port is ephemeral unless `--callback-port` is supplied. If the provider requires an exact registered redirect URI, register `http://127.0.0.1:<port>/callback` and use that same port in the command.

### `forgeo auth login --provider <github|gitlab|jira>`

| Flag | Description |
| --- | --- |
| `--provider` | `github` (default) / `gitlab` / `jira`. |
| `--config <file>` | `forgeo.yaml` to read `auth.oauth.client_id` (defaults to `./forgeo.yaml`). |
| `--client-id <id>` | OAuth client ID (overrides config; required if not in `forgeo.yaml`). |
| `--flow <device|browser>` | `device` for GitHub, `browser` for GitLab/Jira. GitLab device flow is available only when enabled by the GitLab instance; Jira is browser-only. |
| `--scope <scope>` | Scope (default `repo` / `api` / `offline_access read:jira-user read:jira-work`). |
| `--token-file <path>` | Where to store token (default per-provider per-host `~/.config/forgeo/tokens/<provider>.json`). |
| `--callback-port <port>` | Fixed `127.0.0.1` port for browser callbacks. Default is an ephemeral port; use this when the OAuth app requires an exact callback URI. |
| `--cloud-id <id>` | Jira Cloud ID (overrides `jira.auth.oauth.cloud_id`). |
| `--api-base <url>` | Provider API base (default from `forgeo.yaml` backlog or `https://api.github.com`/`https://gitlab.com`). |
| `--no-open-browser` | Print the authorization URL instead of opening it automatically. Applies to browser and device flows. |

Device flow: prints a verification URL and user code, then polls the provider token endpoint. Browser flow: opens (or prints) the provider authorization URL with a PKCE `code_challenge`, listens on loopback, and exchanges the callback code for an access token. Jira also discovers an Atlassian `cloud_id` and stores a refresh token when one is returned. If `client_secret_env` is configured for Jira, export that variable for token refreshes by the daemon. `forgeo init` with OAuth writes `auth.oauth` and offers to run login immediately.

### `forgeo auth status --provider <p>` / `forgeo auth logout --provider <p>`

```bash
forgeo auth status --provider github
forgeo auth status --provider gitlab
forgeo auth status --provider jira
forgeo auth logout --provider github
```

`status` masks the token and shows `scope`/`expires_in`/`cloud_id` (Jira). `logout` removes the file. Both honor `--token-file`/`--api-base`/`--config` and use the project-local `forgeo.yaml` when it exists; pass `--config` for a config elsewhere.

## `forgeo web`

Central dashboard aggregating every registered instance (reads files directly, works whether daemons are running).

| Flag | Description |
| --- | --- |
| `--host <addr>` | Bind address (default `0.0.0.0`). |
| `--port <port>` | Bind port (default `8790`). |
| `-d`, `--detach` | Start in background, return once bound. |
| `--token [TOKEN]` | Bearer auth for `/api/*` (see below). |
| `--timeout <s>` | Wait for bind when detached (default `30`). |

Without `-d`, runs in foreground (Ctrl-C). Host-global lock at `~/.config/forgeo/web.lock`. Second `forgeo web -d` is refused while held; stale lock is taken over.

**Bearer auth** — by default open (anyone on the port can read/mutate). On shared hosts:

```bash
forgeo web --token           # generate, print once, save to web.toml
forgeo web --token my-secret # use your own
```

Persisted to `~/.config/forgeo/web.toml` (0600); present file = auth on even without flag. `curl -H "Authorization: Bearer my-secret" http://127.0.0.1:8790/api/instances`. Static assets and `/central/login.html` stay public; `?token=...` URL auto-signs in. Delete `web.toml` to go open again.

- `GET /` — home: every instance (state, last outcome, counts). Issue providers show `Open in Jira/GitHub/GitLab ↗`.
- `GET /instances/<name>/` — kanban, Create form, logs/history/blocker/config tabs, daemon Start/Stop/Restart.

See [Web console](web-console-api.md) for the HTTP API. Daemons bind no ports — this is the only web interface.

### `forgeo web stop` / `forgeo web status`

| Flag | Description |
| --- | --- |
| `--timeout <s>` | Wait for dashboard to exit (default `30`). |

`stop` exits `0` on success, `1` if not running. `status` always exits `0`:

```
central dashboard: not running
central dashboard: running (pid 12345, http://127.0.0.1:8790)
```

## Process checks

```bash
pgrep -af forgeo
```
