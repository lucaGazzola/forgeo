# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Web console now treats `jira`/`github`/`gitlab` as a **read-mostly mirror** instead of a replacement: the home page cards show `Open in Jira/GitHub/GitLab ↗` (via `external_board_url`/`external_board_label`), the instance page shows a top banner linking to the native board, and each task card/modal links to the native issue (`external_url`). Document backlogs (`file`/`http`) keep the existing primary-editor behaviour. Creating/editing still works through the dashboard, but triage is expected in the native tracker where Forgeo-specific state (BLOCKED/FAILED reasons, `agent_response`, retry budget) is now surfaced on the mirror.
- Central API now exposes `backlog_provider`, `backlog`, `backlog_is_issue_provider`, `external_board_url`/`external_board_label` on `GET /api/instances` and `GET /api/instances/<name>/status`, and `external_url` per task on `GET /api/instances/<name>/tasks` (+ single-task) for issue providers. Covers `https://api.github.com` → `https://github.com` and `https://…/api/v3` → web base mapping for GitHub Enterprise, and `https://jira…/issues/?jql=` / `https://gitlab…/{repo}/-/issues` board links.

### Changed

- `README`, `docs/backlog.md`, `docs/getting-started.md` and `docs/web-console-api.md` document the mirror vs editor split and the new `external_*` API fields.
- `src/forgeo/central.py` deduplicates provider metadata via `_backlog_meta()` and centralises GitHub web-base handling; `instance-card` is now a `div[role=link]` to allow nested external links without invalid HTML.

## [0.9.0] - 2026-08-23

### Added

- GitHub Issues and GitLab Issues task providers. Set `backlog_provider: github`
  or `gitlab` and point `backlog:` at the API base URL
  (`https://api.github.com` or `https://github.example.com/api/v3` for
  Enterprise; `https://gitlab.com` or a self-hosted root for GitLab). Issue
  numbers/`iid`s become Forgeo task ids, `open`/`opened` vs `closed` maps to
  `OPEN`/`COMPLETED`, and `forgeo-running`/`forgeo-blocked`/`forgeo-failed`
  labels (configurable via `label_prefix`) represent the remaining states.
  Engine state — blocker and failure reasons, retry counters, claim time,
  dependencies, and bounded agent output — is stored in a hidden
  `<!-- forgeo: {...} -->` block inside the issue body/description, so the
  visible text stays human-readable and no custom fields or issue properties
  are required.
- `github` and `gitlab` config blocks: `repo` (`owner/repo` or project path/id),
  `token_env` (PAT from an environment variable, never stored in the file),
  `label_prefix`/`property_key`, pagination, timeouts, stale-claim recovery
  via `claim_timeout_seconds`, and optional workflow/field mappings mirroring
  the Jira provider.
- Shared issue-provider helpers extracted to `backlog_issue_base` and a new
  `DocumentBacklogStore` / `IssueBacklogBase` split in `backlog.py`,
  unifying claim, label, and engine-state handling across Jira, GitHub, and
  GitLab. `forgeo validate` now checks any remote backlog with a
  provider-specific message, and `config/forgeo.yaml`, the README, and the
  backlog/configuration docs list and document all five providers
  (`file`, `http`, `jira`, `github`, `gitlab`).

## [0.8.0] - 2026-08-21

### Added

- A Jira task provider selected with `backlog_provider: jira`, including Jira
  Cloud cursor pagination, workflow-based claiming, optional custom-field
  mappings, issue-property engine state, stale-claim recovery, and bearer or
  basic authentication from environment variables.
- A provider-level claim hook so remote task sources can prevent duplicate
  agent runs while preserving the existing JSON and HTTP backlog behavior.

## [0.7.3] - 2026-08-19

### Added

- The agent's stdout/stderr is now persisted on the task as `agent_response`
  when a task transitions (BLOCKED/FAILED/COMPLETED), shown in the web
  console's task modal. A transition that carries no output never wipes a
  previously stored response.
- `agent_response_lines` config key: how many agent output lines the task's
  `agent_response` keeps on a status transition. Unbounded by default; `0`
  disables persisting agent output on the task.

## [0.7.2] - 2026-08-18

### Added

