# Web console & HTTP API

The **central dashboard** (`forgeo web`) is the one and only web interface
for every forgeo instance. Daemons themselves bind no ports: `forgeo start`
just schedules cycles and writes its live state to `daemon.state.json`. The
dashboard reads every registered instance's data straight from its files
(`backlog.json`, `runs.jsonl`, `forgeo.log`, `BLOCKER.md`,
`daemon.state.json`), so it works whether or not each instance's daemon is
running.

```bash
forgeo web               # default 0.0.0.0:8790, foreground
forgeo web --port 9000   # pick a different port
forgeo web --host 127.0.0.1
forgeo web -d            # start it in the background and return
forgeo web status        # running? pid + host + port
forgeo web stop          # SIGTERM the background dashboard
forgeo web --token       # optional: generate a token and require it on /api/*
```

It runs in the foreground by default, or in the background with
`-d`/`--detach` (managed through `forgeo web stop`/`forgeo web status` and
its host-global `~/.config/forgeo/web.lock` — see the
[CLI reference](cli-reference.md#forgeo-web)). By default it binds
**`0.0.0.0`** so you can open it from any machine on your LAN — use
`--host 127.0.0.1` to restrict it to the local machine (open the port in your
firewall too).

![Forgeo web console](img/console.png)

The server is implemented with the standard library (`forgeo.central`);
static files in `src/forgeo/web/` are served at their URL paths.

## Authentication (optional)

By default the dashboard is **open**: anyone who can reach the port can read
every instance's backlog, logs, and config, and can mutate tasks or manage
daemons. On a shared host, enable **bearer-token auth** so every `/api/*`
route requires an `Authorization: Bearer <token>` header and answers `401`
otherwise:

```bash
forgeo web --token            # generates a token, prints it once, saves it
forgeo web --token my-secret  # or set your own token
```

The token is stored in `~/.config/forgeo/web.toml` (or
`$FORGEO_CONFIG_DIR/web.toml`, mode `0600`). Once the file holds a token,
auth is on even without the flag; a generated token is printed exactly once
at the moment it is created. Every `curl`/client then adds the header:

```bash
curl -H "Authorization: Bearer my-secret" http://127.0.0.1:8790/api/instances
```

Static assets and the **token prompt page** (`/central/login.html`) stay
reachable without a token: the page asks for the token and stores it in the
browser, so the console itself keeps working. Opening the dashboard with
`?token=YOUR_TOKEN` in the URL signs the browser in automatically (the token
is stripped from the address bar). Delete `web.toml` to return to the
open-by-default behavior.

## Pages

- `GET /` — home page listing every registered instance: name, repository,
  daemon state (lock held), last outcome, next run, and per-status backlog
  counts, each linking to its instance page.
- `GET /instances/<name>/` — one instance's page: a kanban backlog, a
  **Create** tab with a form to add tasks, plus tabs for **logs**, **history**,
  **blocker** and **config**. The header carries a **DAEMON** section with the
  daemon status tag (`running`/`stopped`) and **Start**/**Stop**/**Restart**
  buttons that call `POST /api/instances/<name>/start|stop|restart`; the
  buttons reflect the current state (Start is disabled while running, Stop
  while stopped), give inline success/error feedback, and the status tag
  refreshes after each action and on the 30-second auto-refresh. The
  **History** tab lists recent finished cycles from `runs.jsonl` in a
  paginated table — time, kind, task id and title, an outcome badge, duration,
  commit SHA and reason — newest first, with a pager once more than a page of
  runs exist (a friendly empty state is shown when no runs have been recorded
  yet). Runs that carry persisted agent output show a collapsible
  "agent output" row under the record: a read-only, monospace view of the
  bounded stdout/stderr tail (see `run_output_lines`). The **Config** tab renders `forgeo.yaml` as an
  editable form (interval, agent command, sandbox, telegram settings, ...):
  **Save** persists it via `PUT /api/instances/<name>/config`, surfaces
  validation errors inline (highlighted next to the failing field), and shows
  a notice that the running daemon applies the change only on its next
  restart. Clicking a task card opens a modal with the full
  task details (description, acceptance criteria, dependencies, files to
  modify, agent command, timestamps); it closes via the close button, the
  backdrop, or Escape. An **Edit** button switches the modal to an editable
  form for those fields; **Save** persists the change via `PATCH` and
  **Cancel** discards it. A task whose dependencies are not all `COMPLETED`
  shows a *Waiting on dependencies* banner listing each uncompleted
  dependency with its current status (or `missing` when the id does not
  exist in the backlog) — Forgeo will not pick the task until every
  dependency is `COMPLETED`. A `BLOCKED` task's modal shows a highlighted
  banner
  at the top with the agent's blocker reason (the persisted per-task
  `blocker_reason`) and how many times the task has blocked (`blocked_count`,
  e.g. "blocked 3x"); a `FAILED` task's modal shows an analogous red banner
  with the failure reason (the persisted per-task `failure_reason`) and the
  task's retry state — how many times it was retried, how many retries
  remain (from `retry_budget` / `retries_remaining`), or a "retries
  exhausted" note when automatic retries are spent (see
  [Backlog format](backlog.md#retrying-a-failed-task)). A failed task that
  was retried also carries a small `retried Nx` badge on its card, so you
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

  Above the board, a **search box** and a **status filter** find a specific
  task without scrolling. Typing filters the columns by `id`, `title`, and
  `description` substring as you type (matching is case-insensitive), and the
  status select narrows the board to a single status column. While either
  filter is active every match is rendered — no "show more" or collapsed
  columns — and a "no matching tasks" state is shown when nothing fits. Both
  filters are reflected in the URL (`?q=…&status=…`) so the view survives
  reloads and can be shared; they are applied entirely client-side against the
  full backlog already returned by the API.
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

List every task in that instance's backlog, in creation order. Each task
carries extra `unsatisfied_dependencies` and retry fields (see below).

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
    "files_to_modify": [],
    "unsatisfied_dependencies": []
  }
]
```

### `GET /api/instances/<name>/tasks/{id}`

Fetch a single task by id.

```bash
curl http://127.0.0.1:8790/api/instances/my-repo/tasks/TASK-001
```

Returns `404` with `{"error": "not found"}` for an unknown id.

### `unsatisfied_dependencies`

Every task returned by `GET .../tasks` and `GET .../tasks/{id}` includes an
`unsatisfied_dependencies` field: a list of the task's `dependencies` that are
**not** `COMPLETED`, in `dependencies` order. Each entry has the dependency id
and its current status — or `missing` when no task with that id exists:

```json
[
  { "id": "TASK-001", "status": "OPEN" },
  { "id": "TASK-003", "status": "missing" }
]
```

Forgeo only picks an `OPEN` task once every dependency is `COMPLETED` (see
[Backlog format](backlog.md)); the field is how the web console explains why a
task is waiting and is `[]` when the task has no (or only `COMPLETED`)
dependencies.

### `retry_budget` and `retries_remaining`

Every task returned by `GET .../tasks` and `GET .../tasks/{id}` also carries
its effective automatic-retry state (when the instance's config is
available): `retry_budget` is the number of retries the task may have (the
per-task `retries_left` override falling back to the config's
`failed_retry_max`) and `retries_remaining` is `max(0, retry_budget -
retry_count)`. The engine-managed `retry_count` and `failed_wait_cycles`
fields are part of the task object itself. The web console uses these to
show how many retries a failed task has left, or that its retries are
exhausted (see [Backlog format](backlog.md#retrying-a-failed-task)).

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
`agent_timeout_seconds`, and `retries_left` (the per-task automatic-retry
budget override; a non-negative integer or `null`). The request body is a
JSON object; omitted fields are left unchanged and `id`, `status`,
`blocker_reason`, `blocked_count`, `failure_reason`, `retry_count`,
`failed_wait_cycles`, and `created_at` are always preserved (they are
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

### `PUT /api/instances/<name>/config`

Validate and persist an instance's `forgeo.yaml`. The request body is the same
shape `GET /api/instances/<name>/config` returns, and is validated against the
same `ForgeoConfig` schema the daemon uses. Relative paths (`repo`, `backlog`,
`blocker_file`, `log_file`) are stored relative to the config file's own
directory, so they resolve to the same absolute paths on the daemon's next
load — sending back the exact payload from `GET` round-trips the file without
hard-coding absolute paths into it.

```bash
curl -X PUT http://127.0.0.1:8790/api/instances/my-repo/config \
  -H 'Content-Type: application/json' \
  -d '{"name": "my-repo", "repo": "/home/me/projects/site-a", "interval_minutes": 60, "backlog": "backlog.json", "blocker_file": "BLOCKER.md", "agent_command": "claude -p \"$FORGEO_TASK\" --model haiku", "log_file": "forgeo.log"}'
