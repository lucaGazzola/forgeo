<div align="center">
  <img src="docs/img/logo.png" alt="Forgeo logo" width="128">
</div>

<div align="center">
  <img src="docs/img/title.svg" alt="Forgeo" width="128">
</div>

[![CI](https://github.com/lucaGazzola/forgeo/actions/workflows/ci.yml/badge.svg)](https://github.com/lucaGazzola/forgeo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

<div align="center">
  <img src="docs/img/demo.gif" alt="Forgeo running a backlog task end to end" width="720">
</div>

**Forgeo is a software factory for your coding agent.**

Give it a backlog and an agent CLI — Forgeo picks the next runnable task, runs the agent, and commits the result. It tracks progress in plain files and a web dashboard, and only interrupts you when a human decision is needed.

- **One task at a time** — oldest `OPEN` task whose dependencies are `COMPLETED` (or a `run_at` schedule). `REVIEW` blocks dependants but not independent tasks.
- **Agent-agnostic** — any CLI that reads `FORGEO_TASK` (aider, Claude, custom script).
- **Refactors when idle** — runs a refactoring pass when the backlog is empty.
- **Handles failure gracefully** — `BLOCKED` for human input (`BLOCKER.md`), `FAILED` with retry policy, snapshots for file backlogs, Telegram/webhook notifications.
- **Optional review** — `review_mode: branch` commits to `forgeo/review/TASK-001`, marks `REVIEW`, pushes and waits for human merge → `Complete`.

Requires a terminal, a git repo, and an agent CLI.

## Quickstart

Full walkthrough: [Getting Started](docs/getting-started.md).

### 1. Install

Pick one (no root; re-run to upgrade):

```bash
brew install lucaGazzola/forgeo/forgeo          # Homebrew, no Python needed
curl -fsSL https://forgeo.org/install.sh | bash  # binary or pip fallback
pipx install forgeo-cli                          # Python 3.11+
```

### 2. Init

```bash
forgeo init
```

Wizard in your project root. Creates `forgeo.yaml` and `.forgeo/` (backlog, logs, blockers). Prompts for backlog provider (`file` / `github` / `gitlab` / `jira` / `http`) and agent command. For issue providers it offers a PAT or OAuth login (`forgeo auth login` → `~/.config/forgeo/tokens/`); GitHub repository detection is automatic when possible, while Jira asks for its URL and JQL.

### 3. Fill the backlog

```bash
# file provider: edit .forgeo/backlog.json (see Backlog format)
# github/gitlab/jira/http: configured in forgeo.yaml, then:
forgeo validate

# or add tasks from the dashboard:
forgeo web      # http://0.0.0.0:8790  (use -d to keep it running)
```

![Forgeo web console](docs/img/console.png)

### 4. Start

```bash
forgeo validate   # dry run: config, repo, backlog, agent, locks
forgeo start      # daemon in background, one cycle per interval_minutes
```

Each cycle: pick task → run agent → commit/push (or `REVIEW` branch when `review_mode: branch`). Empty backlog → refactoring pass.

### Day-to-day

```bash
forgeo status              # config, counts, next task, daemon state, last outcome
forgeo once                # one cycle in foreground, no daemon
forgeo run --task TASK-012 # run a specific OPEN task now
forgeo stop                # stop daemon
forgeo restart             # restart daemon
forgeo web                 # dashboard for all instances
```

`forgeo web` is open by default on `0.0.0.0:8790`. On a shared host use `forgeo web --token` for bearer auth — see [Web console](docs/web-console-api.md).

### Docker sandbox

Run the agent isolated:

```yaml
agent_sandbox: docker
agent_sandbox_image: your-image   # must contain agent CLI + sh
agent_sandbox_network: none       # default, no network
agent_sandbox_mounts: [~/.claude] # read-only mounts
```

Repo is bind-mounted at the same path; task arrives as `FORGEO_TASK`. See [Configuration](docs/configuration.md).

### Multiple repos

One config per repo, fully independent (own backlog, logs, locks). Use the instance registry:

```bash
forgeo instance add site-a --config /path/to/site-a/forgeo.yaml
forgeo start --name site-a
forgeo list          # all instances
forgeo web           # aggregate dashboard
```

## Documentation

| Topic | Doc |
| --- | --- |
| Install, init, first cycle | [Getting Started](docs/getting-started.md) |
| All `forgeo.yaml` keys | [Configuration](docs/configuration.md) |
| Task schema & statuses | [Backlog format](docs/backlog.md) |
| Agent env, exit codes, timeouts | [Agent contract](docs/agent-contract.md) |
| All CLI commands | [CLI reference](docs/cli-reference.md) |
| Dashboard & HTTP API | [Web console & HTTP API](docs/web-console-api.md) |

**Backlog providers:** [file](docs/backlog.md#a-json-file-backlog) · [HTTP](docs/backlog.md#a-backlog-over-http) · [Jira](docs/backlog.md#a-jira-backlog) · [GitHub](docs/backlog.md#a-github-backlog) · [GitLab](docs/backlog.md#a-gitlab-backlog) — file/HTTP exchange the full document; Jira/GitHub/GitLab sync issues individually. File backlogs are snapshotted (`backlog.json.bak`) before each run and restored if corrupt.

**Dashboard:** `forgeo web` aggregates every instance. For `file`/`http` it is the primary editor; for `jira`/`github`/`gitlab` it is a read-mostly mirror (links to native issues, surfaces `BLOCKED`/`FAILED` reasons and retry state).

## Develop

```bash
pip install -e ".[dev]"
pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) (quality gates: `pytest`, `ruff check`, `mypy src/forgeo`).

## License

MIT — see [LICENSE](LICENSE).
