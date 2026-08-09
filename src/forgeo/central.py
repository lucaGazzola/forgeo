"""Central multi-instance web dashboard (``forgeo web``).

A standalone server that aggregates every forgeo registered in the instance
registry (:mod:`forgeo.instances`). It reads each instance's data straight
from its files (``backlog.json``, ``runs.jsonl``, ``forgeo.log``,
``BLOCKER.md``, ``daemon.state.json``), so it works whether or not that
instance's daemon is running — the daemon binds no ports at all.

Routes:

* ``GET /`` — home page listing every registered instance: name, repo,
  daemon state, last outcome, next run, and backlog counts.
* ``GET /instances/<name>/`` — per-instance page: that instance's kanban
  backlog (with a form to add tasks) plus tabs for logs, runs, blocker, and
  config.
* ``GET /api/instances`` — JSON summary of every registered instance.
* ``GET /api/instances/<name>/tasks``, ``/tasks/<id>``, ``/status``,
  ``/logs?lines=N``, ``/runs?limit=N``, ``/blocker``, ``/config`` — the
  per-instance API.
* ``PUT /api/instances/<name>/config`` — validate and persist an instance's
  ``forgeo.yaml`` from a config payload (applies on the daemon's next
  restart; ``name`` and ``telegram_bot_token`` are not editable).
* ``POST /api/instances/<name>/tasks`` — add a new task to that instance's
  backlog.
* ``POST /api/instances/<name>/tasks/<id>/reopen`` — reopen a ``BLOCKED``
  task (status back to ``OPEN``, blocker reason cleared).
* ``PATCH /api/instances/<name>/tasks/<id>`` — update an existing task's
  editable fields (title, description, acceptance criteria, dependencies,
  files to modify, agent command, agent timeout).
* ``DELETE /api/instances/<name>/tasks/<id>`` — delete an ``OPEN`` or
  ``BLOCKED`` task from that instance's backlog.

An unknown instance name returns ``404``; a registered instance with missing
data files renders with empty data and ``daemon_running=false`` rather than
erroring.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import signal
import threading
from collections.abc import Callable
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel

from forgeo.backlog import JSONBacklog, backlog_status_counts
from forgeo.config import save_config
from forgeo.daemon import read_lock_pid
from forgeo.instances import (
    InstanceInfo,
    get_instance,
    list_instances,
    registry_path,
)
from forgeo.models import ForgeoConfig, Task, TaskStatus
from forgeo.runs import RunRecorder, runs_path_for
from forgeo.web_common import (
    DEFAULT_LOG_LINES,
    DEFAULT_RUN_LIMIT,
    MAX_LOG_LINES,
    MAX_RUN_LIMIT,
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

_HOME_PAGE = "/central/index.html"
_INSTANCE_PAGE = "/central/instance.html"

_WEB_TASK_ID_RE = re.compile(r"^WEB-(\d+)$")


def web_task_id_for(tasks: list[Task]) -> str:
    """Next ``WEB-###`` id after the highest existing ``WEB-###`` id."""
    highest = 0
    for task in tasks:
        match = _WEB_TASK_ID_RE.match(task.id)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"WEB-{highest + 1:03d}"


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
    return _read_json_dict(Path(config.backlog).with_suffix(".state.json"))


def _read_tasks(config: ForgeoConfig | None) -> list[Task]:
    """All tasks for ``config``, tolerating a missing or corrupt backlog.

    Reads the file directly so the dashboard never writes to an instance's
    files (``JSONBacklog`` renames a corrupt backlog; here it is skipped).
    """
    if config is None:
        return []
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
    last_run = RunRecorder(runs_path_for(config.backlog)).read_last()
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
    last_run = RunRecorder(runs_path_for(config.backlog)).read_last()
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
    pid: int | None = read_lock_pid(config.backlog.with_suffix(".lock"))
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
        }
    counts = backlog_status_counts(_read_tasks(config))
    return {
        "name": info.name,
        "config_path": str(info.config_path),
        "repo": str(config.repo),
        "daemon_running": info.daemon_running,
        "last_outcome": _last_outcome(config),
        "next_run_at": _next_run(info, config),
        "backlog_counts": counts,
    }


