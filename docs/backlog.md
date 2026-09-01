# Backlog format

The backlog is a **plain JSON document** — a list of tasks Forgeo works through one by one. It can live as:

- [JSON file](#a-json-file-backlog) — local file
- [HTTP endpoint](#a-backlog-over-http) — another app serving the same document
- [Jira](#a-jira-backlog) / [GitHub](#a-github-backlog) / [GitLab](#a-gitlab-backlog) — native issues

## A JSON file backlog

Default is a file wherever `backlog:` points (`backlog.json` or `.forgeo/backlog.json` from `forgeo init`). Keep it outside the repo if possible.

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

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `id` | string | — | Unique task ID. Duplicates rejected. |
| `title` | string | — | Short title (logs, commits, status). |
| `description` | string | — | Full instruction for the agent. Must be non-blank. |
| `status` | `OPEN`/`REVIEW`/`BLOCKED`/`COMPLETED`/`FAILED` | `OPEN` | Task state. |
| `created_at` | ISO-8601 | now (UTC) | Creation time; controls oldest-first order. |
| `updated_at` | ISO-8601 | now (UTC) | Bumped on status changes. |
| `run_at` | ISO-8601 / `null` | `null` | One-shot schedule — earliest pick time. Past = fire now; future = wait. |
| `dependencies` | string[] | `[]` | Task IDs that must be `COMPLETED` first (`REVIEW` not satisfied). |
| `acceptance_criteria` | string[] | `[]` | Rendered under "Acceptance criteria:" in `FORGEO_TASK`. |
| `files_to_modify` | string[] | `[]` | Hint for the agent. |
| `agent_command` | string / string[] | — | Per-task agent override. |
| `agent_timeout_seconds` | number | — | Per-task timeout override. |
| `blocker_reason` | string[] | `[]` | Engine-managed: reason when `BLOCKED`. |
| `blocked_count` | int | `0` | Engine-managed: times transitioned to `BLOCKED`. |
| `failure_reason` | string[] | `[]` | Engine-managed: error when `FAILED`. |
| `agent_response` | string / `null` | `null` | Engine-managed: agent output on transitions. |
| `retries_left` | int / `null` | `null` | Per-task retry budget override (`null` = config default, `0` = no retries). |
| `retry_count` | int | `0` | Engine-managed: retries already attempted. |
| `failed_wait_cycles` | int | `0` | Engine-managed: cycles waiting for retry. |
| `review_required` | bool / `null` | `null` | Per-task review override (`null` = inherit `review_mode`). |
| `review_branch` | string / `null` | `null` | Engine-managed: feature branch for `REVIEW`. |
| `review_commit_sha` | string / `null` | `null` | Engine-managed: commit SHA on review branch. |

Only `id`, `title`, `description` are required.

### Per-task agent routing

```json
{
  "tasks": [
    {"id": "TASK-001", "title": "Add docstrings", "agent_command": "claude -p \"$FORGEO_TASK\" --model haiku"},
    {"id": "TASK-002", "title": "Rearchitect cache", "agent_command": "claude -p \"$FORGEO_TASK\" --model opus"}
  ]
}
```

## Statuses

| Status | Meaning |
| --- | --- |
| `OPEN` | To be picked. |
| `REVIEW` | Awaiting human review on feature branch; blocks dependants, not independent tasks. |
| `BLOCKED` | Needs human decision; Forgeo pauses. |
| `COMPLETED` | Agent finished, committed and pushed (or review approved). |
| `FAILED` | Agent errored; changes discarded, `failure_reason` recorded. |

### Review workflow

With `review_mode: branch` (and per-task `review_required` override) a successful task is committed on `review_branch_prefix + id` (default `forgeo/review/TASK-001`), pushed if `remote` set, and marked `REVIEW`. Independent `OPEN` tasks continue; dependants wait until `REVIEW` → `COMPLETED`.

Human merges the branch manually (PR or `git merge`), then marks **Complete** (`POST .../tasks/<id>/complete-review` → `COMPLETED`, clears `review_branch/sha`) or **Request changes** (`POST .../tasks/<id>/request-changes` → `OPEN`). `REVIEW` tasks are deletable and show `review_branch`/`review_commit_sha`. Issue providers use label `forgeo-review`.

### Retrying failed tasks

With `failed_retry_max` set, a `FAILED` task is moved back to `OPEN` after `failed_retry_wait_cycles` cycles and `retry_count` is incremented. Exhausted tasks stay `FAILED`. `BLOCKED` is never auto-retried.

Per-task override via `retries_left`:

```json
{"id": "TASK-001", "title": "Flaky test", "retries_left": 0},
{"id": "TASK-002", "title": "Migrate cache", "retries_left": 3}
```

Retry state is visible in `runs.jsonl` and the dashboard (task card/modal, History **retry** column). Reopening a `FAILED` task manually (edit backlog or dashboard) resets `retry_count`.

### Resolving blocked tasks

On `BLOCKED`, Forgeo commits partial work as `[partial]` and records `blocker_reason`. `BLOCKER.md` is a derived view — re-rendered each cycle, auto-removed when no `BLOCKED` tasks remain.

Reopen via dashboard (**Reopen** button) or `POST /api/.../tasks/<id>/reopen` — clears `blocker_reason`, keeps `blocked_count`. Editing the file's `status` back to `OPEN` does *not* clear `blocker_reason`; prefer the Reopen action.

### Resolving review tasks

On `REVIEW`, code is on the feature branch. Merge manually, then **Complete** (`POST .../tasks/<id>/complete-review`) or **Request changes** (`POST .../tasks/<id>/request-changes` → `OPEN`). Webhook `review` event fires on entry to `REVIEW` when enabled.

## Picking order

Forgeo picks the **oldest runnable `OPEN` task** — smallest `created_at` whose dependencies are all `COMPLETED` (`REVIEW` does not satisfy) and whose `run_at` is not in the future.

- `run_at` in the **past** → picked before tasks without `run_at` (most overdue first); daemon wakes early for it.
- `run_at` in the **future** → skipped until that moment; daemon sleeps only until then.
- `null` / omitted → plain oldest-first.
- Dependencies: missing or non-`COMPLETED` dependencies keep the task waiting forever. Surfaced in `forgeo status` (`waiting on:`) and the dashboard's *Waiting on dependencies* banner.

## One-shot scheduling

```json
{"id": "TASK-001", "title": "Regenerate docs", "run_at": "2026-08-21T09:00:00Z"}
```

Use for time-sensitive work ("run after deploy"). Ignored for non-`OPEN` tasks or tasks with unmet dependencies. Set/clear via dashboard Create/Edit forms (`PATCH`).

## Dependencies

```json
{"id": "TASK-003", "title": "Deploy", "dependencies": ["TASK-001", "TASK-002"]}
```

Oldest-first among *runnable* tasks — if the oldest `OPEN` task is blocked, the next runnable one is picked. Cyclic or permanently blocked dependencies yield no runnable task → refactoring pass until resolved.

## Execution

Once picked, the task goes to the agent as `FORGEO_TASK`; exit code decides commit/push vs `BLOCKED` vs discarded. See [Agent contract](agent-contract.md).

## File backlog: corruption tolerance & snapshots

- Missing file → empty backlog (created on first write).
- Corrupt file → renamed to `backlog.json.corrupt-<timestamp>`, restored from newest valid snapshot or empty.
- Unparsable row → kept as `FAILED` placeholder, not a store failure.
- Before every agent run (and on daemon start), the backlog is copied to `backlog.json.bak` / `backlog.json.bak.1` (rotating, keeps last 2). On corrupt read, newest valid snapshot is restored. No snapshots for remote backlogs.

## A backlog over HTTP

```yaml
backlog: https://api.example.com/backlog
```

| Operation | Request |
| --- | --- |
| Every read | `GET <url>` returns `{"tasks": [...]}` |
| Every write | `POST <url>` sends the full document |

Add `backlog_auth` for OAuth2. Same schema and ordering. Endpoint must:

- Return `{"tasks": [...]}`; non-object or non-list → empty.
- Replace, not append — POST body is the complete list.
- Send dates as ISO-8601 strings (not epoch numbers).
- Never send `null` for list fields.
- Preserve `agent_command` shape (string vs list).
- Store and return engine fields (`status`, `blocker_reason`, `failure_reason`, etc.).

A failed request fails the cycle (retried next interval), never treated as empty — otherwise a `POST` would overwrite the remote with nothing. Dashboard shows `502` for unreachable instances.

Runtime files (`backlog.lock`, `runs.jsonl`, etc.) go in `state_dir` (defaults to config dir).

## A Jira backlog

```yaml
# PAT:
backlog_provider: jira
backlog: https://jira.example.com
jira:
  jql: 'project = APP AND labels = forgeo'
  project_key: APP
  auth: {scheme: basic, username_env: JIRA_USER, token_env: JIRA_TOKEN}
  workflow: {open_statuses: ["10000", "10001"], open_status: "10000", running_status: "3", completed_status: "10002"}
# OAuth 3LO (Cloud):
# jira:
#   jql: 'project = APP AND labels = forgeo'
#   auth:
#     oauth: {client_id: xxxx, client_secret_env: JIRA_CLIENT_SECRET, scope: "offline_access read:jira-user read:jira-work"}
# # then: forgeo auth login --provider jira --client-id xxxx
```

- Jira keys → task IDs; `summary`/`description`/`created`/`updated` → task fields.
- `open_statuses` = pickable; `running_status` = claimed before agent runs; `completed_status`/`blocked_status`/`failed_status` = terminal (blocked/failed optional — labels `forgeo-blocked`/`forgeo-failed` always applied).
- Engine state (`blocker_reason`, `retry_count`, `agent_response`, etc.) in Jira issue property `forgeo`.
- Optional `jira.fields` maps custom fields for `acceptance_criteria`, `dependencies`, etc. Without `run_at` mapping, Jira `duedate` is used at midnight UTC. Dependencies also inferred from `blocks` issue links.
- Auth from env vars: `basic` (username + token) or `bearer` (PAT). Uses REST API v3 (`/search/jql` + cursor) by default; set `api_version: 2` for older servers.
- Daemon paginates JQL; stale claims released after `claim_timeout_seconds`. Unavailable Jira fails the cycle.

## A GitHub backlog

```yaml
# PAT:
backlog_provider: github
backlog: https://api.github.com   # or https://github.example.com/api/v3
github: {repo: owner/repo, auth: {token_env: GITHUB_TOKEN}}
# OAuth (browser/device):
# github: {repo: owner/repo, auth: {oauth: {client_id: Iv1.xxxx, flow: device, scope: repo}}}
# # then: forgeo auth login --provider github --client-id Iv1.xxxx
```

- Issue numbers → task IDs; `title`/`body`/`created_at`/`updated_at` → task fields.
- `open` → `OPEN`, `closed` → `COMPLETED`; labels `forgeo-running`/`blocked`/`failed` for the rest.
- Engine state in hidden `<!-- forgeo: {...} -->` block inside the body.
- Paginated `GET /repos/{owner}/{repo}/issues?state=all`; claiming adds `forgeo-running` + `claimed_at`.

Test live with `scripts/test-github-backlog-e2e.sh`:

```bash
GITHUB_TOKEN=... ./scripts/test-github-backlog-e2e.sh
```

## A GitLab backlog

```yaml
# PAT:
backlog_provider: gitlab
backlog: https://gitlab.example.com   # instance root, /api/v4 appended
gitlab: {repo: group/project, auth: {token_env: GITLAB_TOKEN}}
# OAuth (browser PKCE):
# gitlab: {repo: group/project, auth: {oauth: {client_id: abc, flow: browser, scope: api}}}
# # then: forgeo auth login --provider gitlab --client-id abc
```

- `iid` → task ID; `opened`/`closed` → `OPEN`/`COMPLETED`; same hidden-block and label mechanics as GitHub.
- Paginated `GET /api/v4/projects/:id/issues?state=all`.

## Managing tasks

Edit the file directly, or use the dashboard: **Create** form (`POST /api/instances/<name>/tasks` → auto `WEB-###` ID, `review_required` checkbox), task modal **Edit** (`PATCH` including `review_required`), **Delete** (`DELETE` for `OPEN`/`BLOCKED`/`REVIEW`), **Reopen** for `BLOCKED`, **Complete** / **Request changes** for `REVIEW`. For `jira`/`github`/`gitlab` the dashboard is a read-mostly mirror with links to native issues and banners to the native board.
