"""Run history tests: one durable JSON line per finished forgeo cycle."""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import UTC, datetime

from forgeo.models import (
    DEFAULT_RUN_OUTPUT_LINES,
    NO_CHANGES_REASON,
    NO_CHANGES_REPORTED_REASON,
    ExecutionResult,
    ExecutionStatus,
    RunKind,
    RunOutcome,
    RunRecord,
    TaskStatus,
)
from forgeo.runs import RunRecorder, runs_path_for
from tests.conftest import git, make_forgeo, make_task


def read_lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_task_success_appends_run_record(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS, exit_code=0)
    agent.effect = lambda: (git_repo / "app.py").write_text(
        "def answer():\n    return 7\n", encoding="utf-8"
    )

    assert await forgeo.run_cycle() == "task"

    lines = read_lines(runs_path_for(forgeo.config.backlog))
    assert len(lines) == 1
    record = lines[0]
    assert record["kind"] == "task"
    assert record["task_id"] == "TASK-001"
    assert record["task_title"] == "Do the thing"
    assert record["outcome"] == "SUCCESS"
    assert record["agent_exit_code"] == 0
    assert record["commit_sha"] == git(git_repo, "rev-parse", "--short", "HEAD")
    assert record["duration_seconds"] >= 0
    assert record["started_at"]
    assert record["finished_at"]


async def test_task_success_without_changes_has_reason(git_repo, tmp_path):
    """A no-change SUCCESS is surfaced on the run record: outcome stays
    SUCCESS (the agent did exit 0) but with an explicit reason, never a
    silent null commit_sha."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS, exit_code=0)

    assert await forgeo.run_cycle() == "task"

    record = read_lines(runs_path_for(forgeo.config.backlog))[0]
    assert record["outcome"] == "SUCCESS"
    assert record["commit_sha"] is None
    assert record["reason"] == NO_CHANGES_REASON
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.FAILED


async def test_explicit_no_changes_record_has_reason(git_repo, tmp_path):
    """An explicit no-change (exit no_changes_exit_code) completes the task and
    the run record explains the missing commit."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(
        status=ExecutionStatus.SUCCESS, exit_code=3, no_changes=True
    )

    assert await forgeo.run_cycle() == "task"

    record = read_lines(runs_path_for(forgeo.config.backlog))[0]
    assert record["outcome"] == "SUCCESS"
    assert record["agent_exit_code"] == 3
    assert record["commit_sha"] is None
    assert record["reason"] == NO_CHANGES_REPORTED_REASON
    assert (await backlog.get_task("TASK-001")).status is TaskStatus.COMPLETED


async def test_task_blocked_record(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["?"], exit_code=2)
    agent.effect = lambda: (git_repo / "wip.txt").write_text("partial\n", encoding="utf-8")

    assert await forgeo.run_cycle() == "task"

    record = read_lines(runs_path_for(forgeo.config.backlog))[0]
    assert record["kind"] == "task"
    assert record["outcome"] == "BLOCKED"
    assert record["agent_exit_code"] == 2
    assert record["commit_sha"] == git(git_repo, "rev-parse", "--short", "HEAD")


async def test_task_error_record(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.ERROR, error="boom", exit_code=3)
    agent.effect = lambda: (git_repo / "app.py").write_text("garbage\n", encoding="utf-8")

    assert await forgeo.run_cycle() == "task"

    record = read_lines(runs_path_for(forgeo.config.backlog))[0]
    assert record["kind"] == "task"
    assert record["task_id"] == "TASK-001"
    assert record["outcome"] == "ERROR"
    assert record["agent_exit_code"] == 3
    assert record["commit_sha"] is None


async def test_run_record_persists_bounded_output_tail(git_repo, tmp_path):
    """The agent's stdout/stderr is persisted as the bounded tail (last lines)."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path, run_output_lines=3)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(
        status=ExecutionStatus.BLOCKED,
        output_logs=[f"line {index}" for index in range(10)],
        questions=["?"],
        exit_code=2,
    )

    assert await forgeo.run_cycle() == "task"

    record = read_lines(runs_path_for(forgeo.config.backlog))[0]
    assert record["output_logs"] == ["line 7", "line 8", "line 9"]


async def test_run_record_default_output_cap_is_200(git_repo, tmp_path):
    """Without an explicit cap a run stores at most the default tail length."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        output_logs=[f"line {index}" for index in range(500)],
        exit_code=0,
    )

    assert await forgeo.run_cycle() == "task"

    record = read_lines(runs_path_for(forgeo.config.backlog))[0]
    assert len(record["output_logs"]) == DEFAULT_RUN_OUTPUT_LINES
    assert record["output_logs"][0] == "line 300"
    assert record["output_logs"][-1] == "line 499"