def make_handler() -> type[BaseHTTPRequestHandler]:
    """Build the request-handler class for the central dashboard."""

    class CentralRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("central web %s - %s", self.address_string(), format % args)

        def _send_json(self, status: int, payload: Any) -> None:
            body = json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, static: Path | None) -> None:
            if static is None:
                self._send_json(404, {"error": "not found"})
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

        def do_GET(self) -> None:
            self._run_safely(self._do_get)

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
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            self._run_safely(self._do_post)

        def _do_post(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path.startswith("/api/instances/"):
                self._post_instance_api(path)
                return
            self._send_json(404, {"error": "not found"})

        def do_PATCH(self) -> None:
            self._run_safely(self._do_patch)

        def _do_patch(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path.startswith("/api/instances/"):
                self._patch_instance_task(path)
                return
            self._send_json(404, {"error": "not found"})

        def do_DELETE(self) -> None:
            self._run_safely(self._do_delete)

        def _do_delete(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path.startswith("/api/instances/"):
                self._delete_instance_task(path)
                return
            self._send_json(404, {"error": "not found"})

        def do_PUT(self) -> None:
            self._run_safely(self._do_put)

        def _do_put(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path.startswith("/api/instances/"):
                self._put_instance_api(path)
                return
            self._send_json(404, {"error": "not found"})

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
            parts = path[len("/api/instances/") :].split("/")
            name = unquote(parts[0])
            info = get_instance(name)
            if info is None:
                self._send_json(404, {"error": "unknown instance"})
                return None
            expected = 3 if with_task_id else 2
            if len(parts) != expected or parts[1] != "tasks":
                self._send_json(404, {"error": "not found"})
                return None
            if info.config is None:
                self._send_json(500, {"error": "instance config not available"})
                return None
            task_id = unquote(parts[2]) if with_task_id else None
            return info.config, task_id

        def _post_instance_api(self, path: str) -> None:
            """Route a POST under ``/api/instances/`` to its handler."""
            parts = path[len("/api/instances/") :].split("/")
            if len(parts) < 2 or parts[1] != "tasks":
                self._send_json(404, {"error": "not found"})
                return
            if len(parts) == 2:
                self._post_instance_task(path)
                return
            if len(parts) == 4 and parts[3] == "reopen":
                self._reopen_instance_task(path)
                return
            self._send_json(404, {"error": "not found"})

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

            backlog = JSONBacklog(config.backlog)
            existing = asyncio.run(backlog.list_tasks())
            task = Task(
                id=web_task_id_for(existing),
                title=title.strip(),
                description=description.strip(),
                acceptance_criteria=acceptance_criteria,
                agent_command=agent_command.strip() if agent_command else None,
            )
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

            backlog = JSONBacklog(config.backlog)
            try:
                updated = asyncio.run(backlog.update_task(task_id, payload))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            if updated is None:
                self._send_json(404, {"error": "not found"})
                return
            self._send_json(200, updated.model_dump(mode="json"))

        def _reopen_instance_task(self, path: str) -> None:
            """Reopen a BLOCKED task: status back to OPEN, reason cleared.

            A dedicated endpoint rather than a generic ``status`` via PATCH,
            so the status transition stays outside the editable-fields model.
            """
            parts = path[len("/api/instances/") :].split("/")
            if len(parts) != 4 or parts[1] != "tasks" or parts[3] != "reopen":
                self._send_json(404, {"error": "not found"})
                return
            name = unquote(parts[0])
            info = get_instance(name)
            if info is None:
                self._send_json(404, {"error": "unknown instance"})
                return
            if info.config is None:
                self._send_json(500, {"error": "instance config not available"})
                return
            task_id = unquote(parts[2])

            backlog = JSONBacklog(info.config.backlog)
            task = asyncio.run(backlog.get_task(task_id))
            if task is None:
                self._send_json(404, {"error": "not found"})
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

            backlog = JSONBacklog(config.backlog)
            task = asyncio.run(backlog.get_task(task_id))
            if task is None:
                self._send_json(404, {"error": "not found"})
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
            parts = path[len("/api/instances/") :].split("/")
            if len(parts) != 2 or parts[1] != "config":
                self._send_json(404, {"error": "not found"})
                return
            self._put_instance_config(path)

        def _put_instance_config(self, path: str) -> None:
            """Validate and persist an instance's ``forgeo.yaml`` from a body.

            Accepts the same shape ``GET /api/instances/<name>/config``
            returns. The config is validated against :class:`ForgeoConfig`
            and written to the instance's ``forgeo.yaml`` atomically; the
            response carries the reloaded config and an explicit note that
            the daemon picks the changes up only on its next restart (a save
            never restarts the daemon).

            ``name`` is owned by the registry and forced to the registered
            instance name (a different value is rejected); ``telegram_bot_token``
            is not editable through the web console — an explicit change is
            rejected and the current value is preserved when the field is
            omitted.
            """
            parts = path[len("/api/instances/") :].split("/")
            name = unquote(parts[0])
            info = get_instance(name)
            if info is None:
                self._send_json(404, {"error": "unknown instance"})
                return
            if info.config is None:
                self._send_json(500, {"error": "instance config not available"})
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
                if payload["telegram_bot_token"] != info.config.telegram_bot_token:
                    self._send_json(
                        400,
                        {"error": "telegram_bot_token is not editable through the web console"},
                    )
                    return
            else:
                payload["telegram_bot_token"] = info.config.telegram_bot_token

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
                    "restart_required": True,
                    "message": (
                        "Config saved. The daemon picks up changes on its next restart."
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
            parts = path[len("/api/instances/") :].split("/")
            name = unquote(parts[0])
            info = get_instance(name)
            if info is None:
                self._send_json(404, {"error": "unknown instance"})
                return
            if len(parts) < 2:
                self._send_json(404, {"error": "not found"})
                return
            endpoint = parts[1]

            if endpoint == "tasks":
                if len(parts) == 2:
                    tasks = _read_tasks(info.config)
                    self._send_json(200, [t.model_dump(mode="json") for t in tasks])
                    return
                if len(parts) == 3:
                    task_id = unquote(parts[2])
                    for task in _read_tasks(info.config):
                        if task.id == task_id:
                            self._send_json(200, task.model_dump(mode="json"))
                            return
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_json(404, {"error": "not found"})
                return
            if len(parts) > 2:
                self._send_json(404, {"error": "not found"})
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
                n = clamp_query_int(query, "limit", DEFAULT_RUN_LIMIT, MAX_RUN_LIMIT)
                if info.config is None:
                    self._send_json(200, [])
                    return
                records = RunRecorder(runs_path_for(info.config.backlog)).read(limit=n)
                self._send_json(200, [r.model_dump(mode="json") for r in records])
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
            self._send_json(404, {"error": "not found"})

    return CentralRequestHandler


class CentralWebServer:
    """Threading HTTP server lifecycle around the central dashboard."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self._port = port
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
        handler = make_handler()
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
            target=httpd.serve_forever,
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


def _instance_count() -> int:
    """The number of registered instances (used by the CLI banner)."""
    return len(list_instances())


async def _serve_forever(server: CentralWebServer, host: str) -> None:
    """Run the foreground server until SIGINT/SIGTERM, then stop it."""
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    Console().print(
        Panel.fit(
            f"[bold]Forgeo central dashboard[/bold]\n"
            f"[bold]Listening:[/bold] http://{host}:{server.port}\n"
            f"[bold]Instances:[/bold] {_instance_count()} registered "
            f"(registry: {registry_path()})",
            title="Forgeo Web",
            border_style="green",
        )
    )
    await stop_event.wait()


def run_foreground(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    """Start ``forgeo web`` in the foreground; returns the process exit code.

    Binds the dashboard, prints the listening banner, and blocks until the
    user interrupts it with Ctrl-C or a SIGTERM arrives.
    """
    server = CentralWebServer(host=host, port=port)
    if not server.start():
        Console().print(f"[red]Central dashboard failed to bind {host}:{port}.[/red]")
        return 1
    try:
        asyncio.run(_serve_forever(server, host))
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0
