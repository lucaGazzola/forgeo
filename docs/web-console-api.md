# Web console & HTTP API

The **central dashboard** (`forgeo web`) is the one and only web interface
for every forgeo instance. Daemons themselves bind no ports: `forgeo start`
just schedules cycles and writes its live state to `daemon.state.json`. The
dashboard reads every registered instance's data straight from its files
(`backlog.json`, `runs.jsonl`, `forgeo.log`, `BLOCKER.md`,
`daemon.state.json`), so it works whether or not each instance's daemon is
running.

```bash
forgeo web               # default 0.0.0.0:8790
forgeo web --port 9000   # pick a different port
forgeo web --host 127.0.0.1
```

It runs in the foreground like `forgeo start`. By default it binds
**`0.0.0.0`** so you can open it from any machine on your LAN — use
`--host 127.0.0.1` to restrict it to the local machine (open the port in your
firewall too).

![Forgeo web console](img/console.png)

The server is implemented with the standard library (`forgeo.central`);
static files in `src/forgeo/web/` are served at their URL paths.

## Pages

- `GET /` — home page listing every registered instance: name, repository,
  daemon state (lock held), last outcome, next run, and per-status backlog
  counts, each linking to its instance page.
- `GET /instances/<name>/` — one instance's page: a kanban backlog, a
  **Create** tab with a form to add tasks, plus tabs for **logs**, **runs**,
  **blocker** and **config**. Clicking a task card opens a modal with the full
  task details (description, acceptance criteria, dependencies, files to
  modify, agent command, timestamps); it closes via the close button, the
  backdrop, or Escape. An **Edit** button switches the modal to an editable
  form for those fields; **Save** persists the change via `PATCH` and
  **Cancel** discards it. A `BLOCKED` task's modal shows a highlighted banner
  at the top with the agent's blocker reason (the persisted per-task
  `blocker_reason`) and how many times the task has blocked (`blocked_count`,
  e.g. "blocked 3x"); a `FAILED` task's modal shows an analogous red banner
  with the failure reason (the persisted per-task `failure_reason`), so you
  never have to open the logs to see why something failed. You can **Edit** a
  `BLOCKED` task to correct it and then **Reopen** it, or **Reopen** it
  as-is — Forgeo retries it on its next scheduled run. `BLOCKED` tasks can
  also be **Deleted** (with confirmation), mirroring the `BLOCKER.md`
  instructions.

  The board compacts itself so a large backlog never renders every task as a
  tall card at once: once a `BLOCKED`/`COMPLETED`/`FAILED` column holds more
  than a few tasks it collapses behind a "show …" toggle (only the `OPEN`
  column — the actionable one — stays expanded), and every column renders at
  most 20 of the most recent cards until its "show N more" button is clicked.
  The header count always shows the real total, every task stays reachable by
  expanding the column, and expanded/show-more state survives the 30-second
  auto-refresh. This is presentation only — the task data itself is never
  trimmed or reordered server-side.
- `GET /style.css`, `/central/central.js`, `/central/central.css` — the
  shared dark theme and dashboard scripts (no frameworks).

## Endpoints

All endpoints return JSON with `Content-Type: application/json` and
`Cache-Control: no-store`. Every per-instance endpoint lives under
`/api/instances/<name>/`.

### `GET /api/instances`

A JSON list of every registered instance with its repo, daemon state, last
outcome, next run, and backlog counts.

```bash
curl http://127.0.0.1:8790/api/instances
```

### `GET /api/instances/<name>/tasks`

List every task in that instance's backlog, in creation order.

```bash
curl http://127.0.0.1:8790/api/instances/my-repo/tasks
```

```json
[
  {
    "id": "TASK-001",
    "title": "Implement fibonacci module",
    "description": "Write a fibonacci module with memoization and tests.",
    "status": "OPEN",
    "blocker_reason": [],
    "blocked_count": 0,
    "failure_reason": [],
    "created_at": "2026-07-31T10:00:00Z",
    "updated_at": "2026-07-31T10:00:00Z",
    "dependencies": [],
    "acceptance_criteria": [],
    "files_to_modify": []
  }
]
```

### `GET /api/instances/<name>/tasks/{id}`

Fetch a single task by id.

```bash
curl http://127.0.0.1:8790/api/instances/my-repo/tasks/TASK-001
```

Returns `404` with `{"error": "not found"}` for an unknown id.

### `POST /api/instances/<name>/tasks`

Create a new task in that instance's backlog. The request body must be a JSON
object with a non-blank `title` and a non-blank `description`;
`acceptance_criteria` (array of strings) and `agent_command` (string or
`null`, overriding the configured agent for this task) are optional. The
server generates the id as the next free `WEB-###` id and stamps
`created_at`/`updated_at`.

