"""Where Forgeo's runtime files land, for a backlog file and a backlog URL.

The file cases are regression tests: those locations predate ``paths.py`` and
existing installations depend on them staying exactly where they are.
"""

from __future__ import annotations

from pathlib import Path

from forgeo.config import load_config, save_config
from forgeo.models import ForgeoConfig
from forgeo.paths import (
    daemon_state_path,
    lock_path,
    run_lock_path,
    runs_path,
    state_dir_for,
    update_state_path,
)

URL = "https://api.example.com/api/forgeo/backlog"


def file_config(backlog: Path, **overrides) -> ForgeoConfig:
    return ForgeoConfig(agent_command="true", backlog=backlog, **overrides)


def url_config(**overrides) -> ForgeoConfig:
    return ForgeoConfig(agent_command="true", backlog=URL, **overrides)


# --------------------------------------------------------------------------- #
# Backlog file: unchanged from before paths.py existed                         #
# --------------------------------------------------------------------------- #


def test_runtime_files_sit_next_to_the_backlog_file(tmp_path: Path) -> None:
    config = file_config(tmp_path / "backlog.json")
    assert lock_path(config) == tmp_path / "backlog.lock"
    assert run_lock_path(config) == tmp_path / "backlog.run"
    assert daemon_state_path(config) == tmp_path / "backlog.state.json"
    assert update_state_path(config) == tmp_path / "backlog.update.json"
    assert runs_path(config) == tmp_path / "runs.jsonl"


def test_runtime_files_follow_a_non_default_backlog_name(tmp_path: Path) -> None:
    """A backlog named ``tasks.json`` keeps yielding ``tasks.lock``."""
    config = file_config(tmp_path / "tasks.json")
    assert lock_path(config) == tmp_path / "tasks.lock"
    assert run_lock_path(config) == tmp_path / "tasks.run"
    assert daemon_state_path(config) == tmp_path / "tasks.state.json"
    assert update_state_path(config) == tmp_path / "tasks.update.json"
    # The run history is shared per directory, so it does not follow the name.
    assert runs_path(config) == tmp_path / "runs.jsonl"


def test_state_dir_defaults_to_the_backlog_directory(tmp_path: Path) -> None:
    config = file_config(tmp_path / "nested" / "backlog.json")
    assert state_dir_for(config) == tmp_path / "nested"


def test_explicit_state_dir_moves_the_runtime_files(tmp_path: Path) -> None:
    config = file_config(tmp_path / "backlog.json", state_dir=tmp_path / "state")
    assert lock_path(config) == tmp_path / "state" / "backlog.lock"
    assert runs_path(config) == tmp_path / "state" / "runs.jsonl"


# --------------------------------------------------------------------------- #
# Backlog URL                                                                  #
# --------------------------------------------------------------------------- #


def test_url_backlog_uses_fixed_names_in_the_state_dir(tmp_path: Path) -> None:
    config = url_config(state_dir=tmp_path)
    assert lock_path(config) == tmp_path / "backlog.lock"
    assert run_lock_path(config) == tmp_path / "backlog.run"
    assert daemon_state_path(config) == tmp_path / "backlog.state.json"
    assert update_state_path(config) == tmp_path / "backlog.update.json"
    assert runs_path(config) == tmp_path / "runs.jsonl"


def test_url_backlog_state_dir_defaults_to_the_config_directory(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "forgeo.yaml"
    save_config(config_path, url_config())
    config = load_config(config_path)
    assert config.state_dir == tmp_path
    assert lock_path(config) == tmp_path / "backlog.lock"


def test_url_backlog_survives_a_config_round_trip(tmp_path: Path) -> None:
    """A URL is not a path: saving must not mangle it into ``https:/host/...``."""
    config_path = tmp_path / "forgeo.yaml"
    saved = save_config(config_path, url_config())
    assert saved.backlog == URL
    assert load_config(config_path).backlog == URL