- `no_changes_retry_max` config key: a task whose agent exits `0` without
  producing any code changes is re-run immediately, in the same cycle, up to
  that many extra times before it is marked `BLOCKED` for human review.

### Changed

- A silent no-change SUCCESS (agent exits `0` with an unchanged working tree)
  now marks the task `BLOCKED` instead of `FAILED`: the only acceptable
  outcome for a run that ends without code changes is a blocked task awaiting
  human review. An agent that needs no code change must still opt in
  explicitly with `no_changes_exit_code` to complete the task.

## [0.7.1] - 2026-08-17

### Added

- An animated demo GIF in the README, showing Forgeo running a backlog task
  end to end, plus an Open Graph social-preview image for the docs site.

### Changed

- The default agent prompt (used by `forgeo init`) now names `AGENTS.md` and
  `CONTEXT.md` explicitly when telling the agent to keep project overview and
  conventions up to date.

## [0.7.0] - 2026-08-16

### Added

- `task_context` config key: a path to a file (e.g. `CONTEXT.md`) whose
  contents are prepended to every agent instruction — tasks and refactoring
  runs alike — before the task description. The agent gets the high-level
  project overview instead of only the isolated task; the file is re-read on
  every run, so the agent's own updates are picked up on the next cycle. A
  missing or unreadable file never fails a cycle: it is logged, `forgeo
  validate` reports it as a warning, and the run proceeds with the bare
  instruction.
- The default agent prompt (used by `forgeo init`) now tells the agent to
  read `AGENTS.md` (and `CONTEXT.md` if present) at the start of the session
  and to keep them updated when a change materially affects the project
  overview.

- `forgeo run --task <id>` runs exactly one specific `OPEN` task by id and
  exits, instead of letting `forgeo once` pick the oldest one — for triage:
  rerun a `FAILED` task (after reopening it) or try a risky task now. It
  shares the same per-forgeo lock as the daemon and `forgeo once`, so it
  never overlaps them; it refuses with a clear error when the task does not
  exist or is not `OPEN`, and while another daemon/`once`/`run` holds the
  lock.

## [0.6.0] - 2026-08-14

### Added

- The backlog can now live in another application instead of a file: set
  `backlog:` to an `http(s)` URL and Forgeo reads the whole document with
  `GET` on every read and writes it back with `POST` on every change, using
  the same JSON shape as `backlog.json`. The endpoint replaces its task list
  with the body it receives. A request that fails (network error, 5xx,
  malformed body) fails the cycle and is retried on the next interval — it is
  never read as an empty backlog, which would start a refactoring pass and let
  the next `POST` overwrite the remote task list with nothing.

- `backlog_auth`: OAuth2 client-credentials access for a backlog URL behind an
  identity provider such as Keycloak. Forgeo obtains an access token for a
  confidential client (a service account, not a human login) and sends it as a
  bearer on every backlog request; tokens are cached in memory, renewed before
  they expire, and refreshed once with a retry when the endpoint answers 401 or
  403. The client secret is never a config value: `client_secret_env` names the
  environment variable holding it, so it stays out of `forgeo.yaml`.

- `state_dir`: where Forgeo's own runtime files go (`backlog.lock`,
  `backlog.run`, `backlog.state.json`, `backlog.update.json`, `runs.jsonl`).
  It only matters with a backlog URL, where there is no backlog file for them
  to sit beside; it then defaults to the directory holding `forgeo.yaml`. With
  a backlog file those paths are unchanged.

- `forgeo validate` now checks a backlog URL by fetching it once (a plain
  `GET`, with `backlog_auth` credentials when configured), so an unreachable
  endpoint or a rejected token is reported by the dry run instead of by the
  first cycle. A file backlog is still read from disk, and nothing is written
  either way.

## [0.5.0] - 2026-08-14

### Added

- `forgeo validate` (and the pre-flight check before a detached
  `forgeo start`) distinguishes a repository with no commits yet: a clean
  tree is now a warning — the first cycle creates the initial commit — while
  a non-clean tree is a problem that names the fix
  (`git add -A && git commit -m "Initial commit"`), since every file is
  untracked and every cycle would otherwise refuse as dirty. Previously
  "no commits" was always a hard problem with a misleading message.

