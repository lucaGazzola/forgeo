"""Best-effort, never-fatal update notification.

When ``forgeo start`` or ``forgeo once`` begins a cycle, Forgeo asks PyPI
for the latest ``forgeo-cli`` release and, if it is newer than the
installed version, prints/logs a short notice naming the upgrade command.

The check is deliberately harmless:

* it only notifies — it never auto-updates or modifies the install;
* it runs at most once per day (remembered in a tiny JSON state file next
  to the backlog) so a long-running daemon never phones home every cycle;
* it uses a short timeout and any network or parse error is logged and
  swallowed, so a cycle always proceeds unchanged.

Set ``FORGEO_UPDATE_CHECK=0`` to disable it entirely.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from forgeo import __version__
from forgeo.io import atomic_write_text

logger = logging.getLogger(__name__)

#: The PyPI JSON API for the ``forgeo-cli`` package.
PYPI_JSON_URL = "https://pypi.org/pypi/forgeo-cli/json"

#: Minimum time between two network checks, so every start does not phone home.
CHECK_INTERVAL = timedelta(days=1)

#: Short timeout so a slow or unreachable PyPI never stalls a cycle for long.
TIMEOUT_SECONDS = 3.0

#: Set to ``0`` to turn the (already harmless) check off, e.g. in tests or
#: on fully offline boxes.
DISABLE_ENV = "FORGEO_UPDATE_CHECK"


def update_state_path(backlog: str | Path) -> Path:
    """The state file remembering the last update check, next to the backlog."""
    return Path(backlog).with_suffix(".update.json")


def update_check_enabled() -> bool:
    """True unless the check was disabled via ``FORGEO_UPDATE_CHECK``."""
    value = os.environ.get(DISABLE_ENV, "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def installed_version() -> str:
    """The installed version: package metadata when available, else the
    fallback constant bundled into the standalone binary."""
    return __version__


def fetch_latest_version(url: str = PYPI_JSON_URL) -> str | None:
    """Return the latest ``forgeo-cli`` version from PyPI, or ``None``.

    Best-effort: any network or parse error is logged and swallowed, never
    raised.
    """
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload["info"]["version"])
    except Exception as exc:  # noqa: BLE001 - best-effort, must never fail
        logger.debug("Update check failed (%s: %s); skipping.", type(exc).__name__, exc)
        return None


def _parse_version(value: str) -> tuple[int, ...]:
    """Split a dotted version into comparable numeric parts.

    Non-digit suffixes (e.g. ``1.2.0rc1``) are dropped; good enough to tell
    "a newer release exists".
    """
    parts: list[int] = []
    for chunk in value.split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def version_is_newer(latest: str, installed: str) -> bool:
    """True when ``latest`` is a strictly newer release than ``installed``."""
    return _parse_version(latest) > _parse_version(installed)


def upgrade_notice(latest: str, installed: str) -> str:
    """The notice naming the newer version and how to upgrade."""
    return (
        f"A newer forgeo-cli version is available: {installed} -> {latest}. "
        "Upgrade by re-running the install.sh one-liner, or with "
        "`pipx upgrade forgeo-cli` / `pip install --user --upgrade forgeo-cli`."
    )


class UpdateState:
    """A tiny JSON state file recording when the last check happened."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def due(self, now: datetime | None = None) -> bool:
        """True when no check happened within the last interval."""
        last = self._last_checked()
        if last is None:
            return True
        return (now or datetime.now(UTC)) - last >= CHECK_INTERVAL

    def mark(self, now: datetime | None = None) -> None:
        """Record that a check happened now (never raises)."""
        payload = {"last_checked": (now or datetime.now(UTC)).isoformat()}
        try:
            atomic_write_text(self.path, json.dumps(payload, indent=2) + "\n")
        except OSError as exc:
            logger.debug("Could not record the update check: %s", exc)

    def _last_checked(self) -> datetime | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return datetime.fromisoformat(payload["last_checked"])
        except (OSError, ValueError, KeyError, TypeError):
            return None


def check_for_update(
    state_path: str | Path,
    *,
    print_fn: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> str | None:
    """Check PyPI at most once a day and notify when the install is outdated.

    Best-effort and never fatal: disabled via ``FORGEO_UPDATE_CHECK=0``,
    skipped when a check already ran today, and any failure is logged and
    swallowed. The install is never modified. The notice (when the installed
    version is outdated) is logged and, if ``print_fn`` is given, also
    printed; returns the notice text or ``None``.
    """
    if not update_check_enabled():
        return None
    state = UpdateState(state_path)
    if not state.due(now):
        return None
    state.mark(now)
    latest = fetch_latest_version()
    if latest is None:
        return None
    installed = installed_version()
    if not version_is_newer(latest, installed):
        logger.debug("forgeo-cli is up to date (installed %s).", installed)
        return None
    message = upgrade_notice(latest, installed)
    logger.warning("%s", message)
    if print_fn is not None:
        print_fn(message)
    return message
