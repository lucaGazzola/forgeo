"""Jira provider mapping, claiming, and lifecycle tests."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from forgeo.backlog import open_backlog
from forgeo.backlog_jira import JiraBacklog, JiraClient
from forgeo.forgeo import Forgeo
from forgeo.git import GitManager
from forgeo.models import (
    ExecutionResult,
    ExecutionStatus,
    ForgeoConfig,
    JiraBacklogConfig,
    TaskStatus,
)
from tests.conftest import FakeAgent, make_config, make_task

STATUSES = {
    "1": {"id": "1", "name": "To Do"},
    "2": {"id": "2", "name": "In Progress"},
    "3": {"id": "3", "name": "Blocked"},
    "4": {"id": "4", "name": "Done"},
}


def issue(
    key: str,
    *,
    status: str = "1",
    labels: list[str] | None = None,
    description: Any = "Build the feature.",
) -> dict[str, Any]:
    return {
        "key": key,
        "fields": {
            "summary": f"Task {key}",
            "description": description,
            "status": STATUSES[status],
            "created": "2026-08-20T10:00:00.000+0000",
            "updated": "2026-08-20T10:00:00.000+0000",
            "labels": labels or [],
            "issuelinks": [],
        },
    }


class FakeJiraClient:
    """In-memory Jira API with workflow transitions and issue properties."""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = {item["key"]: copy.deepcopy(item) for item in issues}
        self.properties: dict[str, dict[str, Any]] = {}
        self.comments: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []
        self.created_fields: list[dict[str, Any]] = []
        self._next_key = 10

    def search_issues(
        self,
        jql,
        *,
        start_at=0,
        max_results=50,
        fields,
        next_page_token=None,
    ):
        self.calls.append(("search", jql))
        rows = list(self.issues.values())[start_at : start_at + max_results]
        return {"issues": copy.deepcopy(rows), "total": len(self.issues)}

    def get_issue(self, issue_key, *, fields):
        self.calls.append(("get", issue_key))
        return copy.deepcopy(self.issues[issue_key])

    def get_transitions(self, issue_key):
        status = self.issues[issue_key]["fields"]["status"]["id"]
        transitions = {
            "1": [{"id": "running", "to": STATUSES["2"]}],
            "2": [
                {"id": "open", "to": STATUSES["1"]},
                {"id": "blocked", "to": STATUSES["3"]},
                {"id": "done", "to": STATUSES["4"]},
            ],
            "3": [{"id": "open", "to": STATUSES["1"]}],
        }
        return transitions.get(status, [])

    def transition(self, issue_key, transition_id):
        targets = {
            "running": "2",
            "open": "1",
            "blocked": "3",
            "done": "4",
        }
        target = targets[transition_id]
        self.issues[issue_key]["fields"]["status"] = copy.deepcopy(STATUSES[target])

    def update_issue(self, issue_key, *, fields=None, update=None):
        current = self.issues[issue_key]["fields"]
        if fields:
            current.update(copy.deepcopy(fields))
        for field, operations in (update or {}).items():
            if field != "labels":
                continue
            labels = current.setdefault("labels", [])
            for operation in operations:
                if "add" in operation and operation["add"] not in labels:
                    labels.append(operation["add"])
                if "remove" in operation and operation["remove"] in labels:
                    labels.remove(operation["remove"])

    def create_issue(self, fields):
        self._next_key += 1
        key = f"APP-{self._next_key}"
        self.created_fields.append(copy.deepcopy(fields))
        self.issues[key] = {
            "key": key,
            "fields": {
                "summary": fields["summary"],
                "description": fields["description"],
                "status": copy.deepcopy(STATUSES["1"]),
                "created": "2026-08-21T10:00:00.000+0000",
                "updated": "2026-08-21T10:00:00.000+0000",
                "labels": [],
                "issuelinks": [],
                **{
                    name: value
                    for name, value in fields.items()
                    if name.startswith("customfield_")
                },
            },
        }
        return {"key": key}

    def delete_issue(self, issue_key):
        del self.issues[issue_key]

    def add_comment(self, issue_key, body):
        self.comments.append((issue_key, body))

    def get_property(self, issue_key, property_key):
        value = self.properties.get(issue_key)
        return {"value": copy.deepcopy(value)} if value is not None else {}

    def set_property(self, issue_key, property_key, value):
        self.properties[issue_key] = copy.deepcopy(value)


def make_jira(issues: list[dict[str, Any]]) -> tuple[JiraBacklog, FakeJiraClient]:
    client = FakeJiraClient(issues)
    config = JiraBacklogConfig(
        auth={"scheme": "bearer", "token_env": "JIRA_TOKEN"},
        jql="project = APP",
        project_key="APP",
        workflow={
            "open_statuses": ["1"],
            "open_status": "1",
            "running_status": "2",
            "blocked_status": "3",
            "completed_status": "4",
        },
        fields={
            "acceptance_criteria": "customfield_10042",
            "dependencies": "customfield_10043",
        },
    )
    return JiraBacklog("https://jira.test", config, client=client), client


def test_open_backlog_selects_jira_provider() -> None:
    config = ForgeoConfig(
        agent_command="true",
        backlog="https://jira.test",
        backlog_provider="jira",
        jira={
            "auth": {"scheme": "bearer", "token_env": "JIRA_TOKEN"},
            "jql": "project = APP",
        },
    )

    provider = open_backlog(config)

    assert isinstance(provider, JiraBacklog)
    assert provider.url == "https://jira.test"


def test_blocks_links_become_dependencies_of_the_blocked_issue() -> None:
    links = [
        {
            "type": {
                "name": "Blocks",
                "inward": "is blocked by",
                "outward": "blocks",
            },
            "inwardIssue": {"key": "APP-1"},
            "outwardIssue": {"key": "APP-2"},
        }
    ]

    assert JiraBacklog._issue_link_dependencies(links) == ["APP-1"]


def test_jira_cloud_search_uses_cursor_endpoint_and_bearer_auth(monkeypatch) -> None:
    config = JiraBacklogConfig(
        auth={"scheme": "bearer", "token_env": "JIRA_TOKEN"},
        jql="project = APP",
    )
    monkeypatch.setenv("JIRA_TOKEN", "secret")
    seen: list[Any] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return b'{"issues": [], "isLast": true}'

    def urlopen(request, timeout):
        seen.append(request)
        return Response()

    monkeypatch.setattr("forgeo.backlog_jira.urllib.request.urlopen", urlopen)

    JiraClient("https://jira.test", config).search_issues(
        "project = APP",
        next_page_token="cursor-1",
        fields=["summary"],
    )

    request = seen[0]
    query = parse_qs(urlparse(request.full_url).query)
    assert urlparse(request.full_url).path == "/rest/api/3/search/jql"
    assert query["nextPageToken"] == ["cursor-1"]
    assert "startAt" not in query
    assert request.headers["Authorization"] == "Bearer secret"


def test_jira_v2_search_keeps_offset_pagination(monkeypatch) -> None:
    config = JiraBacklogConfig(
        auth={"scheme": "bearer", "token_env": "JIRA_TOKEN"},
        jql="project = APP",
        api_version=2,
    )
    monkeypatch.setenv("JIRA_TOKEN", "secret")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return b'{"issues": [], "total": 0}'

    seen: list[Any] = []

    def urlopen(request, timeout):
        seen.append(request)
        return Response()

    monkeypatch.setattr("forgeo.backlog_jira.urllib.request.urlopen", urlopen)
    JiraClient("https://jira.test", config).search_issues(
        "project = APP",
        start_at=25,
        fields=["summary"],
    )

    request = seen[0]
    query = parse_qs(urlparse(request.full_url).query)
    assert urlparse(request.full_url).path == "/rest/api/2/search"
    assert query["startAt"] == ["25"]
    assert "nextPageToken" not in query


@pytest.mark.asyncio
async def test_list_tasks_maps_workflow_and_engine_states() -> None:
    backlog, client = make_jira(
        [
            issue("APP-1"),
            issue("APP-2", labels=["forgeo-blocked"]),
            issue("APP-3", labels=["forgeo-failed"]),
            issue("APP-4", status="4"),
            issue("APP-5", status="2"),
        ]
    )
    client.properties["APP-2"] = {
        "state": "BLOCKED",
        "blocker_reason": ["Need a decision"],
        "blocked_count": 2,
    }
    client.properties["APP-3"] = {
        "state": "FAILED",
        "failure_reason": ["Timed out"],
    }

    tasks = await backlog.list_tasks()

    assert [(task.id, task.status) for task in tasks] == [
        ("APP-1", TaskStatus.OPEN),
        ("APP-2", TaskStatus.BLOCKED),
        ("APP-3", TaskStatus.FAILED),
        ("APP-4", TaskStatus.COMPLETED),
    ]
    assert tasks[1].blocker_reason == ["Need a decision"]
    assert tasks[1].blocked_count == 2
    assert tasks[2].failure_reason == ["Timed out"]


@pytest.mark.asyncio
async def test_claim_moves_issue_to_running_and_records_lease() -> None:
    backlog, client = make_jira([issue("APP-1")])
    task = (await backlog.list_tasks())[0]

    claimed = await backlog.claim_task(task)

    assert claimed is not None and claimed.status is TaskStatus.OPEN
    assert client.issues["APP-1"]["fields"]["status"]["id"] == "2"
    assert "forgeo-running" in client.issues["APP-1"]["fields"]["labels"]
    assert client.properties["APP-1"]["state"] == "RUNNING"
    assert "claimed_at" in client.properties["APP-1"]


@pytest.mark.asyncio
async def test_old_claim_is_released_before_a_new_cycle() -> None:
    backlog, client = make_jira([issue("APP-1", status="2", labels=["forgeo-running"])])
    client.properties["APP-1"] = {
        "state": "RUNNING",
        "claimed_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
    }

    await backlog.recover_claims()

    assert client.issues["APP-1"]["fields"]["status"]["id"] == "1"
    assert client.issues["APP-1"]["fields"]["labels"] == []
    assert client.properties["APP-1"]["state"] == "OPEN"


@pytest.mark.asyncio
async def test_completion_clears_claim_and_transitions_to_done() -> None:
    backlog, client = make_jira([issue("APP-1")])
    task = (await backlog.list_tasks())[0]
    await backlog.claim_task(task)

    completed = await backlog.update_status(
        task.id,
        TaskStatus.COMPLETED,
        ExecutionResult(
            status=ExecutionStatus.SUCCESS,
            output_logs=["[stdout] finished"],
        ),
    )

    assert completed is not None and completed.status is TaskStatus.COMPLETED
    assert client.issues["APP-1"]["fields"]["status"]["id"] == "4"
    assert client.issues["APP-1"]["fields"]["labels"] == []
    assert client.properties["APP-1"]["agent_response"] == "finished"
    assert client.properties["APP-1"]["state"] == "COMPLETED"


@pytest.mark.asyncio
async def test_forgeo_cycle_claims_and_completes_a_jira_issue(git_repo, tmp_path) -> None:
    backlog, client = make_jira([issue("APP-1")])
    config = make_config(
        git_repo,
        tmp_path,
        backlog="https://jira.test",
        backlog_provider="jira",
        jira=backlog.config,
        state_dir=tmp_path,
    )
    agent = FakeAgent()
    agent.effect = lambda: (git_repo / "jira.py").write_text("answer = 42\n", encoding="utf-8")
    forgeo = Forgeo(config, backlog, agent, GitManager(git_repo))

    assert await forgeo.run_cycle() == "task"
    assert client.issues["APP-1"]["fields"]["status"]["id"] == "4"
    assert client.properties["APP-1"]["state"] == "COMPLETED"


@pytest.mark.asyncio
async def test_blocked_task_can_be_reopened() -> None:
    backlog, client = make_jira([issue("APP-1")])
    task = (await backlog.list_tasks())[0]
    await backlog.claim_task(task)

    blocked = await backlog.set_blocked(
        task.id,
        ["Which database?"],
        ExecutionResult(status=ExecutionStatus.BLOCKED),
    )
    assert blocked is not None and blocked.status is TaskStatus.BLOCKED
    assert client.issues["APP-1"]["fields"]["status"]["id"] == "3"
    assert client.properties["APP-1"]["blocker_reason"] == ["Which database?"]
    assert client.comments and "BLOCKED" in client.comments[-1][1]

    reopened = await backlog.reopen_task(task.id)

    assert reopened is not None and reopened.status is TaskStatus.OPEN
    assert client.issues["APP-1"]["fields"]["status"]["id"] == "1"
    assert client.properties["APP-1"]["blocker_reason"] == []


@pytest.mark.asyncio
async def test_failed_task_retries_without_a_failed_workflow_status() -> None:
    backlog, client = make_jira([issue("APP-1")])
    task = (await backlog.list_tasks())[0]
    await backlog.claim_task(task)

    failed = await backlog.set_failed(
        task.id,
        ["Agent timed out"],
        ExecutionResult(status=ExecutionStatus.ERROR, error="timeout"),
    )
    assert failed is not None and failed.status is TaskStatus.FAILED
    assert client.issues["APP-1"]["fields"]["status"]["id"] == "1"
    assert "forgeo-failed" in client.issues["APP-1"]["fields"]["labels"]

    await backlog.bump_failed_wait(task.id)
    retried = await backlog.retry_task(task.id)

    assert retried is not None and retried.status is TaskStatus.OPEN
    assert retried.retry_count == 1
    assert client.issues["APP-1"]["fields"]["labels"] == []


@pytest.mark.asyncio
async def test_create_update_and_delete_use_jira_issue_operations() -> None:
    backlog, client = make_jira([])
    task = await backlog.create_task(
        make_task(
            title="Created remotely",
            description="Remote description.",
            acceptance_criteria=["passes tests"],
        )
    )
    assert task.id == "APP-11"
    assert client.created_fields[0]["project"] == {"key": "APP"}
    assert client.created_fields[0]["customfield_10042"] == ["passes tests"]

    updated = await backlog.update_task(task.id, {"title": "Renamed remotely"})
    assert updated is not None and updated.title == "Renamed remotely"
    deleted = await backlog.delete_task(task.id)
    assert deleted is not None and task.id == deleted.id
    assert task.id not in client.issues
