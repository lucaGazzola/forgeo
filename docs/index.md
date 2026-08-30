# Forgeo

A **scheduled, agent-driven software factory** for one repository. Every `interval_minutes` Forgeo wakes up and does one thing:

1. **Task** — picks the oldest `OPEN` task whose dependencies are `COMPLETED` (or the earliest due `run_at`), runs it via your [agent](agent-contract.md), commits and pushes on the configured branch.
2. **Refactor** — if no task is runnable, runs the agent with `refactor_prompt` instead.
3. **Blocked** — if the agent exits `blocked_exit_code`, marks the task `BLOCKED`, writes `BLOCKER.md`, and pauses until you reopen it.

## Principles

- One repo, one branch (`main` by default), one agent at a time. Overlapping cycles are skipped, never killed.
- No PRs or merge strategies — everything commits directly.
- Any agent CLI works if it reads `FORGEO_TASK`.

## Where state lives

| File | Purpose |
| --- | --- |
| `forgeo.yaml` | Config ([Configuration](configuration.md)) |
| `backlog.json` | Task list — file, HTTP endpoint, or Jira/GitHub/GitLab ([Backlog](backlog.md)) |
| `backlog.json.bak*` | Rotating snapshots of a *file* backlog (before each run) |
| `BLOCKER.md` | Rendered from `BLOCKED` tasks; keep outside repo |
| `forgeo.log` | Rotating log (5 MB × 3), also in dashboard |
| `runs.jsonl` | Durable run history, one JSON line per cycle |
| `backlog.state.json` | Live daemon state (pid, last outcome, next run) |
| `backlog.lock` / `backlog.run` | Daemon and per-cycle locks |
| `~/.config/forgeo/instances.yaml` | Instance registry (multi-repo) |
| `~/.config/forgeo/web.toml` | Dashboard bearer token (optional) |

Runtime files sit next to the backlog file; with a remote backlog they go in `state_dir` (defaults to the config directory).

## Next steps

- [Getting Started](getting-started.md) — install and run your first task.
- [Configuration](configuration.md) — every `forgeo.yaml` key.
- [Backlog format](backlog.md) — task schema, ordering, providers.
- [CLI reference](cli-reference.md) — all commands.
