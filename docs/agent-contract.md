# Agent contract

The coding agent is **any shell command**: a CLI coding tool or a plain command. It must be able to:

1. read the task from the `FORGEO_TASK` environment variable,
2. work on the repository from the current working directory,
3. report its outcome through its **exit code**.

Anything a CLI agent can do works:

```yaml
agent_command: "claude -p \"$FORGEO_TASK\""
```

## Environment

The agent is launched with the **repository as its working directory**, and
the process environment is augmented as follows:

| Variable | Meaning |
| --- | --- |
| <span style="white-space: nowrap">`FORGEO_TASK`</span> | The full instruction for this run: the project context when `task_context` is configured, then title, blank line, description, and an "Acceptance criteria:" list when present. |
| <span style="white-space: nowrap">`FORGEO_REPO`</span> | The absolute path of the repository. |
| <span style="white-space: nowrap">`FORGEO_BRANCH`</span> | The branch everything is committed to (default `main`). |
| <span style="white-space: nowrap">*every `agent_env` key*</span> | Any extra variables from `agent_env` in the config. |
| <span style="white-space: nowrap">*inherited environment*</span> | The daemon's own environment. |

`FORGEO_*` variables are set unconditionally and take precedence over both the
inherited environment and `agent_env`.

### The task is not the whole picture

A task description is isolated by design: it describes one unit of work, not
the project. When `task_context` is set (see
[Configuration](configuration.md#task_context)), Forgeo prepends the contents
of that file — the high-level project overview — to `FORGEO_TASK` before the
task, under a `# Project context` heading, followed by the task under a
`# Task` heading. The file is re-read on every run, so an agent's own updates
to it are seen by the next cycle.

For a refactoring run (empty backlog) the same contract applies: the refactor
prompt arrives as `FORGEO_TASK` (with the context prepended when configured),
with the task id `REFACTOR` and title "Refactoring pass".

## Exit codes

The exit code decides the outcome of the run:

| Exit code | Outcome | What happens |
| --- | --- | --- |
| <span style="white-space: nowrap">`0`</span> | **SUCCESS** | Everything is committed (`git add -A && git commit`) with the message `<title> (#<id>)`, pushed when a remote is set, and the task is marked `COMPLETED`. |
| <span style="white-space: nowrap">`no_changes_exit_code` (default `3`)</span> | **SUCCESS, no changes** | The agent explicitly reports the task needs **no code change**: the task is marked `COMPLETED` without a commit (and the run record notes why). Only accepted when the working tree is clean. |
| <span style="white-space: nowrap">`blocked_exit_code` (default `2`)</span> | **BLOCKED** | The agent needs a human decision. Partial work is committed as `<title> [partial]`, the agent's reason is persisted on the task (`blocker_reason`), optional Telegram and/or webhook notifications are sent, and the task is marked `BLOCKED`. `BLOCKER.md` is rendered from the backlog's `BLOCKED` tasks on the next cycle — real per-task reasons, never generic text — and disappears once the last one is resolved (reopen it from the web console). |
| <span style="white-space: nowrap">anything else</span> | **ERROR** | Changes are discarded (`git reset --hard` + `git clean -fd`), the failure is logged, and the task is marked `FAILED`. |

The blocked exit code is configurable via `blocked_exit_code` in
[forgeo.yaml](configuration.md), and the no-change exit code via
`no_changes_exit_code`.

A `FAILED` task stays `FAILED` until a human reopens it — unless the retry
policy is enabled (`failed_retry_max`, see [Configuration](configuration.md)),
in which case Forgeo moves the task back to `OPEN` after
`failed_retry_wait_cycles` cycles and runs it again, incrementing its retry
count. `BLOCKED` is never retried automatically: it always waits for a human.

## The no-change contract

Forgeo cannot tell "the agent deliberately made no changes" from "the agent
did nothing". A `SUCCESS` exit that produces **no changes is therefore not a
valid completion for a task**:

- exiting `0` while leaving the working tree **unchanged** fails the task
  (`FAILED`, reason: *"Agent exited 0 but produced no changes"*);
- to complete a task **without touching the code**, exit
  `no_changes_exit_code` (default `3`). The working tree must be clean — an
  agent that reports "no changes" while leaving uncommitted work behind fails
  instead.

Refactoring passes are the exception: when the backlog is empty, a refactor
that finds nothing to improve is a normal, successful run (the default
refactor prompt already says "if nothing needs refactoring, make no
changes").

## Timeouts

`agent_timeout_seconds` (optional) kills the agent process after the given
number of seconds:

- on timeout the process is killed and the task fails as an ERROR
  (`error: timed out after <n>s`);
- output captured before the kill is kept in the run log;
- when unset (`null`), the agent runs to completion — there is no default
  timeout.

A run that overruns `interval_minutes` is **never killed by the schedule**. The
daemon skips the next iteration instead: only one agent runs at a time, and an
iteration that wakes up while the previous run is still active is skipped, not
interrupted.

!!! tip

    Set `agent_timeout_seconds` to something comfortably above your expected
    agent runtime. For long interactive agents it is often safer to leave it
    unset and rely on the skip-on-overlap behavior.

## Output

The agent's stdout and stderr are captured live, prefixed with `[stdout]` /
`[stderr]`, and retained in the run result. To keep memory bounded, only the
**last 1000 lines** are kept for a chatty agent. On a BLOCKED result, the
agent's questions (falling back to the captured output lines) are stored on
the task as `blocker_reason` and become the "what the agent needs" section of
`BLOCKER.md` (up to the last 10 lines), rendered on the next cycle.

## Git contract

The agent should **not** commit or push anything itself — Forgeo does
that, based on the exit code. The working contract is:

- make your changes in the repository;
- **do not** run `git add`, `git commit`, `git push`, or reset the tree;
- exit `0` to have your changes committed and pushed as one commit;
- exit `no_changes_exit_code` when the task needs no code change (never exit
  `0` with an empty tree — that fails the task);
- exit `blocked_exit_code` to have partial work preserved and a blocker
  written;
- exit anything else to have your changes discarded.

A good way to keep this contract front and center is to embed it in the
`agent_command` prompt itself, e.g.:

```yaml
agent_command: >
  opencode run --auto "Work on the repository at the current working directory.
  Make the code changes requested below and nothing else. Do NOT run
  git commit, git push, or git add -A — Forgeo commits your work.
  Verify with the test suite where applicable.
  Read AGENTS.md at the start of the session, and CONTEXT.md if present;
  if your change materially affects the project overview or conventions,
  update AGENTS.md and CONTEXT.md accordingly.
  $FORGEO_TASK"
```

## Concurrency

Only one agent runs at a time per forgeo:

- the daemon holds a per-forgeo lock (`backlog.lock`) — a second `start` or
  `once` is refused while it is held;
- each cycle holds a per-iteration lock (`backlog.run`) — a wake-up that finds
  a run still in progress is skipped.
