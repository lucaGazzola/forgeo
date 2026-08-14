# Backlog format

The backlog is a **plain JSON file** you edit by hand. It lives wherever
`backlog:` points in [forgeo.yaml](configuration.md) — by default
`backlog.json` at the project root, and `.forgeo/backlog.json` when generated
by `forgeo init`. Keep it outside the repository if you can so the agent never
touches it.

```json
{
  "tasks": [
    {
      "id": "TASK-001",
      "title": "Implement fibonacci module",
      "description": "Write a fibonacci module with memoization and tests.",
      "status": "OPEN",
      "created_at": "2026-07-31T10:00:00Z"
    }
  ]
}
```

## Task schema

Each entry in `tasks` is a task object:

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `id` | string | — | Unique task id (e.g. `TASK-001`). Duplicate ids are rejected. |
| `title` | string | — | Short title; shown in logs, commit messages and status. |
| `description` | string | — | Longer description handed to the agent. Must be non-blank. |
| `status` | string | `OPEN` | One of `OPEN`, `BLOCKED`, `COMPLETED`, `FAILED`. |
| `created_at` | ISO-8601 datetime | now (UTC) | When the task was created; used for oldest-first ordering. |
| `updated_at` | ISO-8601 datetime | now (UTC) | Bumped whenever the status changes. |
| `dependencies` | list[string] | `[]` | Task ids this task depends on. Forgeo only picks the task once every dependency is `COMPLETED` (missing ids and ids in any other state keep it waiting). |
| `acceptance_criteria` | list[string] | `[]` | Rendered into the `FORGEO_TASK` instruction under an "Acceptance criteria:" heading. |
| `files_to_modify` | list[string] | `[]` | Informational; hints for the agent. |
| `agent_command` | string / list[string] | — | Override the configured `agent_command` for this task (e.g. route it to a different model). Validated like the global key; falls back to the config default when omitted. |
| `agent_timeout_seconds` | number | — | Override the configured `agent_timeout_seconds` for this task (must be positive). Falls back to the config default when omitted. |
| `blocker_reason` | list[string] | `[]` | Engine-managed: the agent's explanation (its questions, falling back to captured output) when the task becomes `BLOCKED`. Cleared on reopen; not editable via `PATCH`. |
| `blocked_count` | integer | `0` | Engine-managed: how many times the task has transitioned into `BLOCKED`. Kept as history when the task is reopened, so you can see a task that keeps blocking needs splitting or rewriting rather than a blind retry. Not editable via `PATCH`. |
| `failure_reason` | list[string] | `[]` | Engine-managed: the agent's error when the task becomes `FAILED` (e.g. a timeout message or a non-zero exit code). Shown in the web console's task modal so you can see why a task failed without opening the logs. Cleared when the task leaves the `FAILED` state; not editable via `PATCH`. |
| `retries_left` | integer / `null` | `null` | Per-task override of the automatic-retry budget (`failed_retry_max` in the config): how many times this task may be retried after a failure. `null` falls back to the config; `0` disables retries for this task. Editable via `PATCH`. |
| `retry_count` | integer | `0` | Engine-managed: how many times this task has already been retried. Shown in `runs.jsonl` and the web console; reset when a human reopens a `FAILED` task. Not editable via `PATCH`. |
| `failed_wait_cycles` | integer | `0` | Engine-managed: how many cycles this task has been `FAILED` awaiting a retry (backed off by `failed_retry_wait_cycles`). Reset when the task leaves `FAILED`. Not editable via `PATCH`. |

Only `id`, `title`, `description`, and `status` (optionally) are required;
every other field is optional.

### Per-task agent routing

A task may override Forgeo's coding agent by setting `agent_command`
(and optionally `agent_timeout_seconds`). Forgeo then runs that command
for that task instead of the configured default; the task still arrives as
`FORGEO_TASK` exactly as usual. This lets you route trivial tasks to a
cheap/fast model and hard ones to a frontier model:

```json
{
  "tasks": [
    {
      "id": "TASK-001",
      "title": "Add docstrings to the public API",
      "agent_command": "claude -p \"$FORGEO_TASK\" --model claude-3-haiku",
      "agent_timeout_seconds": 120
    },
    {
      "id": "TASK-002",
      "title": "Rearchitect the cache layer",
      "agent_command": "claude -p \"$FORGEO_TASK\" --model claude-3-opus"
    }
  ]
}
```

## Statuses

| Status | Meaning |
| --- | --- |
| `OPEN` | To be picked by Forgeo. |
| `BLOCKED` | Waiting on a human decision; Forgeo pauses while any task is blocked. |
| `COMPLETED` | The agent finished and the work was committed (and pushed). |
| `FAILED` | The agent errored; changes were discarded and the reason is recorded in `failure_reason`. |

## Retrying a failed task

Some failures are transient — a network blip, a flaky test, a dependency
version hiccup — and a retry would succeed without a human. When
`failed_retry_max` is set in [forgeo.yaml](configuration.md), Forgeo retries
a `FAILED` task automatically after `failed_retry_wait_cycles` cycles: it is
moved back to `OPEN`, picked up on the next run, and its `retry_count` is
incremented. A task that exhausts its budget stays `FAILED` with its original
`failure_reason` preserved, exactly as before — a human reopens it manually.
`BLOCKED` tasks are never auto-retried.

To give a single task a different budget than the rest of the backlog, set
its `retries_left` field: a number caps how many times it may be retried
(`0` opts it out of retries entirely), and `null` (or an omitted field)
falls back to the config's `failed_retry_max`:

