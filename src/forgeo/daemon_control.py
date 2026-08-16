"""Daemon lifecycle actions shared by the CLI and the central web console.

``forgeo stop``/``restart`` and the web console's
``POST /api/instances/<name>/start``, ``/stop`` and ``/restart`` operate on
the same per-instance lock file: SIGTERM the recorded PID and wait for the
lock to drop (a cycle in progress always finishes first), then — for start
and restart — launch a detached ``forgeo start --foreground`` process that
re-reads ``forgeo.yaml`` on boot. A plain config edit is picked up by the
running daemon on its next cycle; a restart is how path changes (which the
daemon pins to its startup values) take effect.

Everything here is lock-file driven and never touches the config-save flow.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from forgeo.daemon import is_lock_held, read_lock_pid
from forgeo.models import ForgeoConfig
from forgeo.paths import lock_path as daemon_lock_path

logger = logging.getLogger(__name__)

STOP_TIMEOUT_SECONDS = 600.0
START_TIMEOUT_SECONDS = 15.0
_POLL_SECONDS = 0.5


class DaemonError(Exception):
    """A daemon lifecycle action failed; the message is user-facing."""


def wait_for_process_ready(
    proc: subprocess.Popen[Any],
    timeout: float,
    *,
    is_ready: Callable[[], bool],
    ready_pid: Callable[[], int | None] | None = None,
) -> int | None:
    """Wait for a just-launched detached process to report readiness.

    ``is_ready`` says the process is up (typically its lock file is held);
    ``ready_pid`` returns the pid it recorded, preferred over ``proc.pid``.
    Returns the running pid, or ``None`` when the process exits or the
    timeout elapses before it reports ready.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_ready():
            recorded = ready_pid() if ready_pid is not None else None
            return recorded if recorded is not None else proc.pid
        if proc.poll() is not None:
            return None
        time.sleep(_POLL_SECONDS)
    return None


def signal_and_wait_for_release(
    pid: int,
    *,
    name: str,
    is_held: Callable[[], bool],
    timeout: float,
    error_cls: type[Exception],
    timeout_hint: str = "",
) -> None:
    """SIGTERM ``pid`` and wait for the lock it holds to be released.

    Shared by the daemon and the central-dashboard stop commands: SIGTERM the
    recorded PID, tolerate a process that already exited (as long as its lock
    is gone too), refuse without permission, and wait for the lock to drop
    within ``timeout`` (a cycle in progress finishes first). Any failure
    raises ``error_cls`` — each caller's own error type — with a user-facing
    message; on success ``name`` is logged as stopped.
    """
    logger.info("Stopping %s (pid %s)...", name, pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        if not is_held():
            logger.info("%s stopped.", name)
            return
        raise error_cls(
            f"Recorded pid {pid} is gone but the lock is still held; "
            f"check with `pgrep -af forgeo`."
        ) from None
    except PermissionError:
        raise error_cls(f"No permission to stop process {pid}.") from None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_held():
            logger.info("%s stopped.", name)
            return
        time.sleep(_POLL_SECONDS)
    if not is_held():
        logger.info("%s stopped.", name)
        return
    raise error_cls(
        f"{name} is still shutting down after {timeout:.0f}s{timeout_hint}; giving up."
    )


def stop_daemon(
    config: ForgeoConfig, timeout: float = STOP_TIMEOUT_SECONDS
) -> None:
    """SIGTERM the running daemon and wait for it to exit.

    A cycle in progress always finishes first, so partial work is never
    lost. Raises :class:`DaemonError` when the daemon cannot be stopped
    (missing PID, a dead recorded PID, no permission, or a timeout).
    """
    lock_path = daemon_lock_path(config)
    pid = read_lock_pid(lock_path)
    if pid is None:
        raise DaemonError(
            f"The lock file {lock_path} records no PID; find the daemon "
            f"with `pgrep -af forgeo` and stop it manually."
        )
    signal_and_wait_for_release(
        pid,
        name=f"forgeo {config.name!r}",
        is_held=lambda: is_lock_held(lock_path),
        timeout=timeout,
        error_cls=DaemonError,
        timeout_hint=" (a cycle in progress finishes first)",
    )


def start_daemon(
    config_path: str | Path,
    config: ForgeoConfig,
    *,
    extra_args: list[str] | None = None,
) -> int:
    """Launch a detached ``forgeo start --foreground`` daemon; returns its pid.

    The daemon re-reads ``forgeo.yaml`` on boot, so a config saved from the
    web console applies on the next start. ``extra_args`` (e.g. an
    ``--interval-minutes`` override) are forwarded to the child. Raises
    :class:`DaemonError` when the daemon fails to start within
    :data:`START_TIMEOUT_SECONDS`.
    """
    lock_path = daemon_lock_path(config)
    command = [
        sys.executable,
        "-m",
        "forgeo",
        "start",
        "--foreground",
        "--config",
        str(config_path),
    ]
    if extra_args:
        command.extend(extra_args)
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid = wait_for_process_ready(
        proc,
        START_TIMEOUT_SECONDS,
        is_ready=lambda: is_lock_held(lock_path),
        ready_pid=lambda: read_lock_pid(lock_path),
    )
    if pid is None:
        raise DaemonError(
            f"Forgeo daemon did not start; see {config.log_file} for details."
        )
    logger.info("Forgeo %r started (pid %s).", config.name, pid)
    return pid


def restart_daemon(
    config_path: str | Path,
    config: ForgeoConfig,
    timeout: float = STOP_TIMEOUT_SECONDS,
) -> int:
    """Stop the running daemon (if any), then start it detached.

    Returns the new daemon's recorded pid. Raises :class:`DaemonError` when
    the stop or the start fails.
    """
    if is_lock_held(daemon_lock_path(config)):
        stop_daemon(config, timeout)
    return start_daemon(config_path, config)
