# Forgeo

A **scheduled, agent-driven software forgeo** for one repository. Every
`interval_minutes` Forgeo wakes up and runs exactly one of three things:

1. picks the oldest `OPEN` task from the [backlog](backlog.md), runs it through
   a coding [agent](agent-contract.md), and commits + pushes the result
   directly on the single configured branch — no branches, no PRs;
2. if the backlog is empty, runs the agent in **refactoring mode** and commits
   whatever it improves;
3. if the agent signals it needs a human decision, the task is marked
   `BLOCKED` with the agent's reason preserved, `BLOCKER.md` is rendered from
   the backlog's blocked tasks, and Forgeo pauses until you resolve it (reopen
   it from the web console).

## What Forgeo is

Forgeo is a small Python daemon and CLI that turns your repository into a
self-maintaining codebase. You maintain a plain-JSON backlog of tasks; the
forgeo works through it with whatever coding agent you configure (aider,
Claude, a custom script — anything that reads the `FORGEO_TASK` environment
variable). When there is nothing left to do, the same agent switches to
refactoring mode and keeps the codebase tidy.

Forgeo is deliberately single-purpose:

- one repository per config;
- one branch, everything committed on `main` (or whichever `branch` you set);
- one agent at a time — an iteration that wakes up while the agent is still
  working is skipped, never killed;
- no PRs, no merge strategies, no branch juggling.

## Architecture overview

```
forgeo.yaml ──► forgeo start (daemon)
                     │
                     ├── wakes every interval_minutes
                     ▼
                 Forgeo.run_cycle()
                     │
                     ├── BLOCKED task exists ──► render BLOCKER.md from backlog, pause
                     │
                     ├── oldest OPEN task ──► run agent ──► commit & push ──► COMPLETED
                     │
                     └── backlog empty ──► run agent (refactor) ──► commit & push
                                                │
            exit 0                              │            exit blocked_exit_code
        commit & push ──────── ShellAgent ──────┴─────► partial work committed,
            task COMPLETED   (FORGEO_TASK env)          reason persisted on task,
                                                         BLOCKER.md rendered next cycle
                                                         task BLOCKED
```

### Components

| Component | Source | Responsibility |
| --- | --- | --- |
| `forgeo.cli` | `src/forgeo/cli.py` | `init`, `start`, `once`, `status`, `stop`, `restart` commands. |
| `forgeo.daemon` | `src/forgeo/daemon.py` | The scheduled worker: wakes every `interval_minutes`, holds the run locks, records `last_outcome`. |
| `forgeo.daemon_control` | `src/forgeo/daemon_control.py` | Daemon lifecycle shared by the CLI and web console: SIGTERM + wait, detached start/restart. |
| `forgeo.forgeo` | `src/forgeo/forgeo.py` | One cycle of work: task run, refactor pass, blocker handling, git side effects. |
| `forgeo.backlog` | `src/forgeo/backlog.py` | JSON backlog read/write; picks the oldest `OPEN` task. |
| `forgeo.agent` | `src/forgeo/agent.py` | `ShellAgent`: runs your command, maps exit codes to outcomes, delivers `FORGEO_TASK`. |
| `forgeo.git` | `src/forgeo/git.py` | Single-branch git operations: ensure branch, commit all, push, hard reset. |
| `forgeo.config` | `src/forgeo/config.py` | Loads and validates `forgeo.yaml`. |
| `forgeo.central` | `src/forgeo/central.py` | The `forgeo web` dashboard: one HTTP API + UI for every registered instance. |
| `forgeo.setup` | `src/forgeo/setup.py` | The guided `forgeo init` wizard. |
| `forgeo.notify` | `src/forgeo/notify.py` | Optional Telegram notifications for blocked runs. |
| `forgeo.models` | `src/forgeo/models.py` | The data contracts: `Task`, `ForgeoConfig`, `ExecutionResult`, statuses. |

### One cycle, in detail

1. The daemon takes the per-forgeo lock (`backlog.lock`); a second `start` or
   `once` is refused while it is held.
2. `Forgeo.run_cycle()` ensures the configured branch exists and is checked
   out.
3. If any task is `BLOCKED`, Forgeo re-renders `BLOCKER.md` from the backlog
   (real per-task reasons, never generic text) and pauses (`blocked` outcome)
   — it will not start new work until the block is resolved. Once the last
   `BLOCKED` task is reopened, the file disappears automatically on the next
   cycle.
4. Otherwise it takes the oldest `OPEN` task. If the working tree is dirty the
   cycle aborts (`dirty`) rather than running over manual changes.
5. The agent runs with the repository as its working directory and the task in
   `FORGEO_TASK`. The exit code decides what happens to the work — see
   [Agent contract](agent-contract.md) for the exact mapping.
6. With no `OPEN` task and no blocker file, the agent runs in refactoring mode
   and its changes are committed the same way.
7. The daemon sleeps until the next interval. A wake-up that finds a run still
   in progress is skipped, never killed.

## Where state lives

- `forgeo.yaml` — the config (see [Configuration](configuration.md)).
- `backlog.json` (configurable) — the task backlog (see
  [Backlog format](backlog.md)).
- `BLOCKER.md` (configurable) — written when a human decision is needed; keep
  it outside the repo so it is never committed.
- `forgeo.log` — rotating daemon log (5 MB × 3), also served over HTTP.
- `backlog.lock` — per-forgeo lock holding the daemon PID; released
  automatically on exit, even on a crash.
- `backlog.run` — per-iteration lock that prevents two agents running at once.

## Next steps

- [Getting Started](getting-started.md) — install and run your first cycle.
- [CLI reference](cli-reference.md) — every command.