async def test_run_record_without_output_logs_is_null(git_repo, tmp_path):
    """A run that captured no agent output stores output_logs as null, not []."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS, exit_code=0)

    assert await forgeo.run_cycle() == "task"

    record = read_lines(runs_path_for(forgeo.config.backlog))[0]
    assert record["output_logs"] is None


async def test_run_output_lines_zero_disables_persistence(git_repo, tmp_path):
    """run_output_lines: 0 stops persisting agent output entirely."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path, run_output_lines=0)
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(
        status=ExecutionStatus.BLOCKED,
        output_logs=["important"],
        questions=["?"],
        exit_code=2,
    )

    assert await forgeo.run_cycle() == "task"

    record = read_lines(runs_path_for(forgeo.config.backlog))[0]
    assert record["output_logs"] is None


def test_old_records_without_output_logs_field_read_fine(tmp_path):
    """Records written before output_logs existed parse with output_logs=None."""
    recorder = RunRecorder(tmp_path / "runs.jsonl")
    recorder.append(make_record("OLD"))
    raw = json.loads(recorder.path.read_text(encoding="utf-8").strip())
    del raw["output_logs"]
    recorder.path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    records = recorder.read()
    assert len(records) == 1
    assert records[0].task_id == "OLD"
    assert records[0].output_logs is None


async def test_refactor_record(git_repo, tmp_path):
    forgeo, agent, _backlog = make_forgeo(git_repo, tmp_path)
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS, exit_code=0)

    assert await forgeo.run_cycle() == "refactor"

    record = read_lines(runs_path_for(forgeo.config.backlog))[0]
    assert record["kind"] == "refactor"
    assert record["task_id"] == "REFACTOR"
    assert record["task_title"] == "Refactoring pass"
    assert record["outcome"] == "SUCCESS"
    assert record["agent_exit_code"] == 0
    assert record["commit_sha"] is None
    assert record["reason"] is None  # refactor no-diff is normal, not a failure


async def test_every_cycle_appends_exactly_one_line(git_repo, tmp_path):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    runs = runs_path_for(forgeo.config.backlog)

    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS, exit_code=0)
    assert await forgeo.run_cycle() == "task"
    assert len(read_lines(runs)) == 1

    await backlog.update_status("TASK-001", TaskStatus.OPEN)
    agent.result = ExecutionResult(status=ExecutionStatus.ERROR, error="boom", exit_code=3)
    assert await forgeo.run_cycle() == "task"
    assert len(read_lines(runs)) == 2

    await backlog.update_status("TASK-001", TaskStatus.OPEN)
    agent.result = ExecutionResult(status=ExecutionStatus.BLOCKED, questions=["?"], exit_code=2)
    assert await forgeo.run_cycle() == "task"
    assert len(read_lines(runs)) == 3

    assert await forgeo.run_cycle() == "blocked"
    assert len(read_lines(runs)) == 4

    await backlog.update_status("TASK-001", TaskStatus.COMPLETED)
    forgeo.config.blocker_file.write_text("stale", encoding="utf-8")
    assert await forgeo.run_cycle() == "paused"
    assert len(read_lines(runs)) == 5

    forgeo.config.blocker_file.unlink()
    await backlog.update_status("TASK-001", TaskStatus.OPEN)
    (git_repo / "manual.txt").write_text("wip\n", encoding="utf-8")
    assert await forgeo.run_cycle() == "dirty"
    assert len(read_lines(runs)) == 6

    await backlog.update_status("TASK-001", TaskStatus.COMPLETED)
    git(git_repo, "clean", "-fd")
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS, exit_code=0)
    assert await forgeo.run_cycle() == "refactor"
    assert len(read_lines(runs)) == 7


def test_read_missing_file_returns_empty(tmp_path):
    recorder = RunRecorder(tmp_path / "runs.jsonl")
    assert recorder.read() == []
    assert recorder.read_last() is None