```bash
curl -X POST http://127.0.0.1:8790/api/instances/my-repo/tasks \
  -H 'Content-Type: application/json' \
  -d '{"title": "Implement fibonacci module", "description": "With tests.", "acceptance_criteria": ["passes pytest"], "agent_command": "claude -p \"$FORGEO_TASK\" --model claude-3-haiku"}'
```

```json
{
  "id": "WEB-001",
  "title": "Implement fibonacci module",
  "description": "With tests.",
  "status": "OPEN",
  "blocker_reason": [],
  "blocked_count": 0,
  "failure_reason": [],
  "created_at": "2026-08-01T12:00:00Z",
  "updated_at": "2026-08-01T12:00:00Z",
  "dependencies": [],
  "acceptance_criteria": ["passes pytest"],
  "files_to_modify": []
}
```

Returns `201` with the created task. The write is atomic (temp file +
rename), so it is safe even while that instance's daemon is mid-cycle.
Errors:

- `400` with `{"error": "..."}` — missing/blank `title`, missing/blank
  `description`, unparseable or non-object body, or a field of the wrong
  type.
- `404` with `{"error": "unknown instance"}` — the instance is not
  registered.
- `409` with `{"error": "..."}` — the generated id already exists in the
  backlog (e.g. two concurrent requests raced).

### `POST /api/instances/<name>/tasks/{id}/reopen`

Reopen a `BLOCKED` task so Forgeo retries it on its next scheduled run: its
status is set back to `OPEN`, `blocker_reason` is cleared, and `blocked_count`
is kept as history. No request body is required. A dedicated endpoint rather
than a generic `status` via `PATCH` — status transitions stay outside the
editable-fields model.

```bash
curl -X POST http://127.0.0.1:8790/api/instances/my-repo/tasks/TASK-001/reopen
```

```json
{
  "id": "TASK-001",
  "title": "Implement fibonacci module",
  "description": "Write a fibonacci module with memoization and tests.",
  "status": "OPEN",
  "blocker_reason": [],
  "blocked_count": 1,
  "failure_reason": [],
  "created_at": "2026-07-31T10:00:00Z",
  "updated_at": "2026-08-01T12:00:00Z",
  "dependencies": [],
  "acceptance_criteria": [],
  "files_to_modify": []
}
```

Returns `200` with the reopened task. The write is atomic (temp file +
rename), so it is safe even while that instance's daemon is mid-cycle. Once
the last `BLOCKED` task is resolved, the derived `BLOCKER.md` disappears on
the next cycle automatically. Errors:

- `400` with `{"error": "only BLOCKED tasks can be reopened"}` — the task
  exists but is not `BLOCKED` (it is untouched).
- `404` with `{"error": "not found"}` — the task id does not exist in that
  instance's backlog.
- `404` with `{"error": "unknown instance"}` — the instance is not
  registered.

### `PATCH /api/instances/<name>/tasks/{id}`

Update an existing task's editable fields: `title`, `description`,
`acceptance_criteria`, `dependencies`, `files_to_modify`, `agent_command`,
and `agent_timeout_seconds`. The request body is a JSON object; omitted fields
are left unchanged and `id`, `status`, `blocker_reason`, `blocked_count`,
`failure_reason`, and `created_at` are always preserved (they are
engine-managed — `PATCH` rejects them like it rejects `status`).
`agent_command` may be a string, an array, or `null` (clear the per-task
override); `agent_timeout_seconds` may be a positive number or `null`.
`updated_at` is bumped to the current time.

```bash
curl -X PATCH http://127.0.0.1:8790/api/instances/my-repo/tasks/TASK-001 \
  -H 'Content-Type: application/json' \
  -d '{"description": "Write a fibonacci module with tests.", "agent_timeout_seconds": 120}'
```

```json
{
  "id": "TASK-001",
  "title": "Implement fibonacci module",
  "description": "Write a fibonacci module with tests.",
  "status": "OPEN",
  "blocker_reason": [],
  "blocked_count": 0,
  "failure_reason": [],
  "created_at": "2026-07-31T10:00:00Z",
  "updated_at": "2026-08-01T12:00:00Z",
  "dependencies": [],
  "acceptance_criteria": [],
  "files_to_modify": []
}
```

Returns `200` with the updated task. The write is atomic (temp file +
rename), so it is safe even while that instance's daemon is mid-cycle.
Errors:

- `400` with `{"error": "..."}` — unparseable or non-object body, an empty
  body, an unknown field (e.g. `status`, `blocker_reason`, `blocked_count`,
  `failure_reason`), or an invalid value (blank `title`, wrong field types, a
  non-positive `agent_timeout_seconds`).
- `404` with `{"error": "not found"}` — the task id does not exist in that
  instance's backlog.
- `404` with `{"error": "unknown instance"}` — the instance is not
  registered.

### `DELETE /api/instances/<name>/tasks/{id}`

Delete an `OPEN` or `BLOCKED` task from that instance's backlog (e.g. a task
added by mistake, or a `BLOCKED` task the human decides should not be done —
per the `BLOCKER.md` instructions). `COMPLETED` and `FAILED` tasks stay in the
record.

