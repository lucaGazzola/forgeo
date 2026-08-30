# Web console & HTTP API

The **central dashboard** (`forgeo web`) is the only web interface. Daemons bind no ports — they write live state to `daemon.state.json`. The dashboard reads every instance directly from its files (`backlog.json`, `runs.jsonl`, `forgeo.log`, `BLOCKER.md`), so it works whether daemons are running. Remote backlogs are fetched from their provider; unreachable ones return `502` rather than an empty board.

```bash
forgeo web               # 0.0.0.0:8790 foreground
forgeo web --port 9000 --host 127.0.0.1
forgeo web -d            # background
forgeo web status / stop
forgeo web --token       # bearer auth for /api/*
```

Binds `0.0.0.0` by default (LAN-visible); use `--host 127.0.0.1` to restrict. Implemented with stdlib (`forgeo.central`); static files in `src/forgeo/web/`.

![Forgeo web console](img/console.png)

## Authentication (optional)

By default the dashboard is **open** — anyone reaching the port can read and mutate. On shared hosts, enable bearer auth:

```bash
forgeo web --token            # generate, print once, save to web.toml
forgeo web --token my-secret  # set your own
curl -H "Authorization: Bearer my-secret" http://127.0.0.1:8790/api/instances
```

Token stored in `~/.config/forgeo/web.toml` (0600). Once present, every `/api/*` requires `Authorization: Bearer <token>` (401 otherwise). Static assets and `/central/login.html` stay public; `?token=...` in URL auto-signs in. Delete `web.toml` to return to open.

## Pages

- **`GET /`** — home: every instance (name, repo, daemon state, last outcome, next run, per-status counts). Issue providers show `Open in Jira/GitHub/GitLab ↗`.
- **`GET /instances/<name>/`** — kanban board, **Create** tab (with *Run at* for one-shot schedule), plus **Logs**, **History**, **Blocker**, **Config** tabs. Header has daemon Start/Stop/Restart buttons.
  - For `file`/`http` this is the primary editor; for `jira`/`github`/`gitlab` it is a read-mostly mirror — banner links to native board, each card/modal links to the native issue, but Forgeo-specific state (`blocker_reason`, `failure_reason`, `agent_response`, retry budget) is shown here.
  - **History** tab lists `runs.jsonl` (paginated, newest first) with collapsible agent output (`run_output_lines`). **Config** tab edits `forgeo.yaml` via `PUT .../config`.
  - Task click opens a modal (description, criteria, dependencies, timestamps, `run_at`). **Edit** patches fields, **Reopen** retries `BLOCKED`, **Delete** removes `OPEN`/`BLOCKED`. `BLOCKED`/`FAILED` banners show reasons and retry counts. *Waiting on dependencies* banner lists unmet deps.
  - Columns auto-collapse (`OPEN` stays expanded, others behind "show …" after a few tasks, max 20 cards with "show more"). Search box and status filter are client-side, reflected in `?q=…&status=…`.
- `GET /style.css`, `/central/central.js`, `/central/central.css` — shared theme (no frameworks).

## Endpoints

All return `application/json` with `Cache-Control: no-store`. Per-instance paths are under `/api/instances/<name>/`. Writes are atomic (temp file + rename).

