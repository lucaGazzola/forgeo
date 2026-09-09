"""Marker-issue provider tests (GitHub + GitLab), parametrized over one spec table.

Both providers store engine state in a hidden body marker and share the
``MarkerIssueBacklog`` lifecycle; only the wire shape differs (``body`` vs
``description``, ``number`` vs ``iid``, ``open`` vs ``opened``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from forgeo.backlog import open_backlog
from forgeo.backlog_github import GithubBacklog, GithubClient, GithubRequestError
from forgeo.backlog_gitlab import GitlabBacklog, GitlabClient, GitlabRequestError
from forgeo.forgeo import Forgeo
from forgeo.git import GitManager
from forgeo.models import (
    ExecutionResult,
    ExecutionStatus,
    ForgeoConfig,
    GithubBacklogConfig,
    GitlabBacklogConfig,
    TaskStatus,
)
from tests.conftest import FakeAgent, FakeIssueClient, make_config, make_task


def _gh_issue(number: int, *, state: str = "open", labels: list[str] | None = None, body: str = "Build the feature.") -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Task {number}",
        "body": body,
        "state": state,
        "labels": [{"name": name} for name in (labels or [])],
        "created_at": "2026-08-20T10:00:00.000Z",
        "updated_at": "2026-08-20T10:00:00.000Z",
    }


def _gl_issue(iid: int, *, state: str = "opened", labels: list[str] | None = None, description: str = "Build the feature.") -> dict[str, Any]:
    return {
        "iid": iid,
        "id": iid,
        "title": f"Task {iid}",
        "description": description,
        "state": state,
        "labels": labels or [],
        "created_at": "2026-08-20T10:00:00.000Z",
        "updated_at": "2026-08-20T10:00:00.000Z",
    }


SPECS: dict[str, dict[str, Any]] = {
    "github": {
        "backlog_cls": GithubBacklog,
        "config_cls": GithubBacklogConfig,
        "config_kwargs": {"auth": {"token_env": "GITHUB_TOKEN"}, "repo": "owner/repo"},
        "url": "https://api.github.com",
        "provider": "github",
        "id_key": "number",
        "body_key": "body",
        "note_attr": "comments",
        "make_issue": _gh_issue,
        "make_kwargs": {"auth": {"token_env": "GITHUB_TOKEN"}, "repo": "owner/repo"},
        "label_names": lambda issue: [label["name"] for label in issue["labels"]],
    },
    "gitlab": {
        "backlog_cls": GitlabBacklog,
        "config_cls": GitlabBacklogConfig,
        "config_kwargs": {"auth": {"token_env": "GITLAB_TOKEN"}, "repo": "group/project"},
        "url": "https://gitlab.example.com",
        "provider": "gitlab",
        "id_key": "iid",
        "body_key": "description",
        "note_attr": "notes",
        "make_issue": _gl_issue,
        "make_kwargs": {"auth": {"token_env": "GITLAB_TOKEN"}, "repo": "group/project"},
        "label_names": lambda issue: list(issue["labels"]),
    },
}


@pytest.fixture(params=["github", "gitlab"])
def spec(request: pytest.FixtureRequest) -> dict[str, Any]:
    return SPECS[request.param]


def make_provider(spec: dict[str, Any], issues: list[dict[str, Any]]):
    error_cls = GithubRequestError if spec["provider"] == "github" else GitlabRequestError
    client = FakeIssueClient(
        issues,
        id_key=spec["id_key"],
        body_key=spec["body_key"],
        note_attr=spec["note_attr"],
        error_cls=error_cls,
    )
    config = spec["config_cls"](**spec["config_kwargs"])
    return spec["backlog_cls"](spec["url"], config, client=client), client


def stale_body() -> str:
    from forgeo.backlog_issue_base import embed_engine_state

    return embed_engine_state(
        "body", {"claimed_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()}
    )


def test_open_backlog_selects_github_provider() -> None:
    config = ForgeoConfig(
        agent_command="true",
        backlog="https://api.github.com",
        backlog_provider="github",
        github={"auth": {"token_env": "GITHUB_TOKEN"}, "repo": "owner/repo"},
    )
    assert isinstance(open_backlog(config), GithubBacklog)


def test_open_backlog_selects_gitlab_provider() -> None:
    config = ForgeoConfig(
        agent_command="true",
        backlog="https://gitlab.example.com",
        backlog_provider="gitlab",
        gitlab={"auth": {"token_env": "GITLAB_TOKEN"}, "repo": "group/project"},
    )
    assert isinstance(open_backlog(config), GitlabBacklog)


@pytest.mark.asyncio
async def test_list_tasks_maps_workflow_and_engine_states(spec: dict[str, Any]) -> None:
    from forgeo.backlog_issue_base import embed_engine_state

    make_issue = spec["make_issue"]
    body_key = spec["body_key"]
    blocked = embed_engine_state("Blocked body", {"blocker_reason": ["Need decision"], "blocked_count": 2})
    failed = embed_engine_state("Failed body", {"failure_reason": ["Timed out"]})
    backlog, _ = make_provider(
        spec,
        [
            make_issue(1, **{body_key: "Open body"}),
            make_issue(2, labels=["forgeo-blocked"], **{body_key: blocked}),
            make_issue(3, labels=["forgeo-failed"], **{body_key: failed}),
            make_issue(4, state="closed"),
            make_issue(5, labels=["forgeo-running"]),
        ],
    )
    tasks = await backlog.list_tasks()
    assert [(t.id, t.status) for t in tasks] == [
        ("1", TaskStatus.OPEN),
        ("2", TaskStatus.BLOCKED),
        ("3", TaskStatus.FAILED),
        ("4", TaskStatus.COMPLETED),
    ]
    assert tasks[1].blocker_reason == ["Need decision"]
    assert tasks[1].blocked_count == 2
    assert tasks[2].failure_reason == ["Timed out"]


@pytest.mark.asyncio
async def test_claim_moves_issue_to_running_and_records_lease(spec: dict[str, Any]) -> None:
    backlog, client = make_provider(spec, [spec["make_issue"](1)])
    task = (await backlog.list_tasks())[0]
    claimed = await backlog.claim_task(task)
    assert claimed is not None and claimed.status is TaskStatus.OPEN
    assert "forgeo-running" in spec["label_names"](client.issues[1])
    assert "claimed_at" in client.issues[1][spec["body_key"]]


@pytest.mark.asyncio
async def test_old_claim_is_released_before_a_new_cycle(spec: dict[str, Any]) -> None:
    backlog, client = make_provider(
        spec, [spec["make_issue"](1, labels=["forgeo-running"], **{spec["body_key"]: stale_body()})]
    )
    await backlog.recover_claims()
    assert "forgeo-running" not in spec["label_names"](client.issues[1])


@pytest.mark.asyncio
async def test_completion_clears_claim_and_transitions_to_closed(spec: dict[str, Any]) -> None:
    backlog, client = make_provider(spec, [spec["make_issue"](1)])
    task = (await backlog.list_tasks())[0]
    await backlog.claim_task(task)
    completed = await backlog.update_status(
        task.id, TaskStatus.COMPLETED, ExecutionResult(status=ExecutionStatus.SUCCESS, output_logs=["[stdout] finished"])
    )
    assert completed is not None and completed.status is TaskStatus.COMPLETED
    assert client.issues[1]["state"] == "closed"
    assert spec["label_names"](client.issues[1]) == []


@pytest.mark.asyncio
async def test_blocked_task_can_be_reopened(spec: dict[str, Any]) -> None:
    backlog, client = make_provider(spec, [spec["make_issue"](1)])
    task = (await backlog.list_tasks())[0]
    await backlog.claim_task(task)
    blocked = await backlog.set_blocked(task.id, ["Which db?"], ExecutionResult(status=ExecutionStatus.BLOCKED))
    assert blocked is not None and blocked.status is TaskStatus.BLOCKED
    assert "forgeo-blocked" in spec["label_names"](client.issues[1])
    notes = getattr(client, spec["note_attr"])
    assert notes and "BLOCKED" in notes[-1][1]
    reopened = await backlog.reopen_task(task.id)
    assert reopened is not None and reopened.status is TaskStatus.OPEN


@pytest.mark.asyncio
async def test_failed_task_retries(spec: dict[str, Any]) -> None:
    backlog, client = make_provider(spec, [spec["make_issue"](1)])
    task = (await backlog.list_tasks())[0]
    await backlog.claim_task(task)
    failed = await backlog.set_failed(task.id, ["timeout"], ExecutionResult(status=ExecutionStatus.ERROR))
    assert failed is not None and failed.status is TaskStatus.FAILED
    assert "forgeo-failed" in spec["label_names"](client.issues[1])
    await backlog.bump_failed_wait(task.id)
    retried = await backlog.retry_task(task.id)
    assert retried is not None and retried.status is TaskStatus.OPEN
    assert retried.retry_count == 1


@pytest.mark.asyncio
async def test_create_update_and_delete_use_issue_operations(spec: dict[str, Any]) -> None:
    backlog, client = make_provider(spec, [])
    task = await backlog.create_task(make_task(title="Created", description="Desc"))
    assert task.id == "1"
    assert client.issues[1]["title"] == "Created"
    updated = await backlog.update_task(task.id, {"title": "Renamed"})
    assert updated is not None and updated.title == "Renamed"
    deleted = await backlog.delete_task(task.id)
    assert deleted is not None
    assert 1 not in client.issues


@pytest.mark.asyncio
async def test_create_task_preserves_customization_fields(spec: dict[str, Any]) -> None:
    backlog, _ = make_provider(spec, [])
    task = await backlog.create_task(
        make_task(
            title="Created",
            description="Desc",
            acceptance_criteria=["must pass tests"],
            dependencies=["DEP-1"],
            files_to_modify=["a.py"],
            agent_command="claude --model haiku",
            agent_timeout_seconds=42.0,
            run_at=datetime(2026, 9, 1, tzinfo=UTC),
            retries_left=2,
            review_required=True,
        )
    )
    assert task.acceptance_criteria == ["must pass tests"]
    assert task.dependencies == ["DEP-1"]
    assert task.files_to_modify == ["a.py"]
    assert task.agent_command == "claude --model haiku"
    assert task.agent_timeout_seconds == 42.0
    assert task.run_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert task.retries_left == 2
    assert task.review_required is True


@pytest.mark.asyncio
async def test_pagination_respects_page_size(spec: dict[str, Any]) -> None:
    backlog, _ = make_provider(spec, [spec["make_issue"](i) for i in range(1, 6)])
    backlog.config.page_size = 2
    backlog.config.max_issues = 3
    assert len(await backlog.list_tasks()) == 3


def test_github_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    config = GithubBacklogConfig(auth={"token_env": "GITHUB_TOKEN"}, repo="owner/repo")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(Exception, match="GITHUB_TOKEN"):
        GithubClient("https://api.github.com", config)._auth_header()


def test_gitlab_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    config = GitlabBacklogConfig(auth={"token_env": "GITLAB_TOKEN"}, repo="group/project")
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    with pytest.raises(Exception, match="GITLAB_TOKEN"):
        GitlabClient("https://gitlab.example.com", config)._auth_headers()


@pytest.mark.asyncio
async def test_forgeo_cycle_claims_and_completes_an_issue(spec: dict[str, Any], git_repo, tmp_path) -> None:
    backlog, client = make_provider(spec, [spec["make_issue"](1)])
    config = make_config(
        git_repo,
        tmp_path,
        backlog=spec["url"],
        backlog_provider=spec["provider"],
        **{spec["provider"]: backlog.config},
        state_dir=tmp_path,
    )
    agent = FakeAgent()
    agent.effect = lambda: (git_repo / f"{spec['provider']}.py").write_text("x=1\n", encoding="utf-8")
    forgeo = Forgeo(config, backlog, agent, GitManager(git_repo))
    assert await forgeo.run_cycle() == "task"
    assert client.issues[1]["state"] == "closed"