```bash
curl -X DELETE http://127.0.0.1:8790/api/instances/my-repo/tasks/TASK-001
```

Returns `200` with the deleted task. The write is atomic (temp file +
rename), so it is safe even while that instance's daemon is mid-cycle.
Errors:

- `400` with `{"error": "only OPEN or BLOCKED tasks can be deleted"}` — the
  task exists but is `COMPLETED` or `FAILED` (it is untouched).
- `404` with `{"error": "not found"}` — the task id does not exist in that
  instance's backlog.
- `404` with `{"error": "unknown instance"}` — the instance is not
  registered.

### `GET /api/instances/<name>/status`

Daemon status: name, repo, interval, `daemon_running` (whether the instance's
lock is held), the recorded PID, `last_outcome`, and the `next_run_at`.

```bash
curl http://127.0.0.1:8790/api/instances/my-repo/status
```

```json
{
  "name": "my-repo",
  "repo": "/home/me/projects/site-a",
  "interval_minutes": 30,
  "daemon_running": true,
  "pid": 4242,
  "last_outcome": "task",
  "next_run_at": "2026-08-01T12:00:00+00:00"
}
```

`pid`, `last_outcome` and `next_run_at` come from the daemon's
`daemon.state.json` (written after every cycle). When no state file exists,
`last_outcome` falls back to `runs.jsonl` and `next_run_at` to an estimate
(the last run's finish plus the interval) — only while the daemon is running.
`next_run_at` is `null` when the daemon is not running.

### `GET /api/instances/<name>/config`

The resolved `forgeo.yaml` as JSON.

```bash
curl http://127.0.0.1:8790/api/instances/my-repo/config
```

### `GET /api/instances/<name>/logs?lines=N`

The last `N` lines of that instance's `forgeo.log` (`N` defaults to `100`,
max `10000`).

```bash
curl http://127.0.0.1:8790/api/instances/my-repo/logs
curl "http://127.0.0.1:8790/api/instances/my-repo/logs?lines=50"
```

### `GET /api/instances/<name>/runs?limit=N`

That instance's durable run history from `runs.jsonl`, newest first (`limit`
defaults to `10`, max `10000`). Each record has started/finished timestamps,
the run kind (`task` or `refactor`), the task id and title when applicable,
the outcome (`SUCCESS` / `BLOCKED` / `ERROR`), the agent exit code, the commit
SHA when a commit was created, and the duration in seconds.

```bash
curl http://127.0.0.1:8790/api/instances/my-repo/runs
curl "http://127.0.0.1:8790/api/instances/my-repo/runs?limit=5"
```

### `GET /api/instances/<name>/blocker`

The instance's `BLOCKER.md` contents, or `{"content": null}` when none
exists.

```bash
curl http://127.0.0.1:8790/api/instances/my-repo/blocker
```

## Behavior

- An unknown instance name returns `404` (`{"error": "unknown instance"}`).
- A registered instance with missing data files renders with empty data and
  `daemon_running=false` instead of erroring — the instance page and every
  API endpoint still return `200`.
- The write endpoints are `POST /api/instances/<name>/tasks` (append a task),
  `POST /api/instances/<name>/tasks/<id>/reopen` (reopen a `BLOCKED` task),
  `PATCH /api/instances/<name>/tasks/<id>` (update a task's editable fields),
  and `DELETE /api/instances/<name>/tasks/<id>` (delete an `OPEN` or
  `BLOCKED` task).

## Errors

- `400` — malformed `POST`/`PATCH` body (missing/blank title, unparseable
  body, wrong field types, unknown fields), a `POST reopen` of a task that is
  not `BLOCKED`, or `DELETE` of a task that is neither `OPEN` nor `BLOCKED`.
- `404` — unknown API path, unknown instance, unknown task, or missing
  static file.
- `409` — `POST` id collision (a concurrent request won the race).
- `500` — an unexpected handler error (logged server-side).

## Example: a status one-liner

```bash
curl -s http://127.0.0.1:8790/api/instances/my-repo/status
```

## Security notes

- The dashboard binds `0.0.0.0` by default so every forgeo on the host is
  visible from your LAN. Exposing it publicly (`--host 0.0.0.0` on a public
  interface) makes every instance's backlog, logs, and config visible to
  every host that can reach the port — only do that on a trusted network.
- The write endpoints are `POST /api/instances/<name>/tasks` and `PATCH
  /api/instances/<name>/tasks/<id>` (and `POST
  /api/instances/<name>/tasks/<id>/reopen` to retry a `BLOCKED` task, plus
  `DELETE /api/instances/<name>/tasks/<id>` for open or blocked tasks). A
  machine that can reach the port can add tasks to any instance's queue, edit
  their fields, retry blocked ones, and delete open or blocked ones.
