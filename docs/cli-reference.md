# CLI reference

All commands read `forgeo.yaml` from the current directory; pass
`--config <file>` to use a different one. `start`, `once`, `status`, `stop`
and `restart` also accept `--name <instance>` to resolve the config from the
**instance registry** — see [`forgeo instance`](#forgeo-instance) below.

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

Start the scheduled forgeo daemon for a repository. Runs in the foreground;
interrupt with Ctrl-C or stop from another terminal with `forgeo stop`.

| Flag | Description |
| --- | --- |
| `--config <file>` | Forgeo YAML file (default `forgeo.yaml`). Mutually exclusive with `--name`. |
| `--name <name>` | Registered instance name resolved from the registry. Mutually exclusive with `--config`. |
| `--interval-minutes <n>` | Override the schedule interval from the config for this run. |

The daemon wakes every `interval_minutes` and runs one cycle. When no config
exists, `forgeo start` offers the guided setup. A second `start` (or `once`)
is refused while the per-forgeo lock is held.

When given `--config` and that config is not in the instance registry yet,
`forgeo start` registers it automatically under the config's `name` field —
no `forgeo instance add` needed. (With `--name` the instance must already be
registered.)

While running it logs to `log_file` and writes its live state (pid, last
outcome, next run) to `daemon.state.json` next to the backlog. It binds no
ports — the web dashboard for it is served by `forgeo web`
(see [Web console & HTTP API](web-console-api.md)).

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
- `next` — the oldest `OPEN` task (the one Forgeo will pick next).
- `daemon` — whether the per-forgeo lock is currently held.
- `last outcome` — the most recent run recorded in `runs.jsonl`.

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

| Flag | Description |
| --- | --- |
| `--config <file>` | Forgeo YAML file (default `forgeo.yaml`). Mutually exclusive with `--name`. |
| `--name <name>` | Registered instance name resolved from the registry. Mutually exclusive with `--config`. |
| `--timeout <seconds>` | How long to wait for the old daemon to exit (default `600`). |

On success it prints the new daemon PID and interval.

### `--config` vs `--name`

On `start`, `once`, `status`, `stop` and `restart`, `--name` resolves the
`forgeo.yaml` from the instance registry instead of reading `--config`. The
two flags are mutually exclusive — passing both is an argparse error. An
unknown instance name prints a clear error and exits non-zero.

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
| `--timeout <seconds>` | How long to wait for the dashboard to bind when detached (default `30`). |

Without `-d` the dashboard runs in the foreground (like `forgeo start`);
interrupt it with Ctrl-C or stop it from another terminal with
`forgeo web stop`.

The dashboard is **host-global** (one per user, not per-repo), so it cannot
reuse a per-instance `backlog.lock`. Instead it owns a lock file at
`~/.config/forgeo/web.lock` (or `$FORGEO_CONFIG_DIR/web.lock`) that records
the running PID plus its `host`/`port` (written atomically with `O_EXCL`).
A second `forgeo web -d` is refused while the lock is held; a stale lock
whose PID is dead is taken over with a warning.

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
