"""Backlog tests: task lifecycle, oldest-OPEN ordering, corruption tolerance."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from forgeo.backlog import (
    JSONBacklog,
    next_due_run_at,
    oldest_open_task,
    unsatisfied_dependencies,
)
from forgeo.models import Task, TaskStatus
from tests.conftest import make_result, make_task


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


def test_oldest_open_task_skips_task_with_uncompleted_dependency():
    dep = make_task(
        id="DEP-1", title="Dep", status=TaskStatus.BLOCKED,
        created_at=datetime.now(UTC) - timedelta(hours=3),
    )
    waiting = make_task(
        id="WAIT", title="Waits", dependencies=["DEP-1"],
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    eligible = make_task(id="GO", title="Eligible", created_at=datetime.now(UTC))
    assert oldest_open_task([waiting, dep, eligible]) is eligible
    assert oldest_open_task([waiting, dep]) is None


def test_oldest_open_task_runs_open_dependency_first():
    dep = make_task(
        id="DEP-1", title="Dep", status=TaskStatus.OPEN,
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    waiting = make_task(
        id="WAIT", title="Waits", dependencies=["DEP-1"],
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    assert oldest_open_task([waiting, dep]) is dep


def test_oldest_open_task_picks_waiting_task_once_dependency_completed():
    dep = make_task(
        id="DEP-1", title="Dep", status=TaskStatus.COMPLETED,
        created_at=datetime.now(UTC) - timedelta(hours=3),
    )
    waiting = make_task(
        id="WAIT", title="Waits", dependencies=["DEP-1"],
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    eligible = make_task(id="GO", title="Eligible", created_at=datetime.now(UTC))
    assert oldest_open_task([waiting, dep, eligible]) is waiting


def test_oldest_open_task_missing_dependency_never_picked():
    waiting = make_task(
        id="WAIT", title="Waits", dependencies=["GHOST"],
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    eligible = make_task(id="GO", title="Eligible", created_at=datetime.now(UTC))
    assert oldest_open_task([waiting, eligible]) is eligible
    assert oldest_open_task([waiting]) is None


def test_oldest_open_task_requires_all_dependencies_completed():
    done = make_task(id="DONE", status=TaskStatus.COMPLETED)
    dep = make_task(
        id="DEP-1", title="Dep", status=TaskStatus.FAILED,
        created_at=datetime.now(UTC) - timedelta(hours=3),
    )
    waiting = make_task(
        id="WAIT", title="Waits", dependencies=["DONE", "DEP-1"],
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    assert oldest_open_task([waiting, done, dep]) is None


def test_oldest_open_task_cycle_returns_none():
    a = make_task(
        id="A", title="A", dependencies=["B"],
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    b = make_task(
        id="B", title="B", dependencies=["A"],
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    assert oldest_open_task([a, b]) is None


def test_oldest_open_task_self_dependency_never_runnable():
    task = make_task(
        id="SELF", title="Self", dependencies=["SELF"],
        created_at=datetime.now(UTC),
    )
    assert oldest_open_task([task]) is None


def test_oldest_open_task_picks_past_run_at_over_older_open():
    now = datetime.now(UTC)
    scheduled = make_task(
        id="SCHED",
        title="After deploy",
        created_at=now - timedelta(hours=1),
        run_at=now - timedelta(minutes=5),
    )
    older = make_task(
        id="OLD", title="Older normal", created_at=now - timedelta(hours=3)
    )
    assert oldest_open_task([older, scheduled], now=now) is scheduled


def test_oldest_open_task_skips_future_run_at_until_it_fires():
    now = datetime.now(UTC)
    scheduled = make_task(
        id="SCHED",
        title="Weekly report",
        created_at=now - timedelta(hours=2),
        run_at=now + timedelta(hours=1),
    )
    normal = make_task(
        id="GO", title="Normal", created_at=now - timedelta(minutes=30)
    )
    assert oldest_open_task([scheduled, normal], now=now) is normal
    assert oldest_open_task([scheduled], now=now) is None


def test_oldest_open_task_fires_run_at_exactly_at_now():
    now = datetime.now(UTC)
    scheduled = make_task(
        id="SCHED",
        title="Now due",
        run_at=now,
        created_at=now - timedelta(hours=2),
    )
    normal = make_task(id="GO", title="Normal", created_at=now - timedelta(hours=3))
    assert oldest_open_task([normal, scheduled], now=now) is scheduled


def test_oldest_open_task_due_group_picks_earliest_run_at():
    now = datetime.now(UTC)
    most_overdue = make_task(
        id="A",
        title="Most overdue",
        created_at=now - timedelta(hours=1),
        run_at=now - timedelta(hours=2),
    )
    less_overdue = make_task(
        id="B",
        title="Less overdue",
        created_at=now - timedelta(hours=3),
        run_at=now - timedelta(minutes=30),
    )
    assert oldest_open_task([less_overdue, most_overdue], now=now) is most_overdue


def test_oldest_open_task_run_at_past_but_blocked_dependency_not_picked():
    now = datetime.now(UTC)
    dep = make_task(
        id="DEP-1", title="Dep", status=TaskStatus.BLOCKED,
        created_at=now - timedelta(hours=3),
    )
    scheduled = make_task(
        id="SCHED",
        title="After deploy",
        dependencies=["DEP-1"],
        run_at=now - timedelta(minutes=5),
    )
    assert oldest_open_task([scheduled, dep], now=now) is None


def test_oldest_open_task_non_open_run_at_ignored():
    now = datetime.now(UTC)
    done = make_task(
        id="DONE",
        title="Done",
        status=TaskStatus.COMPLETED,
        run_at=now - timedelta(hours=1),
    )
    normal = make_task(id="GO", title="Normal", created_at=now - timedelta(hours=2))
    assert oldest_open_task([done, normal], now=now) is normal


def test_next_due_run_at_returns_earliest_future_or_past():
    now = datetime.now(UTC)
    soon = make_task(id="SOON", title="Soon", run_at=now + timedelta(minutes=10))
    later = make_task(id="LATER", title="Later", run_at=now + timedelta(hours=2))
    assert next_due_run_at([later, soon], now=now) == soon.run_at


def test_next_due_run_at_includes_past_and_normalizes_to_none():
    now = datetime.now(UTC)
    past = make_task(id="PAST", title="Past", run_at=now - timedelta(minutes=5))
    assert next_due_run_at([past], now=now) == past.run_at


def test_next_due_run_at_none_without_run_at_tasks():
    now = datetime.now(UTC)
    normal = make_task(id="GO", title="Normal", created_at=now - timedelta(hours=1))
    assert next_due_run_at([normal], now=now) is None
    assert next_due_run_at([], now=now) is None


def test_next_due_run_at_skips_not_runnable_and_completed():
    now = datetime.now(UTC)
    waiting = make_task(
        id="WAIT",
        title="Waits",
        dependencies=["GHOST"],
        run_at=now + timedelta(minutes=5),
    )
    done = make_task(
        id="DONE",
        title="Done",
        status=TaskStatus.COMPLETED,
        run_at=now + timedelta(minutes=5),
    )
    assert next_due_run_at([waiting, done], now=now) is None


def test_unsatisfied_dependencies_reports_status_and_missing():
    done = make_task(id="DONE", status=TaskStatus.COMPLETED)
    blocked = make_task(id="BLK", status=TaskStatus.BLOCKED)
    open_ = make_task(id="OPEN-1", status=TaskStatus.OPEN)
    task = make_task(
        id="T", title="T", dependencies=["DONE", "BLK", "OPEN-1", "GHOST"]
    )
    assert unsatisfied_dependencies([task, done, blocked, open_], task) == [
        {"id": "BLK", "status": "BLOCKED"},
        {"id": "OPEN-1", "status": "OPEN"},
        {"id": "GHOST", "status": "missing"},
    ]


def test_unsatisfied_dependencies_empty_when_satisfied_or_no_deps():
    done = make_task(id="DONE", status=TaskStatus.COMPLETED)
    waiting = make_task(id="T", title="T", dependencies=["DONE"])
    assert unsatisfied_dependencies([waiting, done], waiting) == []
    assert unsatisfied_dependencies([make_task()], make_task()) == []


async def test_fetch_oldest_open_task(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    older = Task(
        id="A", title="older", description="d", created_at=datetime.now(UTC) - timedelta(hours=2)
    )
    newer = Task(id="B", title="newer", description="d", created_at=datetime.now(UTC))
    await backlog.create_task(newer)
    await backlog.create_task(older)

    fetched = oldest_open_task(await backlog.list_tasks())
    assert fetched.id == "A"


async def test_fetch_skips_non_open_tasks(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    for status in (TaskStatus.BLOCKED, TaskStatus.COMPLETED, TaskStatus.FAILED):
        await backlog.create_task(Task(id=status.value, title="t", description="d", status=status))
    assert oldest_open_task(await backlog.list_tasks()) is None


async def test_fetch_prefers_open_over_blocked(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.create_task(
        Task(id="BLOCKED-1", title="b", description="d", status=TaskStatus.BLOCKED)
    )
    await backlog.create_task(make_task(id="OPEN-1"))
    fetched = oldest_open_task(await backlog.list_tasks())
    assert fetched.id == "OPEN-1"


async def test_update_status_persists_and_bumps_timestamp(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    updated = await backlog.update_status(task.id, TaskStatus.COMPLETED, make_result())
    assert updated.status is TaskStatus.COMPLETED
    stored = await backlog.get_task(task.id)
    assert stored.status is TaskStatus.COMPLETED
    assert stored.updated_at >= task.updated_at


async def test_update_status_unknown_id_returns_none(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.create_task(make_task())
    assert await backlog.update_status("MISSING", TaskStatus.COMPLETED, make_result()) is None


async def test_set_blocked_persists_reason_and_increments_count(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    blocked = await backlog.set_blocked(task.id, ["I need a decision"], make_result())
    assert blocked.status is TaskStatus.BLOCKED
    assert blocked.blocker_reason == ["I need a decision"]
    assert blocked.blocked_count == 1

    blocked = await backlog.set_blocked(task.id, ["Another decision"], make_result())
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
    assert await backlog.set_blocked("MISSING", ["?"], make_result()) is None


async def test_set_failed_persists_reason(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    failed = await backlog.set_failed(task.id, ["timed out after 60s"], make_result())
    assert failed.status is TaskStatus.FAILED
    assert failed.failure_reason == ["timed out after 60s"]

    failed = await backlog.set_failed(task.id, ["exit code 3"], make_result())
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
    assert await backlog.set_failed("MISSING", ["?"], make_result()) is None


async def test_update_status_clears_failure_reason(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    await backlog.set_failed(task.id, ["timed out after 60s"], make_result())
    updated = await backlog.update_status(task.id, TaskStatus.OPEN, make_result())
    assert updated.status is TaskStatus.OPEN
    assert updated.failure_reason == []
    stored = await backlog.get_task(task.id)
    assert stored.failure_reason == []


async def test_set_blocked_clears_failure_reason(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    await backlog.set_failed(task.id, ["timed out after 60s"], make_result())
    blocked = await backlog.set_blocked(task.id, ["I need a decision"], make_result())
    assert blocked.status is TaskStatus.BLOCKED
    assert blocked.failure_reason == []
    assert blocked.blocker_reason == ["I need a decision"]


async def test_reopen_task_clears_failure_reason(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    await backlog.set_failed(task.id, ["timed out after 60s"], make_result())
    reopened = await backlog.reopen_task(task.id)
    assert reopened.status is TaskStatus.OPEN
    assert reopened.failure_reason == []


async def test_reopen_task_clears_reason_keeps_count(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    await backlog.set_blocked(task.id, ["I need a decision"], make_result())
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
    assert oldest_open_task(await backlog.list_tasks()) is None


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


async def test_snapshot_creates_bak_with_current_store(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.create_task(make_task(id="A"))
    await backlog.create_task(make_task(id="B"))

    await backlog.snapshot()

    bak = tmp_path / "backlog.json.bak"
    assert bak.is_file()
    store = json.loads(bak.read_text(encoding="utf-8"))
    assert [task["id"] for task in store["tasks"]] == ["A", "B"]


async def test_snapshot_rotates_keeping_last_two(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    for i in range(1, 4):
        await backlog.create_task(make_task(id=f"T-{i}"))
        await backlog.snapshot()

    baks = sorted(tmp_path.glob("backlog.json.bak*"))
    assert [p.name for p in baks] == ["backlog.json.bak", "backlog.json.bak.1"]
    newest = json.loads((tmp_path / "backlog.json.bak").read_text(encoding="utf-8"))
    assert [task["id"] for task in newest["tasks"]] == ["T-1", "T-2", "T-3"]
    older = json.loads((tmp_path / "backlog.json.bak.1").read_text(encoding="utf-8"))
    assert [task["id"] for task in older["tasks"]] == ["T-1", "T-2"]


async def test_snapshot_keeps_configured_count(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json", snapshot_count=3)
    for i in range(1, 5):
        await backlog.create_task(make_task(id=f"T-{i}"))
        await backlog.snapshot()

    baks = sorted(tmp_path.glob("backlog.json.bak*"))
    assert [p.name for p in baks] == [
        "backlog.json.bak",
        "backlog.json.bak.1",
        "backlog.json.bak.2",
    ]


async def test_snapshot_is_noop_when_backlog_missing(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.snapshot()
    assert not (tmp_path / "backlog.json.bak").exists()


async def test_snapshot_is_noop_when_snapshots_disabled(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json", snapshot_count=0)
    await backlog.create_task(make_task())
    await backlog.snapshot()
    assert not (tmp_path / "backlog.json.bak").exists()


async def test_corrupt_backlog_restores_from_newest_snapshot(tmp_path, caplog):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.create_task(make_task())
    await backlog.snapshot()
    path = tmp_path / "backlog.json"
    path.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="forgeo.backlog"):
        tasks = await backlog.list_tasks()

    assert [task.id for task in tasks] == ["TASK-001"]
    assert json.loads(path.read_text(encoding="utf-8"))["tasks"][0]["id"] == "TASK-001"
    corrupt = list(tmp_path.glob("backlog.json.corrupt-*"))
    assert len(corrupt) == 1
    assert corrupt[0].read_text(encoding="utf-8") == "{not valid json"
    assert "restored from snapshot" in caplog.text


async def test_restore_prefers_newest_snapshot(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.create_task(make_task(id="A"))
    await backlog.snapshot()
    await backlog.create_task(make_task(id="B"))
    await backlog.snapshot()
    (tmp_path / "backlog.json").write_text("{garbage", encoding="utf-8")

    tasks = await backlog.list_tasks()
    assert [task.id for task in tasks] == ["A", "B"]


async def test_malformed_shape_restores_from_snapshot(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    await backlog.create_task(make_task())
    await backlog.snapshot()
    (tmp_path / "backlog.json").write_text(
        json.dumps({"tasks": "not-a-list"}), encoding="utf-8"
    )

    tasks = await backlog.list_tasks()
    assert [task.id for task in tasks] == ["TASK-001"]


# --------------------------------------------------------------------------- #
# Failed-task retry state                                                      #
# --------------------------------------------------------------------------- #


async def test_set_failed_resets_failed_wait_cycles(tmp_path):
    """A fresh FAILED transition restarts the retry backoff."""
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    await backlog.set_failed(task.id, ["boom"], make_result())
    await backlog.bump_failed_wait(task.id)
    assert (await backlog.get_task(task.id)).failed_wait_cycles == 1

    refailed = await backlog.set_failed(task.id, ["boom again"], make_result())
    assert refailed.failed_wait_cycles == 0
    assert refailed.failure_reason == ["boom again"]
    stored = await backlog.get_task(task.id)
    assert stored.failed_wait_cycles == 0
    assert stored.failure_reason == ["boom again"]


async def test_bump_failed_wait_increments(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    await backlog.set_failed(task.id, ["boom"], make_result())
    bumped = await backlog.bump_failed_wait(task.id)
    assert bumped.status is TaskStatus.FAILED
    assert bumped.failed_wait_cycles == 1
    await backlog.bump_failed_wait(task.id)
    stored = await backlog.get_task(task.id)
    assert stored.failed_wait_cycles == 2


async def test_retry_task_reopens_and_increments_retry_count(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    await backlog.set_failed(task.id, ["boom"], make_result())
    retried = await backlog.retry_task(task.id)
    assert retried.status is TaskStatus.OPEN
    assert retried.retry_count == 1
    assert retried.failed_wait_cycles == 0
    assert retried.failure_reason == []

    stored = await backlog.get_task(task.id)
    assert stored.status is TaskStatus.OPEN
    assert stored.retry_count == 1
    assert stored.updated_at >= task.updated_at

    disk = json.loads((tmp_path / "backlog.json").read_text(encoding="utf-8"))
    entry = disk["tasks"][0]
    assert entry["status"] == "OPEN"
    assert entry["retry_count"] == 1


async def test_retry_task_unknown_id_returns_none(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    assert await backlog.retry_task("MISSING") is None
    assert await backlog.bump_failed_wait("MISSING") is None


async def test_update_status_leaving_failed_resets_retry_state(tmp_path):
    """A manual reopen (status away from FAILED) resets the retry budget so
    the human's retry gets a fresh failed_retry_max."""
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    await backlog.set_failed(task.id, ["boom"], make_result())
    await backlog.retry_task(task.id)
    await backlog.set_failed(task.id, ["boom again"], make_result())
    await backlog.bump_failed_wait(task.id)
    stored = await backlog.get_task(task.id)
    assert stored.retry_count == 1
    assert stored.failed_wait_cycles == 1

    reopened = await backlog.update_status(task.id, TaskStatus.OPEN, make_result())
    assert reopened.status is TaskStatus.OPEN
    assert reopened.retry_count == 0
    assert reopened.failed_wait_cycles == 0
    assert reopened.failure_reason == []


