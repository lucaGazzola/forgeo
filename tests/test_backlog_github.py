"""GitHub provider mapping, claiming, and lifecycle tests."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from forgeo.backlog import open_backlog
from forgeo.backlog_github import GithubBacklog, GithubClient
from forgeo.forgeo import Forgeo
from forgeo.git import GitManager
from forgeo.models import (
    ExecutionResult,
    ExecutionStatus,
    ForgeoConfig,
    GithubBacklogConfig,
    TaskStatus,
)
from tests.conftest import FakeAgent, make_config, make_task


def gh_issue(
    number: int,
    *,
    state: str = "open",
    labels: list[str] | None = None,
    body: str = "Build the feature.",
    title: str | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title or f"Task {number}",
        "body": body,
        "state": state,
        "labels": [{"name": name} for name in (labels or [])],
        "created_at": "2026-08-20T10:00:00.000Z",
        "updated_at": "2026-08-20T10:00:00.000Z",
    }


class FakeGithubClient:
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues: dict[int, dict[str, Any]] = {
            item["number"]: copy.deepcopy(item) for item in issues
        }
        self.comments: list[tuple[int, str]] = []
        self.calls: list[tuple[str, Any]] = []
        self._next = max((i["number"] for i in issues), default=0) + 1

    def search_issues(self, *, page=1, per_page=30, state="all"):
        self.calls.append(("search", page))
        rows = list(self.issues.values())
        start = (page - 1) * per_page
        return copy.deepcopy(rows[start : start + per_page])

    def get_issue(self, number: int):
        self.calls.append(("get", number))
        if number not in self.issues:
            from forgeo.backlog_github import GithubRequestError

            raise GithubRequestError("not found", status=404)
        return copy.deepcopy(self.issues[number])

    def update_issue(self, number: int, fields: dict[str, Any]):
        self.calls.append(("update", number))
        issue = self.issues[number]
        for k, v in fields.items():
            if k == "labels":
                issue["labels"] = [{"name": n} for n in v]
            elif k == "state":
                issue["state"] = v
            else:
                issue[k] = copy.deepcopy(v)
        # bump updated_at
        issue["updated_at"] = datetime.now(UTC).isoformat()
        return copy.deepcopy(issue)

    def create_issue(self, fields: dict[str, Any]):
        number = self._next
        self._next += 1
        self.calls.append(("create", number))
        issue = {
            "number": number,
            "title": fields.get("title", f"Task {number}"),
            "body": fields.get("body", ""),
            "state": "open",
            "labels": [{"name": n} for n in fields.get("labels", [])] if isinstance(fields.get("labels"), list) else [],
            "created_at": "2026-08-21T10:00:00.000Z",
            "updated_at": "2026-08-21T10:00:00.000Z",
        }
        self.issues[number] = copy.deepcopy(issue)
        return copy.deepcopy(issue)

    def add_comment(self, number: int, body: str):
        self.comments.append((number, body))

    def delete_issue(self, number: int):
        if number in self.issues:
            del self.issues[number]


def make_github(issues: list[dict[str, Any]]) -> tuple[GithubBacklog, FakeGithubClient]:
    client = FakeGithubClient(issues)
    config = GithubBacklogConfig(auth={"token_env": "GITHUB_TOKEN"}, repo="owner/repo")
    return GithubBacklog("https://api.github.com", config, client=client), client


def test_open_backlog_selects_github_provider() -> None:
    config = ForgeoConfig(
        agent_command="true",
        backlog="https://api.github.com",
        backlog_provider="github",
        github={"auth": {"token_env": "GITHUB_TOKEN"}, "repo": "owner/repo"},
    )
    provider = open_backlog(config)
    assert isinstance(provider, GithubBacklog)


@pytest.mark.asyncio
async def test_list_tasks_maps_workflow_and_engine_states() -> None:
    from forgeo.backlog_issue_base import embed_engine_state

    body_blocked = embed_engine_state("Blocked body", {"blocker_reason": ["Need decision"], "blocked_count": 2})
    body_failed = embed_engine_state("Failed body", {"failure_reason": ["Timed out"]})
    backlog, _ = make_github(
        [
            gh_issue(1, body="Open body"),
            gh_issue(2, labels=["forgeo-blocked"], body=body_blocked),
            gh_issue(3, labels=["forgeo-failed"], body=body_failed),
            gh_issue(4, state="closed"),
            gh_issue(5, labels=["forgeo-running"]),
        ]
    )
    tasks = await backlog.list_tasks()
    assert [(t.id, t.status) for t in tasks] == [
        ("1", TaskStatus.OPEN),
        ("2", TaskStatus.BLOCKED),
        ("3", TaskStatus.FAILED),
        ("4", TaskStatus.COMPLETED),
    ]
    # blocked/failed running is filtered
    assert tasks[1].blocker_reason == ["Need decision"]
    assert tasks[1].blocked_count == 2
    assert tasks[2].failure_reason == ["Timed out"]


@pytest.mark.asyncio
async def test_claim_moves_issue_to_running_and_records_lease() -> None:
    backlog, client = make_github([gh_issue(1)])
    task = (await backlog.list_tasks())[0]
    claimed = await backlog.claim_task(task)
    assert claimed is not None and claimed.status is TaskStatus.OPEN
    assert any(label["name"] == "forgeo-running" for label in client.issues[1]["labels"])
    body = client.issues[1]["body"]
    assert "claimed_at" in body


@pytest.mark.asyncio
async def test_old_claim_is_released_before_a_new_cycle() -> None:
    from forgeo.backlog_issue_base import embed_engine_state

    body = embed_engine_state(
        "body", {"claimed_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()}
    )
    backlog, client = make_github([gh_issue(1, labels=["forgeo-running"], body=body)])
    await backlog.recover_claims()
    assert all(label["name"] != "forgeo-running" for label in client.issues[1]["labels"])


@pytest.mark.asyncio
async def test_completion_clears_claim_and_transitions_to_closed() -> None:
    backlog, client = make_github([gh_issue(1)])
    task = (await backlog.list_tasks())[0]
    await backlog.claim_task(task)
    completed = await backlog.update_status(
        task.id, TaskStatus.COMPLETED, ExecutionResult(status=ExecutionStatus.SUCCESS, output_logs=["[stdout] finished"])
    )
    assert completed is not None and completed.status is TaskStatus.COMPLETED
    assert client.issues[1]["state"] == "closed"
    assert client.issues[1]["labels"] == []


@pytest.mark.asyncio
async def test_forgeo_cycle_claims_and_completes_a_github_issue(git_repo, tmp_path) -> None:
    backlog, client = make_github([gh_issue(1)])
    config = make_config(
        git_repo,
        tmp_path,
        backlog="https://api.github.com",
        backlog_provider="github",
        github=backlog.config,
        state_dir=tmp_path,
    )
    agent = FakeAgent()
    agent.effect = lambda: (git_repo / "gh.py").write_text("x=1\n", encoding="utf-8")
    forgeo = Forgeo(config, backlog, agent, GitManager(git_repo))
    assert await forgeo.run_cycle() == "task"
    assert client.issues[1]["state"] == "closed"


@pytest.mark.asyncio
async def test_blocked_task_can_be_reopened() -> None:
    backlog, client = make_github([gh_issue(1)])
    task = (await backlog.list_tasks())[0]
    await backlog.claim_task(task)
    blocked = await backlog.set_blocked(task.id, ["Which db?"], ExecutionResult(status=ExecutionStatus.BLOCKED))
    assert blocked is not None and blocked.status is TaskStatus.BLOCKED
    assert any(label["name"] == "forgeo-blocked" for label in client.issues[1]["labels"])
    assert client.comments and "BLOCKED" in client.comments[-1][1]
    reopened = await backlog.reopen_task(task.id)
    assert reopened is not None and reopened.status is TaskStatus.OPEN


@pytest.mark.asyncio
async def test_failed_task_retries() -> None:
    backlog, client = make_github([gh_issue(1)])
    task = (await backlog.list_tasks())[0]
    await backlog.claim_task(task)
    failed = await backlog.set_failed(task.id, ["timeout"], ExecutionResult(status=ExecutionStatus.ERROR))
    assert failed is not None and failed.status is TaskStatus.FAILED
    assert any(label["name"] == "forgeo-failed" for label in client.issues[1]["labels"])
    await backlog.bump_failed_wait(task.id)
    retried = await backlog.retry_task(task.id)
    assert retried is not None and retried.status is TaskStatus.OPEN
    assert retried.retry_count == 1


@pytest.mark.asyncio
async def test_create_update_and_delete_use_github_issue_operations() -> None:
    backlog, client = make_github([])
    task = await backlog.create_task(make_task(title="Created", description="Desc"))
    assert task.id == "1"
    assert client.issues[1]["title"] == "Created"
    updated = await backlog.update_task(task.id, {"title": "Renamed"})
    assert updated is not None and updated.title == "Renamed"
    deleted = await backlog.delete_task(task.id)
    assert deleted is not None
    assert 1 not in client.issues


@pytest.mark.asyncio
async def test_pagination_respects_page_size() -> None:
    issues = [gh_issue(i) for i in range(1, 6)]
    client = FakeGithubClient(issues)
    config = GithubBacklogConfig(auth={"token_env": "GITHUB_TOKEN"}, repo="owner/repo", page_size=2, max_issues=3)
    backlog = GithubBacklog("https://api.github.com", config, client=client)
    tasks = await backlog.list_tasks()
    assert len(tasks) == 3


def test_missing_token_raises(monkeypatch) -> None:
    config = GithubBacklogConfig(auth={"token_env": "GITHUB_TOKEN"}, repo="owner/repo")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    client = GithubClient("https://api.github.com", config)
    with pytest.raises(Exception, match="GITHUB_TOKEN"):
        client._auth_header()
