"""Central multi-instance web dashboard (``forgeo web``).

A standalone server that aggregates every forgeo registered in the instance
registry (:mod:`forgeo.instances`). It reads each instance's data straight
from its files (``backlog.json``, ``runs.jsonl``, ``forgeo.log``,
``BLOCKER.md``, ``daemon.state.json``), so it works whether or not that
instance's daemon is running — the daemon binds no ports at all. HTTP and Jira
backlogs are fetched from their providers instead, and an unavailable remote
source is reported as such rather than shown as an empty backlog.

Routes:

* ``GET /`` — home page listing every registered instance: name, repo,
  daemon state, last outcome, next run, and backlog counts.
* ``GET /instances/<name>/`` — per-instance page: that instance's kanban
  backlog (with a form to add tasks) plus tabs for logs, history, blocker, and
  config.
* ``GET /api/instances`` — JSON summary of every registered instance.
* ``GET /api/instances/<name>/tasks``, ``/tasks/<id>``, ``/status``,
  ``/logs?lines=N``, ``/runs?limit=N&offset=M``, ``/blocker``, ``/config`` —
  the per-instance API.
* ``PUT /api/instances/<name>/config`` — validate and persist an instance's
  ``forgeo.yaml`` from a config payload (applies on the daemon's next cycle;
  ``name`` and ``telegram_bot_token`` are not editable).
* ``POST /api/instances/<name>/tasks`` — add a new task to that instance's
  backlog.
* ``POST /api/instances/<name>/tasks/<id>/reopen`` — reopen a ``BLOCKED``
  task (status back to ``OPEN``, blocker reason cleared).
* ``POST /api/instances/<name>/start``, ``/stop``, ``/restart`` — start,
  stop, or restart that instance's daemon from the web console (SIGTERM +
  wait, detached start — the same logic as ``forgeo start``/``stop``/
  ``restart``).
* ``PATCH /api/instances/<name>/tasks/<id>`` — update an existing task's
  editable fields (title, description, acceptance criteria, dependencies,
  files to modify, agent command, agent timeout, retries_left, run_at).
* ``DELETE /api/instances/<name>/tasks/<id>`` — delete an ``OPEN`` or
  ``BLOCKED`` task from that instance's backlog.

An unknown instance name returns ``404``; a registered instance with missing
data files renders with empty data and ``daemon_running=false`` rather than
erroring.

Authentication: ``forgeo web`` takes an optional bearer token (the
``--token`` CLI flag and/or the ``~/.config/forgeo/web.toml`` token file,
auto-generated and printed once when it is unset). When configured, every
``/api/*`` route requires ``Authorization: Bearer <token>`` and answers
``401`` otherwise; static pages and the token-prompt login flow stay
reachable without a token. With no token configured the dashboard behaves
exactly as before (no auth).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import tomllib
from collections.abc import Callable
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel

from forgeo import daemon_control
from forgeo.backlog import (
    backlog_status_counts,
    open_backlog,
    unsatisfied_dependencies,
)
from forgeo.config import save_config
from forgeo.daemon import is_lock_held, read_lock_pid
from forgeo.instances import (
    InstanceInfo,
    get_instance,
    list_instances,
    registry_path,
)
from forgeo.models import ForgeoConfig, Task, TaskStatus
from forgeo.paths import daemon_state_path, lock_path, runs_path
from forgeo.runs import RunRecorder
from forgeo.web_common import (
    DEFAULT_LOG_LINES,
    DEFAULT_RUN_LIMIT,
    DEFAULT_RUN_OFFSET,
    MAX_LOG_LINES,
    MAX_RUN_LIMIT,
    MAX_RUN_OFFSET,
    clamp_query_int,
    guess_content_type,
    iso,
    json_bytes,
    safe_static_path,
    tail_lines,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8790

WEB_START_TIMEOUT_SECONDS = 30.0
WEB_STOP_TIMEOUT_SECONDS = 30.0

DEFAULT_FORGEO_CONFIG_DIR = Path.home() / ".config" / "forgeo"

_HOME_PAGE = "/central/index.html"
_INSTANCE_PAGE = "/central/instance.html"

_WEB_TASK_ID_RE = re.compile(r"^WEB-(\d+)$")


def forgeo_config_dir() -> Path:
    """Forgeo's per-user config directory: ``$FORGEO_CONFIG_DIR`` or
    ``~/.config/forgeo``."""
    env = os.environ.get("FORGEO_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    return DEFAULT_FORGEO_CONFIG_DIR


def web_lock_path() -> Path:
    """The host-global dashboard lock file (one per user, not per-repo)."""
    return forgeo_config_dir() / "web.lock"


def web_token_path() -> Path:
    """The dashboard auth token file: ``$FORGEO_CONFIG_DIR/web.toml``."""
    return forgeo_config_dir() / "web.toml"


AUTOGENERATE_TOKEN = object()
"""Sentinel for ``forgeo web --token`` given without a value: generate one."""


def load_web_token(path: Path | None = None) -> str | None:
    """The bearer token configured in ``web.toml``, or ``None`` when absent.

    A missing, unreadable, or malformed file, or one without a usable
    ``token`` value, reads as ``None`` (auth stays off until a token exists).
    """
    path = web_token_path() if path is None else path
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    token = data.get("token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def save_web_token(token: str, path: Path | None = None) -> None:
    """Persist ``token`` to the dashboard token file (``0600``).

    A failed write is logged and skipped — the token still takes effect for
    this process — never fatal to ``forgeo web``.
    """
    path = web_token_path() if path is None else path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'token = "{token}"\n', encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        logger.warning("Could not persist web token to %s.", path)


def resolve_web_token(cli_token: Any) -> tuple[str | None, bool]:
    """Resolve the effective dashboard token; ``(token, generated_now)``.

    * A non-blank ``cli_token`` string (``forgeo web --token TOKEN``) is
      persisted to ``web.toml`` and used.
    * Otherwise a token already saved in ``web.toml`` is reused.
    * Otherwise, when ``cli_token`` is the :data:`AUTOGENERATE_TOKEN`
      sentinel (``forgeo web --token`` with no value) or ``web.toml`` exists
      but holds no token, a fresh token is generated, persisted, and reported
      as ``generated_now`` so the caller can print it exactly once.
    * No flag and no token file yields ``(None, False)``: auth stays off —
      the historical one-command local behavior.
    """
    path = web_token_path()
    if isinstance(cli_token, str) and cli_token.strip():
        save_web_token(cli_token.strip(), path)
        return cli_token.strip(), False
    existing = load_web_token(path)
    if existing is not None:
        return existing, False
    if cli_token is AUTOGENERATE_TOKEN or path.exists():
        generated = secrets.token_urlsafe(24)
        save_web_token(generated, path)
        return generated, True
    return None, False


class WebLockError(Exception):
    """A central-dashboard lock/stop action failed; the message is user-facing."""


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` belongs to a live process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class WebLock:
    """The host-global lock for the central dashboard.

    Unlike the per-forgeo daemon locks (``fcntl`` flock), this is a plain
    ``pid=<pid>`` file created atomically with ``O_EXCL``, recording the
    bound ``host``/``port`` too; it counts as held while the recorded PID is
    alive. A leftover file whose PID is dead is taken over (with a warning)
    on the next acquire.
    """

    def __init__(self, lock_path: str | Path | None = None) -> None:
        self.lock_path = Path(lock_path) if lock_path is not None else web_lock_path()

    def _read_value(self, key: str) -> str | None:
        try:
            text = self.lock_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith(f"{key}="):
                return line.removeprefix(f"{key}=").strip()
        return None

    @property
    def pid(self) -> int | None:
        """The recorded PID (``read_lock_pid``-compatible shape)."""
        return read_lock_pid(self.lock_path)

    @property
    def host(self) -> str | None:
        """The recorded bind address, or ``None`` when not written."""
        return self._read_value("host")

    @property
    def port(self) -> int | None:
        """The recorded bind port, or ``None`` when not written."""
        value = self._read_value("port")
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def is_held(self) -> bool:
        """True while the recorded PID is a live process."""
        pid = self.pid
        if pid is None:
            return False
        return _pid_alive(pid)

    def acquire(self, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        """Take the lock, refusing while a live dashboard holds it.

        Raises:
            WebLockError: When another live dashboard holds the lock.
        """
        if self.is_held():
            raise WebLockError(
                f"Another central dashboard is already running (pid {self.pid}); "
                f"lock file {self.lock_path}."
            )
        if self.lock_path.exists():
            logger.warning(
                "Stale central-dashboard lock %s (pid %s is dead); taking over.",
                self.lock_path,
                self.pid,
            )
            self.lock_path.unlink()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            raise WebLockError(
                f"Another central dashboard just took the lock {self.lock_path}; retry."
            )
        try:
            os.write(
                fd,
                f"pid={os.getpid()}\nhost={host}\nport={port}\n".encode(),
            )
            os.fsync(fd)
        finally:
            os.close(fd)

    def release(self) -> None:
        """Remove the lock file (idempotent)."""
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass


def web_task_id_for(tasks: list[Task]) -> str:
    """Next ``WEB-###`` id after the highest existing ``WEB-###`` id."""
    highest = 0
    for task in tasks:
        match = _WEB_TASK_ID_RE.match(task.id)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"WEB-{highest + 1:03d}"