- `forgeo start` now starts the daemon **detached in the background and
  exits**, like `forgeo restart` and the web console's start button already
  did; the daemon is managed with `forgeo stop`/`forgeo restart`/`forgeo
  status`. `forgeo start -f` (`--foreground`) keeps the historical
  foreground behavior. A detached start refuses while the per-forgeo lock is
  held and runs the same read-only checks as `forgeo validate` first, so a
  broken config fails fast instead of leaving a silently dead daemon.
  `--interval-minutes` is forwarded to the detached daemon.

- The daemon reloads `forgeo.yaml` on the next cycle boundary when the file
  changes (or on `SIGHUP`): a valid change is revalidated, logged, and used
  from the next cycle; an invalid change is logged and the last valid config
  stays in use. The web console's config save reflects this
  (`restart_required: false`). Path changes (`repo`, `backlog`,
  `blocker_file`, `log_file`) stay pinned to the daemon's startup values and
  still need `forgeo restart`, so the daemon's lock files are never detached
  from the config.

- Optional bearer-token auth for the central web dashboard (`forgeo web`):
  `forgeo web --token` (or a `token` key in `~/.config/forgeo/web.toml`)
  requires `Authorization: Bearer <token>` on every `/api/*` route and
  answers `401` otherwise. `forgeo web --token` with no value generates a
  token, prints it once on startup, and saves it (mode `0600`); a generated
  token is only ever printed once. Static assets and the new token prompt
  page (`/central/login.html`) stay reachable without a token, and a
  `?token=...` URL signs the browser in automatically. With no flag and no
  token file the dashboard keeps its historical open-by-default behavior.

- `forgeo validate` — a read-only dry run that checks whether a forgeo is
  ready to run before starting it: the config schema, the repository (exists,
  is a git repo, `git` on PATH), the branch and remote resolution, the
  backlog parsing, a non-blank agent command, and the run lock state. It
  reports every problem at once, never invokes the agent, and makes no writes
  (no lock, no backlog changes). Exit code `0` when healthy, `1` with a
  summary of problems otherwise. Supports `--config` and `--name`.

- `run_history_keep` config key: `runs.jsonl` is trimmed to that many records
  on append (default `2000`), so a busy Forgeo's run history never grows
  forever. Trimming is atomic (temp file + rename) and a failed trim is
  logged and skipped, never fatal to the cycle; `run_history_keep: 0`
  disables retention entirely (the previous grow-forever behavior).

- Persisted agent output per run: each `RunRecord` now stores a bounded tail
  of the agent's stdout/stderr (last `run_output_lines` lines, default `200`),
  so failed and blocked runs keep the full tail of what the agent said. The
  web console's **History** tab shows it in a read-only, monospace,
  collapsible view; `run_output_lines: 0` disables persistence, and records
  written before the field existed render as empty.

- Backlog snapshots: before every agent run (and on daemon startup) Forgeo
  copies the backlog to a rotating snapshot (`backlog.json.bak`,
  `backlog.json.bak.1`, ... keeping the last 2 by default), and a read that
  finds the backlog corrupt restores the newest valid snapshot in place —
  with the corrupt file still preserved — instead of falling back to an
  empty store. A missing backlog is a no-op.

- Task dependencies are now enforced when picking the next task: Forgeo picks
  the oldest `OPEN` task whose `dependencies` are all `COMPLETED` instead of
  the plain oldest `OPEN` task, so a task never runs before the work it
  depends on. Unsatisfied dependencies (including ids that don't exist in the
  backlog) are surfaced on `forgeo status` (`waiting on:` line) and in the web
  console's task detail modal (*Waiting on dependencies* banner); the
  `GET /api/instances/<name>/tasks*` responses annotate each task with an
  `unsatisfied_dependencies` field.

- Homebrew install support: `brew install lucaGazzola/forgeo/forgeo`
  installs the prebuilt binary on macOS (arm64/Intel) and Linux (Intel). The
  `publish-homebrew` CI job re-renders the tap formula (sha256 + version)
  from `scripts/render_homebrew_formula.py` on every release; it needs the
  `HOMEBREW_TAP_TOKEN` repository secret (PAT with write access to
  `lucaGazzola/homebrew-forgeo`). The update notification now also names
  `brew upgrade lucaGazzola/forgeo/forgeo`.

