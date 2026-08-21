# Forgeo

A **scheduled, agent-driven software forgeo** for one repository. Every
`interval_minutes` Forgeo wakes up and runs exactly one of three things:

1. picks the oldest `OPEN` task whose dependencies are all `COMPLETED` from the
   [backlog](backlog.md) (an optional `run_at` one-shot schedule overrides the
   oldest-first order — see [One-shot scheduling](backlog.md#one-shot-scheduling)),
   runs it through a coding [agent](agent-contract.md),
   and commits + pushes the result directly on the single configured branch —
   no branches, no PRs;
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

## Where state lives

- `forgeo.yaml` — the config (see [Configuration](configuration.md)).
- `backlog.json` (configurable) — the task backlog, a file, an HTTP endpoint,
  or Jira (see [Backlog format](backlog.md)).
- `backlog.json.bak`, `backlog.json.bak.1`, ... — rotating snapshots of a
  *file* backlog, written before every agent run and on daemon startup so a
  bad write can always be rolled back (see [Backlog format](backlog.md)). A
  remote backlog is owned by its provider and is never snapshotted locally.
- `BLOCKER.md` (configurable) — written when a human decision is needed; keep
  it outside the repo so it is never committed.
- `forgeo.log` — rotating daemon log (5 MB × 3), also served over HTTP.
- `backlog.state.json` — the daemon's live state (pid, started at, last
  outcome, next run), rewritten after every cycle (also called
  `daemon.state.json`).
- `runs.jsonl` — the durable run history, one JSON record per finished cycle.
- `backlog.lock` — per-forgeo lock holding the daemon PID; released
  automatically on exit, even on a crash.
- `backlog.run` — per-iteration lock that prevents two agents running at once.
- `backlog.update.json` — remembers when the once-a-day PyPI update check last
  ran, so it never phones home every cycle.
- `~/.config/forgeo/web.toml` — the central dashboard's optional bearer
  token (`forgeo web --token`): when present, every `/api/*` route requires
  `Authorization: Bearer <token>`; with no file the dashboard stays open.

The runtime files above sit next to the backlog file; with a remote backlog
they go in `state_dir`, which defaults to the directory holding `forgeo.yaml`.

## Next steps

- [Getting Started](getting-started.md) — install and run your first cycle.
- [CLI reference](cli-reference.md) — every command.
