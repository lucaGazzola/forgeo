"""A full Forgeo cycle against a real HTTP backlog, served locally.

Unlike ``test_backlog_http.py`` (which stubs ``urlopen``), these tests run the
engine end to end over a socket: the task is read with a GET, the agent runs,
the work is committed, and the completed task is POSTed back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgeo.backlog import BacklogUnavailableError, open_backlog
from forgeo.forgeo import Forgeo
from forgeo.git import GitManager
from forgeo.models import ExecutionResult, ExecutionStatus, TaskStatus
from tests.conftest import BacklogServer, FakeAgent, git, make_config, make_task


def build_forgeo(git_repo: Path, tmp_path: Path, url: str) -> tuple[Forgeo, FakeAgent]:
    """A Forgeo wired to a remote backlog and a scriptable agent."""
    config = make_config(git_repo, tmp_path, backlog=url, state_dir=tmp_path)
    agent = FakeAgent()
    forgeo = Forgeo(config, open_backlog(config), agent, GitManager(git_repo))
    return forgeo, agent


async def test_cycle_reads_a_task_and_posts_it_back_completed(
    git_repo: Path, tmp_path: Path, backlog_server: BacklogServer
) -> None:
    backlog_server.document = {
        "tasks": [json.loads(make_task(id="TASK-001").model_dump_json())]
    }
    forgeo, agent = build_forgeo(git_repo, tmp_path, backlog_server.url)
    agent.effect = lambda: (git_repo / "new.py").write_text("x = 1\n", encoding="utf-8")

    assert await forgeo.run_cycle() == "task"

    stored = backlog_server.tasks()
    assert [t["status"] for t in stored] == [TaskStatus.COMPLETED.value]
    assert "GET" in backlog_server.requests and "POST" in backlog_server.requests
    assert "Do the thing (#TASK-001)" in git(git_repo, "log", "-1", "--pretty=%s")


async def test_blocked_task_is_persisted_remotely_with_its_reason(
    git_repo: Path, tmp_path: Path, backlog_server: BacklogServer
) -> None:
    backlog_server.document = {
        "tasks": [json.loads(make_task(id="TASK-001").model_dump_json())]
    }
    forgeo, agent = build_forgeo(git_repo, tmp_path, backlog_server.url)
    agent.result = ExecutionResult(
        status=ExecutionStatus.BLOCKED, questions=["Which database?"]
    )

    assert await forgeo.run_cycle() == "task"

    stored = backlog_server.tasks()[0]
    assert stored["status"] == TaskStatus.BLOCKED.value
    assert stored["blocker_reason"] == ["Which database?"]
    assert stored["blocked_count"] == 1


async def test_an_unreachable_backlog_fails_the_cycle_without_refactoring(
    git_repo: Path, tmp_path: Path
) -> None:
    """The whole point of failing loudly: no refactor pass, no destructive POST."""
    with BacklogServer() as server:
        url = server.url
    # The server is now down; the port answers nothing.
    forgeo, agent = build_forgeo(git_repo, tmp_path, url)

    with pytest.raises(BacklogUnavailableError):
        await forgeo.run_cycle()

    assert agent.calls == [], "the agent must not run against an unknown backlog"


async def test_runtime_files_land_in_the_state_dir(
    git_repo: Path, tmp_path: Path, backlog_server: BacklogServer
) -> None:
    forgeo, _ = build_forgeo(git_repo, tmp_path, backlog_server.url)

    assert await forgeo.run_cycle() == "refactor"

    assert (tmp_path / "runs.jsonl").exists()
    assert not list(tmp_path.glob("*.json")), "a URL backlog writes no backlog file"