def test_append_failure_is_logged_not_raised(tmp_path, caplog, monkeypatch):
    """A write failure must never break a Forgeo cycle."""
    recorder = RunRecorder(tmp_path / "runs.jsonl")
    record = RunRecord(
        started_at=datetime(2026, 8, 1, tzinfo=UTC),
        finished_at=datetime(2026, 8, 1, 0, 0, 10, tzinfo=UTC),
        kind=RunKind.TASK,
        outcome=RunOutcome.SUCCESS,
        duration_seconds=1.0,
    )

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pathlib.Path, "open", boom)
    with caplog.at_level(logging.ERROR, logger="forgeo.runs"):
        recorder.append(record)

    assert "Could not write run record" in caplog.text
    assert not recorder.path.exists()


def test_read_returns_newest_first(tmp_path):
    recorder = RunRecorder(tmp_path / "runs.jsonl")
    older = RunRecord(
        started_at=datetime(2026, 8, 1, tzinfo=UTC),
        finished_at=datetime(2026, 8, 1, 0, 0, 10, tzinfo=UTC),
        kind=RunKind.TASK,
        task_id="OLD",
        outcome=RunOutcome.SUCCESS,
        duration_seconds=1.0,
    )
    newer = RunRecord(
        started_at=datetime(2026, 8, 2, tzinfo=UTC),
        finished_at=datetime(2026, 8, 2, 0, 0, 10, tzinfo=UTC),
        kind=RunKind.REFACTOR,
        outcome=RunOutcome.ERROR,
        duration_seconds=2.0,
    )
    recorder.append(older)
    recorder.append(newer)

    assert [r.task_id for r in recorder.read()] == [None, "OLD"]
    assert [r.kind for r in recorder.read()] == [RunKind.REFACTOR, RunKind.TASK]
    assert [r.task_id for r in recorder.read(limit=1)] == [None]
    assert recorder.read_last().task_id is None
    assert recorder.read_last().kind is RunKind.REFACTOR


def test_read_supports_offset_pagination(tmp_path):
    recorder = RunRecorder(tmp_path / "runs.jsonl")
    for index in range(1, 6):
        recorder.append(
            RunRecord(
                started_at=datetime(2026, 8, 1, tzinfo=UTC),
                finished_at=datetime(2026, 8, 1, 0, index, 10, tzinfo=UTC),
                kind=RunKind.TASK,
                task_id=f"T-{index}",
                outcome=RunOutcome.SUCCESS,
                duration_seconds=1.0,
            )
        )

    assert [r.task_id for r in recorder.read(limit=2)] == ["T-5", "T-4"]
    assert [r.task_id for r in recorder.read(limit=2, offset=2)] == ["T-3", "T-2"]
    assert [r.task_id for r in recorder.read(limit=2, offset=4)] == ["T-1"]
    assert [r.task_id for r in recorder.read(limit=2, offset=99)] == []
    assert [r.task_id for r in recorder.read(offset=1)] == ["T-4", "T-3", "T-2", "T-1"]


def test_total_counts_readable_records(tmp_path, caplog):
    recorder = RunRecorder(tmp_path / "runs.jsonl")
    assert recorder.total() == 0
    recorder.append(
        RunRecord(
            started_at=datetime(2026, 8, 1, tzinfo=UTC),
            finished_at=datetime(2026, 8, 1, 0, 0, 10, tzinfo=UTC),
            kind=RunKind.TASK,
            task_id="GOOD",
            outcome=RunOutcome.SUCCESS,
            duration_seconds=1.0,
        )
    )
    assert recorder.total() == 1
    with recorder.path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    with caplog.at_level(logging.WARNING, logger="forgeo.runs"):
        assert recorder.total() == 1
    assert "Skipping corrupt run record" in caplog.text


def test_read_skips_corrupt_lines_with_warning(tmp_path, caplog):
    recorder = RunRecorder(tmp_path / "runs.jsonl")
    recorder.append(
        RunRecord(
            started_at=datetime(2026, 8, 1, tzinfo=UTC),
            finished_at=datetime(2026, 8, 1, 0, 0, 10, tzinfo=UTC),
            kind=RunKind.TASK,
            task_id="GOOD",
            outcome=RunOutcome.SUCCESS,
            duration_seconds=1.0,
        )
    )
    with recorder.path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
        handle.write("{not json\n")
        handle.write('{"started_at": "broken"\n')

    with caplog.at_level(logging.WARNING, logger="forgeo.runs"):
        records = recorder.read()

    assert "corrupt" in caplog.text
    assert len(records) == 1
    assert records[0].task_id == "GOOD"


