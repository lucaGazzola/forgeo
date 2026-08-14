"""Instance registry: a stable name for every configured forgeo.

Each forgeo is configured by its own ``forgeo.yaml`` and runs as its own
daemon process, but nothing on the host knows how many forgeos exist or
how to find their configs. The registry gives every forgeo a unique name
mapped to the absolute path of its ``forgeo.yaml``, so the CLI can resolve
a config by name and a single command can enumerate every forgeo.

The registry is a YAML file mapping instance names to config paths. It
lives at ``$FORGEO_REGISTRY`` or ``~/.config/forgeo/instances.yaml`` and is
created on the first write. Writes are atomic (temp file + rename), so a
crash mid-write never corrupts the registry.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from forgeo.config import load_config
from forgeo.daemon import is_lock_held
from forgeo.io import atomic_write_text
from forgeo.models import ForgeoConfig
from forgeo.paths import lock_path

INSTANCE_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

DEFAULT_REGISTRY = Path.home() / ".config" / "forgeo" / "instances.yaml"


def registry_path() -> Path:
    """Path of the registry file: ``$FORGEO_REGISTRY`` or the default."""
    env = os.environ.get("FORGEO_REGISTRY")
    if env:
        return Path(env).expanduser()
    return DEFAULT_REGISTRY


def load_registry() -> dict[str, str]:
    """Load the registry as ``{instance name: absolute config path}``.

    A missing or unreadable file reads as an empty registry.
    """
    path = registry_path()
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(name): str(config)
        for name, config in payload.items()
        if isinstance(name, str) and isinstance(config, str)
    }


def save_registry(registry: dict[str, str]) -> None:
    """Persist ``registry`` atomically (temp file + rename)."""
    atomic_write_text(
        registry_path(),
        yaml.safe_dump(dict(sorted(registry.items())), sort_keys=False),
    )


def resolve_instance(name: str) -> Path | None:
    """Absolute config path for ``name``, or ``None`` when it is not registered."""
    config_path = load_registry().get(name)
    return Path(config_path) if config_path else None


def _validate_name(name: str) -> None:
    """Raise ``ValueError`` unless ``name`` matches the allowed pattern."""
    if not INSTANCE_NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid instance name {name!r}: must match ^[a-zA-Z0-9._-]+$"
        )


def add_instance(name: str, config_path: str | Path) -> str:
    """Register ``name`` -> the absolute path of ``config_path``.

    Validates that the name is well-formed, that it is not already
    registered, and that the config file loads. Returns ``name``.

    Raises:
        ValueError: Invalid or duplicate name, or a config that fails to
            load (a bad payload raises pydantic's ``ValidationError``).
        FileNotFoundError: The config file does not exist.
    """
    _validate_name(name)
    registry = load_registry()
    if name in registry:
        raise ValueError(f"instance {name!r} is already registered")
    absolute = Path(config_path).expanduser().resolve()
    load_config(absolute)
    registry[name] = str(absolute)
    save_registry(registry)
    return name


def ensure_registered(name: str, config_path: str | Path) -> bool:
    """Register ``name`` -> the absolute path of ``config_path`` when missing.

    Unlike :func:`add_instance` this never raises: a name that is already
    registered (whatever it points at), fails the instance-name pattern, or
    maps to a config that cannot be loaded is left untouched. Returns
    ``True`` only when the instance was newly registered.
    """
    if name in load_registry():
        return False
    if not INSTANCE_NAME_RE.fullmatch(name):
        return False
    try:
        add_instance(name, config_path)
    except (ValueError, FileNotFoundError, yaml.YAMLError):
        return False
    return True


def remove_instance(name: str) -> bool:
    """Unregister ``name``; never touches its config file or repository.

    Returns ``True`` when the instance was registered and removed.
    """
    registry = load_registry()
    if name not in registry:
        return False
    del registry[name]
    save_registry(registry)
    return True


@dataclass(frozen=True)
class InstanceInfo:
    """One registered instance plus its live state."""

    name: str
    config_path: Path
    repo: Path | None
    daemon_running: bool
    config: ForgeoConfig | None = None


def _load_info(name: str, config_path: Path) -> InstanceInfo:
    """Build the live state for one registered instance."""
    try:
        config = load_config(config_path)
    except (ValueError, OSError, yaml.YAMLError):
        return InstanceInfo(name, config_path, repo=None, daemon_running=False)
    return InstanceInfo(
        name=name,
        config_path=config_path,
        repo=config.repo,
        daemon_running=is_lock_held(lock_path(config)),
        config=config,
    )


def get_instance(name: str) -> InstanceInfo | None:
    """Build the live state for one registered instance, or ``None``.

    Equivalent to looking up a single entry of :func:`list_instances`; an
    instance whose config can no longer be loaded is still returned, with
    ``repo=None``, ``daemon_running=False`` and ``config=None``.
    """
    config_path = resolve_instance(name)
    if config_path is None:
        return None
    return _load_info(name, config_path)


def list_instances() -> list[InstanceInfo]:
    """Return every registered instance, sorted by name.

    Each entry carries the config path, the configured repository, and
    whether that instance's daemon currently holds its backlog lock. An
    instance whose config can no longer be loaded is still listed, with
    ``repo=None`` and ``daemon_running=False``.
    """
    registry = load_registry()
    return [
        _load_info(name, Path(config_path))
        for name, config_path in sorted(registry.items())
    ]