def _instance_parts(path: str) -> list[str]:
    """The path segments after the ``/api/instances/`` prefix."""
    return path[len("/api/instances/") :].split("/")


def _config_validation_message(exc: ValidationError) -> str:
    """A readable one-line error for a config payload that failed validation."""
    details = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "invalid"))
        details.append(f"{loc}: {message}" if loc else message)
    return "invalid config: " + "; ".join(details)


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    """Read a JSON object from ``path``; ``None`` when it is missing, corrupt,
    or not a JSON object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _daemon_state(config: ForgeoConfig | None) -> dict[str, Any] | None:
    """The daemon's persisted live state, or ``None`` when unavailable.

    The daemon writes ``daemon.state.json`` (next to the backlog) after
    every cycle; a missing, unreadable, or stale file reads as ``None`` and
    callers fall back to estimates from ``runs.jsonl``.
    """
    if config is None:
        return None
    return _read_json_dict(daemon_state_path(config))


def read_tasks(config: ForgeoConfig | None) -> list[Task]:
    """All tasks for ``config``; raises when a remote backlog is unreachable.

    A backlog file is read directly, so the dashboard never writes to an
    instance's files (``JSONBacklog`` renames a corrupt backlog; here it is
    skipped) and a missing or corrupt file simply reads as empty. A remote
    backlog has to be fetched, and that request can fail — callers decide what
    to show when it does, because "unreachable" and "empty" must never look
    the same.
    """
    if config is None:
        return []
    if config.backlog_is_remote:
        return asyncio.run(open_backlog(config).list_tasks())
    data = _read_json_dict(Path(config.backlog))
    if data is None:
        return []
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return []
    parsed: list[Task] = []
    for entry in tasks:
        if not isinstance(entry, dict):
            continue
        try:
            parsed.append(Task.model_validate(entry))
        except ValidationError:
            continue
    return parsed


def _read_tasks_or_error(config: ForgeoConfig | None) -> tuple[list[Task], str | None]:
    """Tasks for ``config`` plus the reason they could not be read, if any.

    The home page aggregates every registered instance, so one unreachable
    backlog endpoint must not take the whole page down with it.
    """
    try:
        return read_tasks(config), None
    except Exception as exc:  # noqa: BLE001 - any backend failure is reportable
        logger.warning("Could not read the backlog: %s", exc)
        return [], str(exc)


def _task_payload(
    task: Task,
    tasks: list[Task],
    config: ForgeoConfig | None = None,
) -> dict[str, Any]:
    """Serialize a task for the API, annotating its unsatisfied dependencies.

    The extra ``unsatisfied_dependencies`` field lists every dependency id
    that is not ``COMPLETED`` yet (with its current status, or ``missing``
    when it does not exist), so the web console can explain why a task is
    waiting before Forgeo may pick it. When the instance's config is
    available, the task also carries its effective retry budget
    (``retry_budget``, the per-task ``retries_left`` override falling back to
    the config's ``failed_retry_max``) and ``retries_remaining`` so the
    console can show whether a FAILED task will be retried automatically.
    """
    payload = task.model_dump(mode="json")
    payload["unsatisfied_dependencies"] = unsatisfied_dependencies(tasks, task)
    if config is not None:
        budget = task.retries_left if task.retries_left is not None else config.failed_retry_max
        payload["retry_budget"] = budget
        payload["retries_remaining"] = max(0, budget - task.retry_count)
    return payload


def _blocker_content(config: ForgeoConfig | None) -> str | None:
    """The ``BLOCKER.md`` contents, or ``None`` when absent or unreadable."""
    if config is None:
        return None
    blocker = Path(config.blocker_file)
    if not blocker.is_file():
        return None
    try:
        return blocker.read_text(encoding="utf-8")
    except OSError:
        return None


def _last_outcome(config: ForgeoConfig | None) -> str | None:
    """The most recent run's outcome string, or ``None``.

    Prefers the daemon's persisted state; falls back to ``runs.jsonl`` when
    no state file exists (e.g. an older daemon version).
    """
    if config is None:
        return None
    state = _daemon_state(config)
    outcome = state.get("last_outcome") if state is not None else None
    if isinstance(outcome, str):
        return outcome
    last_run = RunRecorder(runs_path(config)).read_last()
    return last_run.outcome.value if last_run is not None else None


def _next_run(info: InstanceInfo, config: ForgeoConfig | None) -> str | None:
    """The next scheduled run, when it can be derived.

    Prefers the daemon's persisted state (written every cycle). With no state
    file, the next run is approximated as the last run's finish time plus the
    interval — but only while the daemon is running.
    """
    if not info.daemon_running or config is None:
        return None
    state = _daemon_state(config)
    next_run_at = state.get("next_run_at") if state is not None else None
    if isinstance(next_run_at, str):
        return next_run_at
    last_run = RunRecorder(runs_path(config)).read_last()
    if last_run is None:
        return None
    estimate = last_run.finished_at + timedelta(minutes=config.interval_minutes)
    return iso(estimate)


def _status_payload(info: InstanceInfo) -> dict[str, Any]:
    """The per-instance status payload."""
    config = info.config
    if config is None:
        return {
            "name": info.name,
            "repo": None,
            "interval_minutes": None,
            "daemon_running": False,
            "pid": None,
            "last_outcome": None,
            "next_run_at": None,
        }
    state = _daemon_state(config)
    pid: int | None = read_lock_pid(lock_path(config))
    if state is not None:
        state_pid = state.get("pid")
        if isinstance(state_pid, int):
            pid = state_pid
    return {
        "name": config.name,
        "repo": str(config.repo),
        "interval_minutes": config.interval_minutes,
        "daemon_running": info.daemon_running,
        "pid": pid,
        "last_outcome": _last_outcome(config),
        "next_run_at": _next_run(info, config),
    }


def _summary(info: InstanceInfo) -> dict[str, Any]:
    """One home-page/API row for a registered instance."""
    config = info.config
    if config is None:
        return {
            "name": info.name,
            "config_path": str(info.config_path),
            "repo": None,
            "daemon_running": False,
            "last_outcome": None,
            "next_run_at": None,
            "backlog_counts": {status.value: 0 for status in TaskStatus},
            "backlog_error": None,
        }
    tasks, backlog_error = _read_tasks_or_error(config)
    return {
        "name": info.name,
        "config_path": str(info.config_path),
        "repo": str(config.repo),
        "daemon_running": info.daemon_running,
        "last_outcome": _last_outcome(config),
        "next_run_at": _next_run(info, config),
        "backlog_counts": backlog_status_counts(tasks),
        "backlog_error": backlog_error,
    }


def make_handler(token: str | None = None) -> type[BaseHTTPRequestHandler]:
    """Build the request-handler class for the central dashboard.

    ``token`` enables optional bearer auth: when set, every ``/api/*`` route
    requires an ``Authorization: Bearer <token>`` header and answers ``401``
    otherwise. Static pages (including the token-prompt login flow) stay
    reachable without a token, so a shared host never leaks backlog contents
    to anonymous clients.
    """

    class CentralRequestHandler(BaseHTTPRequestHandler):
        _token = token

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("central web %s - %s", self.address_string(), format % args)

        def _send_json(
            self,
            status: int,
            payload: Any,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            body = json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_unauthorized(self) -> None:
            self._send_json(
                401,
                {"error": "unauthorized"},
                {"WWW-Authenticate": 'Bearer realm="forgeo"'},
            )

        def _send_not_found(self) -> None:
            """Send the shared 404 response for an unknown route."""
            self._send_json(404, {"error": "not found"})

        def _maybe_authorize(self, path: str) -> bool:
            """True when the request may proceed; a 401 is sent otherwise.

            With bearer auth configured, every ``/api/*`` route requires an
            ``Authorization: Bearer <token>`` header, compared in constant
            time. Static pages and assets never need a token.
            """
            if self._token is None or not path.startswith("/api/"):
                return True
            expected = "Bearer " + self._token
            if hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                return True
            self._send_unauthorized()
            return False

        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, static: Path | None) -> None:
            if static is None:
                self._send_not_found()
                return
            self._send_bytes(200, static.read_bytes(), guess_content_type(static))

        def _run_safely(self, handler: Callable[[], None]) -> None:
            """Run ``handler``, sending a 500 and logging on any exception.

            A request handler must never crash the server thread; anything it
            raises is caught here, logged, and answered with a JSON 500.
            """
            try:
                handler()
            except Exception:
                logger.exception("Web request failed: %s", urlparse(self.path).path)
                self._send_json(500, {"error": "internal server error"})

        def _dispatch(self, handler: Callable[[], None]) -> None:
            """Authorize, then run one request handler safely.

            Shared by the five HTTP verbs: a request is authorized first (a
            missing or wrong bearer token answers ``401`` without reaching the
            handler), then the handler runs under :meth:`_run_safely` so a
            failure is answered with a JSON 500 instead of crashing the
            server thread.
            """
            if not self._maybe_authorize(self.path):
                return
            self._run_safely(handler)

        def do_GET(self) -> None:
            self._dispatch(self._do_get)

        def _do_get(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path == "/api/instances":
                self._send_json(200, [_summary(info) for info in list_instances()])
                return
            if path.startswith("/api/instances/"):
                self._handle_instance_api(path, query)
                return
            if path == "/":
                self._send_static(safe_static_path(_HOME_PAGE))
                return
            if path.startswith("/instances/"):
                self._handle_instance_page(path)
                return
            static = safe_static_path(path)
            if static is not None:
                self._send_static(static)
                return
            self._send_not_found()

        def do_POST(self) -> None:
            self._dispatch(self._do_post)

        def _do_post(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path.startswith("/api/instances/"):
                self._post_instance_api(path)
                return
            self._send_not_found()

        def do_PATCH(self) -> None:
            self._dispatch(self._do_patch)

        def _do_patch(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path.startswith("/api/instances/"):
                self._patch_instance_task(path)
                return
            self._send_not_found()

        def do_DELETE(self) -> None:
            self._dispatch(self._do_delete)

        def _do_delete(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path.startswith("/api/instances/"):
                self._delete_instance_task(path)
                return
            self._send_not_found()

        def do_PUT(self) -> None:
            self._dispatch(self._do_put)

        def _do_put(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path.startswith("/api/instances/"):
                self._put_instance_api(path)
                return
            self._send_not_found()

        def _resolve_instance(self, name: str) -> InstanceInfo | None:
            """The registered instance, or ``None`` after sending a 404."""
            info = get_instance(name)
            if info is None:
                self._send_json(404, {"error": "unknown instance"})
                return None
            return info

        def _instance_config(self, info: InstanceInfo) -> ForgeoConfig | None:
            """The instance's config, or ``None`` after sending a 500."""
            if info.config is None:
                self._send_json(500, {"error": "instance config not available"})
                return None
            return info.config

        def _instance_tasks(self, info: InstanceInfo) -> list[Task] | None:
            """The instance's tasks, or ``None`` after sending a 502.

            A remote backlog can be unreachable; saying so is the only honest
            answer, since an empty list would read as "this forgeo has no
            work left".
            """
            try:
                return read_tasks(info.config)
            except Exception as exc:  # noqa: BLE001 - any backend failure is reportable
                logger.warning("Backlog of instance %r is unavailable: %s", info.name, exc)
                self._send_json(502, {"error": str(exc)})
                return None

        def _read_json_body(self) -> dict[str, Any] | None:
            """Read and parse the request body as a JSON object.

            Sends a 400 error and returns ``None`` when the body is missing,
            malformed, or not a JSON object.
            """
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                self._send_json(400, {"error": "request body is required"})
                return None
            try:
                length = int(raw_length)
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return None
            body = self.rfile.read(max(length, 0))
            try:
                payload = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "request body must be JSON"})
                return None
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "request body must be a JSON object"})
                return None
            return payload

        def _resolve_task_target(
            self, path: str, *, with_task_id: bool
        ) -> tuple[ForgeoConfig, str | None] | None:
            """Resolve the instance config (+ optional task id) from an API path.

            Sends the matching 404/500 error and returns ``None`` when the
            instance is unknown, the path shape is wrong, or the instance
            config is unavailable.
            """
            parts = _instance_parts(path)
            name = unquote(parts[0])
            info = self._resolve_instance(name)
            if info is None:
                return None
            expected = 3 if with_task_id else 2
            if len(parts) != expected or parts[1] != "tasks":
                self._send_not_found()
                return None
            config = self._instance_config(info)
            if config is None:
                return None
            task_id = unquote(parts[2]) if with_task_id else None
            return config, task_id

        def _post_instance_api(self, path: str) -> None:
            """Route a POST under ``/api/instances/`` to its handler."""
            parts = _instance_parts(path)
            if len(parts) < 2:
                self._send_not_found()
                return
            if parts[1] in ("start", "stop", "restart"):
                if len(parts) != 2:
                    self._send_not_found()
                    return
                self._post_instance_daemon_action(path, parts[1])
                return
            if parts[1] != "tasks":
                self._send_not_found()
                return
            if len(parts) == 2:
                self._post_instance_task(path)
                return
            if len(parts) == 4 and parts[3] == "reopen":
                self._reopen_instance_task(path)
                return
            self._send_not_found()

        def _post_instance_daemon_action(self, path: str, action: str) -> None:
            """Start, stop, or restart an instance's daemon from the console.

            Reuses the same lock-file driven logic as ``forgeo start``/``stop``/
            ``restart`` (SIGTERM + wait for the lock to drop, then a detached
            ``forgeo start`` that re-reads ``forgeo.yaml`` on boot). The
            response reports the outcome — ``started`` / ``already_running`` /
            ``stopped`` / ``not_running`` / ``restarted`` — plus the resulting
            daemon state.
            """
            parts = _instance_parts(path)
            name = unquote(parts[0])
            info = self._resolve_instance(name)
            if info is None:
                return
            config = self._instance_config(info)
            if config is None:
                return
            lock = lock_path(config)

            if action == "start":
                if is_lock_held(lock):
                    self._send_json(
                        409,
                        {
                            "status": "already_running",
                            "error": "daemon already running",
                            "message": f"Forgeo {config.name!r} is already running.",
                            "daemon_running": True,
                            "pid": read_lock_pid(lock),
                        },
                    )
                    return
                try:
                    pid = daemon_control.start_daemon(info.config_path, config)
                except daemon_control.DaemonError as exc:
                    self._send_json(
                        500,
                        {
                            "status": "start_failed",
                            "error": str(exc),
                            "daemon_running": is_lock_held(lock),
                        },
                    )
                    return
                self._send_json(
                    200,
                    {
                        "status": "started",
                        "message": (
                            f"Forgeo {config.name!r} started "
                            f"(pid {pid}, interval {config.interval_minutes} min)."
                        ),
                        "daemon_running": True,
                        "pid": pid,
                    },
                )
                return

            if action == "stop":
                if not is_lock_held(lock):
                    self._send_json(
                        200,
                        {
                            "status": "not_running",
                            "message": f"Forgeo {config.name!r} is not running.",
                            "daemon_running": False,
                        },
                    )
                    return
                try:
                    daemon_control.stop_daemon(config)
                except daemon_control.DaemonError as exc:
                    self._send_json(
                        500,
                        {
                            "status": "stop_failed",
                            "error": str(exc),
                            "daemon_running": is_lock_held(lock),
                        },
                    )
                    return
                self._send_json(
                    200,
                    {
                        "status": "stopped",
                        "message": f"Forgeo {config.name!r} stopped.",
                        "daemon_running": False,
                    },
                )
                return

            try:
                pid = daemon_control.restart_daemon(info.config_path, config)
            except daemon_control.DaemonError as exc:
                self._send_json(
                    500,
                    {
                        "status": "restart_failed",
                        "error": str(exc),
                        "daemon_running": is_lock_held(lock),
                    },
                )
                return
            self._send_json(
                200,
                {
                    "status": "restarted",
                    "message": (
                        f"Forgeo {config.name!r} restarted "
                        f"(pid {pid}, interval {config.interval_minutes} min)."
                    ),
                    "daemon_running": True,
                    "pid": pid,
                },
            )

        def _post_instance_task(self, path: str) -> None:
            """Create a task in an instance's backlog from a JSON body."""
            target = self._resolve_task_target(path, with_task_id=False)
            if target is None:
                return
            config, _ = target

            payload = self._read_json_body()
            if payload is None:
                return
            title = payload.get("title")
            if not isinstance(title, str) or not title.strip():
                self._send_json(400, {"error": "title is required"})
                return
            description = payload.get("description", "")
            if not isinstance(description, str) or not description.strip():
                self._send_json(400, {"error": "description is required"})
                return
            acceptance_criteria = payload.get("acceptance_criteria", [])
            if not isinstance(acceptance_criteria, list) or not all(
                isinstance(criterion, str) for criterion in acceptance_criteria
            ):
                self._send_json(
                    400, {"error": "acceptance_criteria must be a list of strings"}
                )
                return
            agent_command = payload.get("agent_command")
            if agent_command is not None and (
                not isinstance(agent_command, str) or not agent_command.strip()
            ):
                self._send_json(
                    400, {"error": "agent_command must be a non-blank string or null"}
                )
                return
            run_at = payload.get("run_at")
            run_at_dt: datetime | None = None
            if run_at is not None:
                if not isinstance(run_at, str):
                    self._send_json(
                        400,
                        {"error": "run_at must be an ISO-8601 datetime string or null"},
                    )
                    return
                try:
                    run_at_dt = datetime.fromisoformat(run_at)
                except ValueError:
                    self._send_json(
                        400,
                        {"error": "run_at must be an ISO-8601 datetime string or null"},
                    )
                    return

            backlog = open_backlog(config)
            existing = asyncio.run(backlog.list_tasks())
            try:
                task = Task(
                    id=web_task_id_for(existing),
                    title=title.strip(),
                    description=description.strip(),
                    acceptance_criteria=acceptance_criteria,
                    agent_command=agent_command.strip() if agent_command else None,
                    run_at=run_at_dt,
                )
            except ValidationError as exc:
                self._send_json(400, {"error": f"invalid task field(s): {exc}"})
                return
            try:
                created = asyncio.run(backlog.create_task(task))
            except ValueError:
                self._send_json(
                    409, {"error": f"task id already exists in backlog: {task.id!r}"}
                )
                return
            self._send_json(201, created.model_dump(mode="json"))

        def _patch_instance_task(self, path: str) -> None:
            """Update a task in an instance's backlog from a JSON body."""
            target = self._resolve_task_target(path, with_task_id=True)
            if target is None:
                return
            config, task_id = target
            assert task_id is not None  # with_task_id=True resolves a task id
            payload = self._read_json_body()
            if payload is None:
                return
            if not payload:
                self._send_json(400, {"error": "request body must not be empty"})
                return

            backlog = open_backlog(config)
            try:
                updated = asyncio.run(backlog.update_task(task_id, payload))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            if updated is None:
                self._send_not_found()
                return
            self._send_json(200, updated.model_dump(mode="json"))

        def _reopen_instance_task(self, path: str) -> None:
            """Reopen a BLOCKED task: status back to OPEN, reason cleared.

            A dedicated endpoint rather than a generic ``status`` via PATCH,
            so the status transition stays outside the editable-fields model.
            """
            parts = _instance_parts(path)
            if len(parts) != 4 or parts[1] != "tasks" or parts[3] != "reopen":
                self._send_not_found()
                return
            name = unquote(parts[0])
            info = self._resolve_instance(name)
            if info is None:
                return
            config = self._instance_config(info)
            if config is None:
                return
            task_id = unquote(parts[2])

            backlog = open_backlog(config)
            task = asyncio.run(backlog.get_task(task_id))
            if task is None:
                self._send_not_found()
                return
            if task.status is not TaskStatus.BLOCKED:
                self._send_json(
                    400,
                    {"error": "only BLOCKED tasks can be reopened"},
                )
                return
            reopened = asyncio.run(backlog.reopen_task(task_id))
            assert reopened is not None  # task was just found in the backlog
            self._send_json(200, reopened.model_dump(mode="json"))

        def _delete_instance_task(self, path: str) -> None:
            """Delete an OPEN or BLOCKED task from an instance's backlog."""
            target = self._resolve_task_target(path, with_task_id=True)
            if target is None:
                return
            config, task_id = target
            assert task_id is not None  # with_task_id=True resolves a task id

            backlog = open_backlog(config)
            task = asyncio.run(backlog.get_task(task_id))
            if task is None:
                self._send_not_found()
                return
            if task.status not in (TaskStatus.OPEN, TaskStatus.BLOCKED):
                self._send_json(
                    400,
                    {"error": "only OPEN or BLOCKED tasks can be deleted"},
                )
                return
            deleted = asyncio.run(backlog.delete_task(task_id))
            assert deleted is not None  # task was just found in the backlog
            self._send_json(200, deleted.model_dump(mode="json"))

        def _put_instance_api(self, path: str) -> None:
            """Route a PUT under ``/api/instances/`` to its handler."""
            parts = _instance_parts(path)
            if len(parts) != 2 or parts[1] != "config":
                self._send_not_found()
                return
            self._put_instance_config(path)

        def _put_instance_config(self, path: str) -> None:
            """Validate and persist an instance's ``forgeo.yaml`` from a body.

            Accepts the same shape ``GET /api/instances/<name>/config``
            returns. The config is validated against :class:`ForgeoConfig`
            and written to the instance's ``forgeo.yaml`` atomically; the
            response carries the reloaded config and an explicit note that
            the daemon picks the change up on its next cycle without a
            restart.

            ``name`` is owned by the registry and forced to the registered
            instance name (a different value is rejected); ``telegram_bot_token``
            is not editable through the web console — an explicit change is
            rejected and the current value is preserved when the field is
            omitted. ``backlog_auth``, ``state_dir`` and ``task_context`` are
            nested settings the flat config form does not render, so an omitted
            value keeps what the config already holds instead of clearing it.
            """
            parts = _instance_parts(path)
            name = unquote(parts[0])
            info = self._resolve_instance(name)
            if info is None:
                return
            config = self._instance_config(info)
            if config is None:
                return

            payload = self._read_json_body()
            if payload is None:
                return
            if not payload:
                self._send_json(400, {"error": "request body must not be empty"})
                return

            incoming_name = payload.get("name")
            if incoming_name is not None and incoming_name != info.name:
                self._send_json(
                    400,
                    {"error": "name is managed by the registry and cannot be changed"},
                )
                return
            payload["name"] = info.name
            if "telegram_bot_token" in payload:
                if payload["telegram_bot_token"] != config.telegram_bot_token:
                    self._send_json(
                        400,
                        {"error": "telegram_bot_token is not editable through the web console"},
                    )
                    return
            else:
                payload["telegram_bot_token"] = config.telegram_bot_token
            # The config form is flat, so it carries neither of these nested
            # settings; a save must not drop what it never showed.
            for preserved in ("backlog_auth", "state_dir", "task_context"):
                if preserved not in payload:
                    payload[preserved] = getattr(config, preserved)

            try:
                config = ForgeoConfig.model_validate(payload)
            except ValidationError as exc:
                self._send_json(400, {"error": _config_validation_message(exc)})
                return

            saved = save_config(info.config_path, config)
            self._send_json(
                200,
                {
                    "saved": True,
                    "restart_required": False,
                    "message": (
                        "Config saved. The daemon picks up changes on its next cycle."
                    ),
                    "config": saved.model_dump(mode="json"),
                },
            )

        def _handle_instance_page(self, path: str) -> None:
            name = unquote(path[len("/instances/") :]).strip("/")
            if not name or "/" in name or get_instance(name) is None:
                self._send_json(404, {"error": "unknown instance"})
                return
            self._send_static(safe_static_path(_INSTANCE_PAGE))

        def _handle_instance_api(self, path: str, query: dict[str, list[str]]) -> None:
            parts = _instance_parts(path)
            name = unquote(parts[0])
            info = self._resolve_instance(name)
            if info is None:
                return
            if len(parts) < 2:
                self._send_not_found()
                return
            endpoint = parts[1]

            if endpoint == "tasks":
                if len(parts) not in (2, 3):
                    self._send_not_found()
                    return
                tasks = self._instance_tasks(info)
                if tasks is None:
                    return
                if len(parts) == 2:
                    self._send_json(
                        200,
                        [_task_payload(t, tasks, info.config) for t in tasks],
                    )
                    return
                task_id = unquote(parts[2])
                for task in tasks:
                    if task.id == task_id:
                        self._send_json(200, _task_payload(task, tasks, info.config))
                        return
                self._send_not_found()
                return
            if len(parts) > 2:
                self._send_not_found()
                return

            if endpoint == "status":
                self._send_json(200, _status_payload(info))
                return
            if endpoint == "logs":
                n = clamp_query_int(query, "lines", DEFAULT_LOG_LINES, MAX_LOG_LINES)
                if info.config is None:
                    self._send_json(200, {"lines": []})
                    return
                lines = tail_lines(Path(info.config.log_file), n)
                self._send_json(200, {"lines": lines})
                return
            if endpoint == "runs":
                limit = clamp_query_int(query, "limit", DEFAULT_RUN_LIMIT, MAX_RUN_LIMIT)
                offset = clamp_query_int(query, "offset", DEFAULT_RUN_OFFSET, MAX_RUN_OFFSET)
                if info.config is None:
                    self._send_json(
                        200,
                        {"runs": [], "total": 0, "offset": offset, "limit": limit},
                    )
                    return
                recorder = RunRecorder(runs_path(info.config))
                records, total = recorder.read_with_total(limit=limit, offset=offset)
                self._send_json(
                    200,
                    {
                        "runs": [r.model_dump(mode="json") for r in records],
                        "total": total,
                        "offset": offset,
                        "limit": limit,
                    },
                )
                return
            if endpoint == "blocker":
                self._send_json(200, {"content": _blocker_content(info.config)})
                return
            if endpoint == "config":
                if info.config is None:
                    self._send_json(200, {"error": "config not available"})
                    return
                self._send_json(200, info.config.model_dump(mode="json"))
                return
            self._send_not_found()

    return CentralRequestHandler


class CentralWebServer:
    """Threading HTTP server lifecycle around the central dashboard."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        token: str | None = None,
    ) -> None:
        self.host = host
        self._port = port
        self._token = token
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int | None:
        """The port actually bound, once started (port 0 picks a free one)."""
        if self._httpd is None:
            return None
        return int(self._httpd.server_address[1])

    def start(self) -> bool:
        """Bind and start serving in a background thread. False on bind failure."""
        handler = make_handler(self._token)
        try:
            httpd = ThreadingHTTPServer((self.host, self._port), handler)
        except OSError as exc:
            logger.error(
                "Central web server failed to bind %s:%s: %s",
                self.host,
                self._port,
                exc,
            )
            return False
        self._httpd = httpd
        thread = threading.Thread(
            target=lambda: httpd.serve_forever(poll_interval=0.05),
            name="forgeo-central-web",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        logger.info(
            "Central web server listening on http://%s:%s",
            self.host,
            httpd.server_address[1],
        )
        return True

    def stop(self) -> None:
        """Stop the server and join the serve thread."""
        httpd = self._httpd
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._httpd = None
        self._thread = None
        logger.info("Central web server stopped.")


async def _serve_forever(
    server: CentralWebServer, host: str, stop_requested: threading.Event
) -> None:
    """Run the foreground server until SIGINT/SIGTERM, then stop it."""
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal() -> None:
        stop_requested.set()
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass
    if stop_requested.is_set():
        stop_event.set()
    Console(stderr=True).print(
        Panel.fit(
            f"[bold]Forgeo central dashboard[/bold]\n"
            f"[bold]Listening:[/bold] http://{host}:{server.port}\n"
            f"[bold]Instances:[/bold] {len(list_instances())} registered "
            f"(registry: {registry_path()})",
            title="Forgeo Web",
            border_style="green",
        )
    )
    await stop_event.wait()


def stop_web(timeout: float = WEB_STOP_TIMEOUT_SECONDS) -> None:
    """SIGTERM the running dashboard and wait for its lock to drop.

    Raises:
        WebLockError: When the dashboard is not running, records no live PID,
            cannot be signalled, or does not exit within ``timeout``.
    """
    lock = WebLock()
    if not lock.is_held():
        raise WebLockError("Central dashboard is not running.")
    pid = lock.pid
    if pid is None:
        raise WebLockError(
            f"The lock file {lock.lock_path} records no PID; find the dashboard "
            f"with `pgrep -af forgeo` and stop it manually."
        )
    daemon_control.signal_and_wait_for_release(
        pid,
        name="central dashboard",
        is_held=lock.is_held,
        timeout=timeout,
        error_cls=WebLockError,
    )
    lock.release()


def start_web_detached(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = WEB_START_TIMEOUT_SECONDS,
) -> int:
    """Launch the dashboard as a detached background process.

    The child is a plain foreground ``forgeo web`` (same ``Popen`` +
    ``start_new_session`` pattern as ``forgeo restart``), so it re-does the
    lock handling and binds the socket itself; this returns once the lock
    reports a live PID, i.e. the server has bound successfully.

    Raises:
        WebLockError: When another live dashboard already holds the lock, or
            the server fails to start within ``timeout``.
    """
    lock = WebLock()
    if lock.is_held():
        raise WebLockError(
            f"Another central dashboard is already running (pid {lock.pid})."
        )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "forgeo",
            "web",
            "--host",
            host,
            "--port",
            str(port),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid = daemon_control.wait_for_process_ready(
        proc, timeout, is_ready=lock.is_held, ready_pid=lambda: lock.pid
    )
    if pid is None:
        raise WebLockError(
            f"Central dashboard did not start within {timeout:.0f}s; "
            f"check the process output for bind errors."
        )
    logger.info("Central web server started (pid %s).", pid)
    return pid


def run_foreground(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, token: Any = None
) -> int:
    """Start ``forgeo web`` in the foreground; returns the process exit code.

    Takes the host-global dashboard lock (refusing while another dashboard
    runs), binds the dashboard, prints the listening banner to stderr, and
    blocks until the user interrupts it with Ctrl-C or a SIGTERM arrives.
    The lock is always released on the way out — even after a failed bind.

    ``token`` is the ``--token`` CLI value (or the
    :data:`AUTOGENERATE_TOKEN` sentinel); it resolves to the effective bearer
    token via :func:`resolve_web_token`. When a fresh token was generated it
    is printed to stderr exactly once, with a note about where it lives.
    """
    # Catch SIGINT/SIGTERM before the asyncio loop is up, so a stop signal
    # arriving in the startup window still leads to a clean shutdown with the
    # lock released — never an abrupt exit that orphans the lock file.
    stop_requested = threading.Event()

    def _request_stop(*_args: object) -> None:
        stop_requested.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError):
            pass
    lock = WebLock()
    try:
        lock.acquire(host=host, port=port)
    except WebLockError as exc:
        Console(stderr=True).print(f"[red]{exc}[/red]")
        return 1
    token, generated = resolve_web_token(token)
    server = CentralWebServer(host=host, port=port, token=token)
    if not server.start():
        Console(stderr=True).print(
            f"[red]Central dashboard failed to bind {host}:{port}.[/red]"
        )
        lock.release()
        return 1
    if generated:
        Console(stderr=True).print(
            f"[bold]Web token:[/bold] {token}\n"
            f"[dim]Required on every /api/* request; saved to {web_token_path()}.[/dim]"
        )
    try:
        asyncio.run(_serve_forever(server, host, stop_requested))
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        lock.release()
    return 0
