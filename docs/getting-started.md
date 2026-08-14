# Getting Started

This guide installs the `forgeo` CLI, initializes a config, and runs your
first backlog task.

## 1. Install

Install the `forgeo` CLI with Homebrew on macOS or Linux
(**no Python required**):

```bash
brew install lucaGazzola/forgeo/forgeo
```

On newer Homebrew versions (4.6+) a third-party tap is untrusted by default:
if `brew` refuses to load the formula, run `brew trust
lucaGazzola/forgeo` first and install again.

Or from the public GitHub remote with the one-liner (**no Python required**):

```bash
curl -fsSL https://forgeo.org/install.sh | bash
```

Or with Python 3.11+ via pip:

```bash
pipx install forgeo-cli
```

The Homebrew formula and the one-liner both download a prebuilt standalone
binary from the matching GitHub Release; the one-liner covers Linux, macOS,
and Windows, while Homebrew installs the macOS (arm64/Intel) and Linux (Intel)
binaries. All three installers:

- never need root;
- upgrade an existing install by re-running them (`brew upgrade
  lucaGazzola/forgeo/forgeo`, re-running the one-liner, or
  `pipx upgrade forgeo-cli` / `pip install --user --upgrade forgeo-cli`).

The one-liner additionally falls back to `pipx` and then `pip install --user`
when no prebuilt binary matches the platform and a Python 3.11+ is available,
and warns you when the install location is not on your `PATH`.

You do not need to watch for releases: when `forgeo start` or `forgeo once`
begins a cycle, Forgeo checks PyPI at most once a day and prints a short
notice (also logged) naming the newer version and the upgrade command when
one is available. The check never auto-updates or modifies the install; set
`FORGEO_UPDATE_CHECK=0` to disable it.

## 2. Initialize

Run the guided wizard from your project root:

```bash
forgeo init
```

The wizard asks for three things:

1. **Forgeo folder** — where the backlog, `BLOCKER.md` and the log live
   (default `.forgeo`). It is gitignored by default.
2. **Coding agent command** — the bare command that launches your coding
   agent (default `opencode run --auto`). Forgeo appends the standard task
   prompt (which ends in `$FORGEO_TASK`) automatically, so you never type
   it. Enter a command that already references `$FORGEO_TASK` and it is
   kept verbatim.
3. **Refactor prompt** — the instruction used when the backlog is empty; the
   default is offered, or you can paste a custom one.

`forgeo init` writes `forgeo.yaml`, creates Forgeo folder, and appends
`<folder>/` to `.gitignore` (unless you opt out).

```bash
forgeo init --force    # overwrite an existing forgeo.yaml
```

## 3. Create your first backlog

The backlog is a plain JSON file (see [Backlog format](backlog.md)). Create
the file configured as `backlog:` in your `forgeo.yaml` — by default
`.forgeo/backlog.json`. Once Forgeo is running you can also add tasks
from the [web console](web-console-api.md) — no file editing needed. (If your
tasks already live in another application, `backlog:` also accepts an
[HTTP endpoint](backlog.md#a-backlog-over-http) instead of a file.)

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

!!! tip

    Hand this spec to your favorite LLM to generate the initial backlog for
    the application you want to build.

## 4. Start Forgeo

```bash
forgeo start
```

`forgeo start` launches the daemon **detached in the background** and exits.
The daemon wakes up every `interval_minutes` and runs one cycle. Stop it from
anywhere with `forgeo stop`; `forgeo status` shows whether it is running. To
run the daemon in the foreground instead (interruptible with Ctrl-C), use
`forgeo start -f`. It binds no ports itself; open the dashboard (which
shows every registered instance) with `forgeo web` — see
[Web console & HTTP API](web-console-api.md):

![Forgeo web console](img/console.png)

## 5. Verify

```bash
forgeo status
```

shows the config, backlog counts, the next runnable `OPEN` task (one whose
dependencies are all `COMPLETED`), whether the daemon is running, and the last
run outcome. To run exactly one cycle without leaving a daemon up:

```bash
forgeo once
```

Before starting for the first time you can run a read-only dry run that
validates the config, repository, branch/remote, backlog, agent command and
lock state without invoking the agent or writing anything:

```bash
forgeo validate
```

## 6. Multiple repositories / instances

Forgeo runs one config per repository — nothing stops you from running
several factories on several repositories at the same time. Each config gets
its own backlog, logs, locks and `runs.jsonl`, and each daemon is a separate
process, so instances are fully independent. The **instance registry** gives
every forgeo a stable name so you can enumerate them and manage them from
anywhere.

```bash
# 1. Initialize a config per repository (run the wizard in each project root)
forgeo init

# 2. Start a daemon per instance (background; each config is registered
#    automatically under its `name` on first start — or pre-register with
#    `instance add`)
forgeo start --config /path/to/site-a/forgeo.yaml
forgeo start --config /path/to/site-b/forgeo.yaml

# 3. List every registered instance (also: `forgeo list`)
forgeo instance list

# 4. From anywhere, target an instance by name
forgeo start --name site-a
forgeo stop --name site-a

# 5. Open the central dashboard: one page for every registered instance
forgeo web           # default http://0.0.0.0:8790 (foreground)
forgeo web -d        # ...or keep it running in the background
forgeo web stop      # stop the background dashboard
```

`--name` works on `start`, `once`, `status`, `stop` and `restart` and is
mutually exclusive with `--config`; an unknown name prints a clear error.
`start` and `stop` with `--config` register Forgeo automatically under
its config's `name` when it is not in the registry yet, so the registry stays
in sync without manual `forgeo instance add` steps. See
[Configuration](configuration.md) for the registry file, and
[CLI reference](cli-reference.md) for the commands.

## Next steps

- [Configuration reference](configuration.md) — every `forgeo.yaml` key.
- [Backlog format](backlog.md) — task schema and statuses.
- [Agent contract](agent-contract.md) — how the agent is invoked.
- [CLI reference](cli-reference.md) — all commands.