- Update notification: when `forgeo start` or `forgeo once` begins a cycle,
  Forgeo checks PyPI at most once a day and, if a newer `forgeo-cli` release
  exists, prints/logs a short notice with the upgrade command. The check is
  best-effort (short timeout, failures logged and skipped), never modifies
  the install, and can be disabled with `FORGEO_UPDATE_CHECK=0`.

- Automatic retries for `FAILED` tasks: `failed_retry_max` config key (default
  `0`, unchanged behavior) plus `failed_retry_wait_cycles` (default `1`) let a
  transiently failed task move back to `OPEN` after a backoff and be run
  again. A task that exhausts its budget stays `FAILED` with its original
  `failure_reason`; a per-task `retries_left` field overrides the budget for
  one task. `BLOCKED` tasks are never auto-retried. The retry count is
  recorded in `runs.jsonl` (the run record that succeeds carries it), shown
  in the web console (task cards/modal and a History-tab **retry** column),
  and exposed by the tasks/runs API.

### Fixed

- The Linux prebuilt binary is now built on Ubuntu 22.04 (glibc 2.35) instead
  of 24.04 (glibc 2.38), so it runs on older distros (e.g. Ubuntu 22.04,
  Debian 12, Homebrew-on-Linux). The 0.4.0 `forgeo-linux-amd64` release
  asset was rebuilt and re-uploaded with the same version number.

## [0.4.0] - 2026-08-10

### Added

- Delete OPEN tasks from the web console: a Delete button (with
  confirmation) in the task detail modal, backed by `DELETE
  /api/instances/<name>/tasks/<id>` and `JSONBacklog.delete_task`.
- Resolve BLOCKED tasks from the web console: the task modal shows the
  blocker reason and can reopen the task (back to `OPEN`) via `POST
  /api/instances/<name>/tasks/<id>/reopen`.
- The web console stays usable with many tasks: non-OPEN columns collapse
  behind count badges with an expand toggle, so a long backlog no longer
  renders every task as a tall card up front.
- The failure/block reason is shown prominently in the task detail modal.
- Config editing from the web console: `PUT /api/instances/<name>/config`
  validates and persists `forgeo.yaml` changes, and a new Config tab in the
  instance page edits the fields in a form (with a restart hint).
