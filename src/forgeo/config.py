"""Project configuration loading and saving: YAML <-> :class:`ForgeoConfig`.

Relative paths in the file are resolved against the file's own directory,
so a config file can live anywhere and still point at sibling directories.
:func:`save_config` writes them back relative to that same directory, so a
config round-trips without hard-coding absolute paths into the file.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from forgeo.io import atomic_write_text
from forgeo.models import ForgeoConfig


def load_config(path: str | Path) -> ForgeoConfig:
    """Load and validate a Forgeo YAML file.

    Raises:
        FileNotFoundError: If the file does not exist.
        pydantic.ValidationError: If the payload does not match the schema.
    """
    config_path = Path(path)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config = ForgeoConfig.model_validate(payload)
    base = config_path.parent.resolve()
    updates: dict[str, Path | str] = {}
    if not config.repo.is_absolute():
        updates["repo"] = base / config.repo
    if not config.backlog.is_absolute():
        updates["backlog"] = base / config.backlog
    if not config.blocker_file.is_absolute():
        updates["blocker_file"] = base / config.blocker_file
    if not Path(config.log_file).is_absolute():
        updates["log_file"] = str(base / config.log_file)
    return config if not updates else config.model_copy(update=updates)


_PATH_FIELDS = ("repo", "backlog", "blocker_file", "log_file")


def save_config(path: str | Path, config: ForgeoConfig) -> ForgeoConfig:
    """Persist ``config`` to a Forgeo YAML file with an atomic write.

    Path fields are stored relative to the file's own directory when the value
    is absolute, so the file stays portable and ``load_config`` resolves them
    back to the same absolute paths on the daemon's next load. An absolute path
    that cannot be expressed relative to the file's directory (a different
    drive on Windows) is kept absolute.

    Returns the config as freshly loaded from the file (paths resolved), so
    callers get the exact state a subsequent ``load_config`` produces.
    """
    config_path = Path(path)
    base = config_path.parent.resolve()
    payload = config.model_dump(mode="json")
    for field in _PATH_FIELDS:
        value = Path(payload[field])
        if value.is_absolute():
            try:
                payload[field] = os.path.relpath(value, base)
            except ValueError:
                pass  # different drive (Windows): keep the absolute path
    atomic_write_text(config_path, yaml.safe_dump(payload, sort_keys=False))
    return load_config(config_path)
