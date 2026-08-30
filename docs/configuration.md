# Configuration reference

Forgeo reads `forgeo.yaml` from the current directory (`--config <file>` to override). Relative paths resolve against the config file's location. The daemon re-reads the file on the next cycle (or `SIGHUP`); invalid changes are logged and ignored. Moving `repo`, `backlog`, `blocker_file` or `log_file` requires `forgeo restart`.

## Keys

| Key | Default | Description |
| --- | --- | --- |
| `name` | `forgeo` | Display name in logs, commits, notifications. |
| `repo` | `.` | Git repository path. |
| `interval_minutes` | `60` | Cycle interval (≥ 1). |
| `branch` | `main` | Single branch for all commits. |
| `remote` | — | Remote to push to; omit for local-only commits. |
| `backlog` | `backlog.json` | Task source: file path, HTTP URL, or base URL for `jira`/`github`/`gitlab`. |
| `backlog_provider` | `auto` | `auto` infers file/HTTP; or `file`/`http`/`jira`/`github`/`gitlab`. |
| `state_dir` | — | Runtime files dir (locks, history). Defaults to config dir for remote backlogs. |
| `backlog_auth` | — | OAuth2 client credentials for `http` backlog ([below](#backlog_auth)). |
| `jira` | — | Jira settings (required if `backlog_provider: jira`). |
| `github` | — | GitHub settings (required if `backlog_provider: github`). |
| `gitlab` | — | GitLab settings (required if `backlog_provider: gitlab`). |
| `blocker_file` | `BLOCKER.md` | Blocker output path — keep outside repo. |
| `agent_command` | — | **Required.** Shell command or argv list for the agent. |
| `agent_timeout_seconds` | — | Kill agent after N seconds (`null` = never). |
| `agent_env` | `{}` | Extra env vars for the agent. |
| `agent_sandbox` | `none` | `none` (host) or `docker` (container). |
| `agent_sandbox_image` | — | Image for `docker` mode (must contain agent + `sh`). |
| `agent_sandbox_network` | `none` | Docker `--network` value. |
| `agent_sandbox_mounts` | `[]` | Host paths mounted read-only in container. |
| `blocked_exit_code` | `2` | Exit code meaning "needs human input". |
| `no_changes_exit_code` | `3` | Exit code meaning "no code change needed". |
| `refactor_prompt` | default | Instruction when backlog is empty. |
| `task_context` | — | File prepended to every agent instruction ([below](#task_context)). |
| `log_file` | `forgeo.log` | Daemon log path. |
| `run_history_keep` | `2000` | Max `runs.jsonl` records (0 = unlimited). |
| `run_output_lines` | `200` | Tail lines of agent output per run record (0 = none). |
| `agent_response_lines` | — | Tail lines of agent output on task transitions (unbounded by default, 0 = none). |
| `failed_retry_max` | `0` | Auto-retries for `FAILED` tasks (0 = manual reopen). |
| `failed_retry_wait_cycles` | `1` | Backoff cycles before retry. |
| `no_changes_retry_max` | `0` | Immediate re-runs when agent exits 0 with no changes. |
| `git_timeout_seconds` | `120` | Timeout for git subprocesses. |
| `telegram_bot_token` / `telegram_chat_id` | — | Telegram blocked-run notifications (both required). |
| `notify_webhook_url` | — | Webhook URL for run outcomes. |
| `notify_webhook_events` | `["blocked"]` | Which outcomes to POST (`blocked`/`completed`/`failed`). |

## Minimal example

```yaml
name: my-project
repo: .
interval_minutes: 30
branch: main
backlog: .forgeo/backlog.json
blocker_file: .forgeo/BLOCKER.md
agent_command: "claude -p \"$FORGEO_TASK\""
```

## Backlog providers

### `file` / `http` (default)

```yaml
backlog: .forgeo/backlog.json          # file
backlog: https://api.example.com/backlog  # http — GET returns doc, POST replaces it
```

Add `backlog_auth` for OAuth2-protected HTTP endpoints (see below). A failed HTTP request fails the cycle — never treated as an empty backlog.

### Jira

```yaml
backlog_provider: jira
backlog: https://jira.example.com
state_dir: .forgeo
jira:
  jql: 'project = APP AND labels = forgeo'
  project_key: APP
  issue_type: Task
  auth:
    scheme: basic           # or bearer for Server/DC PAT
    username_env: JIRA_USER
    token_env: JIRA_TOKEN
  workflow:
    open_statuses: ["10000", "10001"]
    open_status: "10000"
    running_status: "3"
    completed_status: "10002"
```

| Key | Default | Description |
| --- | --- | --- |
| `jira.jql` | — | JQL scope — include all lifecycle states. |
| `jira.auth` | — | `basic` (username + token) or `bearer` (token). |
| `jira.project_key` | — | Project for dashboard task creation. |
| `jira.issue_type` | `Task` | Issue type for creation. |
| `jira.api_version` | `3` | `3` = Cloud cursor pagination, `2` = offset. |
| `jira.page_size` | `50` | Issues per page (1–100). |
| `jira.max_issues` | `1000` | Max issues per search. |
| `jira.timeout_seconds` | `30` | HTTP timeout. |
| `jira.claim_timeout_seconds` | `86400` | Stale claim timeout. |
| `jira.label_prefix` | `forgeo` | Prefix for `forgeo-running`/`blocked`/`failed` labels. |
| `jira.property_key` | `forgeo` | Issue property for engine state. |
| `jira.workflow` | defaults | Status IDs/names for open/running/blocked/completed/failed. |
| `jira.fields` | — | Custom-field IDs for `acceptance_criteria`, `dependencies`, `files_to_modify`, `agent_command`, `agent_timeout_seconds`, `run_at`, `retries_left`. |

Status values can be names or IDs (IDs preferred). Jira keys become task IDs. Engine state (`blocker_reason`, `retry_count`, `agent_response`) lives in the `forgeo` issue property. Dependencies can also be inferred from `blocks` issue links.

### GitHub

```yaml
backlog_provider: github
backlog: https://api.github.com   # or https://github.example.com/api/v3
github:
  repo: owner/repo
  token_env: GITHUB_TOKEN
```

| Key | Default | Description |
| --- | --- | --- |
| `github.repo` | — | `owner/repo`. |
| `github.auth` | — | `token_env` for PAT. |
| `github.label_prefix` | `forgeo` | Label prefix. |
| `github.property_key` | `forgeo` | Marker key for hidden body block. |
| `github.page_size` | `30` | Issues per page. |
| `github.max_issues` | `1000` | Max issues. |
| `github.timeout_seconds` | `30` | HTTP timeout. |
| `github.claim_timeout_seconds` | `86400` | Stale claim timeout. |
| `github.workflow` | defaults | State/label mapping. |
| `github.fields` | — | Field mappings for `acceptance_criteria`, `dependencies`, `files_to_modify`, `agent_command`, `agent_timeout_seconds`, `run_at`, `retries_left`. |

Issue numbers become task IDs; `open`/`closed` maps to `OPEN`/`COMPLETED`; `forgeo-running`/`blocked`/`failed` labels cover the rest. Engine state is stored in a hidden `<!-- forgeo: {...} -->` block in the issue body.

### GitLab

```yaml
backlog_provider: gitlab
backlog: https://gitlab.example.com   # instance root, /api/v4 appended
gitlab:
  repo: group/project
  token_env: GITLAB_TOKEN
```

Same keys as GitHub (`gitlab.*`), including `workflow` and `fields` for the same 7 mappings. Issue `iid` becomes task ID; `opened`/`closed` maps to `OPEN`/`COMPLETED`; hidden `<!-- forgeo: {...} -->` block for engine state.

## Key details

### `backlog_auth`

OAuth2 client-credentials for an HTTP backlog behind an IdP (e.g. Keycloak):

```yaml
backlog: https://api.example.com/backlog
backlog_auth:
  token_url: https://keycloak.example.com/realms/dev/protocol/openid-connect/token
  client_id: forgeo
  client_secret_env: FORGEO_BACKLOG_CLIENT_SECRET
  scope: forgeo-backlog
```

Secret stays in the env var (never in `forgeo.yaml`). Tokens are cached and refreshed before expiry; a `401`/`403` triggers one retry with a fresh token. Rejected when `backlog` is a file.

### `agent_command` / `agent_timeout_seconds` / `agent_env`

```yaml
agent_command: "claude -p \"$FORGEO_TASK\""
# or argv list (no shell):
agent_command: ["aider", "--message", "$FORGEO_TASK"]
agent_timeout_seconds: 600
agent_env:
  MODEL: claude-sonnet-4
```

Runs with repo as cwd; task arrives as `FORGEO_TASK`. `FORGEO_*` vars always win. Timeout kills the agent and fails the task; unset means no timeout. Overruns of `interval_minutes` are skipped, not killed. See [Agent contract](agent-contract.md).

### `blocked_exit_code` / `no_changes_exit_code`

- `blocked_exit_code` (default `2`) → `BLOCKED`, partial commit, `BLOCKER.md`.
- `no_changes_exit_code` (default `3`) → `COMPLETED` without commit (tree must be clean).
- Exit `0` with no changes → retried (`no_changes_retry_max`) then `BLOCKED`, never `COMPLETED` silently.

### `agent_sandbox`

```yaml
agent_sandbox: docker
agent_sandbox_image: forgeo-agent
agent_sandbox_network: none
agent_sandbox_mounts: [~/.claude]
```

`docker` runs via `docker run --rm`: repo bind-mounted at same path, `FORGEO_*` and `agent_env` passed through, networking off by default, only listed mounts visible. Requires `docker` binary and image with agent + `sh`.

### `task_context`

```yaml
task_context: CONTEXT.md
```

File contents prepended to every `FORGEO_TASK` (under `# Project context`, task under `# Task`). Re-read each run. Missing file is a warning, not a failure.

### Retries

```yaml
failed_retry_max: 3
failed_retry_wait_cycles: 2   # backoff before moving FAILED → OPEN
no_changes_retry_max: 2       # immediate re-runs for silent no-change
```

- `failed_retry_max: 0` (default) → `FAILED` stays until human reopens.
- Per-task `retries_left` overrides the global budget (see [Backlog](backlog.md)).
- `BLOCKED` is never auto-retried.

### Notifications

**Telegram** — both `telegram_bot_token` and `telegram_chat_id` required. Failures are logged, never fatal.

**Webhook** — `notify_webhook_url` POSTs JSON for selected events:

```json
{"forgeo": "my-forgeo", "outcome": "blocked", "task_id": "TASK-001", "task_title": "Do the thing", "reason": "..."}
```

```yaml
notify_webhook_url: "https://hooks.example.com/forgeo"
notify_webhook_events: [blocked, completed, failed]
```

Any non-200 is a warning (5s timeout, stdlib only).

### Run history

- `run_history_keep` (default `2000`) — trim `runs.jsonl` atomically on append; `0` = grow forever.
- `run_output_lines` (default `200`) — tail of agent stdout/stderr per run record; `0` = none.
- `agent_response_lines` (default unbounded) — tail persisted on the task's `agent_response`; `0` = none.

### Instance registry

Maps name → absolute `forgeo.yaml` path. File: `$FORGEO_REGISTRY` or `~/.config/forgeo/instances.yaml`.

```yaml
site-a: /home/me/site-a/forgeo.yaml
site-b: /home/me/site-b/forgeo.yaml
```

Created on `forgeo instance add` or auto-registered on `forgeo start`/`stop` with `--config`. Names must match `^[a-zA-Z0-9._-]+$`. Manage with `forgeo instance add|rm|list` — see [CLI reference](cli-reference.md).

Each instance is isolated: own backlog, logs, `runs.jsonl`, locks, and `backlog.state.json`. Runtime files sit next to the backlog, or in `state_dir` for remote backlogs. The dashboard (`forgeo web`, default `:8790`) reads them directly — no per-daemon ports.

### Default refactor prompt

When `refactor_prompt` is omitted:

> Review the codebase for improvement opportunities that do not change behavior: dead code, duplication, overly complex functions, missing tests, outdated comments. Apply the safe improvements you find and run the test suite to verify nothing broke. If nothing needs refactoring, make no changes.