```

The write is atomic (temp file + rename), like the other write endpoints.
Returns `200` with the reloaded config plus a note on when it takes effect:

```json
{
  "saved": true,
  "restart_required": false,
  "message": "Config saved. The daemon picks up changes on its next cycle.",
  "config": { "...": "the reloaded config, paths resolved" }
}
```

The daemon watches `forgeo.yaml` and picks the change up on its next cycle
(`SIGHUP` wakes it sooner), so a config save needs no restart; `restart_required`
is `false` on every successful save. Path changes (`repo`, `backlog`,
`blocker_file`, `log_file`) are pinned to the daemon's startup paths and need
a restart (via the **Restart** button or `POST .../restart`). Errors:

- `400` with `{"error": "..."}` — an unparseable, non-object or empty body; a
  payload that fails `ForgeoConfig` validation (e.g. a blank `agent_command`,
  a non-positive `interval_minutes`, a `docker` `agent_sandbox` without an
  `agent_sandbox_image`); a `name` that differs from the registered instance
  name; or an attempt to change `telegram_bot_token`.
- `404` with `{"error": "unknown instance"}` — the instance is not registered.
- `500` with `{"error": "instance config not available"}` — the instance's
  config cannot currently be loaded.

`name` is owned by the registry (it is the key mapping an instance to its
`forgeo.yaml`) and is forced to the registered name — sending a different value
is rejected. `telegram_bot_token` is not editable through the web console: an
explicit change is rejected with `400`, and the current value is preserved when
the field is omitted, so a partial payload never wipes it. Everything else
`GET .../config` returns (including `agent_env`, which can carry credentials
the agent needs) is editable.

### `POST /api/instances/<name>/start`, `/stop`, `/restart`

Start, stop, or restart that instance's daemon — the same lifecycle as
`forgeo start`/`forgeo stop`/`forgeo restart`, exposed to the web console as
an explicit operator action. This is how a config saved from the **Config**
tab that moves paths is applied: the daemon re-reads `forgeo.yaml` on every
start, so a restart picks up the saved settings (a plain config edit is
picked up on the next cycle without one). No request body is required.

- `start` — launch a detached `forgeo start` for the instance (the same
  background daemon `forgeo start` launches). Refused with
  `409` when the daemon is already running.
- `stop` — SIGTERM the running daemon and wait for it to exit. A cycle in
  progress always finishes first, so partial work is never lost. When the
  daemon is not running this is a `200` no-op reporting `not_running`.
- `restart` — stop the daemon when running (waiting for any cycle in
  progress), then start it detached; when it wasn't running it just starts it.

Every endpoint returns the outcome and the post-action daemon state:

```bash
curl -X POST http://127.0.0.1:8790/api/instances/my-repo/start
```

```json
{
  "status": "started",
  "message": "Forgeo 'my-repo' started (pid 4242, interval 30 min).",
  "daemon_running": true,
  "pid": 4242
}
```

`status` is one of `started`, `already_running`, `stopped`, `not_running`,
`restarted`, `start_failed`, `stop_failed`, `restart_failed`; `daemon_running`
is the lock state after the action. Errors:

- `409` with `{"error": "daemon already running", "status": "already_running"}`
  — `start` while the daemon holds the lock (the UI disables **Start** while
  running, so this is an edge case).
- `404` with `{"error": "unknown instance"}` — the instance is not registered.
- `500` with `{"error": "instance config not available"}` — the instance's
  config cannot currently be loaded.
- `500` with `{"error": "..."}` — the stop or start failed: the recorded PID
  is gone but the lock is still held, the daemon did not start within the
  startup timeout, or it is still shutting down when the stop timeout elapses.

### `GET /api/instances/<name>/logs?lines=N`

The last `N` lines of that instance's `forgeo.log` (`N` defaults to `100`,
max `10000`).

```bash
curl http://127.0.0.1:8790/api/instances/my-repo/logs
curl "http://127.0.0.1:8790/api/instances/my-repo/logs?lines=50"
```

### `GET /api/instances/<name>/runs?limit=N&offset=M`

That instance's durable run history from `runs.jsonl`, newest first and
paginated. `limit` defaults to `10` (max `10000`); `offset` defaults to `0`
and skips that many of the newest records, so the web console's **History**
tab pages through old runs. The response is an object: `runs` holds the
requested page, `total` is the number of readable records (for pager
controls), and `limit`/`offset` echo the request. Each record has
started/finished timestamps, the run kind (`task` or `refactor`), the task id
and title when applicable, the outcome (`SUCCESS` / `BLOCKED` / `ERROR`), the
agent exit code, the commit SHA when a commit was created, the duration in
seconds, an optional `reason` when the run completed without a commit (a
no-change SUCCESS is surfaced here instead of silently showing a null commit
SHA), and `output_logs`: the bounded tail of the agent's stdout/stderr for
that run (at most `run_output_lines` lines) or `null` for runs that never
reached the agent or that predate the field. When the run was a retry of a
previously failed task, `retry_count` carries how many times the task had
already been retried (task runs only; `null` otherwise), so the History tab
can show which runs were retries. The web console renders the output tail in
a read-only, collapsible view in the **History** tab. A missing or empty
`runs.jsonl` yields `runs: []` with `total: 0`.

```bash
curl http://127.0.0.1:8790/api/instances/my-repo/runs
curl "http://127.0.0.1:8790/api/instances/my-repo/runs?limit=5"
curl "http://127.0.0.1:8790/api/instances/my-repo/runs?limit=25&offset=25"
```

```json
{
  "runs": [
    {
      "started_at": "2026-08-01T11:55:00Z",
      "finished_at": "2026-08-01T12:00:10Z",
      "kind": "task",
      "task_id": "TASK-001",
      "task_title": "Implement fibonacci module",
      "outcome": "SUCCESS",
      "agent_exit_code": 0,
      "commit_sha": "a1b2c3d4e5f6",
      "duration_seconds": 310.2,
      "reason": null,
      "output_logs": [
        "[stdout] Creating module fibonacci.py",
        "[stdout] Running tests: 12 passed, 0 failed",
        "[shell] Task TASK-001 finished successfully (exit 0)."
      ]
    }
  ],
  "total": 1,
  "offset": 0,
  "limit": 10
}
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
  `POST /api/instances/<name>/start|stop|restart` (daemon lifecycle),
  `PATCH /api/instances/<name>/tasks/<id>` (update a task's editable fields),
  `DELETE /api/instances/<name>/tasks/<id>` (delete an `OPEN` or `BLOCKED`
  task), and `PUT /api/instances/<name>/config` (validate and persist the
  instance's `forgeo.yaml`; applies on the daemon's next cycle).

## Errors

- `400` — malformed `POST`/`PATCH`/`PUT` body (missing/blank title, unparseable
  body, wrong field types, unknown fields), a config payload that fails schema
  validation, a change to an instance's registered `name` or to its
  `telegram_bot_token`, a `POST reopen` of a task that is not `BLOCKED`, or
  `DELETE` of a task that is neither `OPEN` nor `BLOCKED`.
- `404` — unknown API path, unknown instance, unknown task, or missing
  static file.
- `409` — `POST` id collision (a concurrent request won the race), or a
  `POST start` while the instance's daemon is already running.
- `500` — an unexpected handler error (logged server-side), a daemon lifecycle
  action whose stop or start failed, or an instance whose config cannot
  currently be loaded.

## Example: a status one-liner

```bash
curl -s http://127.0.0.1:8790/api/instances/my-repo/status
```

## Security notes

- The dashboard binds `0.0.0.0` by default so every forgeo on the host is
  visible from your LAN. Exposing it publicly (`--host 0.0.0.0` on a public
  interface) makes every instance's backlog, logs, and config visible to
  every host that can reach the port — only do that on a trusted network.
- **Bearer-token auth** (see [Authentication](#authentication-optional))
  closes the API to anonymous clients: every `/api/*` route — read *and*
  write — requires `Authorization: Bearer <token>` and returns `401`
  otherwise, while static pages and the token prompt stay public. It does
  not add transport encryption: put the dashboard behind a TLS proxy (or an
  SSH tunnel) when you use the token over the network, so the token is not
  sent in cleartext. The `?token=...` URL convenience form puts the token in
  the address bar and server logs — prefer pasting it on the prompt page.
- The write endpoints are `POST /api/instances/<name>/tasks` and `PATCH
  /api/instances/<name>/tasks/<id>` (and `POST
  /api/instances/<name>/tasks/<id>/reopen` to retry a `BLOCKED` task, plus
  `DELETE /api/instances/<name>/tasks/<id>` for open or blocked tasks, `POST
  /api/instances/<name>/start|stop|restart` to start/stop/restart that
  instance's daemon, and `PUT /api/instances/<name>/config` for an instance's
  configuration). A
  machine that can reach the port can add tasks to any instance's queue, edit
  their fields, retry blocked ones, delete open or blocked ones, start, stop
  and restart that instance's daemon, and change an
  instance's configuration (interval, agent command, paths, ...). The config
  write cannot change an instance's registered `name` or its
  `telegram_bot_token`, and it never restarts a daemon — the new config
  applies on the daemon's next cycle, except path changes (`repo`, `backlog`,
  `blocker_file`, `log_file`) which need a restart (via the **Restart** button
  or `POST .../restart`).
