"""Shared fixtures: a real git repository, a scriptable fake agent, a local
backlog endpoint, and factories for configs, tasks, and the Forgeo wiring
used across suites."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self

import pytest

from forgeo.agent import BaseAgent
from forgeo.backlog import JSONBacklog
from forgeo.forgeo import Forgeo
from forgeo.git import GitManager
from forgeo.models import (
    ExecutionResult,
    ExecutionStatus,
    ForgeoConfig,
    RepoContext,
    Task,
)


@pytest.fixture(autouse=True)
def _skip_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the PyPI update check for every test (no network in tests).

    The update module reads the env var at call time, so subprocesses spawned
    by tests inherit it too. Suites that test the check itself re-enable it by
    deleting the variable.
    """
    monkeypatch.setenv("FORGEO_UPDATE_CHECK", "0")


#: True when the platform has POSIX process-group semantics (``os.killpg``).
HAVE_POSIX = hasattr(os, "killpg")

#: True when a ``docker`` binary is on PATH (the docker-sandbox tests need it).
HAVE_DOCKER = shutil.which("docker") is not None

requires_posix = pytest.mark.skipif(
    not HAVE_POSIX,
    reason="requires POSIX process-group semantics (os.killpg)",
)
requires_docker = pytest.mark.skipif(
    not HAVE_DOCKER, reason="docker binary is not on PATH"
)


async def wait_for_async(
    predicate: Callable[[], bool],
    *,
    timeout: float = 10.0,
    interval: float = 0.02,
) -> None:
    """Poll ``predicate`` until it is truthy; raise ``TimeoutError`` otherwise.

    Used instead of a bare ``while ... : await asyncio.sleep(0.01)`` loop so a
    slow or loaded CI runner cannot hang a test forever.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError(f"condition not met within {timeout:g}s")


def wait_for(
    predicate: Callable[[], bool],
    timeout: float = 15.0,
    interval: float = 0.05,
) -> bool:
    """Poll ``predicate`` until truthy or ``timeout``; return whether it passed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeResponse:
    """Minimal ``urlopen`` stub: ``status`` plus a JSON ``read()`` body."""

    def __init__(self, payload: object = None, *, status: int = 200) -> None:
        import io as _io

        self._io = _io
        self._payload = json.dumps(payload).encode("utf-8")
        self.status = status
        self._body: object = None

    def __enter__(self) -> FakeResponse:
        self._body = self._io.BytesIO(self._payload)
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self, *args: object) -> bytes:
        assert self._body is not None
        return self._body.read(*args)  # type: ignore[operator]


class FakeIssueClient:
    """Generic marker-issue fake for GitHub/GitLab-style backlog tests.

    ``id_key``/``body_key`` select the provider shape (``number``/``body``
    for GitHub, ``iid``/``description`` for GitLab); ``note_attr`` names the
    list recording comments (``comments`` vs ``notes``).
    """

    def __init__(
        self,
        issues: list[dict],
        *,
        id_key: str,
        body_key: str,
        note_attr: str = "comments",
        error_cls: type[Exception] | None = None,
    ) -> None:
        import copy as _copy
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        self._copy = _copy
        self._now = lambda: _datetime.now(_UTC).isoformat()
        self.id_key = id_key
        self.body_key = body_key
        self.note_attr = note_attr
        self.error_cls = error_cls
        self.issues: dict[int, dict] = {_copy.deepcopy(i)[id_key]: _copy.deepcopy(i) for i in issues}
        self._note_list: list = []
        self.calls: list[tuple[str, object]] = []
        self._next = max((i[id_key] for i in issues), default=0) + 1

    @property
    def comments(self) -> list:
        return self._note_list

    @property
    def notes(self) -> list:
        return self._note_list

    def _new_issue(self, number: int, fields: dict) -> dict:
        labels = fields.get("labels", [])
        if isinstance(labels, str):
            labels = [labels]
        return {
            self.id_key: number,
            "id": number,
            "title": fields.get("title", f"Task {number}"),
            self.body_key: fields.get(self.body_key, ""),
            "state": "open",
            "labels": [{"name": n} for n in labels] if self.body_key == "body" else list(labels),
            "created_at": "2026-08-21T10:00:00.000Z",
            "updated_at": "2026-08-21T10:00:00.000Z",
        }

    def search_issues(self, *, page=1, per_page=30, **kwargs):
        self.calls.append(("search", page))
        rows = list(self.issues.values())
        start = (page - 1) * per_page
        return self._copy.deepcopy(rows[start : start + per_page])

    def get_issue(self, number: int):
        self.calls.append(("get", number))
        if number not in self.issues:
            assert self.error_cls is not None
            raise self.error_cls("not found", status=404)
        return self._copy.deepcopy(self.issues[number])

    def update_issue(self, number: int, fields: dict):
        self.calls.append(("update", number))
        issue = self.issues[number]
        for k, v in fields.items():
            if k == "labels":
                issue["labels"] = (
                    [{"name": n} for n in v] if self.body_key == "body" else list(v)
                )
            elif k == "state":
                issue["state"] = v
            elif k == "state_event":
                issue["state"] = "closed" if v == "close" else "opened"
            else:
                issue[k] = self._copy.deepcopy(v)
        issue["updated_at"] = self._now()
        return self._copy.deepcopy(issue)

    def create_issue(self, fields: dict):
        number = self._next
        self._next += 1
        self.calls.append(("create", number))
        issue = self._new_issue(number, fields)
        issue["state"] = "opened" if self.body_key != "body" else "open"
        self.issues[number] = self._copy.deepcopy(issue)
        return self._copy.deepcopy(issue)

    def add_comment(self, number: int, body: str):
        self._note_list.append((number, body))

    def add_note(self, number: int, body: str):
        self._note_list.append((number, body))

    def delete_issue(self, number: int):
        if number in self.issues:
            del self.issues[number]


