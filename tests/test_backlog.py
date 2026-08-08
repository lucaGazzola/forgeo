"""Backlog tests: task lifecycle, oldest-OPEN ordering, corruption tolerance."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from forgeo.backlog import JSONBacklog, oldest_open_task
from forgeo.models import Task, TaskStatus
from tests.conftest import make_task


def test_oldest_open_task_picks_oldest():
    older = make_task(
        id="OLD",
        title="Older",
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    newer = make_task(id="NEW", title="Newer", created_at=datetime.now(UTC))
    done = make_task(id="DONE", status=TaskStatus.COMPLETED)
    assert oldest_open_task([newer, done, older]) is older


def test_oldest_open_task_none_when_empty_or_no_open():
    assert oldest_open_task([]) is None
    assert oldest_open_task([make_task(status=TaskStatus.COMPLETED)]) is None


async def test_fetch_oldest_open_task(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    older = Task(
        id="A", title="older", description="d", created_at=datetime.now(UTC) - timedelta(hours=2)
    )
    newer = Task(id="B", title="newer", description="d", created_at=datetime.now(UTC))
    await backlog.create_task(newer)
    await backlog.create_task(older)

    fetched = await backlog.fetch_next_task()
    assert fetched.id == "A"


async def test_fetch_skips_non_open_tasks(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    for status in (TaskStatus.BLOCKED, TaskStatus.COMPLETED, TaskStatus.FAILED):
        await backlog.create_task(Task(id=status.value, title="t", description="d", status=status))
    assert await backlog.fetch_next_task() is None


async def test_fetch_prefers_open_over_blocked(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.create_task(
        Task(id="BLOCKED-1", title="b", description="d", status=TaskStatus.BLOCKED)
    )
    await backlog.create_task(make_task(id="OPEN-1"))
    fetched = await backlog.fetch_next_task()
    assert fetched.id == "OPEN-1"


async def test_update_status_persists_and_bumps_timestamp(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    updated = await backlog.update_status(task.id, TaskStatus.COMPLETED)
    assert updated.status is TaskStatus.COMPLETED
    stored = await backlog.get_task(task.id)
    assert stored.status is TaskStatus.COMPLETED
    assert stored.updated_at >= task.updated_at


async def test_update_status_unknown_id_returns_none(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.create_task(make_task())
    assert await backlog.update_status("MISSING", TaskStatus.COMPLETED) is None


async def test_set_blocked_persists_reason_and_increments_count(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    blocked = await backlog.set_blocked(task.id, ["I need a decision"])
    assert blocked.status is TaskStatus.BLOCKED
    assert blocked.blocker_reason == ["I need a decision"]
    assert blocked.blocked_count == 1

    blocked = await backlog.set_blocked(task.id, ["Another decision"])
    assert blocked.blocked_count == 2
    assert blocked.blocker_reason == ["Another decision"]

    stored = await backlog.get_task(task.id)
    assert stored.status is TaskStatus.BLOCKED
    assert stored.blocker_reason == ["Another decision"]
    assert stored.blocked_count == 2
    assert stored.updated_at >= task.updated_at

    disk = json.loads((tmp_path / "backlog.json").read_text(encoding="utf-8"))
    entry = disk["tasks"][0]
    assert entry["blocker_reason"] == ["Another decision"]
    assert entry["blocked_count"] == 2


async def test_set_blocked_unknown_id_returns_none(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.create_task(make_task())
    assert await backlog.set_blocked("MISSING", ["?"]) is None


async def test_set_failed_persists_reason(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    failed = await backlog.set_failed(task.id, ["timed out after 60s"])
    assert failed.status is TaskStatus.FAILED
    assert failed.failure_reason == ["timed out after 60s"]

    failed = await backlog.set_failed(task.id, ["exit code 3"])
    assert failed.status is TaskStatus.FAILED
    assert failed.failure_reason == ["exit code 3"]

    stored = await backlog.get_task(task.id)
    assert stored.status is TaskStatus.FAILED
    assert stored.failure_reason == ["exit code 3"]
    assert stored.updated_at >= task.updated_at

    disk = json.loads((tmp_path / "backlog.json").read_text(encoding="utf-8"))
    entry = disk["tasks"][0]
    assert entry["failure_reason"] == ["exit code 3"]


async def test_set_failed_unknown_id_returns_none(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.create_task(make_task())
    assert await backlog.set_failed("MISSING", ["?"]) is None


async def test_update_status_clears_failure_reason(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    await backlog.set_failed(task.id, ["timed out after 60s"])
    updated = await backlog.update_status(task.id, TaskStatus.OPEN)
    assert updated.status is TaskStatus.OPEN
    assert updated.failure_reason == []
    stored = await backlog.get_task(task.id)
    assert stored.failure_reason == []


async def test_set_blocked_clears_failure_reason(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    await backlog.set_failed(task.id, ["timed out after 60s"])
    blocked = await backlog.set_blocked(task.id, ["I need a decision"])
    assert blocked.status is TaskStatus.BLOCKED
    assert blocked.failure_reason == []
    assert blocked.blocker_reason == ["I need a decision"]


async def test_reopen_task_clears_failure_reason(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    await backlog.set_failed(task.id, ["timed out after 60s"])
    reopened = await backlog.reopen_task(task.id)
    assert reopened.status is TaskStatus.OPEN
    assert reopened.failure_reason == []


async def test_reopen_task_clears_reason_keeps_count(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    await backlog.set_blocked(task.id, ["I need a decision"])
    reopened = await backlog.reopen_task(task.id)
    assert reopened.status is TaskStatus.OPEN
    assert reopened.blocker_reason == []
    assert reopened.blocked_count == 1

    stored = await backlog.get_task(task.id)
    assert stored.status is TaskStatus.OPEN
    assert stored.blocker_reason == []
    assert stored.blocked_count == 1


async def test_reopen_task_unknown_id_returns_none(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.create_task(make_task())
    assert await backlog.reopen_task("MISSING") is None


async def test_delete_task_removes_from_backlog(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    deleted = await backlog.delete_task(task.id)
    assert deleted.id == task.id
    assert deleted.title == task.title
    assert await backlog.list_tasks() == []
    assert await backlog.get_task(task.id) is None
    disk = json.loads((tmp_path / "backlog.json").read_text(encoding="utf-8"))
    assert disk["tasks"] == []


async def test_delete_task_keeps_other_tasks(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    keep = await backlog.create_task(make_task(id="KEEP"))
    await backlog.create_task(make_task(id="GONE"))
    deleted = await backlog.delete_task("GONE")
    assert deleted.id == "GONE"
    remaining = await backlog.list_tasks()
    assert [task.id for task in remaining] == ["KEEP"]
    assert remaining[0].model_dump(mode="json") == keep.model_dump(mode="json")


async def test_delete_task_unknown_id_returns_none(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.create_task(make_task())
    assert await backlog.delete_task("MISSING") is None
    assert len(await backlog.list_tasks()) == 1


async def test_create_duplicate_id_raises(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.create_task(make_task())
    with pytest.raises(ValueError):
        await backlog.create_task(make_task())


async def test_update_task_persists_and_preserves_identity(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(
        make_task(
            dependencies=["D-1"],
            acceptance_criteria=["passes pytest"],
            files_to_modify=["src/app.py"],
            agent_command="claude -p",
            agent_timeout_seconds=120,
        )
    )
    updated = await backlog.update_task(
        task.id,
        {
            "title": "New title",
            "description": "New description.",
            "acceptance_criteria": ["passes pytest", "no regressions"],
            "dependencies": ["D-2"],
            "files_to_modify": ["src/new.py"],
            "agent_command": ["claude", "-p"],
            "agent_timeout_seconds": 60,
        },
    )
    assert updated.id == task.id
    assert updated.status is task.status
    assert updated.created_at == task.created_at
    assert updated.title == "New title"
    assert updated.description == "New description."
    assert updated.acceptance_criteria == ["passes pytest", "no regressions"]
    assert updated.dependencies == ["D-2"]
    assert updated.files_to_modify == ["src/new.py"]
    assert updated.agent_command == ["claude", "-p"]
    assert updated.agent_timeout_seconds == 60
    assert updated.updated_at >= task.updated_at

    stored = await backlog.get_task(task.id)
    assert stored.model_dump(mode="json") == updated.model_dump(mode="json")
    disk = json.loads((tmp_path / "backlog.json").read_text(encoding="utf-8"))
    entry = disk["tasks"][0]
    assert entry["id"] == task.id
    assert entry["status"] == task.status.value
    assert datetime.fromisoformat(entry["created_at"]) == task.created_at


async def test_update_task_partial_fields(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task(description="Keep me."))
    updated = await backlog.update_task(task.id, {"title": "Only title"})
    assert updated.title == "Only title"
    assert updated.description == "Keep me."
    assert updated.id == task.id
    assert updated.status is task.status


async def test_update_task_clears_optional_fields(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(
        make_task(agent_command="claude -p", agent_timeout_seconds=120)
    )
    updated = await backlog.update_task(
        task.id, {"agent_command": None, "agent_timeout_seconds": None}
    )
    assert updated.agent_command is None
    assert updated.agent_timeout_seconds is None
    stored = await backlog.get_task(task.id)
    assert stored.agent_command is None
    assert stored.agent_timeout_seconds is None


async def test_update_task_unknown_id_returns_none(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.create_task(make_task())
    assert (
        await backlog.update_task("MISSING", {"title": "Nope"})
    ) is None


async def test_update_task_unknown_field_raises(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    with pytest.raises(ValueError, match="unknown task field"):
        await backlog.update_task(task.id, {"status": "COMPLETED"})
    with pytest.raises(ValueError, match="unknown task field"):
        await backlog.update_task(task.id, {"blocker_reason": ["x"]})
    with pytest.raises(ValueError, match="unknown task field"):
        await backlog.update_task(task.id, {"blocked_count": 1})
    with pytest.raises(ValueError, match="unknown task field"):
        await backlog.update_task(task.id, {"bogus": 1})


async def test_update_task_invalid_values_raise(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())

    for bad in (
        {"title": ""},
        {"title": "   "},
        {"title": 42},
        {"description": ""},
        {"description": "   "},
        {"description": ["not", "a", "string"]},
        {"acceptance_criteria": "nope"},
        {"acceptance_criteria": [1, 2]},
        {"dependencies": 7},
        {"files_to_modify": [None]},
        {"agent_command": ""},
        {"agent_command": []},
        {"agent_timeout_seconds": 0},
        {"agent_timeout_seconds": -1},
    ):
        with pytest.raises(ValueError):
            await backlog.update_task(task.id, bad)

    stored = await backlog.get_task(task.id)
    assert stored.title == "Do the thing"
    assert stored.description == "Build it."
    assert stored.updated_at == task.updated_at


async def test_update_task_non_dict_updates_raise(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    with pytest.raises(TypeError, match="dict"):
        await backlog.update_task(task.id, ["title"])


async def test_missing_file_yields_empty_backlog(tmp_path):
    backlog = JSONBacklog(tmp_path / "nope.json")
    assert await backlog.list_tasks() == []
    assert await backlog.fetch_next_task() is None


async def test_corrupt_file_is_preserved_and_yields_empty_backlog(tmp_path, caplog):
    path = tmp_path / "backlog.json"
    path.write_text("{not valid json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="forgeo.backlog"):
        assert await JSONBacklog(path).list_tasks() == []
    assert not path.exists()
    corrupt = list(tmp_path.glob("backlog.json.corrupt-*"))
    assert len(corrupt) == 1
    assert corrupt[0].read_text(encoding="utf-8") == "{not valid json"
    assert "corrupt" in caplog.text.lower()


async def test_invalid_task_row_does_not_kill_the_store(tmp_path):
    path = tmp_path / "backlog.json"
    path.write_text(json.dumps({"tasks": [{"id": "BAD"}], "junk": [1]}), encoding="utf-8")
    tasks = await JSONBacklog(path).list_tasks()
    assert len(tasks) == 1
    assert tasks[0].id == "BAD"
    assert tasks[0].status is TaskStatus.FAILED
