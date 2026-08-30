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

Run **one specific `OPEN` task** by ID (triage).

```bash
forgeo run --task TASK-012
```

| Flag | Description |
| --- | --- |
| `--task <id>` | **Required.** `OPEN` task ID. |
| `--config <file>` / `--name <name>` | Config file or registry name. |

Refuses if task missing, not `OPEN`, or a daemon/once/run holds the lock.

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
```

`waiting on` appears when the oldest `OPEN` task has unmet dependencies. `run_at` due tasks are shown ahead of older ones.

## `forgeo validate`

Read-only dry run — never invokes agent or writes.

| Flag | Description |
| --- | --- |
| `--config <file>` / `--name <name>` | Config file or registry name. |

Checks: config schema, repo is a git tree, branch/remote resolve, backlog parses (HTTP/Jira fetched once), agent command non-blank, lock state. Reports all problems at once. Exit `0` healthy, `1` otherwise.

- Missing file backlog → fine (empty on first cycle); missing branch → warning (created on first cycle).
- No commits + clean tree → warning; no commits + dirty tree → error (run `git add -A && git commit`).

## `forgeo stop` / `forgeo restart`

Graceful shutdown via SIGTERM (cycle in progress finishes first).

| Flag | Description |
| --- | --- |
| `--config <file>` / `--name <name>` | Config file or registry name. |
| `--timeout <seconds>` | Wait for exit (default `600`). |

`stop` exits `1` if not running or timeout elapses; auto-registers with `--config` if missing. `restart` stops then starts detached, re-reading `forgeo.yaml`. Config edits apply on next cycle without restart, except `repo`/`backlog`/`blocker_file`/`log_file` which need `restart`.

`--config` vs `--name` applies to `start`, `once`, `run`, `status`, `validate`, `stop`, `restart` — passing both is an error; unknown name exits non-zero.

## `forgeo instance`

Registry at `$FORGEO_REGISTRY` or `~/.config/forgeo/instances.yaml` (atomic writes).

### `forgeo instance add <name> --config <file>`

Register a `forgeo.yaml` under a stable name. Names must match `^[a-zA-Z0-9._-]+$`. Validates config before registering; relative paths stored as absolute.

### `forgeo instance rm <name>`

Unregister (never touches config/repo). Errors if unknown.

### `forgeo instance list` / `forgeo list`

Table of all instances: name, daemon state, last outcome (from `runs.jsonl`). Exits `0` with hint if none.

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
