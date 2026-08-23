"""Tests for the central multi-instance web dashboard (``forgeo web``)."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from forgeo.central import CentralWebServer, web_task_id_for
from forgeo.cli import build_parser, cmd_web
from forgeo.config import load_config
from forgeo.daemon import acquire_run_lock, is_lock_held
from forgeo.daemon_control import DaemonError
from forgeo.instances import add_instance
from forgeo.models import RunKind, RunOutcome, RunRecord, TaskStatus
from forgeo.runs import RunRecorder
from tests.conftest import BacklogServer, make_task

FINISHED = datetime(2026, 8, 1, 1, 0, 10, tzinfo=UTC)


def _get(url: str) -> tuple[int, dict | list | str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            ctype = resp.headers.get_content_type()
            if ctype == "application/json":
                return resp.status, json.loads(body)
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def _get_headers(
    url: str, headers: dict[str, str]
) -> tuple[int, dict | list | str]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            ctype = resp.headers.get_content_type()
            if ctype == "application/json":
                return resp.status, json.loads(body)
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def _post(url: str, data: str | None) -> tuple[int, dict | list | str]:
    body = data.encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, method="POST")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            resp_body = resp.read().decode("utf-8")
            ctype = resp.headers.get_content_type()
            if ctype == "application/json":
                return resp.status, json.loads(resp_body)
            return resp.status, resp_body
    except urllib.error.HTTPError as exc:
        resp_body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(resp_body)
        except json.JSONDecodeError:
            return exc.code, resp_body


def _patch(url: str, data: str | None) -> tuple[int, dict | list | str]:
    body = data.encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, method="PATCH")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            resp_body = resp.read().decode("utf-8")
            ctype = resp.headers.get_content_type()
            if ctype == "application/json":
                return resp.status, json.loads(resp_body)
            return resp.status, resp_body
    except urllib.error.HTTPError as exc:
        resp_body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(resp_body)
        except json.JSONDecodeError:
            return exc.code, resp_body


def _delete(url: str) -> tuple[int, dict | list | str]:
    request = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            resp_body = resp.read().decode("utf-8")
            ctype = resp.headers.get_content_type()
            if ctype == "application/json":
                return resp.status, json.loads(resp_body)
            return resp.status, resp_body
    except urllib.error.HTTPError as exc:
        resp_body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(resp_body)
        except json.JSONDecodeError:
            return exc.code, resp_body


def _put(url: str, data: str | None) -> tuple[int, dict | list | str]:
    body = data.encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, method="PUT")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            resp_body = resp.read().decode("utf-8")
            ctype = resp.headers.get_content_type()
            if ctype == "application/json":
                return resp.status, json.loads(resp_body)
            return resp.status, resp_body
    except urllib.error.HTTPError as exc:
        resp_body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(resp_body)
        except json.JSONDecodeError:
            return exc.code, resp_body


def task_json(task_id: str, title: str, status: TaskStatus) -> dict:
    return make_task(
        id=task_id,
        title=title,
        status=status,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    ).model_dump(mode="json")


def run_record(task_id: str, outcome: RunOutcome) -> RunRecord:
    return RunRecord(
        started_at=datetime(2026, 8, 1, 1, 0, tzinfo=UTC),
        finished_at=FINISHED,
        kind=RunKind.TASK,
        task_id=task_id,
        task_title="Do the thing",
        outcome=outcome,
        agent_exit_code=0,
        duration_seconds=5.0,
    )


def write_instance(
    tmp_path: Path,
    name: str,
    *,
    repo: str,
    tasks: list[dict] | None = None,
    log_lines: list[str] | None = None,
    runs: list[RunRecord] | None = None,
    blocker: str | None = None,
) -> tuple[Path, Path]:
    """Create a registered instance in ``tmp_path/<name>``; returns the dir
    and config path. Only the files passed are written, so omitting them
    yields an instance with missing data files."""
    config_dir = tmp_path / name
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "forgeo.yaml"
    backlog = config_dir / "backlog.json"
    config_path.write_text(
        f"name: {name}\n"
        f"repo: {repo}\n"
        f"backlog: {backlog}\n"
        f"blocker_file: {config_dir / 'BLOCKER.md'}\n"
        f"agent_command: echo hi\n"
        f"log_file: {config_dir / 'forgeo.log'}\n"
        f"interval_minutes: 30\n",
        encoding="utf-8",
    )
    if tasks is not None:
        backlog.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    if log_lines:
        (config_dir / "forgeo.log").write_text(
            "\n".join(log_lines) + "\n", encoding="utf-8"
        )
    if runs:
        recorder = RunRecorder(config_dir / "runs.jsonl")
        for record in runs:
            recorder.append(record)
    if blocker is not None:
        (config_dir / "BLOCKER.md").write_text(blocker, encoding="utf-8")
    add_instance(name, config_path)
    return config_dir, config_path


def write_daemon_instance(
    tmp_path: Path,
    name: str,
    *,
    repo: str,
    interval_minutes: int = 600,
) -> Path:
    """Register an instance whose daemon can actually run; returns the config
    path. The long interval keeps the spawned daemon idle between cycles."""
    config_dir = tmp_path / name
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "forgeo.yaml"
    config_path.write_text(
        f"name: {name}\n"
        f"repo: {repo}\n"
        f"backlog: {config_dir / 'backlog.json'}\n"
        f"blocker_file: {config_dir / 'BLOCKER.md'}\n"
        f"agent_command: echo hi\n"
        f"log_file: {config_dir / 'forgeo.log'}\n"
        f"interval_minutes: {interval_minutes}\n",
        encoding="utf-8",
    )
    add_instance(name, config_path)
    return config_path


def spawn_daemon(config_path: Path) -> subprocess.Popen[bytes]:
    """Start a real ``forgeo start`` subprocess, detached like restart does."""
    return subprocess.Popen(
        [sys.executable, "-m", "forgeo", "start", "--config", str(config_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def wait_for(predicate: Callable[[], bool], timeout: float = 15.0) -> bool:
    """Poll ``predicate`` until it holds; False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    return tmp_path


@pytest.fixture
def central_server():
    server = CentralWebServer(host="127.0.0.1", port=0)
    assert server.start() is True
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def web_env(registry, central_server):
    """Two registered instances: ``alpha`` (with data files) and ``beta``."""
    write_instance(
        registry,
        "alpha",
        repo=str(registry / "repos" / "alpha"),
        tasks=[
            task_json("TASK-001", "First", TaskStatus.OPEN),
            task_json("TASK-002", "Done", TaskStatus.COMPLETED),
        ],
        log_lines=[
            "2026-08-01 01:00:00 INFO     forgeo.daemon: Run finished: task",
            "trailing line",
        ],
        runs=[run_record("TASK-001", RunOutcome.SUCCESS)],
        blocker="# Blocker\nPlease decide.\n",
    )
    write_instance(
        registry,
        "beta",
        repo=str(registry / "repos" / "beta"),
        tasks=[task_json("B-1", "Beta task", TaskStatus.OPEN)],
    )
    return central_server, registry


def test_home_page_served(web_env):
    server, _ = web_env
    status, body = _get(f"http://127.0.0.1:{server.port}/")
    assert status == 200
    assert isinstance(body, str)
    assert "<!doctype html" in body.lower()
    assert 'href="/style.css"' in body
    assert 'src="/central/central.js"' in body


def test_home_page_lists_registered_instances_via_api(web_env):
    server, _ = web_env
    status, data = _get(f"http://127.0.0.1:{server.port}/api/instances")
    assert status == 200
    assert isinstance(data, list)
    names = [entry["name"] for entry in data]
    assert names == ["alpha", "beta"]
    alpha = data[0]
    assert alpha["repo"].endswith("repos/alpha")
    assert alpha["daemon_running"] is False
    assert alpha["last_outcome"] == "SUCCESS"
    assert alpha["backlog_counts"] == {
        "OPEN": 1,
        "BLOCKED": 0,
        "COMPLETED": 1,
        "FAILED": 0,
    }


def test_instance_page_served(web_env):
    server, _ = web_env
    status, body = _get(f"http://127.0.0.1:{server.port}/instances/alpha/")
    assert status == 200
    assert isinstance(body, str)
    assert "Backlog" in body


# --------------------------------------------------------------------------- #
# Optional bearer-token auth                                                  #
# --------------------------------------------------------------------------- #


def _auth_server(token: str | None = None) -> CentralWebServer:
    server = CentralWebServer(host="127.0.0.1", port=0, token=token)
    assert server.start() is True
    return server


def test_auth_disabled_without_token_serves_api(registry):
    server = _auth_server(token=None)
    try:
        status, data = _get(f"http://127.0.0.1:{server.port}/api/instances")
        assert status == 200
        assert isinstance(data, list)
    finally:
        server.stop()


