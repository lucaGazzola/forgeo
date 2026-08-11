"""Shared fixtures: a real git repository, a scriptable fake agent, and
factories for configs, tasks, and the Forgeo wiring used across suites."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

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

    async def run_cycle(self) -> str:
        if self.block:
            import asyncio

            await asyncio.sleep(3600)
        if self.crash:
            raise RuntimeError("boom")
        self.cycles += 1
        return "task"


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
