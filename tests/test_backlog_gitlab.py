"""GitLab provider mapping, claiming, and lifecycle tests."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from forgeo.backlog import open_backlog
from forgeo.backlog_gitlab import GitlabBacklog, GitlabClient
from forgeo.forgeo import Forgeo
from forgeo.git import GitManager
from forgeo.models import (
    ExecutionResult,
    ExecutionStatus,
    ForgeoConfig,
    GitlabBacklogConfig,
    TaskStatus,
)
from tests.conftest import FakeAgent, make_config, make_task


def gl_issue(
    iid: int,
    *,
    state: str = "opened",
    labels: list[str] | None = None,
    description: str = "Build the feature.",
    title: str | None = None,
) -> dict[str, Any]:
    return {
        "iid": iid,
        "id": iid,
        "title": title or f"Task {iid}",
        "description": description,
        "state": state,
        "labels": labels or [],
        "created_at": "2026-08-20T10:00:00.000Z",
        "updated_at": "2026-08-20T10:00:00.000Z",
    }


class FakeGitlabClient:
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues: dict[int, dict[str, Any]] = {item["iid"]: copy.deepcopy(item) for item in issues}
        self.notes: list[tuple[int, str]] = []
        self.calls: list[tuple[str, Any]] = []
        self._next = max((i["iid"] for i in issues), default=0) + 1

    def search_issues(self, *, page=1, per_page=20):
        self.calls.append(("search", page))
        rows = list(self.issues.values())
        start = (page - 1) * per_page
        return copy.deepcopy(rows[start : start + per_page])

    def get_issue(self, iid: int):
        self.calls.append(("get", iid))
        if iid not in self.issues:
            from forgeo.backlog_gitlab import GitlabRequestError

            raise GitlabRequestError("not found", status=404)
        return copy.deepcopy(self.issues[iid])

    def update_issue(self, iid: int, fields: dict[str, Any]):
        self.calls.append(("update", iid))
        issue = self.issues[iid]
        for k, v in fields.items():
            if k == "state_event":
                issue["state"] = "closed" if v == "close" else "opened"
            elif k == "labels":
                issue["labels"] = list(v)
            else:
                issue[k] = copy.deepcopy(v)
        issue["updated_at"] = datetime.now(UTC).isoformat()
        return copy.deepcopy(issue)

    def create_issue(self, fields: dict[str, Any]):
        iid = self._next
        self._next += 1
        issue = {
            "iid": iid,
            "id": iid,
            "title": fields.get("title", f"Task {iid}"),
            "description": fields.get("description", ""),
            "state": "opened",
            "labels": [fields.get("labels")] if isinstance(fields.get("labels"), str) else fields.get("labels", []),
            "created_at": "2026-08-21T10:00:00.000Z",
            "updated_at": "2026-08-21T10:00:00.000Z",
        }
        # normalize labels string case
        if isinstance(issue["labels"], str):
            issue["labels"] = [issue["labels"]]
        self.issues[iid] = copy.deepcopy(issue)
        return copy.deepcopy(issue)

    def add_note(self, iid: int, body: str):
        self.notes.append((iid, body))

    def delete_issue(self, iid: int):
        if iid in self.issues:
            del self.issues[iid]


def make_gitlab(issues: list[dict[str, Any]]) -> tuple[GitlabBacklog, FakeGitlabClient]:
    client = FakeGitlabClient(issues)
    config = GitlabBacklogConfig(auth={"token_env": "GITLAB_TOKEN"}, repo="group/project")
    return GitlabBacklog("https://gitlab.example.com", config, client=client), client


def test_open_backlog_selects_gitlab_provider() -> None:
    config = ForgeoConfig(
        agent_command="true",
        backlog="https://gitlab.example.com",
        backlog_provider="gitlab",
        gitlab={"auth": {"token_env": "GITLAB_TOKEN"}, "repo": "group/project"},
    )
    provider = open_backlog(config)
    assert isinstance(provider, GitlabBacklog)


@pytest.mark.asyncio
async def test_list_tasks_maps_workflow_and_engine_states() -> None:
    from forgeo.backlog_issue_base import embed_engine_state

    body_blocked = embed_engine_state("Blocked", {"blocker_reason": ["Need decision"], "blocked_count": 1})
    body_failed = embed_engine_state("Failed", {"failure_reason": ["err"]})
    backlog, _ = make_gitlab(
        [
            gl_issue(1, description="Open"),
            gl_issue(2, labels=["forgeo-blocked"], description=body_blocked),
            gl_issue(3, labels=["forgeo-failed"], description=body_failed),
            gl_issue(4, state="closed"),
            gl_issue(5, labels=["forgeo-running"]),
        ]
    )
    tasks = await backlog.list_tasks()
    assert [(t.id, t.status) for t in tasks] == [
        ("1", TaskStatus.OPEN),
        ("2", TaskStatus.BLOCKED),
        ("3", TaskStatus.FAILED),
        ("4", TaskStatus.COMPLETED),
    ]
    assert tasks[1].blocker_reason == ["Need decision"]
    assert tasks[2].failure_reason == ["err"]


@pytest.mark.asyncio
async def test_claim_and_recover() -> None:
    from forgeo.backlog_issue_base import embed_engine_state

    backlog, client = make_gitlab([gl_issue(1)])
    task = (await backlog.list_tasks())[0]
    claimed = await backlog.claim_task(task)
    assert claimed is not None
    assert "forgeo-running" in client.issues[1]["labels"]
    # old claim recovery
    body = embed_engine_state("body", {"claimed_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()})
    backlog2, client2 = make_gitlab([gl_issue(1, labels=["forgeo-running"], description=body)])
    await backlog2.recover_claims()
    assert "forgeo-running" not in client2.issues[1]["labels"]


@pytest.mark.asyncio
async def test_completion_and_blocked_flows() -> None:
    backlog, client = make_gitlab([gl_issue(1)])
    task = (await backlog.list_tasks())[0]
    await backlog.claim_task(task)
    completed = await backlog.update_status(
        task.id, TaskStatus.COMPLETED, ExecutionResult(status=ExecutionStatus.SUCCESS, output_logs=["[stdout] done"])
    )
    assert completed is not None and completed.status is TaskStatus.COMPLETED
    assert client.issues[1]["state"] == "closed"
    # blocked flow
    backlog, client = make_gitlab([gl_issue(1)])
    task = (await backlog.list_tasks())[0]
    await backlog.claim_task(task)
    blocked = await backlog.set_blocked(task.id, ["why"], ExecutionResult(status=ExecutionStatus.BLOCKED))
    assert blocked is not None and blocked.status is TaskStatus.BLOCKED
    reopened = await backlog.reopen_task(task.id)
    assert reopened is not None and reopened.status is TaskStatus.OPEN


@pytest.mark.asyncio
async def test_failed_retry() -> None:
    backlog, _ = make_gitlab([gl_issue(1)])
    task = (await backlog.list_tasks())[0]
    await backlog.claim_task(task)
    failed = await backlog.set_failed(task.id, ["err"], ExecutionResult(status=ExecutionStatus.ERROR))
    assert failed is not None and failed.status is TaskStatus.FAILED
    await backlog.bump_failed_wait(task.id)
    retried = await backlog.retry_task(task.id)
    assert retried is not None and retried.status is TaskStatus.OPEN


@pytest.mark.asyncio
async def test_create_update_delete() -> None:
    backlog, client = make_gitlab([])
    task = await backlog.create_task(make_task(title="Created", description="Desc"))
    assert task.id == "1"
    updated = await backlog.update_task(task.id, {"title": "Renamed"})
    assert updated is not None and updated.title == "Renamed"
    deleted = await backlog.delete_task(task.id)
    assert deleted is not None
    assert 1 not in client.issues


@pytest.mark.asyncio
async def test_pagination() -> None:
    issues = [gl_issue(i) for i in range(1, 6)]
    client = FakeGitlabClient(issues)
    config = GitlabBacklogConfig(auth={"token_env": "GITLAB_TOKEN"}, repo="group/project", page_size=2, max_issues=3)
    backlog = GitlabBacklog("https://gitlab.example.com", config, client=client)
    tasks = await backlog.list_tasks()
    assert len(tasks) == 3


def test_missing_token_raises(monkeypatch) -> None:
    config = GitlabBacklogConfig(auth={"token_env": "GITLAB_TOKEN"}, repo="group/project")
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    client = GitlabClient("https://gitlab.example.com", config)
    with pytest.raises(Exception, match="GITLAB_TOKEN"):
        client._auth_headers()


@pytest.mark.asyncio
async def test_forgeo_cycle_gitlab(git_repo, tmp_path) -> None:
    backlog, client = make_gitlab([gl_issue(1)])
    config = make_config(
        git_repo, tmp_path, backlog="https://gitlab.example.com", backlog_provider="gitlab", gitlab=backlog.config, state_dir=tmp_path
    )
    agent = FakeAgent()
    agent.effect = lambda: (git_repo / "gl.py").write_text("x=1\n", encoding="utf-8")
    forgeo = Forgeo(config, backlog, agent, GitManager(git_repo))
    assert await forgeo.run_cycle() == "task"
    assert client.issues[1]["state"] == "closed"