| Method & Path | Description |
| --- | --- |
| `GET /api/instances` | List all instances with repo, daemon state, last outcome, counts, provider metadata. |
| `GET /api/instances/<name>/tasks` | List tasks (creation order). `502` if remote unreachable. |
| `GET /api/instances/<name>/tasks/{id}` | Single task. `404` if not found. |
| `POST /api/instances/<name>/tasks` | Create task (auto `WEB-###` ID). |
| `POST /api/instances/<name>/tasks/{id}/reopen` | Reopen `BLOCKED` → `OPEN`. |
| `PATCH /api/instances/<name>/tasks/{id}` | Edit task fields. |
| `DELETE /api/instances/<name>/tasks/{id}` | Delete `OPEN`/`BLOCKED` task. |
| `GET /api/instances/<name>/status` | Daemon status + provider metadata. |
| `GET /api/instances/<name>/config` | Resolved `forgeo.yaml` as JSON. |
| `PUT /api/instances/<name>/config` | Validate and persist config. |
| `POST /api/instances/<name>/start` | Start daemon (409 if running). |
| `POST /api/instances/<name>/stop` | Stop daemon (no-op if not running). |
| `POST /api/instances/<name>/restart` | Restart daemon. |
| `GET /api/instances/<name>/logs?lines=N` | Last N log lines (default 100, max 10000). |
| `GET /api/instances/<name>/runs?limit=N&offset=M` | Paginated run history, newest first. |
| `GET /api/instances/<name>/blocker` | `BLOCKER.md` contents or `{"content": null}`. |

### `GET /api/instances`

```bash
curl http://127.0.0.1:8790/api/instances
```

Each row includes `backlog_provider`, `backlog`, `backlog_is_issue_provider`, `external_board_url`/`external_board_label`, `backlog_error` (null or reason; counts are 0 when unreachable). One unreachable provider never fails the whole listing.

### `GET /api/instances/<name>/tasks` (+ single)

```bash
curl http://127.0.0.1:8790/api/instances/my-repo/tasks
curl http://127.0.0.1:8790/api/instances/my-repo/tasks/TASK-001
```

Each task includes:

- `unsatisfied_dependencies: [{id, status}]` — deps not `COMPLETED` (`missing` if absent).
- `retry_budget` / `retries_remaining` — derived from `retries_left` vs `failed_retry_max`.
- `external_url` — native issue link for `jira`/`github`/`gitlab`.

```json
{
  "id": "TASK-001",
  "title": "Implement fibonacci module",
  "description": "...",
  "status": "OPEN",
  "created_at": "2026-07-31T10:00:00Z",
  "run_at": null,
  "dependencies": [],
  "unsatisfied_dependencies": [],
  "blocker_reason": [],
  "failure_reason": [],
  "agent_response": null
}
```

### `POST /api/instances/<name>/tasks`

Body requires non-blank `title` + `description`; optional `acceptance_criteria` (string[]), `agent_command` (string/`null`), `run_at` (ISO-8601/`null`).

```bash
curl -X POST http://127.0.0.1:8790/api/instances/my-repo/tasks \
  -H 'Content-Type: application/json' \
  -d '{"title": "Fibonacci", "description": "With tests.", "run_at": "2026-08-20T12:30:00Z"}'
```

Returns `201` with created task. Errors: `400` bad body, `404` unknown instance, `409` ID collision.

### `POST /api/instances/<name>/tasks/{id}/reopen`

No body. Sets `BLOCKED` → `OPEN`, clears `blocker_reason`, keeps `blocked_count`.

```bash
curl -X POST http://127.0.0.1:8790/api/instances/my-repo/tasks/TASK-001/reopen
```

Returns `200`. Errors: `400` if not `BLOCKED`, `404` unknown instance/task.

### `PATCH /api/instances/<name>/tasks/{id}`

Editable: `title`, `description`, `acceptance_criteria`, `dependencies`, `files_to_modify`, `agent_command`, `agent_timeout_seconds`, `retries_left`, `run_at`. Omitted fields unchanged; engine fields (`status`, `blocker_reason`, etc.) are rejected. Bumps `updated_at`.

```bash
curl -X PATCH http://127.0.0.1:8790/api/instances/my-repo/tasks/TASK-001 \
  -H 'Content-Type: application/json' \
  -d '{"description": "With tests.", "run_at": null}'
```

Returns `200`. Errors: `400` bad body/unknown field, `404` not found.

### `DELETE /api/instances/<name>/tasks/{id}`

Only `OPEN` or `BLOCKED` (400 otherwise). Returns `200` with deleted task.

