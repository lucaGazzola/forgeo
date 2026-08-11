<div align="center">
  <img src="docs/img/logo.png" alt="Forgeo logo" width="128">
</div>

<div align="center">
  <img src="docs/img/title.svg" alt="Forgeo" width="128">
</div>


[![CI](https://github.com/lucaGazzola/forgeo/actions/workflows/ci.yml/badge.svg)](https://github.com/lucaGazzola/forgeo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Forgeo is a software factory for your coding-agent.**
You're already working with an AI coding agent, prompting it task by task
or giving it a goal. Forgeo organizes your work in a structured way with
a backlog, and it decides what to work on next, runs your
agent on it, and commits the result. Progress, pending decisions, and history
are tracked in plain files you can inspect at any time, plus a web dashboard.
Forgeo only interrupts you when a decision is genuinely yours to
make, everything else happens autonomously.

All you need is basic comfort with a terminal, a git repository, and any coding
agent CLI.

## Quickstart

The full walkthrough is in [Getting started](docs/getting-started.md).

```bash
# 1. Install (any one of these)

# Homebrew (macOS / Linux)
brew install lucaGazzola/forgeo/forgeo

# or: the one-liner (prebuilt binary, no Python required)
curl -fsSL https://forgeo.org/install.sh | bash

# or: pipx
pipx install forgeo-cli

# 2. Create your Forgeo (guided wizard, run from your project root)
forgeo init

# 3. Start Forgeo
forgeo start   # run forever: every interval_minutes, implement the oldest OPEN task
```

`forgeo init` writes `forgeo.yaml` and a `.forgeo/` folder for the backlog
and logs. Fill the backlog with plain JSON tasks (see
[Backlog format](docs/backlog.md)) or add them from the web console while
it runs, Forgeo does the rest. Open the dashboard with `forgeo web` (default <http://0.0.0.0:8790>), or keep it always-on with `forgeo web -d` (stop it with `forgeo web stop`, check it with `forgeo web status`):

![Forgeo web console](docs/img/console.png)

One-off commands: `forgeo once` (single cycle), `forgeo status` (summary),
`forgeo stop`, `forgeo restart`, every command is in the
[CLI reference](docs/cli-reference.md).

You can run several factories at once, one per repository, each config is
fully independent (own backlog, logs, locks). Register each `forgeo.yaml`
in the instance registry with `forgeo instance add NAME --config PATH`,
manage any of them by name with `forgeo start/status/stop --name NAME`,
list them all with `forgeo list`, and get one aggregate overview with the
central dashboard, `forgeo web`.

## Documentation

| Topic | Where |
| --- | --- |
| Install, init, first cycle | [Getting started](docs/getting-started.md) |
| Every `forgeo.yaml` key | [Configuration](docs/configuration.md) |
| Task schema and statuses | [Backlog format](docs/backlog.md) |
| How the agent is invoked (env, exit codes, timeouts) | [Agent contract](docs/agent-contract.md) |
| All CLI commands | [CLI reference](docs/cli-reference.md) |
| Web dashboard & HTTP API | [Web console & HTTP API](docs/web-console-api.md) |

Everything is stored in plain files: the backlog, `forgeo.log`, and
`BLOCKER.md` whenever a decision is pending.

## Develop

```bash
pip install -e ".[dev]"
pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, quality
gates (`pytest`, `ruff check`, `mypy src/forgeo`), and the pull-request
process.

## License

MIT — see [LICENSE](LICENSE).
