"""The scheduled forgeo daemon.

Wakes up every ``interval_minutes``, runs one cycle of the :class:`Forgeo`,
and sleeps. A lock file prevents two daemons from running on the same
forgeo. A per-run lock prevents two agents from ever working on the same
repository at the same time: when a run is still in progress at the next
wake-up, that iteration is skipped instead of killing the running agent.
Everything else is logged to the configured log file.

One-shot task schedules shorten the sleep: after a cycle the daemon reads the
backlog for the earliest ``run_at`` among runnable ``OPEN`` tasks and wakes at
(or just after) that moment instead of waiting out the full interval, so a
"run this after deploy" task fires promptly instead of waiting for the next
scheduled pick.

Live state (pid, started at, last outcome, next run) is written to a small
``daemon.state.json`` next to the backlog after every cycle, so external
observers (the central dashboard, the CLI) can read it without the daemon
serving any port. The file is written atomically; a crash mid-write never
corrupts it, and a missing/stale file simply reads as unknown state.

The daemon watches its ``forgeo.yaml`` and reloads it on the next cycle
boundary when the file changes (or a ``SIGHUP`` arrives): a changed config is
revalidated, logged, and used from the next cycle; an invalid change is
logged and the last valid config stays in use. Path-bearing fields (repo,
backlog, blocker_file, log_file) are pinned to the daemon's current values
when they change, because relocating them mid-flight would detach the
daemon's lock files from its config and let a second daemon start on the new
paths — those changes need a restart and are only logged.
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

import yaml
from pydantic import ValidationError

from forgeo.backlog import next_due_run_at
from forgeo.config import load_config
from forgeo.forgeo import Forgeo
from forgeo.io import atomic_write_text
from forgeo.models import ForgeoConfig
from forgeo.paths import daemon_state_path, run_lock_path

logger = logging.getLogger(__name__)

#: Config fields whose relocation would detach the daemon's lock/state files
#: from the config they guard. These are pinned to the running daemon's values
#: on a reload; changing them needs a ``forgeo restart``.
_RELOAD_PATH_FIELDS = ("repo", "backlog", "blocker_file", "log_file")

#: The shortest sleep the daemon allows after a cycle. A ``run_at``-driven
#: wake target is clamped to this floor so a task that stays due — because the
#: run lock is held, the tree is dirty, or the forgeo is paused on a blocker —
#: cannot hot-loop the daemon into an immediate wake cycle.
MIN_WAKE_SLEEP_SECONDS = 1.0


def _config_mtime_ns(config_path: Path | None) -> int | None:
    """The config file's mtime in nanoseconds, or ``None`` when unavailable."""
    if config_path is None:
        return None
    try:
        return config_path.stat().st_mtime_ns
    except OSError:
        return None


_fcntl_module: Any | None = None
_fcntl_checked = False


