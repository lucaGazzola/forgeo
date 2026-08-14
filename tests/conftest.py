"""Shared fixtures: a real git repository, a scriptable fake agent, a local
backlog endpoint, and factories for configs, tasks, and the Forgeo wiring
used across suites."""

from __future__ import annotations

import json
import subprocess
import threading
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


def git(repo: Path, *args: str) -> str:
    """Run a git command inside ``repo`` and return its stdout."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A fresh git repository on ``main`` with one committed file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "forgeo@test.local")
    git(repo, "config", "user.name", "Forgeo Test")
    (repo / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "initial")
    return repo


class FakeAgent(BaseAgent):
    """Scriptable agent: returns a fixed result and records its input."""

    name = "fake"

    def __init__(self, result: ExecutionResult | None = None) -> None:
        self.result = result or ExecutionResult(status=ExecutionStatus.SUCCESS)
        self.effect: Callable[[], None] | None = None
        self.calls: list[tuple[Task, RepoContext]] = []
        self.overrides: list[tuple[str | list[str] | None, float | None]] = []

    async def run_task(
        self,
        task: Task,
        context: RepoContext,
        *,
        command: str | list[str] | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecutionResult:
        self.calls.append((task, context))
        self.overrides.append((command, timeout_seconds))
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

    async def run_cycle(self) -> str:
        if self.block:
            import asyncio

            await asyncio.sleep(3600)
        if self.crash:
            raise RuntimeError("boom")
        self.cycles += 1
        return "task"

    async def run_task_id(self, task_id: str) -> str:
        if self.block:
            import asyncio

            await asyncio.sleep(3600)
        if self.crash:
            raise RuntimeError("boom")
        self.cycles += 1
        self.run_task_ids.append(task_id)
        return "task"


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
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

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


def make_forgeo(
    git_repo: Path, tmp_path: Path, **overrides
) -> tuple[Forgeo, FakeAgent, JSONBacklog]:
    """A real :class:`Forgeo` wired to the fixture repo and a fake agent."""
    config = make_config(git_repo, tmp_path, **overrides)
    agent = FakeAgent()
    backlog = JSONBacklog(config.backlog)
    forgeo = Forgeo(config, backlog, agent, GitManager(git_repo))
    return forgeo, agent, backlog