def test_auth_requires_token_on_api(registry):
    server = _auth_server(token="secret")
    try:
        status, data = _get(f"http://127.0.0.1:{server.port}/api/instances")
        assert status == 401
        assert data["error"] == "unauthorized"
    finally:
        server.stop()


def test_auth_rejects_wrong_token(registry):
    server = _auth_server(token="secret")
    try:
        status, data = _get_headers(
            f"http://127.0.0.1:{server.port}/api/instances",
            {"Authorization": "Bearer wrong"},
        )
        assert status == 401
        assert data["error"] == "unauthorized"
    finally:
        server.stop()


def test_auth_accepts_valid_token(registry):
    server = _auth_server(token="secret")
    try:
        status, data = _get_headers(
            f"http://127.0.0.1:{server.port}/api/instances",
            {"Authorization": "Bearer secret"},
        )
        assert status == 200
        assert isinstance(data, list)
    finally:
        server.stop()


def test_auth_applies_to_write_endpoints(registry):
    server = _auth_server(token="secret")
    try:
        status, _data = _post(
            f"http://127.0.0.1:{server.port}/api/instances/alpha/start", None
        )
        assert status == 401
        status, _data = _put(
            f"http://127.0.0.1:{server.port}/api/instances/alpha/config", "{}"
        )
        assert status == 401
        status, _data = _patch(
            f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001",
            "{}",
        )
        assert status == 401
        status, _data = _delete(
            f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001"
        )
        assert status == 401
    finally:
        server.stop()


def test_auth_leaves_static_and_login_unprotected(registry):
    server = _auth_server(token="secret")
    try:
        status, body = _get(f"http://127.0.0.1:{server.port}/")
        assert status == 200
        status, body = _get(f"http://127.0.0.1:{server.port}/central/login.html")
        assert status == 200
        assert isinstance(body, str)
        assert "Token required" in body
        status, body = _get(f"http://127.0.0.1:{server.port}/central/central.js")
        assert status == 200
        status, body = _get(f"http://127.0.0.1:{server.port}/central/central.css")
        assert status == 200
    finally:
        server.stop()


def test_instance_page_has_new_task_form(web_env):
    server, _ = web_env
    status, body = _get(f"http://127.0.0.1:{server.port}/instances/alpha/")
    assert status == 200
    assert 'data-tab="create"' in body
    assert 'id="new-task"' in body
    assert 'id="task-title"' in body
    backlog_panel = body.split('<main id="tab-backlog"')[1].split("</main>")[0]
    assert "new-task" not in backlog_panel
    create_panel = body.split('<main id="tab-create"')[1].split("</main>")[0]
    assert 'id="new-task"' in create_panel


def test_instance_page_has_daemon_controls(web_env):
    server, _ = web_env
    status, body = _get(f"http://127.0.0.1:{server.port}/instances/alpha/")
    assert status == 200
    assert 'id="meta-daemon"' in body
    assert 'id="daemon-start"' in body
    assert 'id="daemon-stop"' in body
    assert 'id="daemon-restart"' in body
    assert 'id="daemon-feedback"' in body


def test_instance_page_has_task_edit_modal(web_env):
    server, _ = web_env
    status, body = _get(f"http://127.0.0.1:{server.port}/instances/alpha/")
    assert status == 200
    assert 'id="task-modal-edit"' in body
    assert 'id="task-modal-reopen"' in body
    assert 'id="task-modal-blocker-section"' in body
    assert 'id="task-modal-failure-section"' in body
    assert 'id="task-modal-agent-response-section"' in body
    assert 'id="task-modal-delete"' in body
    assert 'id="task-modal-edit-form"' in body
    assert 'id="task-modal-save"' in body
    assert 'id="task-modal-cancel"' in body
    assert 'id="task-edit-title"' in body
    assert 'id="task-edit-timeout"' in body


def test_unknown_instance_returns_404(web_env):
    server, _ = web_env
    status, data = _get(f"http://127.0.0.1:{server.port}/instances/nope/")
    assert status == 404
    assert data["error"] == "unknown instance"

    status, data = _get(f"http://127.0.0.1:{server.port}/api/instances/nope/tasks")
    assert status == 404
    assert data["error"] == "unknown instance"


def test_status_reads_files_without_daemon(web_env):
    server, _ = web_env
    status, data = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/status")
    assert status == 200
    assert data["name"] == "alpha"
    assert data["daemon_running"] is False
    assert data["pid"] is None
    assert data["interval_minutes"] == 30
    assert data["last_outcome"] == "SUCCESS"
    assert data["next_run_at"] is None


def test_status_reports_running_daemon_and_next_run(web_env):
    server, registry = web_env
    lock = acquire_run_lock(registry / "alpha" / "backlog.lock")
    assert lock is not None
    try:
        status, data = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/status")
        assert status == 200
        assert data["daemon_running"] is True
        assert data["pid"] is not None
        assert data["next_run_at"] == "2026-08-01T01:30:10+00:00"
    finally:
        lock.close()