- Daemon control from the web console: start, stop, and restart an
  instance's daemon from the top bar via `POST
  /api/instances/<name>/{start,stop,restart}`.
- `forgeo init` now asks only for the bare agent command and appends the
  task prompt automatically.

### Changed

- Commit messages no longer carry the `forgeo: ` prefix.
- The instance is registered before the run lock is taken, so a `--name`
  lookup never fails while starting.
- README revised for clarity and formatting.

## [0.3.0] - 2026-08-07

### Added

- `forgeo web [--host HOST] [--port PORT]` — a standalone central dashboard
  (default `0.0.0.0:8790`, foreground like `forgeo start`) that aggregates
  every registered instance. It reads each instance's data straight from its
  files (`backlog.json`, `runs.jsonl`, `forgeo.log`, `BLOCKER.md`), so it
  works whether or not that instance's daemon is running. Home page at `/`,
  per-instance pages at `/instances/<name>/` (kanban backlog plus logs,
  runs, blocker and config tabs), and a per-instance API under
  `/api/instances/<name>/`.
- Shared web-server helpers (`forgeo.web_common`) used by the central
  dashboard.
- Task editing in the web console: the task detail modal gained an **Edit**
  mode (Save/Cancel), backed by a new `PATCH
  /api/instances/<name>/tasks/<id>` endpoint and `JSONBacklog.update_task`.
- PyPI publishing: tagging a release now also publishes the `forgeo-cli`
  wheel and sdist to PyPI via trusted publishing.

### Changed

- **The embedded per-daemon web server is gone.** `forgeo start` no longer
  binds any port (`web_host`/`web_port` config keys are removed). The daemon
  instead writes its live state (pid, started at, last outcome, next run) to
  `daemon.state.json` next to the backlog after every cycle.
- The central dashboard (`forgeo web`) is now the **only** web interface:
  it reads the daemon state files for accurate status, and it gained the
  instance backlog's write endpoints, `POST
  /api/instances/<name>/tasks` (with the web form on each instance page) and
  `PATCH /api/instances/<name>/tasks/<id>`, so no feature was lost.
- `forgeo.web_common` docstring/API updated; `forgeo.server` module
  removed.

## [0.2.1] - 2026-08-05

### Added

- `web_host` config option: the web dashboard/API bind address. Default
  `127.0.0.1` (unchanged behavior); set `0.0.0.0` to reach it from other
  hosts on the local network.
- `install.sh` now prefers a prebuilt standalone binary downloaded from the
  matching GitHub Release for the host OS/arch — **no Python required**.
  The pipx/pip fallback remains, used only when no prebuilt binary matches
  the platform and a Python >= 3.11 is available.
- Tag-triggered CI builds single-file executables with PyInstaller on
  Linux (amd64), macOS (amd64/arm64), and Windows (amd64) and attaches them
  to the GitHub Release (`forgeo.spec`).
- Installer tests cover the binary-download path and the pipx/pip fallback
  with stubs (no network).

## [0.2.0] - 2026-08-04

### Added

- `CHANGELOG.md` in Keep a Changelog format, with the `0.1.0` history
  backfilled.
- Tag-triggered CI job that builds the wheel and sdist and attaches them to a
  GitHub Release.
- Release steps documented in `CONTRIBUTING.md`.
- Web console frontend in `src/forgeo/web/`: a self-contained
  HTML/CSS/JS dashboard (no framework, no build step, no external assets)
  served at `/` showing the backlog grouped by status and daemon status,
  auto-refreshing every 30 seconds.
- `install.sh` is now hosted on the project's own server and served from
  <https://forgeo.org/install.sh>; README and docs use it in the one-liner.

## [0.1.0] - 2026-08-03

Initial release of the scheduled, agent-driven software forgeo.

### Added

- `forgeo start` persistent daemon: every `interval_minutes` it picks the
  oldest `OPEN` task, runs it through the configured agent command, and commits
  and pushes the result directly on `main`.
- Refactoring mode: when the backlog is empty, runs the agent with the
  configured `refactor_prompt`.
- Blocker flow: an agent exiting with `blocked_exit_code` commits partial work,
  writes `BLOCKER.md`, and pauses Forgeo until the task is reopened.
- Guided first-time setup: `forgeo init` wizard.
- `forgeo once` command to run a single cycle and exit.
- `forgeo status`, `forgeo stop`, and `forgeo restart` commands.
- `--auto` flag for the agent command for unattended runs.
- Local web dashboard and HTTP API served by the daemon.
- Durable run history recorded to `runs.jsonl` and exposed through the API.
- Telegram notification when a task is marked `BLOCKED`.
- Curl-to-bash one-liner installer (`install.sh`), pipx-first.
- MkDocs documentation website, published at <https://forgeo.org/>.
- GitHub Actions CI running `pytest`, `ruff`, and `mypy` on Python 3.11-3.13.
- Optional Docker sandbox for agent execution.
- Per-task `agent_command` override for cheap/expensive model routing.
- Project renamed to Forgeo, with MIT `LICENSE` and `CONTRIBUTING.md`.

### Changed

- Project slimmed down to a single-purpose scheduled worker; the interactive
  backlog generator utility was removed.
- Duplicated commit/blocker handling unified between task and refactor runs.
- Agent stdout/stderr streamed into run logs instead of buffered.
- Corrupt backlog files preserved instead of silently discarded.
- Git command timeout made configurable; agent timeout made optional with
  overlapping-run skipping.
- Dogfooding docs removed; local configs kept out of the repository.

[Unreleased]: https://github.com/lucaGazzola/forgeo/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/lucaGazzola/forgeo/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/lucaGazzola/forgeo/compare/v0.7.3...v0.8.0
[0.7.3]: https://github.com/lucaGazzola/forgeo/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/lucaGazzola/forgeo/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/lucaGazzola/forgeo/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/lucaGazzola/forgeo/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/lucaGazzola/forgeo/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/lucaGazzola/forgeo/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/lucaGazzola/forgeo/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/lucaGazzola/forgeo/compare/v0.2.0...v0.3.0
[0.2.1]: https://github.com/lucaGazzola/forgeo/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/lucaGazzola/forgeo/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/lucaGazzola/forgeo/releases/tag/v0.1.0