async def test_update_task_accepts_retries_left(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    updated = await backlog.update_task(task.id, {"retries_left": 3})
    assert updated.retries_left == 3
    cleared = await backlog.update_task(task.id, {"retries_left": None})
    assert cleared.retries_left is None
    stored = await backlog.get_task(task.id)
    assert stored.retries_left is None


async def test_update_task_accepts_and_clears_run_at(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    run_at = datetime(2026, 8, 20, 12, 30, tzinfo=UTC)
    updated = await backlog.update_task(task.id, {"run_at": run_at.isoformat()})
    assert updated.run_at == run_at
    stored = await backlog.get_task(task.id)
    assert stored.run_at == run_at

    cleared = await backlog.update_task(task.id, {"run_at": None})
    assert cleared.run_at is None
    stored = await backlog.get_task(task.id)
    assert stored.run_at is None


async def test_update_task_invalid_run_at_raise(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    for bad in (
        {"run_at": "not-a-datetime"},
        {"run_at": 42},
        {"run_at": []},
        {"run_at": {}},
    ):
        with pytest.raises(ValueError):
            await backlog.update_task(task.id, bad)
    stored = await backlog.get_task(task.id)
    assert stored.run_at is None


async def test_update_task_invalid_retries_left_raise(tmp_path):
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    for bad in (
        {"retries_left": -1},
        {"retries_left": "nope"},
        {"retries_left": 2.5},
        {"retries_left": True},
    ):
        with pytest.raises(ValueError):
            await backlog.update_task(task.id, bad)
    stored = await backlog.get_task(task.id)
    assert stored.retries_left is None


async def test_update_task_retry_state_fields_not_editable(tmp_path):
    """retry_count and failed_wait_cycles are engine-managed: PATCH rejects
    them exactly like blocker_reason/blocked_count."""
    backlog = JSONBacklog(tmp_path / "backlog.json")
    task = await backlog.create_task(make_task())
    for field in ("retry_count", "failed_wait_cycles"):
        with pytest.raises(ValueError, match="unknown task field"):
            await backlog.update_task(task.id, {field: 5})
