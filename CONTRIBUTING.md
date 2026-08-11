# Contributing to Forgeo

Thanks for contributing! This project is an agent-driven software forgeo.

## Development setup

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

`.[dev]` installs the package plus the toolchain: `pytest`, `ruff`, and
`mypy`.

## Quality gates

Run all three before opening a PR. CI enforces the same gates.

```bash
pytest           # the test suite (tests/)
ruff check       # linting (src, tests)
mypy src/forgeo # type checking
```

The full suite should pass and `ruff check` and `mypy src/forgeo` should be
clean.

## Writing a backlog task

Backlog tasks are how Forgeo receives work. Tasks are JSON objects
in the backlog file (see [docs/backlog.md](docs/backlog.md) for the full
schema). A good task has three things:

- **`id`** — a unique identifier, e.g. `TASK-001`. Duplicate ids are rejected.
- **`description`** — a self-contained specification handed to the agent. State
  the current behavior, the desired behavior, and where the change lives. The
  agent does not have your mental context, so spell it out.
- **`acceptance_criteria`** — a list of concrete, verifiable outcomes. The
  forgeo renders these into the agent's `FORGEO_TASK` instruction, so write
  them as checks the agent can confirm itself (e.g. "`pytest` passes", "the
  `--help` output lists `once`").

Example:

```json
{
  "id": "TASK-002",
  "title": "Add `forgeo once` command to run a single cycle",
  "description": "The CLI only offers `forgeo start` (the persistent daemon). Add a `once` subcommand that runs exactly one cycle and exits.",
  "acceptance_criteria": [
    "`forgeo once --config forgeo.yaml` runs one cycle and exits 0",
    "`forgeo --help` lists `once`",
    "Tests cover the new command"
  ],
  "status": "OPEN",
  "created_at": "2026-07-31T20:01:00Z"
}
```

Keep the description scoped to one task, keep acceptance criteria minimal and
testable, and never commit the backlog file — it is gitignored.

## Pull-request process

Human contributions use the normal GitHub flow:

1. Create a feature branch off `main`: `git checkout -b feat/my-change`.
2. Make your change and commit it with a concise, descriptive message.
3. Run the [quality gates](#quality-gates) and fix any failures.
4. Push the branch and open a pull request against `main`.
5. CI runs `pytest`, `ruff check`, and `mypy src/forgeo` on the PR — it must
   be green.
6. Address review feedback; keep the branch rebased on `main` if it drifts.
7. Once approved and green, merge. Follow-up work is welcome as a new PR or as
   a backlog task for Forgeo.

## Releasing

Releases are cut from `main` and published as GitHub Releases. Tagging the
repo triggers CI, which builds the wheel, sdist, **and prebuilt standalone
binaries** and attaches them to the release; `install.sh` downloads the
matching prebuilt binary from the release (`pipx`/`pip` fallback only when no
binary matches the platform), and the `publish-homebrew` job re-renders and
pushes the formula of the `lucaGazzola/homebrew-forgeo` tap.

> Patch and minor releases **must** include the built binaries, otherwise the
> `install.sh` binary path (the default, no-Python install) breaks. The CI
> `build-binaries` job builds them automatically on any `v*` tag, but make
> sure the release actually carries them — the `forgeo-<os>-<arch>` assets
> listed below are what the installer downloads.
>
> The `publish-homebrew` job needs the `HOMEBREW_TAP_TOKEN` repository secret
> (a PAT with write access to `lucaGazzola/homebrew-forgeo`); a release cut
> without it fails that job and leaves the tap outdated until re-run.

1. Confirm the [quality gates](#quality-gates) are green on `main`.
2. Bump the version in `pyproject.toml` (`version = "x.y.z"`) and in
   `src/forgeo/__init__.py`, following
   [Semantic Versioning](https://semver.org/).
3. Update the `VERSION=` at the top of `install.sh` to the new `x.y.z` so the
   installer downloads the new release's binaries.
4. Update `CHANGELOG.md`: move the entries from `## [Unreleased]` under a new
   `## [x.y.z] - <date>` section, add the compare links at the bottom, and
   leave a fresh `## [Unreleased]` heading.
5. Commit the bump and changelog update, e.g. `git commit -m "Release x.y.z"`.
6. Tag and push the tag — the `build-binaries` and `release` jobs in
   `.github/workflows/ci.yml` build the wheel, sdist, and PyInstaller binaries
   for Linux (amd64), macOS (amd64/arm64), and Windows (amd64) and attach them
   to a GitHub Release:

   ```bash
   git tag vx.y.z
   git push origin vx.y.z
   ```

7. Confirm the release and its artifacts (wheel, sdist, and the
   `forgeo-linux-amd64`, `forgeo-darwin-amd64`, `forgeo-darwin-arm64`,
   `forgeo-windows-amd64.exe` binaries) are listed under
   <https://github.com/lucaGazzola/forgeo/releases>.
8. Confirm the `publish-homebrew` job updated the tap: `brew update` and
   `brew upgrade lucaGazzola/forgeo/forgeo` should now install the new version.

## License

This project is MIT-licensed; see [LICENSE](LICENSE).