def _fcntl() -> Any | None:
    """The ``fcntl`` module, or ``None`` on platforms without it (Windows)."""
    global _fcntl_module, _fcntl_checked
    if _fcntl_checked:
        return _fcntl_module
    _fcntl_checked = True
    try:
        import fcntl

        _fcntl_module = fcntl
    except ImportError:
        _fcntl_module = None
    return _fcntl_module


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
    """Runs :class:`Forgeo` cycles on a fixed schedule until stopped.

    Args:
        config: The config the daemon starts with (and the last valid config
            it falls back to when a reload is rejected).
        forgeo: The :class:`Forgeo` to run each cycle. With
            ``forgeo_factory`` this is replaced on a config reload.
        config_path: Path of the ``forgeo.yaml`` to watch for changes. When
            ``None`` the daemon never reloads.
        forgeo_factory: Callable ``(ForgeoConfig) -> Forgeo`` used to rebuild
            the forgeo from a reloaded config. When ``None`` a reload only
            updates ``forgeo.config`` (when the forgeo exposes one) and the
            daemon's own config/interval.
        interval_override: Preserve the CLI's ``--interval-minutes`` override
            across config reloads, so a reload never silently drops it.
    """

    def __init__(
        self,
        config: ForgeoConfig,
        forgeo: Forgeo,
        *,
        config_path: str | Path | None = None,
        forgeo_factory: Any | None = None,
        interval_override: int | None = None,
    ) -> None:
        self.config = config
        self.config_path = Path(config_path) if config_path is not None else None
        self.forgeo = forgeo
        self._forgeo_factory = forgeo_factory
        self._interval_override = interval_override
        self.interval_seconds: float = config.interval_minutes * 60.0
        self.run_lock = RunLock(run_lock_path(config))
        self._state_file = daemon_state_path(config)
        self._stop_event = asyncio.Event()
        self._reload_event = asyncio.Event()
        self._config_mtime_ns = _config_mtime_ns(self.config_path)
        self.pid: int = os.getpid()
        self.started_at: datetime = datetime.now(UTC)
        self.last_outcome: str | None = None
        self.next_run_at: datetime | None = None

    @property
    def state_file(self) -> Path:
        """The daemon state path, pinned to the startup config.

        Derived once in ``__init__`` rather than from ``self.config``, which a
        SIGHUP reload replaces: readers (the CLI, the web console) resolve the
        path from the config *on disk*, so a reload that moved the backlog
        must not strand the state file the running daemon still writes.
        """
        return self._state_file

    def stop(self) -> None:
        """Request a graceful shutdown after the current cycle."""
        self._stop_event.set()

    def request_reload(self) -> None:
        """Ask the daemon to re-read ``forgeo.yaml`` at the next cycle boundary.

        Backed by ``SIGHUP``; wakes an in-progress sleep so a changed config
        (and a changed interval) take effect promptly instead of waiting out
        the old interval.
        """
        self._reload_event.set()

    def _config_changed(self) -> bool:
        """True when the config file's mtime differs from the last seen one."""
        if self.config_path is None:
            return False
        mtime = _config_mtime_ns(self.config_path)
        if mtime is None:
            return False
        changed = mtime != self._config_mtime_ns
        if changed:
            self._config_mtime_ns = mtime
        return changed

    def _reload_config(self) -> bool:
        """Re-read ``forgeo.yaml`` when it changed and apply it from the next
        cycle. An invalid change is logged and the previous config stays in
        use. Returns True when the config was replaced."""
        if self.config_path is None:
            return False
        force = self._reload_event.is_set()
        if force:
            self._reload_event.clear()
        if not force and not self._config_changed():
            return False
        try:
            new_config = load_config(self.config_path)
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            logger.error(
                "Config change rejected (%s): %s; keeping the previous config.",
                self.config_path,
                exc,
            )
            return False
        logger.info(
            "Config reloaded from %s; next cycle uses the new settings.",
            self.config_path,
        )
        self._apply_config(new_config)
        return True

    def _apply_config(self, new_config: ForgeoConfig) -> None:
        """Adopt ``new_config`` for the next cycle.

        Path fields that the daemon's locks and state files derive from stay
        pinned to the running daemon's values when they change — relocating
        them mid-flight would detach the lock files from the config and let a
        second daemon start on the new paths. Those changes are logged and
        deferred to a restart; every other setting (interval, agent command,
        refactor prompt, notifications, ...) takes effect from the next cycle.
        """
        changed_paths = [
            field
            for field in _RELOAD_PATH_FIELDS
            if getattr(new_config, field) != getattr(self.config, field)
        ]
        if changed_paths:
            logger.warning(
                "Config change cannot relocate path(s) %s while the daemon "
                "runs; keeping the current value(s) until a restart "
                "(forgeo restart).",
                ", ".join(changed_paths),
            )
            new_config = new_config.model_copy(
                update={field: getattr(self.config, field) for field in changed_paths}
            )
        if self._interval_override is not None:
            new_config = new_config.model_copy(
                update={"interval_minutes": self._interval_override}
            )
        if self._forgeo_factory is not None:
            self.forgeo = self._forgeo_factory(new_config)
        elif getattr(self.forgeo, "config", None) is not None:
            self.forgeo.config = new_config
        self.config = new_config
        self.interval_seconds = new_config.interval_minutes * 60.0

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
        backlog = getattr(self.forgeo, "backlog", None)
        if backlog is not None:
            await backlog.snapshot()
            logger.info("Backlog startup snapshot completed for %s", self.config.backlog)
        while not self._stop_event.is_set():
            try:
                self._reload_config()
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
            self.next_run_at = await self._compute_next_run_at()
            self.write_state()
            await self._sleep_until_next_cycle()
        self.write_state()
        logger.info("Forgeo stopped.")

    async def _compute_next_run_at(self) -> datetime:
        """The moment the daemon should wake after a finished cycle.

        Normally the next interval, but an ``OPEN`` task scheduled with a
        ``run_at`` that is sooner (or already due) shortens the sleep so a
        one-shot task fires promptly instead of waiting for the next scheduled
        pick. The wake target is clamped to a small floor so a task that stays
        due across skipped iterations cannot hot-loop the daemon.
        """
        now = datetime.now(UTC)
        fallback = now + timedelta(seconds=self.interval_seconds)
        backlog = getattr(self.forgeo, "backlog", None)
        if backlog is None:
            return fallback
        try:
            tasks = await backlog.list_tasks()
        except Exception:
            logger.exception("Could not read backlog for the next wake time; using the interval.")
            return fallback
        run_at = next_due_run_at(tasks, now=now)
        if run_at is None:
            return fallback
        delay = (run_at - now).total_seconds()
        if delay >= self.interval_seconds:
            return fallback
        return now + timedelta(seconds=max(MIN_WAKE_SLEEP_SECONDS, delay))

    async def _sleep_until_next_cycle(self) -> None:
        """Sleep until the next run, an earlier stop, or a config reload.

        ``next_run_at`` holds the next wake moment (the interval, or a sooner
        ``run_at`` one-shot schedule); a ``SIGHUP`` (``request_reload``) wakes
        the daemon so a changed config is re-read and the new interval takes
        effect promptly instead of waiting out the old one.
        """
        now = datetime.now(UTC)
        target = self.next_run_at
        if target is None:
            target = now + timedelta(seconds=self.interval_seconds)
        delay = max(0.0, (target - now).total_seconds())
        sleep = asyncio.create_task(asyncio.sleep(delay))
        stop = asyncio.create_task(self._stop_event.wait())
        reload_task = asyncio.create_task(self._reload_event.wait())
        try:
            done, pending = await asyncio.wait(
                {sleep, stop, reload_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            for cancelled_task in (sleep, stop, reload_task):
                cancelled_task.cancel()
            raise
        for pending_task in pending:
            pending_task.cancel()
        for done_task in done:
            await done_task
