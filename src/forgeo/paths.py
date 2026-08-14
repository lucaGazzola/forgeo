"""Where Forgeo's own runtime files live.

Forgeo keeps five files of its own next to the work it coordinates: the
daemon lock, the per-iteration run lock, the daemon's live state, the run
history, and the update-check stamp. They are not the backlog — they are
local bookkeeping — but historically each one was derived from the backlog
file's path, which stops working the moment the backlog is an HTTP endpoint.

This module is the single place that decides those locations:

* **backlog file** — unchanged from the day one behavior: every runtime file
  is a sibling of the backlog, named after it (``tasks.json`` yields
  ``tasks.lock``, ``tasks.run``, ...), with the run history as the shared
  ``runs.jsonl``.
* **backlog URL** — there is no file to sit beside, so they go in
  ``state_dir`` (defaulting to the directory of ``forgeo.yaml``, filled in by
  :func:`forgeo.config.load_config`) under fixed ``backlog.*`` names.

Keeping the rule here also means callers never spell a suffix themselves,
which is what let three different spellings of the same lock path drift
apart across the CLI, the registry and the web console.
"""

from __future__ import annotations

from pathlib import Path

from forgeo.models import ForgeoConfig

#: The run history is shared per directory, not named after the backlog.
RUNS_FILENAME = "runs.jsonl"

#: Stem used for the runtime files of a URL backlog, which has no filename.
_URL_BACKLOG_STEM = "backlog"


def state_dir_for(config: ForgeoConfig) -> Path:
    """The directory holding Forgeo's runtime files for ``config``.

    An explicit ``state_dir`` always wins; otherwise a backlog file keeps
    them beside itself. A URL backlog always has one, because ``load_config``
    resolves ``state_dir`` to the config file's own directory when it is left
    unset.
    """
    if config.state_dir is not None:
        return Path(config.state_dir)
    return Path(config.backlog).parent


def _runtime_file(config: ForgeoConfig, suffix: str) -> Path:
    """A runtime file for ``config``, named after the backlog when it is one."""
    directory = state_dir_for(config)
    if config.backlog_is_url:
        return directory / f"{_URL_BACKLOG_STEM}{suffix}"
    return directory / Path(config.backlog).with_suffix(suffix).name


def lock_path(config: ForgeoConfig) -> Path:
    """The daemon lock; holding it is what "the daemon is running" means."""
    return _runtime_file(config, ".lock")


def run_lock_path(config: ForgeoConfig) -> Path:
    """The per-iteration lock: one agent run at a time per forgeo."""
    return _runtime_file(config, ".run")


def daemon_state_path(config: ForgeoConfig) -> Path:
    """The daemon's live state, re-written after every cycle."""
    return _runtime_file(config, ".state.json")


def update_state_path(config: ForgeoConfig) -> Path:
    """The stamp recording when the last PyPI update check happened."""
    return _runtime_file(config, ".update.json")


def runs_path(config: ForgeoConfig) -> Path:
    """The durable run history, one JSON record per finished cycle."""
    return state_dir_for(config) / RUNS_FILENAME
