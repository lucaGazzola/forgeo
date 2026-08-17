# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -e ".[dev]"      # dev setup (Python 3.11+)

pytest                       # full suite
pytest tests/test_factory.py::test_name   # a single test
ruff check src tests         # lint (line-length 100)
python -m mypy src/forgeo    # type check (strict-ish: see [tool.mypy])
```

All three gates run in CI on Python 3.11/3.12/3.13 and must be clean before a PR.
`asyncio_mode = "auto"` is set, so `async def` tests need no decorator.

The CLI entry point is `forgeo = forgeo.cli:main`; `python -m forgeo` works too.

## Architecture

Forgeo runs a coding agent against one git repository on a schedule. It is a
plain-file system: no database, no server-side state, no ports bound by the
worker. Everything a reader (CLI, dashboard) needs is on disk next to the
backlog.

**The cycle** (`forgeo.py:Forgeo.run_cycle`) does exactly one of five things and
always appends one `RunRecord`:

1. any `BLOCKED` task exists → re-render `BLOCKER.md` from the backlog, pause (`blocked`)
2. an `OPEN` task exists but the tree is dirty → refuse (`dirty`)
3. an `OPEN` task exists → run the agent, commit + push on `branch` (`task`)
4. backlog empty and a blocker file remains → pause (`paused`)
5. backlog empty → run the agent in refactoring mode (`refactor`)

There are no feature branches or PRs: everything is `git add -A && git commit`
on the configured branch, pushed when `remote` is set.

**Agent contract** (`agent.py`, documented in `docs/agent-contract.md`) — the
agent is any shell command, run with the repo as cwd and the task in
`FORGEO_TASK` (plus `FORGEO_REPO`, `FORGEO_BRANCH`). Exit code decides the
outcome: `0` SUCCESS, `blocked_exit_code` (2) BLOCKED, `no_changes_exit_code`
(3) SUCCESS-with-no-changes, anything else ERROR (work is hard-reset).

A subtlety that shapes several code paths: **exit 0 with an unchanged working
tree fails the task**, because the engine cannot distinguish "deliberately did
nothing" from "did nothing". Opting into a no-op requires exit `3` *and* a clean
tree. Refactoring passes are exempt. See `NO_CHANGES_*` constants in `models.py`.

`ShellAgent` spawns with `start_new_session=True` and kills the whole process
group on timeout (agents commonly run as grandchildren of a shell); stream
draining after the kill is bounded so a leaked grandchild can never hang a
cycle. `DockerSandboxAgent` subclasses it and overrides only `_spawn`.

**Blocker file states.** `BLOCKER.md` has two distinct modes. A *task* blocker is
a derived view: re-rendered every cycle from the `BLOCKED` tasks' real
`blocker_reason`, marked with the `_TASK_BLOCKER_MARKER` HTML comment, and
auto-deleted once no task is blocked. A *refactor* blocker has no task to carry
the reason, so it is written once and pauses Forgeo until a human deletes the
file. Don't collapse these two.

**Two backlog backends.** `backlog:` is either a file path or an `http(s)` URL,
normalized by a before-validator in `models.py` (a URL must never reach `Path`,
which would collapse `https://` into `https:/`). `BacklogStore` in `backlog.py`
holds every task transition and leaves only `_read`/`_write` to `JSONBacklog`
and `backlog_http.HttpBacklog`; build one with `open_backlog(config)`, never by
naming a class. The HTTP backend GETs and POSTs the whole document and **raises**
on any failure — reading an unreachable endpoint as empty would trigger a
refactor pass and then POST that emptiness over the real task list.
`oauth.py` supplies the bearer token (client-credentials, secret read from the
env var named by `backlog_auth.client_secret_env`).

**Files on disk.** Locations live in `paths.py` — call `lock_path(config)` and
friends rather than deriving a suffix. With a backlog file they are its
siblings (`tasks.json` → `tasks.lock`); with a backlog URL they use fixed
`backlog.*` names inside `state_dir` (defaulted to the config's directory by
`load_config`).