```json
{
  "tasks": [
    {
      "id": "TASK-001",
      "title": "Fork a chatty upstream test dependency",
      "description": "Swap the network call for a stub.",
      "retries_left": 0
    },
    {
      "id": "TASK-002",
      "title": "Migrate the caching layer",
      "description": "Flaky under load; give it a few attempts.",
      "retries_left": 3
    }
  ]
}
```

The retry count is visible in `runs.jsonl` (the run record that eventually
succeeds carries it) and in the web console: a failed task's card and modal
show `retried Nx`, the task modal shows the retry budget and how many retries
remain, and the **History** tab has a **retry** column.

You add, remove, or reopen tasks by editing the file directly — or use the
[web console](web-console-api.md): the **new-task form** (`POST
/api/instances/<name>/tasks`) assigns the next free `WEB-###` id for you, and
the task detail modal's **Edit** button updates an existing task's fields
(`PATCH /api/instances/<name>/tasks/<id>`), while its **Delete** button
removes an `OPEN` or `BLOCKED` task (`DELETE /api/instances/<name>/tasks/<id>`).

### Resolving a blocked task

When the agent signals BLOCKED, Forgeo commits its partial work as a
`[partial]` commit on `main` and marks the task `BLOCKED`, recording the
agent's reason in `blocker_reason`. `BLOCKER.md` is a *derived view* of the
backlog's `BLOCKED` tasks — it is re-rendered every cycle with the real
per-task reasons and disappears automatically once the last `BLOCKED` task is
resolved.

To retry a `BLOCKED` task, **reopen** it: in the web console, open the task
card and press **Reopen** (edit the task first if you want to correct
something — editing is optional). Reopen is also available as `POST
/api/instances/<name>/tasks/<id>/reopen`; either way the status goes back to
`OPEN`, `blocker_reason` is cleared, and `blocked_count` is kept. Forgeo
picks the task up on the next scheduled run, building on the preserved
partial work. Reopening by hand is the same as setting the status back to
`OPEN` in this file — but that does *not* clear `blocker_reason`, so prefer
the web console's Reopen when the task was blocked by the agent.

## Oldest-first ordering

Forgeo picks the **oldest `OPEN` task whose dependencies are all `COMPLETED`**,
i.e. the `OPEN` task with the smallest `created_at` that is not waiting on
anything. Tasks in other states are ignored for picking:

- `BLOCKED` tasks do not get picked, but their presence pauses Forgeo.
- `COMPLETED` and `FAILED` tasks are skipped.
- An `OPEN` task whose `dependencies` are not all `COMPLETED` is skipped:
  Forgeo runs its dependencies first. A dependency that is `missing` (no task
  with that id exists) or stuck in another state (e.g. `FAILED`) keeps the
  task waiting forever, so it can never run and is not picked.

Set `created_at` deliberately (e.g. back-date a task) if you want to control
the order in which tasks are processed.

## Dependencies

`dependencies` is a list of task ids that must be `COMPLETED` before this task
runs. Forgeo enforces them when picking the next task: the oldest `OPEN` task
whose dependencies are all `COMPLETED` is chosen, so a task is never run before
the work it depends on. A task without `dependencies` behaves exactly as
before.

Ordering is oldest-first among *runnable* tasks: if the oldest `OPEN` task is
still waiting on an uncompleted dependency, Forgeo picks the next-oldest
`OPEN` task that is runnable instead. When nothing is runnable — e.g. a cycle
where `A` depends on `B` and `B` depends on `A` — Forgeo reports no next task
and runs a refactoring pass until a dependency is `COMPLETED`.

Unsatisfied dependencies are surfaced so a waiting task is never a silent
black hole:

- `forgeo status` shows a `waiting on:` line naming the oldest `OPEN` task that
  is not yet runnable and the dependency ids keeping it waiting (with their
  current status, or `missing`).
- the web console task detail shows a *Waiting on dependencies* banner listing
  each uncompleted dependency with its status; a dependency id that does not
  exist in the backlog is shown as `missing`.

To unblock a waiting task, complete (or delete and re-add, or fix) the
referenced task — or edit the task's `dependencies` from the web console /
the backlog file.

## How a task is executed

Once picked, the task is handed to the agent as `FORGEO_TASK`, and the exit
code decides what happens to the work (commit & push, partial commit +
`BLOCKER.md`, or discard). See [Agent contract](agent-contract.md) for the
full mapping.

## Corruption tolerance

The backlog is the single source of truth, so it is guarded on both ends:

- a missing file is treated as an empty backlog (and is created on first
  write);
- a corrupt file is renamed to `backlog.json.corrupt-<timestamp>` and the
  forgeo starts from an empty store — nothing is silently discarded;
- an unparsable task row is kept as a `FAILED` task rather than killing the
  whole store;
- before every agent run (and on daemon startup) the current backlog is
  copied to a **rotating snapshot** next to it, so a bad write is always
  recoverable.

### Snapshots

Forgeo writes a snapshot of the current backlog to `backlog.json.bak` before
every agent run and whenever the daemon starts (a config change that reloads
the backlog is snapped on that cycle too).
Snapshots are rotated so only the last few are kept — by default 2:
`backlog.json.bak` (newest) and `backlog.json.bak.1` (older). The newest
snapshot is always `backlog.json.bak`; older snapshots gain an index.

If a read ever finds the backlog corrupt (a half-written file, a hostile
agent, an accidental manual edit), the newest **valid** snapshot is restored
in place automatically and the corrupt file is preserved under
`backlog.json.corrupt-<timestamp>` as before. A corrupt snapshot is skipped in
favor of an older valid one; when no snapshot exists, the forgeo falls back to
an empty store exactly as before. A missing backlog is a no-op — no snapshot
is created for a file that does not exist.