async def test_corrupt_runs_never_break_a_cycle(git_repo, tmp_path, caplog):
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    runs = runs_path_for(forgeo.config.backlog)
    runs.write_text("{not json\n", encoding="utf-8")
    await backlog.create_task(make_task())
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS, exit_code=0)

    with caplog.at_level(logging.WARNING, logger="forgeo.runs"):
        assert await forgeo.run_cycle() == "task"

    records = RunRecorder(runs).read()
    assert len(records) == 1
    assert records[0].outcome is RunOutcome.SUCCESS
    assert records[0].task_id == "TASK-001"


def make_record(task_id: str | None = None) -> RunRecord:
    return RunRecord(
        started_at=datetime(2026, 8, 1, tzinfo=UTC),
        finished_at=datetime(2026, 8, 1, 0, 0, 10, tzinfo=UTC),
        kind=RunKind.TASK,
        task_id=task_id,
        outcome=RunOutcome.SUCCESS,
        duration_seconds=1.0,
    )


def test_keep_trims_growing_file(tmp_path):
    """With retention set, the file never grows past ``keep`` lines and the
    oldest records are dropped first."""
    recorder = RunRecorder(tmp_path / "runs.jsonl", keep=3)
    for index in range(10):
        recorder.append(make_record(f"T-{index}"))

    lines = read_lines(recorder.path)
    assert len(lines) == 3
    assert [line["task_id"] for line in lines] == ["T-7", "T-8", "T-9"]
    assert {r.task_id for r in recorder.read()} == {"T-7", "T-8", "T-9"}


def test_keep_below_limit_leaves_file_untouched(tmp_path):
    recorder = RunRecorder(tmp_path / "runs.jsonl", keep=5)
    for index in range(3):
        recorder.append(make_record(f"T-{index}"))

    assert len(read_lines(recorder.path)) == 3
    assert [line["task_id"] for line in read_lines(recorder.path)] == ["T-0", "T-1", "T-2"]


def test_keep_zero_disables_retention(tmp_path):
    """``run_history_keep: 0`` keeps every record, exactly as before."""
    recorder = RunRecorder(tmp_path / "runs.jsonl", keep=0)
    for index in range(5):
        recorder.append(make_record(f"T-{index}"))

    lines = read_lines(recorder.path)
    assert len(lines) == 5
    assert [line["task_id"] for line in lines] == ["T-0", "T-1", "T-2", "T-3", "T-4"]


def test_keep_one_keeps_only_latest(tmp_path):
    recorder = RunRecorder(tmp_path / "runs.jsonl", keep=1)
    for index in range(4):
        recorder.append(make_record(f"T-{index}"))

    assert [line["task_id"] for line in read_lines(recorder.path)] == ["T-3"]


def test_trim_failure_is_logged_not_raised(tmp_path, caplog, monkeypatch):
    """A failed trim is logged and skipped; the record still lands via the
    plain append so retention can never break a cycle."""
    recorder = RunRecorder(tmp_path / "runs.jsonl", keep=1)
    recorder.append(make_record("T-0"))
    recorder.append(make_record("T-1"))
    assert len(read_lines(recorder.path)) == 1

    def boom(*args, **kwargs):
        raise OSError("read only")

    monkeypatch.setattr(pathlib.Path, "read_text", boom)
    with caplog.at_level(logging.ERROR, logger="forgeo.runs"):
        recorder.append(make_record("T-2"))

    with recorder.path.open(encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    assert "Could not trim run history" in caplog.text
    assert len(lines) == 2
    assert lines[-1] == make_record("T-2").model_dump_json()


async def test_cycle_applies_configured_retention(git_repo, tmp_path):
    """The daemon's recorder trims per ``run_history_keep`` from the config."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path, run_history_keep=2)
    runs = runs_path_for(forgeo.config.backlog)
    await backlog.create_task(make_task())

    for _ in range(4):
        agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS, exit_code=0)
        assert await forgeo.run_cycle() == "task"
        await backlog.update_status("TASK-001", TaskStatus.OPEN)

    lines = read_lines(runs)
    assert len(lines) == 2
    assert all(line["task_id"] == "TASK-001" for line in lines)