| Path | Written by | Purpose |
| --- | --- | --- |
| `backlog.json` | `backlog.py` | the tasks (atomic writes, asyncio-lock serialized) |
| `backlog.lock` | `daemon.py` | daemon flock; `is_lock_held` = "daemon running" |
| `backlog.run` | `daemon.py` | per-cycle flock; a still-running cycle makes the next iteration skip |
| `backlog.state.json` | `daemon.py` | live pid/started_at/last_outcome/next_run_at |
| `runs.jsonl` | `runs.py` | one `RunRecord` per finished cycle |
| `backlog.update.json` | `update.py` | once-a-day PyPI update-check stamp |
| `BLOCKER.md`, `forgeo.log` | | per config |

The daemon binds no ports. `forgeo web` (`central.py`) is a separate stdlib
`http.server` process that reads every registered instance's files directly, so
the dashboard works whether or not a daemon is running. It never writes to an
instance's files: task *reads* go through `read_tasks`, which reads a backlog
file directly (bypassing `JSONBacklog`, which would quarantine a corrupt one)
and only fetches over HTTP for a URL backlog.

**Instance registry** (`instances.py`) maps a name → absolute `forgeo.yaml` path
in `$FORGEO_REGISTRY` or `~/.config/forgeo/instances.yaml`. `start`/`stop`
auto-register under the config's `name`. Every CLI command takes either
`--config PATH` or `--name NAME` (mutually exclusive, wired by
`cli.py:_add_config_or_name`).

The central dashboard is host-global (one per user): its lock is
`~/.config/forgeo/web.lock` (or `$FORGEO_CONFIG_DIR/web.lock`), separate from
any per-repo lock. Daemon start/stop/restart from the web console and from the
CLI share one implementation in `daemon_control.py` — SIGTERM the pid in the
lock file, wait for the lock to drop, relaunch detached so `forgeo.yaml` is
re-read (this is how a config saved from the web console takes effect).

**Conventions worth keeping.** Every write that outlives a process goes through
`io.atomic_write_text` (temp file + `os.replace`) — a crash mid-write must never
corrupt state, and readers treat missing/stale files as unknown rather than
erroring. Config paths are stored relative to the YAML file's own directory and
resolved on load (`config.py`), so configs stay portable. Optional integrations
(Telegram in `notify.py`, the update check in `update.py`) are stdlib-only,
never raise, and never change a cycle's outcome.

## Docs and web assets

`docs/` is the mkdocs-material site published at forgeo.org/docs; `www/` is the
hand-written landing page. `src/forgeo/web/` holds the dashboard's static
assets, bundled into the PyInstaller binary via `datas` in `forgeo.spec`, so
`Path(__file__).parent / "web"` must keep resolving at runtime.

Behavior changes usually need a matching update in `docs/` (configuration,
agent-contract, cli-reference, web-console-api), `config/forgeo.yaml` (the
annotated reference config), and `CHANGELOG.md` under `## [Unreleased]`.

## Releasing

The version lives in **three** places that must move together: `pyproject.toml`,
the standalone-binary fallback in `src/forgeo/__init__.py`, and `VERSION=` in
`install.sh` (the installer downloads that release's binaries). Pushing a `v*`
tag builds wheel/sdist plus PyInstaller binaries for linux-amd64,
darwin-amd64/arm64 and windows-amd64, publishes the GitHub Release and PyPI, and
re-renders the Homebrew tap formula. A release without those binary assets
breaks the default `install.sh` path. Full checklist in `CONTRIBUTING.md`.

## Self-hosting note

This repository is itself run by Forgeo: commits titled `<title> (#SELF-0xx)`
were produced by the agent from the backlog, and `refactoring pass` commits come
from idle cycles. The runtime artifacts (`.forgeo/`, `forgeo.yaml`,
`BLOCKER.md`, `forgeo.log`) are gitignored — never commit them.
