# Agent contract

The coding agent is **any shell command** that can:

1. Read the task from `FORGEO_TASK`.
2. Work on the repository in the current directory.
3. Report its outcome via **exit code**.

```yaml
agent_command: "claude -p \"$FORGEO_TASK\""
```

## Environment

Launched with the repo as cwd and:

| Variable | Value |
| --- | --- |
| `FORGEO_TASK` | Full instruction — project context (if `task_context`), title, description, and "Acceptance criteria:" when present. |
| `FORGEO_REPO` | Absolute repo path. |
| `FORGEO_BRANCH` | Target branch (default `main`). |
| `agent_env` keys | Extra vars from config. |
| Inherited env | Daemon's own environment. |

`FORGEO_*` always wins. For refactoring runs (empty backlog), `FORGEO_TASK` carries the `refactor_prompt` (task ID `REFACTOR`).

When `task_context` is set, its file contents are prepended under `# Project context` before `# Task`. Re-read each run; missing file is a warning.

## Exit codes

| Code | Outcome | What happens |
| --- | --- | --- |
| `0` | **SUCCESS** | `git add -A && git commit` as `<title> (#<id>)`, push if `remote` set, task → `COMPLETED`. |
| `no_changes_exit_code` (default `3`) | **SUCCESS, no changes** | Task → `COMPLETED` without commit. Tree must be clean. |
| `blocked_exit_code` (default `2`) | **BLOCKED** | Partial work committed as `<title> [partial]`, `blocker_reason` saved, notifications sent, task → `BLOCKED`, `BLOCKER.md` rendered next cycle. |
| anything else | **ERROR** | Changes discarded (`git reset --hard` + `clean -fd`), task → `FAILED`, `failure_reason` saved. |

`FAILED` stays `FAILED` until human reopens (or auto-retried via `failed_retry_max`). `BLOCKED` is never auto-retried.

## The no-change contract

Forgeo cannot distinguish "deliberately no changes" from "did nothing":

- Exit `0` with **unchanged tree** → retried `no_changes_retry_max` times in the same cycle, then `BLOCKED` (never `FAILED` or silent `COMPLETED`).
- To complete without code change, exit `no_changes_exit_code` with a **clean tree**. Reporting no-change while leaving uncommitted work → `FAILED`.

Refactoring passes are exempt — a refactor finding nothing to improve is a normal success.

## Timeouts

`agent_timeout_seconds` kills the agent after N seconds (`error: timed out after <n>s`). Unset = no timeout. A run overrunning `interval_minutes` is never killed — the next cycle is skipped. Tip: leave unset for long agents and rely on skip-on-overlap.

## Output

Stdout/stderr are prefixed `[stdout]`/`[stderr]` and the last **1000 lines** are kept. On `BLOCKED`, the agent's questions (or output) become `blocker_reason` and the `BLOCKER.md` section (last 10 lines). Per-run tails go to `runs.jsonl` (`run_output_lines`); per-task `agent_response` is bounded by `agent_response_lines`.

## Git contract

The agent should **not** run `git` itself — Forgeo handles it:

- Make changes in the repo.
- Do not `git add`/`commit`/`push`/`reset`.
- Exit `0` for commit, `no_changes_exit_code` for intentional no-op, `blocked_exit_code` for human input, anything else for discard.

Embed the contract in your prompt:

```yaml
agent_command: >
  opencode run --auto "Work on the repository at the current working directory.
  Make the requested changes and nothing else. Do NOT run git commit/push/add.
  Verify with tests. Read AGENTS.md and CONTEXT.md if present; update them
  if your change affects the overview.
  $FORGEO_TASK"
```

## Concurrency

One agent per Forgeo: `backlog.lock` prevents a second `start`/`once`, and `backlog.run` makes a waking daemon skip while a run is active.