### `GET /api/instances/<name>/status`

```bash
curl http://127.0.0.1:8790/api/instances/my-repo/status
```

```json
{
  "name": "my-repo",
  "repo": "/home/me/site-a",
  "interval_minutes": 30,
  "daemon_running": true,
  "pid": 4242,
  "last_outcome": "task",
  "next_run_at": "2026-08-01T12:00:00+00:00",
  "backlog_provider": "github",
  "external_board_url": "https://github.com/owner/repo/issues"
}
```

`next_run_at` is `null` when daemon not running; falls back to `runs.jsonl` estimate if `daemon.state.json` missing.

### `GET /api/instances/<name>/config` / `PUT /api/instances/<name>/config`

`GET` returns resolved `forgeo.yaml` as JSON. `PUT` validates against `ForgeoConfig` and persists atomically:

```bash
curl -X PUT http://127.0.0.1:8790/api/instances/my-repo/config \
  -H 'Content-Type: application/json' \
  -d '{"name": "my-repo", "repo": "/home/me/site-a", "interval_minutes": 60, "agent_command": "claude -p \"$FORGEO_TASK\""}'
```

Returns `200`:

```json
{"saved": true, "restart_required": false, "message": "Config saved. The daemon picks up changes on its next cycle.", "config": {...}}
```

`name` must match registry; `telegram_bot_token` cannot be changed via API (preserved if omitted). Same for `backlog_auth`, `state_dir`, `task_context`. Daemon picks up changes next cycle (except `repo`/`backlog`/`blocker_file`/`log_file` which need restart). Errors: `400` invalid payload/name/token change, `404` unknown instance, `500` config unavailable.

### `POST /api/instances/<name>/start|stop|restart`

```bash
curl -X POST http://127.0.0.1:8790/api/instances/my-repo/start
```

Returns outcome + daemon state:

```json
{"status": "started", "message": "Forgeo 'my-repo' started (pid 4242, interval 30 min).", "daemon_running": true, "pid": 4242}
```

`status` is `started`/`already_running`/`stopped`/`not_running`/`restarted`/`start_failed`/`stop_failed`/`restart_failed`. Errors: `409` already running (start), `404` unknown instance, `500` config unavailable or lifecycle failed.

### `GET /api/instances/<name>/logs?lines=N` / `runs?limit&offset` / `blocker`

```bash
curl "http://127.0.0.1:8790/api/instances/my-repo/logs?lines=50"
curl "http://127.0.0.1:8790/api/instances/my-repo/runs?limit=5&offset=25"
curl http://127.0.0.1:8790/api/instances/my-repo/blocker
```

- **logs** — last N lines (default 100, max 10000).
- **runs** — `{runs: [...], total, limit, offset}`, newest first. Each run has `started_at`, `finished_at`, `kind` (`task`/`refactor`), `task_id`/`task_title`, `outcome`, `agent_exit_code`, `commit_sha`, `duration_seconds`, `reason`, `output_logs` (bounded tail), `retry_count`.
- **blocker** — `{"content": "..."}` or `{"content": null}`.

## Behavior & errors

- Unknown instance → `404 {"error": "unknown instance"}`.
- Missing data files → `200` with empty data and `daemon_running: false`.
- `400` malformed body, invalid config, bad status transition, or deleting `COMPLETED`/`FAILED`.
- `409` ID collision or start while running.
- `500` handler error or lifecycle failure.

## Security notes

- Default `0.0.0.0` exposes everything on the LAN — use `--host 127.0.0.1` or a firewall, and `--token` on shared hosts. Token is sent as bearer on every `/api/*`; without TLS it is cleartext — use a TLS proxy or SSH tunnel. `?token=...` appears in logs; prefer the login page.
- Anyone with network access can add/edit/delete tasks and start/stop/restart daemons and change config (except `name`/`telegram_bot_token`). Auth covers all `/api/*` routes.
