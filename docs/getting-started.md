# Getting Started

Install the CLI, init a config, and run your first task.

## 1. Install

```bash
brew install lucaGazzola/forgeo/forgeo          # macOS/Linux, no Python needed
curl -fsSL https://forgeo.org/install.sh | bash  # binary or pip fallback
pipx install forgeo-cli                          # Python 3.11+
```

- Homebrew and the one-liner fetch a prebuilt binary from GitHub Releases.
- The one-liner falls back to `pipx` → `pip install --user` if no binary matches.
- Re-run the same command to upgrade. No root required.
- On Homebrew 4.6+, run `brew trust lucaGazzola/forgeo` first if the tap is untrusted.

Forgeo checks PyPI at most once a day on `forgeo start`/`once` and notifies when an upgrade exists. Set `FORGEO_UPDATE_CHECK=0` to disable. It never auto-updates.

## 2. Initialize

From your project root:

```bash
forgeo init
```

The wizard asks for:

1. **Forgeo folder** — where backlog/logs live (default `.forgeo`, gitignored).
2. **Backlog provider** — `file` (local JSON), `github`/`gitlab`/`jira`/`http`. For `github` it auto-detects `owner/repo` from `git remote` and can persist a pasted `GITHUB_TOKEN` to `~/.config/forgeo/github_token_env.sh`.
3. **Agent command** — bare command for your agent (default `opencode run --auto`). Forgeo appends the task prompt (`$FORGEO_TASK`); if your command already references `$FORGEO_TASK` it is kept verbatim.
4. **Refactor prompt** — used when the backlog is empty.

Writes `forgeo.yaml`, creates the folder, and appends it to `.gitignore` (opt-out available). With `github`/`gitlab`/`jira` it also sets `backlog_provider`, `backlog` URL, provider block, and `state_dir`.

```bash
forgeo init --force    # overwrite existing config
```

## 3. Create the backlog

**File provider** — edit `.forgeo/backlog.json` (see [Backlog format](backlog.md)):

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

You can also add tasks from the dashboard once Forgeo is running — no file editing needed.

**Remote providers** — `forgeo.yaml` already points at the provider. Export credentials and validate:

```bash
export GITHUB_TOKEN=ghp_...   # or GITLAB_TOKEN / JIRA_USER + JIRA_TOKEN
forgeo validate
```

See [Backlog format](backlog.md) for Jira/GitHub/GitLab details. The dashboard for issue providers is a read-mostly mirror — triage stays in the native tracker.

!!! tip
    Ask your LLM to generate the initial backlog from your project spec.

## 4. Start

```bash
forgeo validate   # dry run: checks config, repo, backlog, agent, locks
forgeo start      # daemon in background; wakes every interval_minutes
forgeo start -f   # foreground, Ctrl-C to stop
forgeo status     # config, counts, next task, daemon state, last outcome
```

`forgeo start` runs pre-flight checks (same as `validate`) and refuses to start on errors. The daemon itself binds no ports — open the dashboard with `forgeo web` (see [Web console](web-console-api.md)):

![Forgeo web console](img/console.png)

## 5. Run one task

```bash
forgeo once                  # one cycle in foreground, no daemon
forgeo run --task TASK-012   # one specific OPEN task (triage)
```

`forgeo run` refuses if the task is missing or not `OPEN`, or if a daemon is already running.

## 6. Multiple repos

One config per repo, each daemon independent. The **instance registry** gives each a name:

```bash
forgeo init                                            # in each repo
forgeo start --config /path/to/site-a/forgeo.yaml      # auto-registers as `name`
forgeo start --config /path/to/site-b/forgeo.yaml
forgeo instance list   # or: forgeo list
forgeo stop --name site-a
forgeo web             # aggregate dashboard on :8790
forgeo web -d && forgeo web stop   # background dashboard
```

`--name` works with `start`, `once`, `run`, `status`, `validate`, `stop`, `restart` (mutually exclusive with `--config`).

## Next steps

- [Configuration](configuration.md) — every `forgeo.yaml` key.
- [Backlog format](backlog.md) — schema, ordering, dependencies.
- [Agent contract](agent-contract.md) — env, exit codes, timeouts.
- [CLI reference](cli-reference.md) — all commands.