def git(repo: Path, *args: str) -> str:
    """Run a git command inside ``repo`` and return its stdout."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture(scope="session")
def git_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A template git repo built once per session and copied for each test."""
    template = tmp_path_factory.mktemp("git-template")
    repo = template / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "forgeo@test.local")
    git(repo, "config", "user.name", "Forgeo Test")
    (repo / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def git_repo(tmp_path: Path, git_template: Path) -> Path:
    """A fresh git repository on ``main`` with one committed file (copied)."""
    repo = tmp_path / "repo"
    shutil.copytree(git_template, repo, symlinks=False)
    # Make the copy writable (git objects may be read-only).
    return repo


class FakeAgent(BaseAgent):
    """Scriptable agent: returns a fixed result and records its input."""

    name = "fake"

    def __init__(self, result: ExecutionResult | None = None) -> None:
        self.result = result or ExecutionResult(status=ExecutionStatus.SUCCESS)
        self.effect: Callable[[], None] | None = None
        self.calls: list[tuple[Task, RepoContext]] = []
        self.overrides: list[tuple[str | list[str] | None, float | None]] = []
        self.instructions: list[str | None] = []

    async def run_task(
        self,
        task: Task,
        context: RepoContext,
        *,
        command: str | list[str] | None = None,
        timeout_seconds: float | None = None,
        instruction: str | None = None,
    ) -> ExecutionResult:
        self.calls.append((task, context))
        self.overrides.append((command, timeout_seconds))
        self.instructions.append(instruction)
        if self.effect is not None:
            self.effect()
        return self.result


class FakeForgeo:
    """A runnable stand-in for :class:`Forgeo`: counts cycles, can block or crash."""

    def __init__(self) -> None:
        self.cycles = 0
        self.crash = False
        self.block = False
        self.run_task_ids: list[str] = []

    async def _step(self) -> str:
        if self.block:
            await asyncio.sleep(3600)
        if self.crash:
            raise RuntimeError("boom")
        self.cycles += 1
        return "task"

    async def run_cycle(self) -> str:
        return await self._step()

    async def run_task_id(self, task_id: str) -> str:
        result = await self._step()
        self.run_task_ids.append(task_id)
        return result


class BacklogServer:
    """A minimal backlog endpoint: GET returns the document, POST replaces it.

    Used as a context manager; leaving the block shuts the server down, which
    is also how tests produce an unreachable endpoint.
    """

    def __init__(self, document: dict | None = None) -> None:
        self.document: dict = document if document is not None else {"tasks": []}
        self.requests: list[str] = []
        self.authorizations: list[str | None] = []
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _record(self) -> None:
                server_self.requests.append(self.command)
                server_self.authorizations.append(self.headers.get("Authorization"))

            def do_GET(self) -> None:
                self._record()
                body = json.dumps(server_self.document).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                self._record()
                length = int(self.headers.get("Content-Length", 0))
                server_self.document = json.loads(self.rfile.read(length))
                self.send_response(204)
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.05), daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/backlog"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def tasks(self) -> list[dict]:
        return list(self.document["tasks"])


@pytest.fixture
def backlog_server():
    """A running local backlog endpoint, shut down at the end of the test."""
    with BacklogServer() as server:
        yield server


def make_config(git_repo: Path, tmp_path: Path, **overrides) -> ForgeoConfig:
    """A forgeo config wired to the fixture repo and an out-of-repo backlog."""
    defaults = {
        "name": "test-forgeo",
        "repo": git_repo,
        "backlog": tmp_path / "backlog.json",
        "blocker_file": tmp_path / "BLOCKER.md",
        "agent_command": "echo hi",
    }
    defaults.update(overrides)
    return ForgeoConfig(**defaults)


def make_task(**overrides) -> Task:
    defaults = {"id": "TASK-001", "title": "Do the thing", "description": "Build it."}
    defaults.update(overrides)
    return Task(**defaults)


def make_result(**overrides) -> ExecutionResult:
    """An agent result with no output, for transitions under test directly."""
    defaults = {"status": ExecutionStatus.SUCCESS}
    defaults.update(overrides)
    return ExecutionResult(**defaults)


def make_forgeo(
    git_repo: Path, tmp_path: Path, **overrides
) -> tuple[Forgeo, FakeAgent, JSONBacklog]:
    """A real :class:`Forgeo` wired to the fixture repo and a fake agent."""
    config = make_config(git_repo, tmp_path, **overrides)
    agent = FakeAgent()
    backlog = JSONBacklog(config.backlog)
    forgeo = Forgeo(config, backlog, agent, GitManager(git_repo))
    return forgeo, agent, backlog
