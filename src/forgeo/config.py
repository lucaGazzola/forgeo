"""Project configuration loading and saving: YAML <-> :class:`ForgeoConfig`.

Relative paths in the file are resolved against the file's own directory,
so a config file can live anywhere and still point at sibling directories.
:func:`save_config` writes them back relative to that same directory, so a
config round-trips without hard-coding absolute paths into the file.

A remote ``backlog`` URL is not a path and is left exactly as written. It is
also the case where Forgeo's runtime files have no backlog file to sit beside,
so loading fills in ``state_dir`` with the config file's own directory (see
:mod:`forgeo.paths`).
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from forgeo.io import atomic_write_text
from forgeo.models import ForgeoConfig


def _maybe_resolve(path: Path | None, base: Path) -> Path | None:
    if path is None or path.is_absolute():
        return None
    return base / path


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
    for field in ("repo", "blocker_file"):
        value: Path = getattr(config, field)
        if resolved := _maybe_resolve(value, base):
            updates[field] = resolved
    if not config.backlog_is_url:
        backlog_path = Path(config.backlog)
        if resolved := _maybe_resolve(backlog_path, base):
            updates["backlog"] = resolved
    if config.state_dir is None:
        if config.backlog_is_remote:
            # A remote backlog has no file for the locks and the run history
            # to sit beside, so they go next to the config that describes it.
            updates["state_dir"] = base
    elif resolved := _maybe_resolve(config.state_dir, base):
        updates["state_dir"] = resolved
    if resolved := _maybe_resolve(config.task_context, base):
        updates["task_context"] = resolved
    log_path = Path(config.log_file)
    if resolved := _maybe_resolve(log_path, base):
        updates["log_file"] = str(resolved)
    return config if not updates else config.model_copy(update=updates)


_PATH_FIELDS = ("repo", "backlog", "blocker_file", "log_file", "state_dir", "task_context")


def save_config(path: str | Path, config: ForgeoConfig) -> ForgeoConfig:
    """Persist ``config`` to a Forgeo YAML file with an atomic write.

    Path fields are stored relative to the file's own directory when the value
    is absolute, so the file stays portable and ``load_config`` resolves them
    back to the same absolute paths on the daemon's next load. An absolute path
    that cannot be expressed relative to the file's directory (a different
    drive on Windows) is kept absolute. A URL backlog is not a path and is
    written back untouched.

    Returns the config as freshly loaded from the file (paths resolved), so
    callers get the exact state a subsequent ``load_config`` produces.
    """
    config_path = Path(path)
    base = config_path.parent.resolve()
    payload = config.model_dump(mode="json")
    for field in _PATH_FIELDS:
        if payload[field] is None or (field == "backlog" and config.backlog_is_url):
            continue
        value = Path(payload[field])
        if value.is_absolute():
            try:
                payload[field] = os.path.relpath(value, base)
            except ValueError:
                pass  # different drive (Windows): keep the absolute path
    atomic_write_text(config_path, yaml.safe_dump(payload, sort_keys=False))
    return load_config(config_path)
