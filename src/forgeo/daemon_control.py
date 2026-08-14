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

import functools
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from forgeo.daemon import is_lock_held, read_lock_pid
from forgeo.models import ForgeoConfig
from forgeo.paths import lock_path as daemon_lock_path

logger = logging.getLogger(__name__)

STOP_TIMEOUT_SECONDS = 600.0
START_TIMEOUT_SECONDS = 15.0
_POLL_SECONDS = 0.5


class DaemonError(Exception):
    """A daemon lifecycle action failed; the message is user-facing."""


def wait_for_lock_release(
    lock_path: Path,
    timeout: float,
    *,
    is_held: Callable[[], bool] | None = None,
) -> bool:
    """Poll until the lock is released; False on timeout.

    ``is_held`` defaults to the flock-based :func:`is_lock_held`; pass a
    custom predicate (e.g. a PID-alive check for the web-dashboard lock) to
    reuse the same wait loop for other lock kinds.
    """
    if is_held is None:
        is_held = functools.partial(is_lock_held, lock_path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_held():
            return True
        time.sleep(_POLL_SECONDS)
    return not is_held()


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
    logger.info("Stopping forgeo %r (pid %s)...", config.name, pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        if not is_lock_held(lock_path):
            logger.info("Forgeo %r stopped.", config.name)
            return
        raise DaemonError(
            f"Recorded pid {pid} is gone but the lock is still held; "
            f"check with `pgrep -af forgeo`."
        )
    except PermissionError:
        raise DaemonError(f"No permission to stop process {pid}.")
    if wait_for_lock_release(lock_path, timeout):
        logger.info("Forgeo %r stopped.", config.name)
        return
    raise DaemonError(
        f"Forgeo is still shutting down after {timeout:.0f}s "
        f"(a cycle in progress finishes first); giving up."
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
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_lock_held(lock_path):
            pid = read_lock_pid(lock_path) or proc.pid
            logger.info("Forgeo %r started (pid %s).", config.name, pid)
            return pid
        if proc.poll() is not None:
            break
        time.sleep(_POLL_SECONDS)
    raise DaemonError(
        f"Forgeo daemon did not start; see {config.log_file} for details."
    )


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
