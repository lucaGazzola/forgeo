"""The scheduled forgeo daemon.

Wakes up every ``interval_minutes``, runs one cycle of the :class:`Forgeo`,
and sleeps. A lock file prevents two daemons from running on the same
forgeo. A per-run lock prevents two agents from ever working on the same
repository at the same time: when a run is still in progress at the next
wake-up, that iteration is skipped instead of killing the running agent.
Everything else is logged to the configured log file.

Live state (pid, started at, last outcome, next run) is written to a small
``daemon.state.json`` next to the backlog after every cycle, so external
observers (the central dashboard, the CLI) can read it without the daemon
serving any port. The file is written atomically; a crash mid-write never
corrupts it, and a missing/stale file simply reads as unknown state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from forgeo.forgeo import Forgeo
from forgeo.io import atomic_write_text
from forgeo.models import ForgeoConfig

logger = logging.getLogger(__name__)


def _fcntl() -> Any | None:
    """The ``fcntl`` module, or ``None`` on platforms without it (Windows)."""
    try:
        import fcntl
    except ImportError:
        return None
    return fcntl


def _take_flock(lock_path: str | Path) -> Any | None:
    """Open the lock file and take a non-blocking exclusive flock.

    Uses ``fcntl`` flock so the lock is released automatically when the
    process exits (even on crash). Returns ``None`` when another process
    holds the lock. Falls back to no locking when ``fcntl`` is unavailable.
    The file is opened without truncation so a failed acquire keeps the
    running holder's recorded PID intact (``forgeo stop`` needs it).
    """
    lock_file = Path(lock_path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a+")
    fcntl = _fcntl()
    if fcntl is not None:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
    handle.truncate(0)
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    return handle


def acquire_run_lock(lock_path: str | Path) -> Any:
    """Take an exclusive, non-blocking lock; returns the handle or ``None``.

    The lock is released automatically when the process exits (even on
    crash). Returns ``None`` when another daemon holds the lock.
    """
    return _take_flock(lock_path)


def read_lock_pid(lock_path: str | Path) -> int | None:
    """Return the PID recorded in the lock file, or ``None`` when unknown."""
    try:
        text = Path(lock_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("pid="):
            try:
                return int(line.removeprefix("pid=").strip())
            except ValueError:
                return None
    return None


def is_lock_held(lock_path: str | Path) -> bool:
    """Return True when another process currently holds the exclusive flock.

    Does not create the lock file when it is missing. A leftover file with
    no live holder counts as not held.
    """
    lock_file = Path(lock_path)
    if not lock_file.exists():
        return False
    fcntl = _fcntl()
    if fcntl is None:
        return False
    try:
        handle = lock_file.open("r")
    except OSError:
        return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        handle.close()


class RunLock:
    """Per-iteration lock: one agent run at a time per forgeo.

    Held for the duration of one cycle so that a run still in progress (an
    overlong agent, an orphaned process after a daemon restart) makes the
    next iteration skip instead of starting a second agent on the same
    repository.
    """

    def __init__(self, lock_path: str | Path) -> None:
        self.lock_path = Path(lock_path)

    @contextmanager
    def held(self) -> Iterator[bool]:
        """Acquire for the duration of the block; yields True when acquired."""
        handle = _take_flock(self.lock_path)
        try:
            yield handle is not None
        finally:
            if handle is not None:
                handle.close()


class ForgeoDaemon:
    """Runs :class:`Forgeo` cycles on a fixed schedule until stopped."""

    def __init__(self, config: ForgeoConfig, forgeo: Forgeo) -> None:
        self.config = config
        self.forgeo = forgeo
        self.interval_seconds: float = config.interval_minutes * 60.0
        self.run_lock = RunLock(config.backlog.with_suffix(".run"))
        self._stop_event = asyncio.Event()
        self.pid: int = os.getpid()
        self.started_at: datetime = datetime.now(UTC)
        self.last_outcome: str | None = None
        self.next_run_at: datetime | None = None

    @property
    def state_file(self) -> Path:
        """The ``daemon.state.json`` path, next to the backlog's lock files."""
        return self.config.backlog.with_suffix(".state.json")

    def stop(self) -> None:
        """Request a graceful shutdown after the current cycle."""
        self._stop_event.set()

    def write_state(self) -> None:
        """Atomically persist the daemon's live state for external readers.

        A missing or stale file is fine: readers treat it as unknown state.
        """
        payload = {
            "pid": self.pid,
            "started_at": self.started_at.isoformat(),
            "last_outcome": self.last_outcome,
            "next_run_at": (
                self.next_run_at.isoformat() if self.next_run_at is not None else None
            ),
        }
        path = self.state_file
        atomic_write_text(path, json.dumps(payload, indent=2) + "\n")

    async def run_forever(self) -> None:
        """Wake up on the schedule interval until ``stop()`` is called."""
        logger.info(
            "Forgeo %r started (repo=%s, interval=%s min, branch=%s).",
            self.config.name,
            self.config.repo,
            self.config.interval_minutes,
            self.config.branch,
        )
        self.write_state()
        while not self._stop_event.is_set():
            try:
                with self.run_lock.held() as acquired:
                    if not acquired:
                        logger.info("Previous run still in progress; skipping this iteration.")
                        outcome = "skipped"
                    else:
                        outcome = await self.forgeo.run_cycle()
                self.last_outcome = outcome
                logger.info("Run finished: %s", outcome)
            except Exception:
                self.last_outcome = "error"
                logger.exception("Run crashed; continuing on the next interval.")
            self.next_run_at = datetime.now(UTC) + timedelta(seconds=self.interval_seconds)
            self.write_state()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass
        self.write_state()
        logger.info("Forgeo stopped.")
