# CLI reference

All commands read `forgeo.yaml` from the current directory; pass
`--config <file>` to use a different one. `start`, `once`, `status`,
`validate`, `stop` and `restart` also accept `--name <instance>` to resolve
the config from the **instance registry** — see [`forgeo instance`](#forgeo-instance)
below.

```
forgeo --version
forgeo <command> --help
```

Bare `forgeo` (no subcommand) starts the guided wizard when no config exists,
and prints the CLI help once a config is present.

## `forgeo init`

Guided first-time setup: interactively write a `forgeo.yaml`.

| Flag | Description |
| --- | --- |
| `--config <file>` | Where to write the config (default `forgeo.yaml`). |
| `--force` | Overwrite an existing config file. |

Exit codes:

- `0` — config written.
- `2` — a config already exists and `--force` was not given.
- `130` — setup aborted; nothing was written.

See [Getting Started](getting-started.md) for what the wizard asks.

## `forgeo start`

Start the scheduled forgeo daemon for a repository. By default the daemon is
started **detached in the background** and the command exits immediately; the
daemon keeps running, logs to `log_file`, and is managed with `forgeo stop`
and `forgeo restart`. Pass `-f`/`--foreground` to run the daemon in the
foreground instead, interruptible with Ctrl-C.

| Flag | Description |
| --- | --- |
| `--config <file>` | Forgeo YAML file (default `forgeo.yaml`). Mutually exclusive with `--name`. |
| `--name <name>` | Registered instance name resolved from the registry. Mutually exclusive with `--config`. |
| `--interval-minutes <n>` | Override the schedule interval from the config for this run. |
| `-f`, `--foreground` | Run the daemon in the foreground instead of starting it detached. |

The daemon wakes every `interval_minutes` and runs one cycle. When no config
exists, `forgeo start` offers the guided setup. A second `start` (or `once`)
is refused while the per-forgeo lock is held. Before detaching, `start` runs
the same read-only checks as `forgeo validate` and refuses to start when it
finds problems, so a broken config never leaves a silently dead daemon.

When given `--config` and that config is not in the instance registry yet,
`forgeo start` registers it automatically under the config's `name` field —
no `forgeo instance add` needed. (With `--name` the instance must already be
registered.)

While running it logs to `log_file` and writes its live state (pid, last
outcome, next run) to `daemon.state.json` next to the backlog. It binds no
ports — the web dashboard for it is served by `forgeo web`
(see [Web console & HTTP API](web-console-api.md)).

When `forgeo start` begins a cycle, Forgeo checks PyPI at most once a day
for a newer `forgeo-cli` release and, when one exists, prints/logs a short
notice naming the newer version and the upgrade command (re-run the
`install.sh` one-liner, or `pipx upgrade forgeo-cli` /
`pip install --user --upgrade forgeo-cli`). The check is best-effort: it
never auto-updates or modifies the install, a network or parse failure is
logged and skipped, and it can be disabled with `FORGEO_UPDATE_CHECK=0`.

## `forgeo once`

Run exactly **one cycle** and exit; no daemon needed.

| Flag | Description |
| --- | --- |
| `--config <file>` | Forgeo YAML file (default `forgeo.yaml`). Mutually exclusive with `--name`. |
| `--name <name>` | Registered instance name resolved from the registry. Mutually exclusive with `--config`. |

`forgeo once` shares the run lock with the daemon, so it never overlaps a
running `forgeo start` — useful to test a config or process a backlog without
leaving a daemon up. On success it prints `Cycle finished: <outcome>`.

Outcomes a cycle can produce:

| Outcome | Meaning |
| --- | --- |
| `task` | A task ran and finished. |
| `refactor` | A refactoring pass ran (backlog was empty). |
| `blocked` | A `BLOCKED` task exists; `BLOCKER.md` re-rendered from the backlog; paused. |
| `paused` | A blocker file exists; nothing ran. |
| `dirty` | The working tree was dirty; the task was not started. |
| `skipped` | A previous run was still in progress (daemon only). |
| `error` | A cycle crashed (daemon only). |

## `forgeo status`

Print a read-only summary of Forgeo. Never starts an agent.

| Flag | Description |
| --- | --- |
| `--config <file>` | Forgeo YAML file (default `forgeo.yaml`). Mutually exclusive with `--name`. |
| `--name <name>` | Registered instance name resolved from the registry. Mutually exclusive with `--config`. |

Output:

```
name: my-forgeo
repo: /path/to/repo
interval: 30 min
branch: main
backlog: OPEN=2 BLOCKED=1 COMPLETED=5 FAILED=0
next: TASK-001 — First open
daemon: not running
last outcome: task
```

- `backlog` — per-status task counts.
- `next` — the oldest `OPEN` task whose dependencies are all `COMPLETED` (the
  one Forgeo will pick next), or `(none)` when nothing is runnable.
- `waiting on` — when present, names the oldest `OPEN` task that is *not* yet
  runnable and the dependency ids keeping it waiting (with their current
  status, or `missing`) — e.g. `waiting on: TASK-002 (needs COMPLETED:
  TASK-001 (OPEN))`. Omitted when no `OPEN` task is blocked on a dependency.
- `daemon` — whether the per-forgeo lock is currently held.
- `last outcome` — the most recent run recorded in `runs.jsonl`.

## `forgeo validate`

Read-only **dry run**: checks that Forgeo is ready to run without starting
anything. It never invokes the agent and makes no writes — no lock is taken,
no backlog or snapshot is touched — so it is safe to run at any time, even
while a daemon is active.

| Flag | Description |
| --- | --- |
| `--config <file>` | Forgeo YAML file (default `forgeo.yaml`). Mutually exclusive with `--name`. |
| `--name <name>` | Registered instance name resolved from the registry. Mutually exclusive with `--config`. |

It validates, reporting **all** problems at once:

- the config file parses and matches the schema (a blank `agent_command`,
  missing `agent_sandbox_image` in docker mode, ...);
- the repository exists and is a git working tree (and `git` is on `PATH`);
- the branch resolves — a missing branch is a warning (it is created on the
  first cycle), unless the repository has no commits to create it from;
- the remote resolves when `remote` is set (`git remote get-url`);
- the backlog parses and every task is valid (a missing backlog is fine: it
  is treated as empty on the first cycle);
- the run lock state (`backlog.lock`); a held lock is a warning, since
  `forgeo start`/`forgeo once` will refuse to run until it is released.

Output on a healthy setup:

```
name: my-forgeo
repo: /path/to/repo
branch: main
agent command: claude -p "$FORGEO_TASK"
backlog: /path/to/backlog.json (2 tasks)
lock: not held

Forgeo is ready to run.
```

Exit code is `0` when no problem is found, `1` otherwise (with a summary of
every problem found).

## `forgeo stop`

Stop a running daemon gracefully (SIGTERM; a cycle in progress finishes
first).

| Flag | Description |
| --- | --- |
| `--config <file>` | Forgeo YAML file (default `forgeo.yaml`). Mutually exclusive with `--name`. |
| `--name <name>` | Registered instance name resolved from the registry. Mutually exclusive with `--config`. |
| `--timeout <seconds>` | How long to wait for the daemon to exit (default `600`). |

Exit code is `0` on success, `1` when Forgeo is not running, the lock
records a dead PID, or the daemon did not exit within the timeout.

Like `start`, a `--config` invocation registers Forgeo under its config's
`name` when it is not in the registry yet.

## `forgeo restart`

Stop the daemon when running, then start it again **in the background**
(detached), re-reading `forgeo.yaml`.

A running daemon already re-reads `forgeo.yaml` on the next cycle when the
file changes (or on `SIGHUP`), so a plain config edit needs no restart.
`restart` is still the way to apply changes to the `repo`, `backlog`,
`blocker_file` or `log_file` paths: the daemon pins those to its startup
values while running so its lock files are never detached from the config.

| Flag | Description |
| --- | --- |
| `--config <file>` | Forgeo YAML file (default `forgeo.yaml`). Mutually exclusive with `--name`. |
| `--name <name>` | Registered instance name resolved from the registry. Mutually exclusive with `--config`. |
| `--timeout <seconds>` | How long to wait for the old daemon to exit (default `600`). |

On success it prints the new daemon PID and interval.

### `--config` vs `--name`

On `start`, `once`, `status`, `validate`, `stop` and `restart`, `--name`
resolves the `forgeo.yaml` from the instance registry instead of reading
`--config`. The two flags are mutually exclusive — passing both is an
argparse error. An unknown instance name prints a clear error and exits
non-zero.

`start` and `stop` with `--config` register Forgeo under its config's
`name` when it is not registered yet, so instances are created automatically
the first time a Forgeo is started or stopped.

## `forgeo instance`

Register, list, and unregister named forgeo instances. Instances live in a
registry file — `$FORGEO_REGISTRY` or `~/.config/forgeo/instances.yaml` — that
maps each name to the absolute path of its `forgeo.yaml` (see
[Configuration](configuration.md#instance-registry)).

### `forgeo instance add <name> --config <file>`

Register an existing `forgeo.yaml` under a stable name. Optional: `forgeo
start` and `forgeo stop` already register Forgeo automatically under its
config's `name` — use `add` to pre-register an explicit name or one that
differs from `config.name`.

| Flag | Description |
| --- | --- |
| `--config <file>` | Path to the `forgeo.yaml` to register. **Required.** |

- The name must match `^[a-zA-Z0-9._-]+$`; invalid or duplicate names are
  rejected with a clear error (exit `1`).
- The config is validated (it must load) before registering.
- Relative config paths are stored as absolute paths.

### `forgeo instance rm <name>`

Unregister an instance. Never touches the config file or the repository; a
missing instance prints an error and exits `1`.

### `forgeo instance list` / `forgeo list`

List every registered instance as a compact table that fits narrow
terminals: name, daemon state (running/stopped), and last outcome (from
`runs.jsonl`). `forgeo list` is a direct alias for `forgeo instance
list`. With no registered instances it prints a hint and exits `0`.

## `forgeo web`

Serve the **central multi-instance dashboard**: one page that aggregates every
registered instance. It reads each instance's data straight from its files
(`backlog.json`, `runs.jsonl`, `forgeo.log`, `BLOCKER.md`,
`daemon.state.json`), so it works whether or not each instance's daemon is
running.

| Flag | Description |
| --- | --- |
| `--host <address>` | Bind address (default `0.0.0.0`). |
| `--port <port>` | Bind port (default `8790`). |
| `-d`, `--detach` | Start the dashboard in the background and return once it binds. |
| `--token [TOKEN]` | Require a bearer token on every `/api/*` route (see below). |
| `--timeout <seconds>` | How long to wait for the dashboard to bind when detached (default `30`). |

Without `-d` the dashboard runs in the foreground; interrupt it with Ctrl-C
or stop it from another terminal with `forgeo web stop`.

The dashboard is **host-global** (one per user, not per-repo), so it cannot
reuse a per-instance `backlog.lock`. Instead it owns a lock file at
`~/.config/forgeo/web.lock` (or `$FORGEO_CONFIG_DIR/web.lock`) that records
the running PID plus its `host`/`port` (written atomically with `O_EXCL`).
A second `forgeo web -d` is refused while the lock is held; a stale lock
whose PID is dead is taken over with a warning.

**Optional bearer-token auth.** By default the dashboard is open — anyone who
can reach the port can read every instance's backlog, logs, and config, and
can add/edit/delete tasks or start/stop/restart daemons. On a shared host,
turn on auth so every `/api/*` route requires
`Authorization: Bearer <token>` and answers `401` otherwise:

```bash
forgeo web --token           # generate a token: printed once, saved to web.toml
forgeo web --token supersecret  # use your own token
```

The token is persisted to `~/.config/forgeo/web.toml` (or
`$FORGEO_CONFIG_DIR/web.toml`, mode `0600`); once it exists, auth is enabled
even without the flag, and the generated token is only ever printed at the
moment it is created. The token prompt page (`/central/login.html`) is served
without a token and stores your token in the browser; opening the console
with `?token=YOUR_TOKEN` in the URL signs in automatically. Delete
`web.toml` to go back to the open-by-default behavior.

- `GET /` — home page listing every registered instance (daemon state, last
  outcome, next run, backlog counts).
- `GET /instances/<name>/` — one instance's page: its kanban backlog, a
  Create tab with a task form, plus tabs for logs, runs, blocker, and config.

See [Web console & HTTP API](web-console-api.md) for the full API. This is
the only web dashboard: daemons themselves bind no ports.

### `forgeo web stop`

Stop the running dashboard gracefully (SIGTERM) and wait for it to exit.

| Flag | Description |
| --- | --- |
| `--timeout <seconds>` | How long to wait for the dashboard to exit (default `30`). |

Exit code is `0` on success, `1` when the dashboard is not running, the lock
records a dead PID, or it did not exit within the timeout. The lock file is
removed on success.

### `forgeo web status`

Print whether the dashboard is running.

```
central dashboard: not running
central dashboard: running (pid 12345, http://127.0.0.1:8790)
```

Exit code is `0` whether it is running or not (the output says which).

## Process checks

```bash
pgrep -af forgeo    # process check; empty output = not running
```