def test_status_prefers_daemon_state_file(web_env):
    """``next_run_at``/``last_outcome``/``pid`` come from daemon.state.json
    when present, even without a lock held."""
    server, registry = web_env
    state_path = registry / "alpha" / "backlog.state.json"
    state_path.write_text(
        json.dumps(
            {
                "pid": 4242,
                "started_at": "2026-08-01T01:00:00+00:00",
                "last_outcome": "task",
                "next_run_at": "2026-08-01T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    status, data = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/status")
    assert status == 200
    assert data["pid"] == 4242
    assert data["last_outcome"] == "task"
    assert data["next_run_at"] is None  # daemon not running: no schedule
    assert data["daemon_running"] is False


def test_status_daemon_state_file_next_run(web_env):
    server, registry = web_env
    state_path = registry / "alpha" / "backlog.state.json"
    state_path.write_text(
        json.dumps(
            {
                "pid": 4242,
                "started_at": "2026-08-01T01:00:00+00:00",
                "last_outcome": "task",
                "next_run_at": "2026-08-01T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    lock = acquire_run_lock(registry / "alpha" / "backlog.lock")
    assert lock is not None
    try:
        status, data = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/status")
        assert status == 200
        assert data["daemon_running"] is True
        assert data["pid"] == 4242
        assert data["next_run_at"] == "2026-08-01T12:00:00+00:00"
        assert data["last_outcome"] == "task"
    finally:
        lock.close()


def test_tasks_endpoints(web_env):
    server, _ = web_env
    status, data = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks")
    assert status == 200
    assert [task["id"] for task in data] == ["TASK-001", "TASK-002"]

    status, data = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-002")
    assert status == 200
    assert data["title"] == "Done"

    status, data = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/MISSING")
    assert status == 404
    assert data["error"] == "not found"


def test_task_detail_reports_unsatisfied_dependencies(web_env):
    server, registry = web_env
    write_instance(
        registry,
        "gamma",
        repo=str(registry / "repos" / "gamma"),
        tasks=[
            make_task(
                id="T1", title="Done dep", status=TaskStatus.COMPLETED
            ).model_dump(mode="json"),
            make_task(
                id="T2", title="Pending dep", status=TaskStatus.OPEN
            ).model_dump(mode="json"),
            make_task(
                id="T3",
                title="Waits",
                status=TaskStatus.OPEN,
                dependencies=["T1", "T2", "GHOST"],
            ).model_dump(mode="json"),
        ],
    )
    base = f"http://127.0.0.1:{server.port}/api/instances/gamma/tasks"

    status, data = _get(f"{base}/T3")
    assert status == 200
    assert data["unsatisfied_dependencies"] == [
        {"id": "T2", "status": "OPEN"},
        {"id": "GHOST", "status": "missing"},
    ]

    status, data = _get(base)
    assert status == 200
    by_id = {task["id"]: task for task in data}
    assert by_id["T3"]["unsatisfied_dependencies"] == [
        {"id": "T2", "status": "OPEN"},
        {"id": "GHOST", "status": "missing"},
    ]
    assert by_id["T1"]["unsatisfied_dependencies"] == []
    assert by_id["T2"]["unsatisfied_dependencies"] == []


def test_post_task_creates_in_backlog(web_env):
    server, _ = web_env
    base = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks"
    status, data = _post(
        base, json.dumps({"title": "Build a thing", "description": "Do it."})
    )
    assert status == 201
    assert isinstance(data, dict)
    assert data["id"] == "WEB-001"
    assert data["title"] == "Build a thing"
    assert data["description"] == "Do it."
    assert data["acceptance_criteria"] == []
    assert data["status"] == "OPEN"

    status, tasks = _get(base)
    assert status == 200
    assert [t["id"] for t in tasks] == ["TASK-001", "TASK-002", "WEB-001"]

    status, tasks = _get(f"http://127.0.0.1:{server.port}/api/instances/beta/tasks")
    assert status == 200
    assert [t["id"] for t in tasks] == ["B-1"]


def test_post_task_increments_web_ids(web_env):
    server, _ = web_env
    base = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks"
    _, first = _post(base, json.dumps({"title": "One", "description": "A."}))
    _, second = _post(base, json.dumps({"title": "Two", "description": "B."}))
    assert first["id"] == "WEB-001"
    assert second["id"] == "WEB-002"


def test_post_task_includes_optional_fields(web_env):
    server, _ = web_env
    base = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks"
    status, data = _post(
        base,
        json.dumps(
            {
                "title": "  Refactor the cache  ",
                "description": "  Make it faster.  ",
                "acceptance_criteria": ["no regressions", "tests pass"],
                "agent_command": 'claude -p "$FORGEO_TASK" --model haiku',
            }
        ),
    )
    assert status == 201
    assert data["title"] == "Refactor the cache"
    assert data["description"] == "Make it faster."
    assert data["acceptance_criteria"] == ["no regressions", "tests pass"]
    assert data["agent_command"] == 'claude -p "$FORGEO_TASK" --model haiku'
    assert data["created_at"]
    assert data["updated_at"]


def test_post_task_accepts_run_at(web_env):
    server, _ = web_env
    base = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks"
    run_at = "2026-08-20T12:30:00Z"
    status, data = _post(
        base,
        json.dumps({"title": "After deploy", "description": "Run it.", "run_at": run_at}),
    )
    assert status == 201
    assert data["run_at"] == run_at

    status, tasks = _get(base)
    assert status == 200
    created = next(t for t in tasks if t["id"] == "WEB-001")
    assert created["run_at"] == run_at

    disk = json.loads(
        (web_env[1] / "alpha" / "backlog.json").read_text(encoding="utf-8")
    )
    entry = next(t for t in disk["tasks"] if t["id"] == "WEB-001")
    assert entry["run_at"] == run_at


def test_post_task_accepts_null_run_at(web_env):
    server, _ = web_env
    base = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks"
    status, data = _post(
        base,
        json.dumps({"title": "Plain", "description": "No schedule.", "run_at": None}),
    )
    assert status == 201
    assert data["run_at"] is None


def test_post_task_rejects_invalid_run_at(web_env):
    server, _ = web_env
    base = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks"
    for payload in (
        {"title": "x", "description": "y", "run_at": 42},
        {"title": "x", "description": "y", "run_at": ["2026-08-20T12:30:00Z"]},
        {"title": "x", "description": "y", "run_at": "not-a-datetime"},
    ):
        status, data = _post(base, json.dumps(payload))
        assert status == 400, payload
        assert data["error"]


def test_post_task_validation_errors(web_env):
    server, _ = web_env
    base = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks"

    for payload in ({}, {"title": "   "}, {"title": 42}):
        status, data = _post(base, json.dumps(payload))
        assert status == 400
        assert data["error"]

    for payload in (
        {"title": "x"},
        {"title": "x", "description": "   "},
        {"title": "x", "description": ""},
    ):
        status, data = _post(base, json.dumps(payload))
        assert status == 400
        assert data["error"] == "description is required"

    status, data = _post(base, "{not json")
    assert status == 400
    assert data["error"]

    status, data = _post(base, "[1, 2]")
    assert status == 400
    assert data["error"]

    status, data = _post(base, None)
    assert status == 400
    assert data["error"]

    status, data = _post(
        base, json.dumps({"title": "x", "description": 1})
    )
    assert status == 400
    assert data["error"]

    status, data = _post(
        base, json.dumps({"title": "x", "description": "y", "acceptance_criteria": "nope"})
    )
    assert status == 400
    assert data["error"]

    status, data = _post(
        base,
        json.dumps({"title": "x", "description": "y", "agent_command": 42}),
    )
    assert status == 400
    assert data["error"]

    status, data = _post(
        base, json.dumps({"title": "x", "description": "y", "agent_command": ""})
    )
    assert status == 400
    assert data["error"]


def test_post_task_unknown_instance_404(web_env):
    server, _ = web_env
    status, data = _post(
        f"http://127.0.0.1:{server.port}/api/instances/nope/tasks",
        json.dumps({"title": "x"}),
    )
    assert status == 404
    assert data["error"] == "unknown instance"


def test_post_task_wrong_path_404(web_env):
    server, _ = web_env
    status, data = _post(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/bogus",
        json.dumps({"title": "x"}),
    )
    assert status == 404
    assert data["error"] == "not found"


def test_post_task_id_collision_409(web_env, monkeypatch):
    import forgeo.central as central_module

    server, _ = web_env
    monkeypatch.setattr(central_module, "web_task_id_for", lambda tasks: "TASK-001")
    status, data = _post(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks",
        json.dumps({"title": "Duplicate", "description": "x"}),
    )
    assert status == 409
    assert data["error"]


def test_post_task_does_not_leak_failed_task(web_env, monkeypatch):
    import forgeo.central as central_module

    server, _ = web_env
    monkeypatch.setattr(central_module, "web_task_id_for", lambda tasks: "TASK-001")
    _post(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks",
        json.dumps({"title": "Duplicate", "description": "x"}),
    )
    status, tasks = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks")
    assert status == 200
    assert [t["id"] for t in tasks] == ["TASK-001", "TASK-002"]


def test_patch_task_updates_fields(web_env):
    server, registry = web_env
    url = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001"
    _, before = _get(url)
    status, data = _patch(
        url,
        json.dumps(
            {
                "title": "Updated first",
                "description": "Now updated.",
                "acceptance_criteria": ["tests pass"],
                "dependencies": ["D-1"],
                "files_to_modify": ["src/app.py"],
                "agent_command": "claude -p",
                "agent_timeout_seconds": 60,
            }
        ),
    )
    assert status == 200
    assert data["id"] == before["id"]
    assert data["status"] == before["status"]
    assert data["created_at"] == before["created_at"]
    assert data["title"] == "Updated first"
    assert data["description"] == "Now updated."
    assert data["acceptance_criteria"] == ["tests pass"]
    assert data["dependencies"] == ["D-1"]
    assert data["files_to_modify"] == ["src/app.py"]
    assert data["agent_command"] == "claude -p"
    assert data["agent_timeout_seconds"] == 60
    assert data["updated_at"] >= before["updated_at"]

    status, tasks = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks")
    assert status == 200
    assert [t["id"] for t in tasks] == ["TASK-001", "TASK-002"]
    updated = next(t for t in tasks if t["id"] == "TASK-001")
    assert updated["title"] == "Updated first"
    assert updated["description"] == "Now updated."

    disk = json.loads(
        (registry / "alpha" / "backlog.json").read_text(encoding="utf-8")
    )
    entry = next(t for t in disk["tasks"] if t["id"] == "TASK-001")
    assert entry["title"] == "Updated first"
    assert entry["status"] == "OPEN"
    assert entry["created_at"] == before["created_at"]


def test_patch_task_partial_update(web_env):
    server, _ = web_env
    url = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-002"
    _, before = _get(url)
    status, data = _patch(url, json.dumps({"description": "Only desc"}))
    assert status == 200
    assert data["title"] == before["title"]
    assert data["description"] == "Only desc"
    assert data["status"] == before["status"]
    assert data["created_at"] == before["created_at"]
    assert data["acceptance_criteria"] == before["acceptance_criteria"]


def test_patch_task_clears_optional_fields(web_env):
    server, _ = web_env
    url = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001"
    status, data = _patch(
        url, json.dumps({"agent_command": None, "agent_timeout_seconds": None})
    )
    assert status == 200
    assert data["agent_command"] is None
    assert data["agent_timeout_seconds"] is None


def test_patch_task_sets_and_clears_run_at(web_env):
    server, _ = web_env
    url = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001"
    run_at = "2026-08-20T12:30:00Z"
    status, data = _patch(url, json.dumps({"run_at": run_at}))
    assert status == 200
    assert data["run_at"] == run_at

    status, tasks = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks")
    assert status == 200
    updated = next(t for t in tasks if t["id"] == "TASK-001")
    assert updated["run_at"] == run_at

    status, data = _patch(url, json.dumps({"run_at": None}))
    assert status == 200
    assert data["run_at"] is None
    status, tasks = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks")
    assert status == 200
    updated = next(t for t in tasks if t["id"] == "TASK-001")
    assert updated["run_at"] is None


def test_patch_task_rejects_invalid_run_at(web_env):
    server, _ = web_env
    url = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001"
    for payload in (
        {"run_at": 42},
        {"run_at": ["2026-08-20T12:30:00Z"]},
        {"run_at": "not-a-datetime"},
    ):
        status, data = _patch(url, json.dumps(payload))
        assert status == 400, payload
        assert data["error"]

    status, task = _get(url)
    assert status == 200
    assert task["run_at"] is None


def test_patch_task_unknown_id_404(web_env):
    server, _ = web_env
    status, data = _patch(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/MISSING",
        json.dumps({"title": "Nope"}),
    )
    assert status == 404
    assert data["error"] == "not found"


def test_patch_task_validation_errors(web_env):
    server, _ = web_env
    url = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001"
    for payload in (
        {"title": ""},
        {"title": "   "},
        {"title": 42},
        {"description": ["bad"]},
        {"acceptance_criteria": "nope"},
        {"acceptance_criteria": [1]},
        {"dependencies": 7},
        {"files_to_modify": "x"},
        {"agent_command": ""},
        {"agent_command": []},
        {"agent_timeout_seconds": 0},
        {"agent_timeout_seconds": -5},
        {"status": "COMPLETED"},
        {"bogus": 1},
    ):
        status, data = _patch(url, json.dumps(payload))
        assert status == 400, payload
        assert data["error"]

    status, data = _patch(url, "{not json")
    assert status == 400
    assert data["error"]

    status, data = _patch(url, "[1, 2]")
    assert status == 400
    assert data["error"]

    status, data = _patch(url, None)
    assert status == 400
    assert data["error"]

    status, data = _patch(url, json.dumps({}))
    assert status == 400
    assert data["error"]

    status, task = _get(url)
    assert status == 200
    assert task["title"] == "First"
    assert task["description"] == "Build it."


def test_patch_task_unknown_instance_404(web_env):
    server, _ = web_env
    status, data = _patch(
        f"http://127.0.0.1:{server.port}/api/instances/nope/tasks/TASK-001",
        json.dumps({"title": "x"}),
    )
    assert status == 404
    assert data["error"] == "unknown instance"


def test_patch_task_wrong_path_404(web_env):
    server, _ = web_env
    status, data = _patch(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/bogus",
        json.dumps({"title": "x"}),
    )
    assert status == 404
    assert data["error"] == "not found"

    status, data = _patch(
        f"http://127.0.0.1:{server.port}/instances/alpha/",
        json.dumps({"title": "x"}),
    )
    assert status == 404


def test_delete_task_removes_open_task(web_env):
    server, registry = web_env
    url = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001"
    _, before = _get(url)
    status, data = _delete(url)
    assert status == 200
    assert data["id"] == before["id"]
    assert data["title"] == before["title"]

    status, tasks = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks")
    assert status == 200
    assert [t["id"] for t in tasks] == ["TASK-002"]

    disk = json.loads(
        (registry / "alpha" / "backlog.json").read_text(encoding="utf-8")
    )
    assert [entry["id"] for entry in disk["tasks"]] == ["TASK-002"]


def test_delete_task_keeps_other_instances(web_env):
    server, _ = web_env
    url = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001"
    status, _ = _delete(url)
    assert status == 200
    status, tasks = _get(f"http://127.0.0.1:{server.port}/api/instances/beta/tasks")
    assert status == 200
    assert [t["id"] for t in tasks] == ["B-1"]


def test_delete_task_non_open_400(web_env):
    server, _ = web_env
    url = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-002"
    status, data = _delete(url)
    assert status == 400
    assert data["error"] == "only OPEN or BLOCKED tasks can be deleted"

    status, tasks = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks")
    assert status == 200
    assert [t["id"] for t in tasks] == ["TASK-001", "TASK-002"]


def test_delete_task_unknown_id_404(web_env):
    server, _ = web_env
    status, data = _delete(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/MISSING"
    )
    assert status == 404
    assert data["error"] == "not found"


def test_delete_task_unknown_instance_404(web_env):
    server, _ = web_env
    status, data = _delete(
        f"http://127.0.0.1:{server.port}/api/instances/nope/tasks/TASK-001"
    )
    assert status == 404
    assert data["error"] == "unknown instance"


def test_delete_task_wrong_path_404(web_env):
    server, _ = web_env
    status, data = _delete(f"http://127.0.0.1:{server.port}/api/instances/alpha/bogus")
    assert status == 404
    assert data["error"] == "not found"


def blocked_task_json(task_id: str = "TASK-003") -> dict:
    return make_task(
        id=task_id,
        title="Blocked task",
        status=TaskStatus.BLOCKED,
        blocker_reason=["Which retry policy should I use?"],
        blocked_count=2,
    ).model_dump(mode="json")


def test_task_json_exposes_blocker_fields(web_env, registry):
    server, registry = web_env
    write_instance(
        registry,
        "blocked",
        repo=str(registry / "repos" / "blocked"),
        tasks=[blocked_task_json("B-9")],
    )
    status, task = _get(
        f"http://127.0.0.1:{server.port}/api/instances/blocked/tasks/B-9"
    )
    assert status == 200
    assert task["status"] == "BLOCKED"
    assert task["blocker_reason"] == ["Which retry policy should I use?"]
    assert task["blocked_count"] == 2

    status, tasks = _get(f"http://127.0.0.1:{server.port}/api/instances/blocked/tasks")
    assert status == 200
    assert tasks[0]["blocker_reason"] == ["Which retry policy should I use?"]
    assert tasks[0]["blocked_count"] == 2


def failed_task_json(task_id: str = "TASK-004") -> dict:
    return make_task(
        id=task_id,
        title="Failed task",
        status=TaskStatus.FAILED,
        failure_reason=["timed out after 60s"],
    ).model_dump(mode="json")


def test_task_json_exposes_failure_reason(web_env, registry):
    server, registry = web_env
    write_instance(
        registry,
        "failed",
        repo=str(registry / "repos" / "failed"),
        tasks=[failed_task_json("F-1")],
    )
    status, task = _get(
        f"http://127.0.0.1:{server.port}/api/instances/failed/tasks/F-1"
    )
    assert status == 200
    assert task["status"] == "FAILED"
    assert task["failure_reason"] == ["timed out after 60s"]

    status, tasks = _get(f"http://127.0.0.1:{server.port}/api/instances/failed/tasks")
    assert status == 200
    assert tasks[0]["failure_reason"] == ["timed out after 60s"]


def agent_response_task_json(task_id: str = "TASK-005") -> dict:
    return make_task(
        id=task_id,
        title="Task with agent output",
        agent_response="line one\nline two",
    ).model_dump(mode="json")


def test_task_json_exposes_agent_response(web_env, registry):
    server, registry = web_env
    write_instance(
        registry,
        "withoutput",
        repo=str(registry / "repos" / "withoutput"),
        tasks=[agent_response_task_json("R-1")],
    )
    status, task = _get(
        f"http://127.0.0.1:{server.port}/api/instances/withoutput/tasks/R-1"
    )
    assert status == 200
    assert task["agent_response"] == "line one\nline two"

    status, tasks = _get(f"http://127.0.0.1:{server.port}/api/instances/withoutput/tasks")
    assert status == 200
    assert tasks[0]["agent_response"] == "line one\nline two"


def test_reopen_blocked_task(web_env, registry):
    server, registry = web_env
    write_instance(
        registry,
        "blocked",
        repo=str(registry / "repos" / "blocked"),
        tasks=[blocked_task_json("B-9")],
    )
    url = f"http://127.0.0.1:{server.port}/api/instances/blocked/tasks/B-9"
    status, task = _post(f"{url}/reopen", None)
    assert status == 200
    assert task["status"] == "OPEN"
    assert task["blocker_reason"] == []
    assert task["blocked_count"] == 2  # kept as history

    status, stored = _get(url)
    assert status == 200
    assert stored["status"] == "OPEN"
    assert stored["blocker_reason"] == []
    assert stored["blocked_count"] == 2

    disk = json.loads(
        (registry / "blocked" / "backlog.json").read_text(encoding="utf-8")
    )
    entry = disk["tasks"][0]
    assert entry["status"] == "OPEN"
    assert entry["blocker_reason"] == []
    assert entry["blocked_count"] == 2


def test_reopen_open_task_400(web_env):
    server, _ = web_env
    status, data = _post(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001/reopen",
        None,
    )
    assert status == 400
    assert data["error"] == "only BLOCKED tasks can be reopened"

    status, task = _get(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001"
    )
    assert status == 200
    assert task["status"] == "OPEN"


def test_reopen_unknown_task_404(web_env):
    server, _ = web_env
    status, data = _post(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/MISSING/reopen",
        None,
    )
    assert status == 404
    assert data["error"] == "not found"


def test_reopen_unknown_instance_404(web_env):
    server, _ = web_env
    status, data = _post(
        f"http://127.0.0.1:{server.port}/api/instances/nope/tasks/TASK-001/reopen",
        None,
    )
    assert status == 404
    assert data["error"] == "unknown instance"


def test_reopen_wrong_path_404(web_env):
    server, _ = web_env
    status, data = _post(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/bogus/reopen",
        None,
    )
    assert status == 404
    assert data["error"] == "not found"

    status, data = _post(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001/other",
        None,
    )
    assert status == 404
    assert data["error"] == "not found"


def test_reopen_keeps_other_tasks(web_env, registry):
    server, registry = web_env
    write_instance(
        registry,
        "blocked",
        repo=str(registry / "repos" / "blocked"),
        tasks=[blocked_task_json("B-9"), task_json("B-10", "Other", TaskStatus.OPEN)],
    )
    status, _ = _post(
        f"http://127.0.0.1:{server.port}/api/instances/blocked/tasks/B-9/reopen",
        None,
    )
    assert status == 200
    status, tasks = _get(
        f"http://127.0.0.1:{server.port}/api/instances/blocked/tasks"
    )
    assert status == 200
    assert [t["id"] for t in tasks] == ["B-9", "B-10"]
    assert tasks[0]["status"] == "OPEN"


def test_delete_blocked_task(web_env, registry):
    server, registry = web_env
    write_instance(
        registry,
        "blocked",
        repo=str(registry / "repos" / "blocked"),
        tasks=[blocked_task_json("B-9")],
    )
    url = f"http://127.0.0.1:{server.port}/api/instances/blocked/tasks/B-9"
    status, data = _delete(url)
    assert status == 200
    assert data["id"] == "B-9"
    assert data["status"] == "BLOCKED"

    status, tasks = _get(f"http://127.0.0.1:{server.port}/api/instances/blocked/tasks")
    assert status == 200
    assert tasks == []


def test_patch_rejects_engine_managed_blocker_fields(web_env):
    server, _ = web_env
    url = f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001"
    for payload in (
        {"blocker_reason": ["I need a decision"]},
        {"blocked_count": 5},
        {"failure_reason": ["timed out"]},
        {"blocker_reason": ["x"], "blocked_count": 1, "failure_reason": ["y"]},
    ):
        status, data = _patch(url, json.dumps(payload))
        assert status == 400, payload
        assert data["error"]

    status, task = _get(url)
    assert status == 200
    assert task["blocker_reason"] == []
    assert task["blocked_count"] == 0
    assert task["failure_reason"] == []


def test_do_delete_returns_500_on_unexpected_error(web_env, monkeypatch):
    import forgeo.central as central_module

    server, _ = web_env

    def boom(name: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(central_module, "get_instance", boom)
    status, data = _delete(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001"
    )
    assert status == 500
    assert data["error"] == "internal server error"


def test_web_task_id_for():
    tasks = [make_task(id="TASK-001", title="a"), make_task(id="WEB-007", title="b")]
    assert web_task_id_for(tasks) == "WEB-008"
    assert web_task_id_for([]) == "WEB-001"


def test_logs_endpoint(web_env):
    server, _ = web_env
    status, data = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/logs?lines=1")
    assert status == 200
    assert data["lines"] == ["trailing line"]


def test_runs_endpoint(web_env):
    server, _ = web_env
    status, data = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/runs")
    assert status == 200
    assert data["runs"][0]["task_id"] == "TASK-001"
    assert data["runs"][0]["outcome"] == "SUCCESS"
    assert data["total"] == 1
    assert data["offset"] == 0
    assert data["limit"] == 10


def test_runs_endpoint_paginates(registry, central_server):
    """The runs endpoint pages newest-first and reports the real total."""
    records = []
    for index in range(1, 8):
        record = run_record(f"TASK-{index:03d}", RunOutcome.SUCCESS)
        record.finished_at = FINISHED.replace(second=index)
        records.append(record)
    write_instance(
        registry,
        "alpha",
        repo=str(registry / "repos" / "alpha"),
        runs=records,
    )
    base = f"http://127.0.0.1:{central_server.port}/api/instances/alpha/runs"

    status, data = _get(f"{base}?limit=3&offset=0")
    assert status == 200
    assert data["total"] == 7
    assert data["limit"] == 3
    assert data["offset"] == 0
    assert [run["task_id"] for run in data["runs"]] == ["TASK-007", "TASK-006", "TASK-005"]

    status, data = _get(f"{base}?limit=3&offset=3")
    assert [run["task_id"] for run in data["runs"]] == ["TASK-004", "TASK-003", "TASK-002"]

    status, data = _get(f"{base}?limit=3&offset=6")
    assert [run["task_id"] for run in data["runs"]] == ["TASK-001"]

    status, data = _get(f"{base}?limit=3&offset=99")
    assert data["runs"] == []
    assert data["total"] == 7


def test_runs_endpoint_surfaces_no_changes_reason(registry, central_server):
    """A no-change run is surfaced with its reason, not a silent null commit."""
    record = run_record("TASK-001", RunOutcome.SUCCESS)
    record.reason = "Agent exited 0 but produced no changes"
    write_instance(
        registry,
        "alpha",
        repo=str(registry / "repos" / "alpha"),
        tasks=[task_json("TASK-001", "First", TaskStatus.OPEN)],
        runs=[record],
    )
    status, data = _get(f"http://127.0.0.1:{central_server.port}/api/instances/alpha/runs")
    assert status == 200
    assert data["runs"][0]["commit_sha"] is None
    assert data["runs"][0]["reason"] == "Agent exited 0 but produced no changes"


def test_runs_endpoint_surfaces_output_logs(registry, central_server):
    """The runs API returns the persisted bounded agent-output tail."""
    record = run_record("TASK-001", RunOutcome.BLOCKED)
    record.output_logs = [
        "[stdout] Trying a heuristic",
        "[stdout] Needs a human decision",
    ]
    write_instance(
        registry,
        "alpha",
        repo=str(registry / "repos" / "alpha"),
        tasks=[task_json("TASK-001", "First", TaskStatus.BLOCKED)],
        runs=[record],
    )
    status, data = _get(f"http://127.0.0.1:{central_server.port}/api/instances/alpha/runs")
    assert status == 200
    assert data["runs"][0]["output_logs"] == [
        "[stdout] Trying a heuristic",
        "[stdout] Needs a human decision",
    ]


def test_runs_endpoint_old_record_without_output_logs_renders_null(
    registry, central_server,
):
    """Records that predate the output_logs field surface null and render fine."""
    record = run_record("TASK-001", RunOutcome.SUCCESS)
    raw = json.loads(record.model_dump_json())
    del raw["output_logs"]
    runs_path = registry / "alpha" / "runs.jsonl"
    runs_path.parent.mkdir(parents=True, exist_ok=True)
    runs_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    (registry / "alpha" / "forgeo.yaml").write_text(
        "name: alpha\n"
        f"repo: {registry / 'repos' / 'alpha'}\n"
        f"backlog: {registry / 'alpha' / 'backlog.json'}\n"
        f"blocker_file: {registry / 'alpha' / 'BLOCKER.md'}\n"
        "agent_command: echo hi\n"
        f"log_file: {registry / 'alpha' / 'forgeo.log'}\n"
        "interval_minutes: 30\n",
        encoding="utf-8",
    )
    add_instance("alpha", registry / "alpha" / "forgeo.yaml")

    status, data = _get(f"http://127.0.0.1:{central_server.port}/api/instances/alpha/runs")
    assert status == 200
    run = data["runs"][0]
    assert run["task_id"] == "TASK-001"
    assert run["output_logs"] is None


def test_history_script_renders_collapsible_agent_output(web_env):
    """The served History-tab script renders persisted output collapsibly."""
    server, _ = web_env
    status, body = _get(f"http://127.0.0.1:{server.port}/central/central.js")
    assert status == 200
    assert "output_logs" in body
    assert "run-output" in body
    assert "agent output" in body


def test_runs_endpoint_surfaces_retry_count(registry, central_server):
    """A run record's retry_count is exposed, so the History tab can show it."""
    record = run_record("TASK-001", RunOutcome.SUCCESS)
    record.retry_count = 2
    write_instance(
        registry,
        "alpha",
        repo=str(registry / "repos" / "alpha"),
        tasks=[task_json("TASK-001", "First", TaskStatus.OPEN)],
        runs=[record],
    )
    status, data = _get(f"http://127.0.0.1:{central_server.port}/api/instances/alpha/runs")
    assert status == 200
    assert data["runs"][0]["retry_count"] == 2


def test_tasks_endpoint_surfaces_retry_state(registry, central_server):
    """The tasks API carries the effective retry budget and remaining retries
    (from the per-task override falling back to the config)."""
    config_dir = registry / "alpha"
    config_dir.mkdir(parents=True, exist_ok=True)
    backlog = config_dir / "backlog.json"
    (config_dir / "forgeo.yaml").write_text(
        "name: alpha\n"
        f"repo: {registry / 'repos' / 'alpha'}\n"
        f"backlog: {backlog}\n"
        f"blocker_file: {config_dir / 'BLOCKER.md'}\n"
        "agent_command: echo hi\n"
        f"log_file: {config_dir / 'forgeo.log'}\n"
        "interval_minutes: 30\n"
        "failed_retry_max: 2\n",
        encoding="utf-8",
    )
    failed = task_json("TASK-001", "First", TaskStatus.FAILED)
    failed["retry_count"] = 1
    failed["failed_wait_cycles"] = 1
    backlog.write_text(json.dumps({"tasks": [failed]}), encoding="utf-8")
    add_instance("alpha", config_dir / "forgeo.yaml")

    status, data = _get(f"http://127.0.0.1:{central_server.port}/api/instances/alpha/tasks")
    assert status == 200
    task = data[0]
    assert task["retry_count"] == 1
    assert task["failed_wait_cycles"] == 1
    assert task["retry_budget"] == 2
    assert task["retries_remaining"] == 1


def test_tasks_endpoint_retry_override_wins(registry, central_server):
    """A per-task retries_left override replaces the config budget."""
    config_dir = registry / "alpha"
    config_dir.mkdir(parents=True, exist_ok=True)
    backlog = config_dir / "backlog.json"
    (config_dir / "forgeo.yaml").write_text(
        "name: alpha\n"
        f"repo: {registry / 'repos' / 'alpha'}\n"
        f"backlog: {backlog}\n"
        f"blocker_file: {config_dir / 'BLOCKER.md'}\n"
        "agent_command: echo hi\n"
        f"log_file: {config_dir / 'forgeo.log'}\n"
        "interval_minutes: 30\n"
        "failed_retry_max: 0\n",
        encoding="utf-8",
    )
    failed = task_json("TASK-001", "First", TaskStatus.FAILED)
    failed["retries_left"] = 4
    backlog.write_text(json.dumps({"tasks": [failed]}), encoding="utf-8")
    add_instance("alpha", config_dir / "forgeo.yaml")

    status, data = _get(f"http://127.0.0.1:{central_server.port}/api/instances/alpha/tasks")
    assert status == 200
    assert data[0]["retry_budget"] == 4
    assert data[0]["retries_remaining"] == 4


def test_history_script_renders_retry_column(web_env):
    """The History-tab script renders the retry count and the retried badge."""
    server, _ = web_env
    status, body = _get(f"http://127.0.0.1:{server.port}/central/central.js")
    assert status == 200
    assert '"retry"' in body
    assert "retry_count" in body
    assert "badge--retry" in body


def test_retry_config_fields_editable_in_config_tab(web_env):
    """The Config-tab script exposes the retry policy keys."""
    server, _ = web_env
    status, body = _get(f"http://127.0.0.1:{server.port}/central/central.js")
    assert status == 200
    assert "failed_retry_max" in body
    assert "failed_retry_wait_cycles" in body
    assert "no_changes_retry_max" in body


def test_tasks_endpoint_surfaces_run_at(registry, central_server):
    """The tasks API returns a task's run_at one-shot schedule."""
    config_dir = registry / "alpha"
    config_dir.mkdir(parents=True, exist_ok=True)
    backlog = config_dir / "backlog.json"
    (config_dir / "forgeo.yaml").write_text(
        "name: alpha\n"
        f"repo: {registry / 'repos' / 'alpha'}\n"
        f"backlog: {backlog}\n"
        f"blocker_file: {config_dir / 'BLOCKER.md'}\n"
        "agent_command: echo hi\n"
        f"log_file: {config_dir / 'forgeo.log'}\n"
        "interval_minutes: 30\n",
        encoding="utf-8",
    )
    task = task_json("TASK-001", "First", TaskStatus.OPEN)
    task["run_at"] = "2026-08-20T12:30:00Z"
    backlog.write_text(json.dumps({"tasks": [task]}), encoding="utf-8")
    add_instance("alpha", config_dir / "forgeo.yaml")

    status, data = _get(
        f"http://127.0.0.1:{central_server.port}/api/instances/alpha/tasks"
    )
    assert status == 200
    assert data[0]["run_at"] == "2026-08-20T12:30:00Z"


def test_run_at_form_inputs_in_instance_page(web_env):
    """The instance page and script expose the run_at datetime-local input."""
    server, _ = web_env
    status, body = _get(f"http://127.0.0.1:{server.port}/instances/alpha/")
    assert status == 200
    assert 'id="task-run-at"' in body
    assert 'type="datetime-local"' in body
    assert 'id="task-edit-run-at"' in body

    status, body = _get(f"http://127.0.0.1:{server.port}/central/central.js")
    assert status == 200
    assert "run_at" in body
    assert "toLocalInputValue" in body
    assert "task-modal-run-at" in body


def test_blocker_endpoint(web_env):
    server, _ = web_env
    status, data = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/blocker")
    assert status == 200
    assert data["content"] == "# Blocker\nPlease decide.\n"


def test_blocker_null_when_missing(web_env):
    server, _ = web_env
    status, data = _get(f"http://127.0.0.1:{server.port}/api/instances/beta/blocker")
    assert status == 200
    assert data["content"] is None


def test_config_endpoint(web_env):
    server, _ = web_env
    status, data = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/config")
    assert status == 200
    assert data["name"] == "alpha"
    assert data["interval_minutes"] == 30


def test_put_config_persists_and_returns_reloaded(web_env):
    server, registry = web_env
    url = f"http://127.0.0.1:{server.port}/api/instances/alpha/config"
    _, before = _get(url)
    payload = dict(before)
    payload["interval_minutes"] = 15
    payload["branch"] = "dev"
    status, data = _put(url, json.dumps(payload))
    assert status == 200
    assert data["saved"] is True
    assert data["restart_required"] is False
    assert data["message"]
    assert data["config"]["name"] == "alpha"
    assert data["config"]["interval_minutes"] == 15
    assert data["config"]["branch"] == "dev"

    status, after = _get(url)
    assert status == 200
    assert after["interval_minutes"] == 15
    assert after["branch"] == "dev"

    disk = yaml.safe_load((registry / "alpha" / "forgeo.yaml").read_text(encoding="utf-8"))
    assert disk["interval_minutes"] == 15
    assert disk["branch"] == "dev"


def test_put_config_round_trips_relative_paths(web_env, registry):
    server, registry = web_env
    config_dir = registry / "rel"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "forgeo.yaml"
    config_path.write_text(
        "name: rel\n"
        "repo: ../repo\n"
        "backlog: tasks.json\n"
        "blocker_file: BLOCKER.md\n"
        "agent_command: echo hi\n"
        "log_file: forgeo.log\n"
        "interval_minutes: 30\n",
        encoding="utf-8",
    )
    add_instance("rel", config_path)
    url = f"http://127.0.0.1:{server.port}/api/instances/rel/config"

    status, config = _get(url)
    assert status == 200
    assert config["name"] == "rel"
    assert Path(config["repo"]).resolve() == (config_dir / ".." / "repo").resolve()
    assert config["backlog"] == str((config_dir / "tasks.json").resolve())

    status, data = _put(url, json.dumps(config))
    assert status == 200
    assert data["config"]["backlog"] == str((config_dir / "tasks.json").resolve())

    disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert disk["repo"] == "../repo"
    assert disk["backlog"] == "tasks.json"
    assert disk["blocker_file"] == "BLOCKER.md"
    assert disk["log_file"] == "forgeo.log"

    reloaded = load_config(config_path)
    assert reloaded.repo.resolve() == (config_dir / ".." / "repo").resolve()
    assert reloaded.backlog == (config_dir / "tasks.json").resolve()
    assert reloaded.blocker_file == (config_dir / "BLOCKER.md").resolve()
    assert reloaded.log_file == str((config_dir / "forgeo.log").resolve())


def test_put_config_validation_errors(web_env):
    server, _ = web_env
    url = f"http://127.0.0.1:{server.port}/api/instances/alpha/config"
    for payload in (
        {"name": "alpha", "agent_command": ""},
        {"name": "alpha", "agent_command": "echo", "interval_minutes": 0},
        {"name": "alpha", "agent_command": "echo", "agent_sandbox": "sandbox"},
        {"name": "alpha", "agent_command": "echo", "branch": 42},
        {"name": "alpha", "agent_command": "echo", "agent_sandbox": "docker"},
        {"name": "alpha", "agent_command": "echo", "no_changes_exit_code": 0},
        {
            "name": "alpha",
            "agent_command": "echo",
            "blocked_exit_code": 2,
            "no_changes_exit_code": 2,
        },
    ):
        status, data = _put(url, json.dumps(payload))
        assert status == 400, payload
        assert data["error"]

    status, data = _put(url, "{not json")
    assert status == 400
    assert data["error"]

    status, data = _put(url, "[1, 2]")
    assert status == 400
    assert data["error"]

    status, data = _put(url, None)
    assert status == 400
    assert data["error"]

    status, data = _put(url, json.dumps({}))
    assert status == 400
    assert data["error"]

    status, config = _get(url)
    assert status == 200
    assert config["interval_minutes"] == 30


def test_put_config_rejects_changed_name(web_env):
    server, _ = web_env
    url = f"http://127.0.0.1:{server.port}/api/instances/alpha/config"
    _, config = _get(url)
    payload = dict(config)
    payload["name"] = "other"
    status, data = _put(url, json.dumps(payload))
    assert status == 400
    assert data["error"]

    status, after = _get(url)
    assert status == 200
    assert after["name"] == "alpha"


def test_put_config_rejects_changed_telegram_token(web_env, registry):
    server, registry = web_env
    config_dir = registry / "tok"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "forgeo.yaml"
    config_path.write_text(
        "name: tok\n"
        "repo: ../repo\n"
        "backlog: backlog.json\n"
        "agent_command: echo hi\n"
        "telegram_bot_token: real-secret\n",
        encoding="utf-8",
    )
    add_instance("tok", config_path)
    url = f"http://127.0.0.1:{server.port}/api/instances/tok/config"

    _, config = _get(url)
    assert config["telegram_bot_token"] == "real-secret"
    payload = dict(config)
    payload["telegram_bot_token"] = "attacker-token"
    status, data = _put(url, json.dumps(payload))
    assert status == 400
    assert data["error"]

    status, after = _get(url)
    assert status == 200
    assert after["telegram_bot_token"] == "real-secret"


def test_put_config_cannot_null_telegram_token(web_env, registry):
    server, registry = web_env
    config_dir = registry / "tok"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "forgeo.yaml"
    config_path.write_text(
        "name: tok\n"
        "repo: ../repo\n"
        "backlog: backlog.json\n"
        "agent_command: echo hi\n"
        "telegram_bot_token: real-secret\n",
        encoding="utf-8",
    )
    add_instance("tok", config_path)
    url = f"http://127.0.0.1:{server.port}/api/instances/tok/config"

    _, config = _get(url)
    payload = dict(config)
    payload["telegram_bot_token"] = None
    status, data = _put(url, json.dumps(payload))
    assert status == 400
    assert data["error"]

    status, after = _get(url)
    assert status == 200
    assert after["telegram_bot_token"] == "real-secret"


def test_put_config_preserves_telegram_token(web_env, registry):
    server, registry = web_env
    config_dir = registry / "tok"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "forgeo.yaml"
    config_path.write_text(
        "name: tok\n"
        "repo: ../repo\n"
        "backlog: backlog.json\n"
        "agent_command: echo hi\n"
        "telegram_bot_token: real-secret\n",
        encoding="utf-8",
    )
    add_instance("tok", config_path)
    url = f"http://127.0.0.1:{server.port}/api/instances/tok/config"

    _, config = _get(url)
    payload = dict(config)
    payload["interval_minutes"] = 45
    status, data = _put(url, json.dumps(payload))
    assert status == 200
    assert data["config"]["telegram_bot_token"] == "real-secret"

    _, after = _get(url)
    payload = dict(after)
    del payload["telegram_bot_token"]
    payload["interval_minutes"] = 60
    status, data = _put(url, json.dumps(payload))
    assert status == 200
    assert data["config"]["telegram_bot_token"] == "real-secret"

    status, after = _get(url)
    assert status == 200
    assert after["telegram_bot_token"] == "real-secret"
    assert after["interval_minutes"] == 60


def test_put_config_unknown_instance_404(web_env):
    server, _ = web_env
    status, data = _put(
        f"http://127.0.0.1:{server.port}/api/instances/nope/config",
        json.dumps({"agent_command": "echo"}),
    )
    assert status == 404
    assert data["error"] == "unknown instance"


def test_put_config_wrong_path_404(web_env):
    server, _ = web_env
    status, data = _put(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/bogus",
        json.dumps({"agent_command": "echo"}),
    )
    assert status == 404
    assert data["error"] == "not found"


def test_do_put_returns_500_on_unexpected_error(web_env, monkeypatch):
    import forgeo.central as central_module

    server, _ = web_env

    def boom(name: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(central_module, "get_instance", boom)
    status, data = _put(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/config",
        json.dumps({"agent_command": "echo"}),
    )
    assert status == 500
    assert data["error"] == "internal server error"


def test_missing_data_files_render_empty(registry, central_server):
    write_instance(
        registry,
        "ghost",
        repo=str(registry / "repos" / "ghost"),
        tasks=None,
        log_lines=None,
        runs=None,
        blocker=None,
    )
    base = f"http://127.0.0.1:{central_server.port}/api/instances/ghost"

    status, data = _get(f"{base}/status")
    assert status == 200
    assert data["daemon_running"] is False
    assert data["last_outcome"] is None
    assert data["next_run_at"] is None

    status, data = _get(f"{base}/tasks")
    assert status == 200
    assert data == []

    status, data = _get(f"{base}/logs")
    assert status == 200
    assert data["lines"] == []

    status, data = _get(f"{base}/runs")
    assert status == 200
    assert data["runs"] == []
    assert data["total"] == 0

    status, data = _get(f"{base}/blocker")
    assert status == 200
    assert data["content"] is None

    status, _body = _get(f"http://127.0.0.1:{central_server.port}/instances/ghost/")
    assert status == 200


def test_unknown_api_endpoint_returns_404(web_env):
    server, _ = web_env
    status, data = _get(f"http://127.0.0.1:{server.port}/api/instances/alpha/bogus")
    assert status == 404
    assert data["error"] == "not found"


def test_static_assets_served(web_env):
    server, _ = web_env
    status, body = _get(f"http://127.0.0.1:{server.port}/style.css")
    assert status == 200
    assert isinstance(body, str)

    status, body = _get(f"http://127.0.0.1:{server.port}/central/central.js")
    assert status == 200
    assert "REFRESH_MS" in body

    status, body = _get(f"http://127.0.0.1:{server.port}/central/central.css")
    assert status == 200


def test_parser_help_lists_web(capsys):
    build_parser().print_help()
    assert "web" in capsys.readouterr().out


def test_do_get_returns_500_on_unexpected_error(web_env, monkeypatch):
    import forgeo.central as central_module

    server, _ = web_env

    def boom() -> list:
        raise RuntimeError("boom")

    monkeypatch.setattr(central_module, "list_instances", boom)
    status, data = _get(f"http://127.0.0.1:{server.port}/api/instances")
    assert status == 500
    assert data["error"] == "internal server error"


def test_do_post_returns_500_on_unexpected_error(web_env, monkeypatch):
    import forgeo.central as central_module

    server, _ = web_env

    def boom(name: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(central_module, "get_instance", boom)
    status, data = _post(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks",
        json.dumps({"title": "x", "description": "y"}),
    )
    assert status == 500
    assert data["error"] == "internal server error"


def test_do_patch_returns_500_on_unexpected_error(web_env, monkeypatch):
    import forgeo.central as central_module

    server, _ = web_env

    def boom(name: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(central_module, "get_instance", boom)
    status, data = _patch(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001",
        json.dumps({"title": "x"}),
    )
    assert status == 500
    assert data["error"] == "internal server error"


def test_do_post_reopen_returns_500_on_unexpected_error(web_env, monkeypatch):
    import forgeo.central as central_module

    server, _ = web_env

    def boom(name: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(central_module, "get_instance", boom)
    status, data = _post(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/tasks/TASK-001/reopen",
        None,
    )
    assert status == 500
    assert data["error"] == "internal server error"


def test_web_bind_failure_exits_nonzero_and_releases_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    lock_path = tmp_path / "web.lock"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        args = build_parser().parse_args(
            ["web", "--host", "127.0.0.1", "--port", str(port)]
        )
        assert cmd_web(args) == 1
        assert not lock_path.exists()
    finally:
        sock.close()


# --------------------------------------------------------------------------- #
# Daemon lifecycle endpoints (POST /start, /stop, /restart)                  #
# --------------------------------------------------------------------------- #


def test_post_start_starts_daemon(web_env, git_repo):
    server, registry = web_env
    write_daemon_instance(registry, "daemon-a", repo=str(git_repo))
    lock_path = registry / "daemon-a" / "backlog.lock"
    try:
        status, data = _post(
            f"http://127.0.0.1:{server.port}/api/instances/daemon-a/start", None
        )
        assert status == 200
        assert data["status"] == "started"
        assert data["daemon_running"] is True
        assert isinstance(data["pid"], int)
        assert "started" in data["message"]
        assert wait_for(lambda: is_lock_held(lock_path))

        status, status_data = _get(
            f"http://127.0.0.1:{server.port}/api/instances/daemon-a/status"
        )
        assert status == 200
        assert status_data["daemon_running"] is True
    finally:
        _post(f"http://127.0.0.1:{server.port}/api/instances/daemon-a/stop", None)
        assert wait_for(lambda: not is_lock_held(lock_path))


def test_post_start_already_running_409(web_env):
    server, registry = web_env
    lock_path = registry / "alpha" / "backlog.lock"
    lock = acquire_run_lock(lock_path)
    assert lock is not None
    try:
        status, data = _post(
            f"http://127.0.0.1:{server.port}/api/instances/alpha/start", None
        )
        assert status == 409
        assert data["status"] == "already_running"
        assert data["daemon_running"] is True
    finally:
        lock.close()


def test_post_stop_not_running_noop(web_env):
    server, _ = web_env
    status, data = _post(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/stop", None
    )
    assert status == 200
    assert data["status"] == "not_running"
    assert data["daemon_running"] is False


def test_post_stop_stops_running_daemon(web_env, git_repo):
    server, registry = web_env
    config_path = write_daemon_instance(registry, "daemon-b", repo=str(git_repo))
    lock_path = registry / "daemon-b" / "backlog.lock"
    proc = spawn_daemon(config_path)
    try:
        assert wait_for(lambda: is_lock_held(lock_path))
        status, data = _post(
            f"http://127.0.0.1:{server.port}/api/instances/daemon-b/stop", None
        )
        assert status == 200
        assert data["status"] == "stopped"
        assert data["daemon_running"] is False
        assert wait_for(lambda: proc.poll() is not None)
        assert not is_lock_held(lock_path)
    finally:
        if proc.poll() is None:
            proc.kill()
        _post(f"http://127.0.0.1:{server.port}/api/instances/daemon-b/stop", None)


def test_post_restart_starts_daemon_when_not_running(web_env, git_repo):
    server, registry = web_env
    write_daemon_instance(registry, "daemon-c", repo=str(git_repo))
    lock_path = registry / "daemon-c" / "backlog.lock"
    try:
        status, data = _post(
            f"http://127.0.0.1:{server.port}/api/instances/daemon-c/restart", None
        )
        assert status == 200
        assert data["status"] == "restarted"
        assert data["daemon_running"] is True
        assert isinstance(data["pid"], int)
        assert wait_for(lambda: is_lock_held(lock_path))
    finally:
        _post(f"http://127.0.0.1:{server.port}/api/instances/daemon-c/stop", None)


def test_post_restart_replaces_running_daemon(web_env, git_repo):
    server, registry = web_env
    config_path = write_daemon_instance(registry, "daemon-d", repo=str(git_repo))
    lock_path = registry / "daemon-d" / "backlog.lock"
    old_proc = spawn_daemon(config_path)
    try:
        assert wait_for(lambda: is_lock_held(lock_path))
        old_pid = None
        try:
            old_pid = int((lock_path.read_text(encoding="utf-8")).split("=", 1)[1])
        except (OSError, IndexError, ValueError):
            pass

        status, data = _post(
            f"http://127.0.0.1:{server.port}/api/instances/daemon-d/restart", None
        )
        assert status == 200
        assert data["status"] == "restarted"
        assert data["daemon_running"] is True
        new_pid = data["pid"]
        assert isinstance(new_pid, int)
        if old_pid is not None:
            assert new_pid != old_pid
        assert wait_for(lambda: old_proc.poll() is not None)
        assert is_lock_held(lock_path)
    finally:
        if old_proc.poll() is None:
            old_proc.kill()
        _post(f"http://127.0.0.1:{server.port}/api/instances/daemon-d/stop", None)


def test_post_daemon_action_unknown_instance_404(web_env):
    server, _ = web_env
    for action in ("start", "stop", "restart"):
        status, data = _post(
            f"http://127.0.0.1:{server.port}/api/instances/nope/{action}", None
        )
        assert status == 404
        assert data["error"] == "unknown instance"


def test_post_daemon_action_wrong_path_404(web_env):
    server, _ = web_env
    for path in (
        "/api/instances/alpha/bogus/start",
        "/api/instances/alpha/start/extra",
    ):
        status, data = _post(f"http://127.0.0.1:{server.port}{path}", None)
        assert status == 404
        assert data["error"] == "not found"


def test_post_start_config_unavailable_500(web_env):
    server, registry = web_env
    _, config_path = write_instance(
        registry, "broken", repo=str(registry / "repos" / "broken")
    )
    config_path.write_text("not: [valid", encoding="utf-8")
    status, data = _post(
        f"http://127.0.0.1:{server.port}/api/instances/broken/start", None
    )
    assert status == 500
    assert data["error"] == "instance config not available"


def test_post_daemon_action_failure_500(web_env, monkeypatch):
    import forgeo.central as central_module

    server, _ = web_env

    def boom(config_path: Path, config: object) -> int:
        raise DaemonError("boom")

    monkeypatch.setattr(central_module.daemon_control, "start_daemon", boom)
    status, data = _post(
        f"http://127.0.0.1:{server.port}/api/instances/alpha/start", None
    )
    assert status == 500
    assert data["status"] == "start_failed"
    assert data["error"] == "boom"


# --------------------------------------------------------------------------- #
# Instances whose backlog lives behind an HTTP endpoint                        #
# --------------------------------------------------------------------------- #


def write_remote_instance(tmp_path: Path, name: str, url: str) -> Path:
    """Register an instance whose backlog is an HTTP endpoint."""
    config_dir = tmp_path / name
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "forgeo.yaml"
    config_path.write_text(
        f"name: {name}\n"
        f"repo: {tmp_path / 'repos' / name}\n"
        f"backlog: {url}\n"
        f"blocker_file: {config_dir / 'BLOCKER.md'}\n"
        f"agent_command: echo hi\n"
        f"log_file: {config_dir / 'forgeo.log'}\n",
        encoding="utf-8",
    )
    add_instance(name, config_path)
    return config_path


def test_remote_backlog_tasks_are_served_from_the_endpoint(registry, central_server):
    with BacklogServer({"tasks": [task_json("R-1", "Remote", TaskStatus.OPEN)]}) as ep:
        write_remote_instance(registry, "remote", ep.url)
        status, data = _get(
            f"http://127.0.0.1:{central_server.port}/api/instances/remote/tasks"
        )
    assert status == 200
    assert [t["id"] for t in data] == ["R-1"]


def test_adding_a_task_posts_it_to_the_endpoint(registry, central_server):
    with BacklogServer() as ep:
        write_remote_instance(registry, "remote", ep.url)
        status, created = _post(
            f"http://127.0.0.1:{central_server.port}/api/instances/remote/tasks",
            json.dumps({"title": "From the console", "description": "Do it."}),
        )
        assert status == 201
        assert created["id"] == "WEB-001"
        assert [t["id"] for t in ep.tasks()] == ["WEB-001"]


def test_unreachable_backlog_reports_502_not_an_empty_list(registry, central_server):
    with BacklogServer() as ep:
        url = ep.url
    write_remote_instance(registry, "remote", url)

    status, data = _get(
        f"http://127.0.0.1:{central_server.port}/api/instances/remote/tasks"
    )
    assert status == 502
    assert "error" in data


def test_unreachable_backlog_does_not_break_the_instance_list(registry, central_server):
    """One dead endpoint must not take the whole home page down with it."""
    with BacklogServer() as ep:
        url = ep.url
    write_remote_instance(registry, "remote", url)
    write_instance(
        registry,
        "alpha",
        repo=str(registry / "repos" / "alpha"),
        tasks=[task_json("TASK-001", "First", TaskStatus.OPEN)],
    )

    status, data = _get(f"http://127.0.0.1:{central_server.port}/api/instances")
    assert status == 200
    rows = {row["name"]: row for row in data}
    assert rows["alpha"]["backlog_counts"]["OPEN"] == 1
    assert rows["alpha"]["backlog_error"] is None
    assert rows["remote"]["backlog_counts"]["OPEN"] == 0
    assert rows["remote"]["backlog_error"]


def test_saving_the_config_preserves_credentials_it_never_showed(
    registry, central_server
):
    """The flat config form does not render backlog_auth; a save must keep it."""
    with BacklogServer() as ep:
        config_path = write_remote_instance(registry, "remote", ep.url)
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + "backlog_auth:\n"
            + "  token_url: https://keycloak.test/realms/dev/protocol/openid-connect/token\n"
            + "  client_id: forgeo\n"
            + "  client_secret_env: FORGEO_BACKLOG_CLIENT_SECRET\n"
            + "task_context: CONTEXT.md\n",
            encoding="utf-8",
        )
        payload = load_config(config_path).model_dump(mode="json")
        payload.pop("backlog_auth")
        payload.pop("task_context")
        payload["interval_minutes"] = 45

        status, _ = _put(
            f"http://127.0.0.1:{central_server.port}/api/instances/remote/config",
            json.dumps(payload),
        )
    assert status == 200
    saved = load_config(config_path)
    assert saved.interval_minutes == 45
    assert saved.backlog_auth is not None
    assert saved.backlog_auth.client_id == "forgeo"
    assert saved.task_context is not None
    assert saved.task_context.name == "CONTEXT.md"
