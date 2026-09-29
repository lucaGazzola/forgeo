"""CLI tests for the ``forgeo once``/``status``/``stop``/``restart`` commands."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forgeo.backlog import open_backlog
from forgeo.cli import (
    DEFAULT_CONFIG,
    _expand_task_id_shorthand,
    _resolve_backlog_task_id,
    backlog_status_counts,
    build_parser,
    cmd_auth_login,
    cmd_auth_status,
    cmd_instance,
    cmd_instance_add,
    cmd_instance_list,
    cmd_instance_rm,
    cmd_logs,
    cmd_once,
    cmd_restart,
    cmd_run,
    cmd_start,
    cmd_status,
    cmd_stop,
    cmd_task,
    cmd_task_add,
    cmd_task_complete_review,
    cmd_task_edit,
    cmd_task_list,
    cmd_task_next,
    cmd_task_reopen,
    cmd_task_request_changes,
    cmd_task_rm,
    cmd_task_show,
    cmd_validate,
    last_outcome_from_runs,
    main,
    render_status,
    render_task_next,
)
from forgeo.config import load_config
from forgeo.daemon import acquire_run_lock, is_lock_held, read_lock_pid
from forgeo.instances import list_instances, load_registry
from forgeo.models import RunKind, RunOutcome, RunRecord, TaskStatus
from forgeo.paths import lock_path, runs_path
from forgeo.runs import RunRecorder
from tests.conftest import FakeForgeo, git, make_config, make_task, requires_posix, wait_for


def write_config_in(dir_path: Path, git_repo: Path, tmp_path: Path, **overrides) -> Path:
    """A forgeo.yaml inside ``dir_path`` wired to ``git_repo``; returns its path."""
    config = make_config(git_repo, tmp_path, **overrides)
    path = dir_path / "forgeo.yaml"
    path.write_text(
        f"name: {config.name}\n"
        f"repo: {config.repo}\n"
        f"backlog: {config.backlog}\n"
        f"blocker_file: {config.blocker_file}\n"
        f"agent_command: {config.agent_command}\n"
        f"log_file: {config.log_file}\n"
        f"interval_minutes: {config.interval_minutes}\n"
        f"branch: {config.branch}\n",
        encoding="utf-8",
    )
    return path


def write_config(git_repo: Path, tmp_path: Path, **overrides) -> Path:
    """A config file wired to the fixture repo; returns its path."""
    return write_config_in(tmp_path, git_repo, tmp_path, **overrides)


def once_args(config_path: Path) -> argparse.Namespace:
    return argparse.Namespace(config=config_path)


def run_args(config_path: Path, task_id: str = "TASK-001") -> argparse.Namespace:
    return argparse.Namespace(config=config_path, task=task_id, reopen=False)


def status_args(config_path: Path) -> argparse.Namespace:
    return argparse.Namespace(config=config_path)


def validate_args(config_path: Path) -> argparse.Namespace:
    return argparse.Namespace(config=config_path)


def stop_args(config_path: Path, timeout: float = 30.0) -> argparse.Namespace:
    return argparse.Namespace(config=config_path, timeout=timeout)


def restart_args(config_path: Path, timeout: float = 30.0) -> argparse.Namespace:
    return argparse.Namespace(config=config_path, timeout=timeout)


def start_args(config_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        config=config_path,
        interval_minutes=None,
        foreground=False,
    )


def spawn_daemon(config_path: Path) -> subprocess.Popen[bytes]:
    """Start a real ``forgeo start --foreground`` subprocess, like restart does."""
    return subprocess.Popen(
        [sys.executable, "-m", "forgeo", "start", "--foreground", "--config", str(config_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def test_once_runs_one_cycle_and_exits_zero(git_repo, tmp_path, monkeypatch, capsys):
    config_path = write_config(git_repo, tmp_path)
    fake = FakeForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: fake)

    assert cmd_once(once_args(config_path)) == 0
    assert fake.cycles == 1
    assert "Cycle finished: task" in capsys.readouterr().out

    lock_path = tmp_path / "backlog.lock"
    released = acquire_run_lock(lock_path)
    assert released is not None
    released.close()


def test_once_triggers_update_check(git_repo, tmp_path, monkeypatch, capsys):
    """An outdated install prints the upgrade notice when a cycle begins."""
    config_path = write_config(git_repo, tmp_path)
    fake = FakeForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: fake)
    checked_paths: list[Path] = []

    def fake_check(state_path, *, print_fn):
        checked_paths.append(state_path)
        print_fn("A newer forgeo-cli version is available: 0.4.0 -> 0.5.0. "
                 "Upgrade with `pipx upgrade forgeo-cli`.")

    monkeypatch.setattr("forgeo.cli.check_for_update", fake_check)

    assert cmd_once(once_args(config_path)) == 0
    assert fake.cycles == 1
    assert checked_paths == [tmp_path / "backlog.update.json"]
    out = capsys.readouterr().out
    assert "A newer forgeo-cli version is available" in out
    assert "0.5.0" in out


@requires_posix
def test_once_refuses_while_lock_held(git_repo, tmp_path, monkeypatch, capsys):
    config_path = write_config(git_repo, tmp_path)
    fake = FakeForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: fake)
    lock = acquire_run_lock(tmp_path / "backlog.lock")
    assert lock is not None

    assert cmd_once(once_args(config_path)) == 1
    assert fake.cycles == 0
    assert "already running" in capsys.readouterr().out

    lock.close()


@requires_posix
def test_once_refuses_while_daemon_lock_held(git_repo, tmp_path, monkeypatch):
    config_path = write_config(git_repo, tmp_path)
    fake = FakeForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: fake)
    config = make_config(git_repo, tmp_path)
    lock = acquire_run_lock(lock_path(config))
    assert lock is not None

    assert cmd_once(once_args(config_path)) == 1
    assert fake.cycles == 0

    lock.close()


def test_once_missing_config_offers_setup(monkeypatch, tmp_path):
    monkeypatch.setattr("forgeo.cli.Confirm.ask", lambda *a, **k: False)
    args = argparse.Namespace(config=tmp_path / "forgeo.yaml")
    assert cmd_once(args) == 1


def test_parser_help_lists_once(capsys):
    build_parser().print_help()
    out = capsys.readouterr().out
    for cmd in ("once", "run", "status", "validate", "stop", "restart"):
        assert cmd in out


def test_parser_allows_missing_task_for_run():
    args = build_parser().parse_args(["run", "--config", "forgeo.yaml"])
    assert args.action == "run"
    assert args.task is None
    assert args.task_id is None


def test_parser_parses_positional_task_for_run():
    args = build_parser().parse_args(["run", "--config", "forgeo.yaml", "SELF-012"])
    assert args.action == "run"
    assert args.task_id == "SELF-012"


def test_run_refuses_missing_task_id(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    args = argparse.Namespace(config=config_path, task=None, task_id=None, reopen=False)
    assert cmd_run(args) == 2
    assert "Missing task id" in capsys.readouterr().out


def test_run_refuses_doubled_task_id(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    args = argparse.Namespace(
        config=config_path, task="TASK-001", task_id="TASK-002", reopen=False
    )
    assert cmd_run(args) == 2
    assert "not both" in capsys.readouterr().out


def test_run_accepts_positional_task_id(git_repo, tmp_path, monkeypatch, capsys):
    config_path = write_config(git_repo, tmp_path)
    fake = FakeForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: fake)
    args = argparse.Namespace(config=config_path, task=None, task_id="SELF-012", reopen=False)
    assert cmd_run(args) == 0
    assert fake.run_task_ids == ["SELF-012"]


def test_parser_parses_task_for_run():
    args = build_parser().parse_args(["run", "--task", "SELF-012"])
    assert args.action == "run"
    assert args.task == "SELF-012"


def test_run_executes_specific_task_not_oldest(git_repo, tmp_path, monkeypatch, capsys):
    config_path = write_config(git_repo, tmp_path)
    fake = FakeForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: fake)

    assert cmd_run(run_args(config_path, "SELF-012")) == 0
    assert fake.cycles == 1
    assert fake.run_task_ids == ["SELF-012"]
    assert "Cycle finished: task" in capsys.readouterr().out


def test_run_triggers_update_check(git_repo, tmp_path, monkeypatch, capsys):
    config_path = write_config(git_repo, tmp_path)
    fake = FakeForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: fake)
    checked_paths: list[Path] = []

    def fake_check(state_path, *, print_fn):
        checked_paths.append(state_path)
        print_fn("A newer forgeo-cli version is available: 0.4.0 -> 0.5.0. "
                 "Upgrade with `pipx upgrade forgeo-cli`.")

    monkeypatch.setattr("forgeo.cli.check_for_update", fake_check)

    assert cmd_run(run_args(config_path, "SELF-012")) == 0
    assert fake.cycles == 1
    assert checked_paths == [tmp_path / "backlog.update.json"]
    assert "0.5.0" in capsys.readouterr().out


def test_run_refuses_unknown_task(git_repo, tmp_path, monkeypatch, capsys):
    config_path = write_config(git_repo, tmp_path)

    class RefusingForgeo:
        cycles = 0

        async def run_task_id(self, task_id: str, *, reopen: bool = False) -> str:
            from forgeo.forgeo import TaskNotRunnableError

            self.cycles += 1
            raise TaskNotRunnableError(
                f"Task {task_id!r} does not exist in the backlog."
            )

    refusing = RefusingForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: refusing)

    assert cmd_run(run_args(config_path, "SELF-999")) == 1
    assert refusing.cycles == 1
    out = capsys.readouterr().out
    assert "does not exist" in out


def test_run_refuses_non_open_task(git_repo, tmp_path, monkeypatch, capsys):
    config_path = write_config(git_repo, tmp_path)
    backlog = tmp_path / "backlog.json"
    backlog.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "SELF-012",
                        "title": "Already done",
                        "description": "Do the thing.",
                        "status": "COMPLETED",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert cmd_run(run_args(config_path, "SELF-012")) == 1
    out = capsys.readouterr().out
    assert "COMPLETED" in out
    assert "only OPEN tasks" in out


@requires_posix
def test_run_refuses_while_lock_held(git_repo, tmp_path, monkeypatch, capsys):
    config_path = write_config(git_repo, tmp_path)
    fake = FakeForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: fake)
    lock = acquire_run_lock(tmp_path / "backlog.lock")
    assert lock is not None

    assert cmd_run(run_args(config_path, "SELF-012")) == 1
    assert fake.cycles == 0
    assert "already running" in capsys.readouterr().out

    lock.close()


def test_run_does_not_register_instance(git_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    fake = FakeForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: fake)

    assert cmd_run(run_args(config_path, "SELF-012")) == 0
    assert load_registry() == {}


def test_backlog_status_counts_empty():
    assert backlog_status_counts([]) == {
        "OPEN": 0,
        "REVIEW": 0,
        "BLOCKED": 0,
        "COMPLETED": 0,
        "FAILED": 0,
    }


def test_backlog_status_counts_by_status():
    tasks = [
        make_task(id="T1", status=TaskStatus.OPEN),
        make_task(id="T2", status=TaskStatus.OPEN),
        make_task(id="T3", status=TaskStatus.COMPLETED),
        make_task(id="T4", status=TaskStatus.FAILED),
        make_task(id="T5", status=TaskStatus.BLOCKED),
    ]
    assert backlog_status_counts(tasks) == {
        "OPEN": 2,
        "REVIEW": 0,
        "BLOCKED": 1,
        "COMPLETED": 1,
        "FAILED": 1,
    }


def test_render_status_includes_summary_fields(git_repo, tmp_path):
    config = make_config(git_repo, tmp_path, interval_minutes=30, branch="main")
    tasks = [
        make_task(id="TASK-001", title="Do the thing", status=TaskStatus.OPEN),
        make_task(id="TASK-002", title="Done", status=TaskStatus.COMPLETED),
    ]
    text = render_status(config, tasks, daemon_running=True, last_outcome="task")
    assert "name: test-forgeo" in text
    assert f"repo: {config.repo}" in text
    assert "interval: 30 min" in text
    assert "branch: main" in text
    assert "OPEN=1" in text
    assert "COMPLETED=1" in text
    assert "next: TASK-001 — Do the thing" in text
    assert "daemon: running" in text
    assert "last outcome: task" in text


def test_render_status_empty_backlog_and_no_outcome(git_repo, tmp_path):
    config = make_config(git_repo, tmp_path)
    text = render_status(config, [], daemon_running=False, last_outcome=None)
    assert "OPEN=0" in text
    assert "next: (none)" in text
    assert "daemon: not running" in text
    assert "last outcome: (none)" in text


def test_render_status_reports_waiting_on_dependency(git_repo, tmp_path):
    config = make_config(git_repo, tmp_path)
    now = datetime.now(UTC)
    dep = make_task(
        id="DEP-1", title="Dep", status=TaskStatus.BLOCKED,
        created_at=now - timedelta(hours=1),
    )
    waiting = make_task(
        id="TASK-001", title="Waits", status=TaskStatus.OPEN,
        dependencies=["DEP-1"], created_at=now - timedelta(hours=2),
    )
    text = render_status(config, [waiting, dep], daemon_running=False, last_outcome=None)
    assert "next: (none)" in text
    assert "waiting on: TASK-001 (needs COMPLETED: DEP-1 (BLOCKED))" in text


def test_render_status_no_waiting_line_when_runnable(git_repo, tmp_path):
    config = make_config(git_repo, tmp_path)
    tasks = [make_task(id="TASK-001", title="Do the thing", status=TaskStatus.OPEN)]
    text = render_status(config, tasks, daemon_running=False, last_outcome=None)
    assert "waiting on:" not in text


def test_render_status_shows_blocked_reason_and_action(git_repo, tmp_path):
    config = make_config(git_repo, tmp_path)
    tasks = [
        make_task(
            id="TASK-007",
            title="Needs human",
            status=TaskStatus.BLOCKED,
            blocker_reason=["Need the DB password", "second line"],
        ),
    ]
    text = render_status(config, tasks, daemon_running=False, last_outcome=None)
    assert "blocked: TASK-007 — Needs human — Need the DB password" in text
    assert "action: resolve BLOCKED" in text


def test_render_status_shows_failed_reason_and_action(git_repo, tmp_path):
    config = make_config(git_repo, tmp_path)
    tasks = [
        make_task(
            id="TASK-009",
            title="Crashed",
            status=TaskStatus.FAILED,
            failure_reason=["exit 3: boom"],
        ),
    ]
    text = render_status(config, tasks, daemon_running=False, last_outcome=None)
    assert "failed: TASK-009 — Crashed — exit 3: boom" in text
    assert "action: inspect FAILED" in text


def test_render_status_truncates_many_blocked(git_repo, tmp_path):
    config = make_config(git_repo, tmp_path)
    now = datetime.now(UTC)
    tasks = [
        make_task(
            id=f"TASK-{i:03d}",
            title=f"Blocked {i}",
            status=TaskStatus.BLOCKED,
            created_at=now - timedelta(hours=i),
        )
        for i in range(5)
    ]
    text = render_status(config, tasks, daemon_running=False, last_outcome=None)
    assert text.count("blocked: TASK-") == 3
    assert "+2 more blocked" in text


def test_render_status_action_empty_backlog(git_repo, tmp_path):
    config = make_config(git_repo, tmp_path)
    stopped = render_status(config, [], daemon_running=False, last_outcome=None)
    assert "action: backlog empty" in stopped
    assert "forgeo start" in stopped
    running = render_status(config, [], daemon_running=True, last_outcome=None)
    assert "refactor cycle" in running


def test_render_status_action_start_when_open_and_stopped(git_repo, tmp_path):
    config = make_config(git_repo, tmp_path)
    tasks = [make_task(id="TASK-001", title="Do the thing", status=TaskStatus.OPEN)]
    text = render_status(config, tasks, daemon_running=False, last_outcome=None)
    assert "action: run `forgeo start` to process 1 OPEN task" in text


def test_render_status_no_action_when_running_with_open(git_repo, tmp_path):
    config = make_config(git_repo, tmp_path)
    tasks = [make_task(id="TASK-001", title="Do the thing", status=TaskStatus.OPEN)]
    text = render_status(config, tasks, daemon_running=True, last_outcome=None)
    assert "action:" not in text


def test_status_prints_summary_and_exits_zero(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    backlog = tmp_path / "backlog.json"
    backlog.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "TASK-001",
                        "title": "First open",
                        "description": "Do the thing.",
                        "status": "OPEN",
                        "created_at": "2026-01-01T00:00:00Z",
                    },
                    {
                        "id": "TASK-002",
                        "title": "Done already",
                        "description": "Do the thing.",
                        "status": "COMPLETED",
                        "created_at": "2026-01-02T00:00:00Z",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    RunRecorder(backlog.with_name("runs.jsonl")).append(
        RunRecord(
            started_at=datetime(2026, 8, 1, 1, 0, tzinfo=UTC),
            finished_at=datetime(2026, 8, 1, 1, 0, 5, tzinfo=UTC),
            kind=RunKind.TASK,
            task_id="TASK-001",
            task_title="First open",
            outcome=RunOutcome.SUCCESS,
            agent_exit_code=0,
            commit_sha="abc1234",
            duration_seconds=5.0,
        )
    )

    assert cmd_status(status_args(config_path)) == 0
    out = capsys.readouterr().out
    assert "name: test-forgeo" in out
    assert "OPEN=1" in out
    assert "COMPLETED=1" in out
    assert "next: TASK-001 — First open" in out
    assert "daemon: not running" in out
    assert "last outcome: SUCCESS" in out


def test_status_renders_last_run_from_runs(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    recorder = RunRecorder(tmp_path / "runs.jsonl")
    recorder.append(
        RunRecord(
            started_at=datetime(2026, 8, 1, 1, 0, tzinfo=UTC),
            finished_at=datetime(2026, 8, 1, 1, 0, 5, tzinfo=UTC),
            kind=RunKind.REFACTOR,
            outcome=RunOutcome.BLOCKED,
            agent_exit_code=2,
            duration_seconds=5.0,
        )
    )
    recorder.append(
        RunRecord(
            started_at=datetime(2026, 8, 1, 2, 0, tzinfo=UTC),
            finished_at=datetime(2026, 8, 1, 2, 0, 5, tzinfo=UTC),
            kind=RunKind.TASK,
            task_id="TASK-001",
            task_title="First open",
            outcome=RunOutcome.ERROR,
            agent_exit_code=3,
            duration_seconds=5.0,
        )
    )

    assert cmd_status(status_args(config_path)) == 0
    assert "last outcome: ERROR" in capsys.readouterr().out


def test_status_works_with_missing_runs(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    assert not (tmp_path / "runs.jsonl").exists()

    assert cmd_status(status_args(config_path)) == 0
    assert "last outcome: (none)" in capsys.readouterr().out


def test_last_outcome_from_runs_missing(tmp_path):
    config = make_config(tmp_path, tmp_path)
    assert last_outcome_from_runs(config) is None


def test_last_outcome_from_runs_skips_corrupt(tmp_path, caplog):
    import logging

    config = make_config(tmp_path, tmp_path)
    recorder = RunRecorder(runs_path(config))
    recorder.append(
        RunRecord(
            started_at=datetime(2026, 8, 1, 1, 0, tzinfo=UTC),
            finished_at=datetime(2026, 8, 1, 1, 0, 5, tzinfo=UTC),
            kind=RunKind.TASK,
            task_id="TASK-001",
            outcome=RunOutcome.SUCCESS,
            duration_seconds=5.0,
        )
    )
    recorder.path.write_text(
        recorder.path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger="forgeo.runs"):
        assert last_outcome_from_runs(config) == "SUCCESS"
    assert "corrupt" in caplog.text


def test_status_works_with_missing_backlog(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    assert not (tmp_path / "backlog.json").exists()

    assert cmd_status(status_args(config_path)) == 0
    out = capsys.readouterr().out
    assert "OPEN=0" in out
    assert "next: (none)" in out


@requires_posix
def test_status_reports_daemon_running(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    lock = acquire_run_lock(tmp_path / "backlog.lock")
    assert lock is not None
    try:
        assert cmd_status(status_args(config_path)) == 0
        assert "daemon: running" in capsys.readouterr().out
    finally:
        lock.close()


def test_status_does_not_invoke_agent(git_repo, tmp_path, monkeypatch):
    config_path = write_config(git_repo, tmp_path)
    called: list[str] = []

    def boom(*_a, **_k):
        called.append("agent")
        raise AssertionError("agent must not be started")

    monkeypatch.setattr("forgeo.cli._make_forgeo", boom)
    monkeypatch.setattr("forgeo.cli.ShellAgent", boom)

    assert cmd_status(status_args(config_path)) == 0
    assert called == []


def test_status_missing_config(tmp_path, capsys):
    assert cmd_status(status_args(tmp_path / "missing.yaml")) == 1
    assert "not found" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# forgeo logs: tail (and follow) the log file                                 #
# --------------------------------------------------------------------------- #


def logs_args(
    config_path: Path, lines: int = 100, follow: bool = False
) -> argparse.Namespace:
    return argparse.Namespace(config=config_path, lines=lines, follow=follow)


def test_logs_prints_last_n_lines(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    log_path = tmp_path / "forgeo.log"
    log_path.write_text(
        "".join(f"line{i}\n" for i in range(1, 6)), encoding="utf-8"
    )

    assert cmd_logs(logs_args(config_path, lines=3)) == 0
    out = capsys.readouterr().out
    assert "line1" not in out
    assert "line2" not in out
    assert "line3" in out
    assert "line4" in out
    assert "line5" in out


def test_logs_prints_markup_literally(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    log_path = tmp_path / "forgeo.log"
    log_path.write_text("[red]not markup[/red]\n", encoding="utf-8")

    assert cmd_logs(logs_args(config_path)) == 0
    assert "[red]not markup[/red]" in capsys.readouterr().out


def test_logs_missing_file_hints_at_start(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    assert not (tmp_path / "forgeo.log").exists()

    assert cmd_logs(logs_args(config_path)) == 0
    assert "No log file yet" in capsys.readouterr().out


def test_logs_missing_config(tmp_path, capsys):
    assert cmd_logs(logs_args(tmp_path / "missing.yaml")) == 1
    assert "not found" in capsys.readouterr().out


def test_logs_does_not_invoke_agent(git_repo, tmp_path, monkeypatch):
    config_path = write_config(git_repo, tmp_path)
    called: list[str] = []

    def boom(*_a, **_k):
        called.append("agent")
        raise AssertionError("agent must not be started")

    monkeypatch.setattr("forgeo.cli._make_forgeo", boom)
    monkeypatch.setattr("forgeo.cli.ShellAgent", boom)

    assert cmd_logs(logs_args(config_path)) == 0
    assert called == []


def test_logs_follow_prints_appended_lines(git_repo, tmp_path, capsys, monkeypatch):
    config_path = write_config(git_repo, tmp_path)
    log_path = tmp_path / "forgeo.log"
    log_path.write_text("line1\nline2\n", encoding="utf-8")
    polls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        polls["n"] += 1
        if polls["n"] == 1:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write("line3\n")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr("forgeo.cli.time.sleep", fake_sleep)

    assert cmd_logs(logs_args(config_path, follow=True)) == 0
    out = capsys.readouterr().out
    assert "line1" in out
    assert "line2" in out
    assert "line3" in out


def test_logs_parser_rejects_non_positive_lines(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["logs", "--lines", "0"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["logs", "--lines", "not-a-number"])
    args = build_parser().parse_args(["logs"])
    assert args.lines == 100
    assert args.follow is False


# --------------------------------------------------------------------------- #
# forgeo validate: read-only dry run                                          #
# --------------------------------------------------------------------------- #


def write_validate_config(path: Path, **overrides) -> None:
    """Write a minimal forgeo.yaml for validate tests (repo + agent command)."""
    fields = {"agent_command": "echo hi"}
    fields.update(overrides)
    body = "".join(f"{key}: {value}\n" for key, value in fields.items())
    path.write_text(body, encoding="utf-8")


def test_validate_healthy_reports_ready(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    backlog = tmp_path / "backlog.json"
    backlog.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "TASK-001",
                        "title": "First open",
                        "description": "Do the thing.",
                        "status": "OPEN",
                        "created_at": "2026-01-01T00:00:00Z",
                    },
                    {
                        "id": "TASK-002",
                        "title": "Done already",
                        "description": "Do the thing.",
                        "status": "COMPLETED",
                        "created_at": "2026-01-02T00:00:00Z",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    assert cmd_validate(validate_args(config_path)) == 0
    out = capsys.readouterr().out
    assert "Forgeo is ready to run." in out
    assert "lock: not held" in out
    assert "agent command: echo hi" in out
    assert "backlog parses (2 tasks)" in out


def test_validate_healthy_without_backlog(git_repo, tmp_path, capsys):
    """A missing backlog is not a problem: the daemon treats it as empty."""
    config_path = write_config(git_repo, tmp_path)
    assert not (tmp_path / "backlog.json").exists()

    assert cmd_validate(validate_args(config_path)) == 0
    assert "Forgeo is ready to run." in capsys.readouterr().out


def test_validate_missing_task_context_is_a_warning(git_repo, tmp_path, capsys):
    """A configured context file that is gone warns but does not block."""
    config_path = tmp_path / "forgeo.yaml"
    write_validate_config(
        config_path, repo=git_repo, task_context=tmp_path / "CONTEXT.md"
    )
    assert not (tmp_path / "CONTEXT.md").exists()

    assert cmd_validate(validate_args(config_path)) == 0
    out = capsys.readouterr().out
    assert "Forgeo is ready to run." in out
    assert "task_context not found" in out


def test_validate_present_task_context_raises_no_warning(git_repo, tmp_path, capsys):
    (tmp_path / "CONTEXT.md").write_text("# overview\n", encoding="utf-8")
    config_path = tmp_path / "forgeo.yaml"
    write_validate_config(
        config_path, repo=git_repo, task_context=tmp_path / "CONTEXT.md"
    )

    assert cmd_validate(validate_args(config_path)) == 0
    out = capsys.readouterr().out
    assert "Forgeo is ready to run." in out
    assert "task_context" not in out


def test_validate_missing_config(tmp_path, capsys):
    assert cmd_validate(validate_args(tmp_path / "missing.yaml")) == 1
    assert "not found" in capsys.readouterr().out


def test_validate_invalid_yaml(tmp_path, capsys):
    config_path = tmp_path / "forgeo.yaml"
    config_path.write_text("name: [unclosed\n", encoding="utf-8")

    assert cmd_validate(validate_args(config_path)) == 1
    out = capsys.readouterr().out
    assert "not valid YAML" in out
    assert "name" in out


def test_validate_invalid_schema(tmp_path, capsys):
    config_path = tmp_path / "forgeo.yaml"
    config_path.write_text("agent_command: ''\n", encoding="utf-8")

    assert cmd_validate(validate_args(config_path)) == 1
    out = capsys.readouterr().out
    assert "is invalid" in out
    assert "agent_command" in out


def test_validate_repo_missing(git_repo, tmp_path, capsys):
    config_path = tmp_path / "forgeo.yaml"
    write_validate_config(config_path, repo=tmp_path / "nope")

    assert cmd_validate(validate_args(config_path)) == 1
    assert "repository does not exist" in capsys.readouterr().out


def test_validate_repo_not_a_git_repo(tmp_path, capsys):
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    config_path = tmp_path / "forgeo.yaml"
    write_validate_config(config_path, repo=plain_dir)

    assert cmd_validate(validate_args(config_path)) == 1
    assert "not a git repository" in capsys.readouterr().out


def test_validate_remote_not_configured(git_repo, tmp_path, capsys):
    config_path = tmp_path / "forgeo.yaml"
    write_validate_config(config_path, repo=git_repo, remote="origin")

    assert cmd_validate(validate_args(config_path)) == 1
    assert "remote 'origin' is not configured" in capsys.readouterr().out


def test_validate_remote_resolves(git_repo, tmp_path, capsys):
    from tests.conftest import git

    git(git_repo, "remote", "add", "origin", "git@example.com:repo.git")
    config_path = tmp_path / "forgeo.yaml"
    write_validate_config(config_path, repo=git_repo, remote="origin")

    assert cmd_validate(validate_args(config_path)) == 0
    out = capsys.readouterr().out
    assert "remote 'origin' resolves to git@example.com:repo.git" in out
    assert "Forgeo is ready to run." in out


def test_validate_empty_repo_with_files_needs_initial_commit(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "forgeo.yaml").write_text("agent_command: echo\n", encoding="utf-8")
    config_path = write_config(repo, tmp_path)

    assert cmd_validate(validate_args(config_path)) == 1
    out = capsys.readouterr().out
    assert "no commits yet" in out
    assert "git add -A && git commit" in out


def test_validate_empty_repo_with_clean_tree_is_warning(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    config_path = write_config(repo, tmp_path)

    assert cmd_validate(validate_args(config_path)) == 0
    out = capsys.readouterr().out
    assert "no commits yet" in out
    assert "first cycle will create the initial commit" in out
    assert "Forgeo is ready to run." in out


def test_validate_bad_backlog_json(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    (tmp_path / "backlog.json").write_text("{not json\n", encoding="utf-8")

    assert cmd_validate(validate_args(config_path)) == 1
    assert "not valid JSON" in capsys.readouterr().out


def test_validate_backlog_not_a_tasks_object(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    (tmp_path / "backlog.json").write_text(json.dumps({"nope": 1}), encoding="utf-8")

    assert cmd_validate(validate_args(config_path)) == 1
    assert "'tasks' array" in capsys.readouterr().out


def test_validate_backlog_invalid_task(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    (tmp_path / "backlog.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {"id": "T-1", "title": "Bad", "description": "", "status": "OPEN"}
                ]
            }
        ),
        encoding="utf-8",
    )

    assert cmd_validate(validate_args(config_path)) == 1
    out = capsys.readouterr().out
    assert "backlog task #0 is invalid" in out
    assert "description" in out


def test_validate_reports_all_problems_at_once(git_repo, tmp_path, capsys):
    config_path = tmp_path / "forgeo.yaml"
    (tmp_path / "backlog.json").write_text("{not json\n", encoding="utf-8")
    write_validate_config(
        config_path,
        repo=git_repo,
        remote="origin",
        backlog=tmp_path / "backlog.json",
    )

    assert cmd_validate(validate_args(config_path)) == 1
    out = capsys.readouterr().out
    assert "not valid JSON" in out
    assert "remote 'origin' is not configured" in out
    assert "not ready to run (2 problem(s))" in out


@requires_posix
def test_validate_reports_lock_held(git_repo, tmp_path, monkeypatch, capsys):
    config_path = write_config(git_repo, tmp_path)
    lock = acquire_run_lock(tmp_path / "backlog.lock")
    assert lock is not None
    try:
        assert cmd_validate(validate_args(config_path)) == 0
        out = capsys.readouterr().out
        assert "lock: held" in out
        assert "run lock held" in out
        assert "Forgeo is ready to run." in out
    finally:
        lock.close()


def test_validate_never_invokes_agent_or_writes(git_repo, tmp_path, monkeypatch, capsys):
    config_path = write_config(git_repo, tmp_path)
    called: list[str] = []

    def boom(*_a, **_k):
        called.append("agent")
        raise AssertionError("agent must not be started")

    monkeypatch.setattr("forgeo.cli._make_forgeo", boom)
    monkeypatch.setattr("forgeo.cli.ShellAgent", boom)

    assert not (tmp_path / "backlog.lock").exists()
    assert not (tmp_path / "runs.jsonl").exists()
    assert cmd_validate(validate_args(config_path)) == 0
    assert called == []
    assert not (tmp_path / "backlog.lock").exists()
    assert not (tmp_path / "runs.jsonl").exists()
    assert not (tmp_path / "backlog.json").exists()


def test_validate_fetches_a_url_backlog(git_repo, tmp_path, backlog_server, capsys):
    """The endpoint answering is what "ready to run" means for a URL backlog."""
    backlog_server.document = {"tasks": [json.loads(make_task().model_dump_json())]}
    config_path = tmp_path / "forgeo.yaml"
    write_validate_config(config_path, repo=git_repo, backlog=backlog_server.url)

    assert cmd_validate(validate_args(config_path)) == 0
    out = capsys.readouterr().out
    assert "backlog endpoint answers (1 tasks)" in out
    assert "GET" in backlog_server.requests
    assert "POST" not in backlog_server.requests


def test_validate_reports_an_unreachable_url_backlog(git_repo, tmp_path, capsys):
    """A dead endpoint is a problem here, not a surprise at the first cycle."""
    config_path = tmp_path / "forgeo.yaml"
    write_validate_config(
        config_path, repo=git_repo, backlog="http://127.0.0.1:9/backlog"
    )

    assert cmd_validate(validate_args(config_path)) == 1
    assert "backlog endpoint could not be read" in capsys.readouterr().out


def test_validate_resolves_name_from_registry(tmp_path, git_repo, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    assert cmd_instance_add(argparse.Namespace(name="my-repo", config=config_path)) == 0

    assert cmd_validate(argparse.Namespace(config=DEFAULT_CONFIG, name="my-repo")) == 0
    assert "Forgeo is ready to run." in capsys.readouterr().out


@pytest.mark.parametrize("command", ["validate", "status", "once", "run"])
def test_unknown_name_exits_nonzero(tmp_path, monkeypatch, capsys, command):
    """Every command refuses an instance name that is not in the registry."""
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    if command == "run":
        args = argparse.Namespace(config=DEFAULT_CONFIG, name="nope", task="SELF-012")
    else:
        args = argparse.Namespace(config=DEFAULT_CONFIG, name="nope")
    dispatch = {
        "validate": cmd_validate,
        "status": cmd_status,
        "once": cmd_once,
        "run": cmd_run,
    }
    assert dispatch[command](args) == 1
    assert "Unknown instance" in capsys.readouterr().out


def test_stop_not_running(git_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    assert cmd_stop(stop_args(config_path)) == 1
    assert "not running" in capsys.readouterr().out


def test_stop_registers_unregistered_instance(git_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)

    assert cmd_stop(stop_args(config_path)) == 1
    assert "not running" in capsys.readouterr().out
    assert load_registry() == {"test-forgeo": str(config_path.resolve())}


@requires_posix
def test_start_registers_instance_in_registry(git_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path, interval_minutes=600)
    lock_path = tmp_path / "backlog.lock"

    assert cmd_start(start_args(config_path)) == 0
    try:
        out = capsys.readouterr().out
        assert "started in the background" in out
        assert "interval 600 min" in out
        assert wait_for(lambda: is_lock_held(lock_path))
        assert load_registry() == {"test-forgeo": str(config_path.resolve())}
        assert read_lock_pid(lock_path) is not None
        assert cmd_stop(stop_args(config_path)) == 0
        assert wait_for(lambda: not is_lock_held(lock_path))
    finally:
        if is_lock_held(lock_path):
            cmd_stop(stop_args(config_path))


@requires_posix
def test_start_detached_refuses_when_already_running(git_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path, interval_minutes=600)
    lock_path = tmp_path / "backlog.lock"
    proc = spawn_daemon(config_path)
    try:
        assert wait_for(lambda: is_lock_held(lock_path))

        assert cmd_start(start_args(config_path)) == 1
        assert "already running" in capsys.readouterr().out
    finally:
        if proc.poll() is None:
            proc.kill()
        if is_lock_held(lock_path):
            cmd_stop(stop_args(config_path))


def test_start_detached_invalid_config_fails_fast(git_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path, interval_minutes=600)
    lock_path = tmp_path / "backlog.lock"
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write("repo: /nonexistent/forgeo-repo\n")

    assert cmd_start(start_args(config_path)) == 1
    assert not is_lock_held(lock_path)
    assert "not ready to run" in capsys.readouterr().out


def test_stop_unknown_name_does_not_register(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    assert (
        cmd_stop(argparse.Namespace(config=DEFAULT_CONFIG, name="nope", timeout=30.0))
        == 1
    )
    assert "Unknown instance" in capsys.readouterr().out
    assert load_registry() == {}


def test_once_does_not_register_instance(git_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    fake = FakeForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: fake)

    assert cmd_once(once_args(config_path)) == 0
    assert load_registry() == {}


def test_stop_missing_config(tmp_path, capsys):
    assert cmd_stop(stop_args(tmp_path / "missing.yaml")) == 1
    assert "not found" in capsys.readouterr().out


@requires_posix
def test_stop_stale_pid_errors(git_repo, tmp_path, monkeypatch, capsys):
    """Lock held by an unknown process with a dead recorded pid: refuse."""
    import fcntl

    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    handle = (tmp_path / "backlog.lock").open("w")
    handle.write("pid=999999999\n")
    handle.flush()
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert cmd_stop(stop_args(config_path)) == 1
        assert "is gone" in capsys.readouterr().out
    finally:
        handle.close()


@requires_posix
def test_stop_terminates_running_daemon(git_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path, interval_minutes=600)
    lock_path = tmp_path / "backlog.lock"
    proc = spawn_daemon(config_path)
    try:
        assert wait_for(lambda: is_lock_held(lock_path))

        assert cmd_stop(stop_args(config_path)) == 0
        assert "stopped" in capsys.readouterr().out
        assert wait_for(lambda: proc.poll() is not None)
        assert not is_lock_held(lock_path)
    finally:
        if proc.poll() is None:
            proc.kill()


@requires_posix
def test_restart_starts_daemon_when_not_running(git_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path, interval_minutes=600)
    lock_path = tmp_path / "backlog.lock"

    assert cmd_restart(restart_args(config_path)) == 0
    try:
        out = capsys.readouterr().out
        assert "restarted" in out
        assert "interval 600 min" in out
        assert is_lock_held(lock_path)
        pid = read_lock_pid(lock_path)
        assert pid is not None
    finally:
        cmd_stop(stop_args(config_path))


@requires_posix
def test_restart_replaces_running_daemon(git_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path, interval_minutes=600)
    lock_path = tmp_path / "backlog.lock"
    old_proc = spawn_daemon(config_path)
    try:
        assert wait_for(lambda: is_lock_held(lock_path))
        old_pid = read_lock_pid(lock_path)
        assert old_pid is not None
        capsys.readouterr()

        assert cmd_restart(restart_args(config_path)) == 0
        out = capsys.readouterr().out
        assert "restarted" in out
        new_pid = read_lock_pid(lock_path)
        assert new_pid is not None
        assert new_pid != old_pid
        assert wait_for(lambda: old_proc.poll() is not None)
        assert is_lock_held(lock_path)
    finally:
        if old_proc.poll() is None:
            old_proc.kill()
        cmd_stop(stop_args(config_path))


# --------------------------------------------------------------------------- #
# Instance registry CLI (--name, instance add/rm/list, forgeo list alias)    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command", ["start", "once", "run", "status", "validate", "stop", "restart"]
)
def test_parser_accepts_name_for_commands(command):
    argv = [command, "--name", "my-repo"]
    if command == "run":
        argv += ["--task", "SELF-012"]
    args = build_parser().parse_args(argv)
    assert getattr(args, "name", None) == "my-repo"


@pytest.mark.parametrize(
    "command", ["start", "once", "run", "status", "validate", "stop", "restart"]
)
def test_parser_rejects_name_with_config(command):
    argv = [command, "--name", "x", "--config", "forgeo.yaml"]
    if command == "run":
        argv += ["--task", "SELF-012"]
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(argv)
    assert excinfo.value.code == 2


def test_parser_parses_instance_subcommands():
    args = build_parser().parse_args(["instance", "add", "my-repo", "--config", "a.yaml"])
    assert args.action == "instance"
    assert args.instance_action == "add"
    assert args.name == "my-repo"
    assert args.config == Path("a.yaml")

    assert build_parser().parse_args(["instance", "rm", "my-repo"]).instance_action == "rm"
    assert build_parser().parse_args(["instance", "list"]).instance_action == "list"
    assert build_parser().parse_args(["list"]).action == "list"


def test_auth_login_parser_accepts_callback_options():
    args = build_parser().parse_args(
        [
            "auth",
            "login",
            "--provider",
            "gitlab",
            "--flow",
            "browser",
            "--callback-port",
            "8765",
            "--no-open-browser",
        ]
    )
    assert args.callback_port == 8765
    assert args.no_open_browser is True


def test_auth_login_passes_browser_options_to_github_flow(tmp_path, monkeypatch):
    calls = {}

    def fake_browser_flow(client_id, oauth_base, scope, **kwargs):
        calls.update(client_id=client_id, oauth_base=oauth_base, scope=scope, **kwargs)
        return {"access_token": "secret"}

    monkeypatch.setattr("forgeo.oauth_github.run_browser_flow", fake_browser_flow)
    args = build_parser().parse_args(
        [
            "auth",
            "login",
            "--provider",
            "github",
            "--client-id",
            "client",
            "--flow",
            "browser",
            "--token-file",
            str(tmp_path / "token.json"),
            "--callback-port",
            "8765",
            "--no-open-browser",
        ]
    )

    assert cmd_auth_login(args) == 0
    assert calls["client_id"] == "client"
    assert calls["open_browser"] is False
    assert calls["callback_port"] == 8765
    assert (tmp_path / "token.json").exists()


def test_auth_status_uses_project_default_config(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "project"
    token_path = config_dir / "tokens" / "github.json"
    config_dir.mkdir()
    (config_dir / "forgeo.yaml").write_text(
        "backlog_provider: github\n"
        "backlog: https://api.github.com\n"
        "github:\n"
        "  repo: owner/repo\n"
        "  auth:\n"
        "    oauth:\n"
        "      client_id: client\n"
        "      token_file: tokens/github.json\n"
        "agent_command: echo\n",
        encoding="utf-8",
    )
    from forgeo.oauth_github import GithubTokenStore

    GithubTokenStore(path=token_path).save({"access_token": "secret-token"})
    monkeypatch.chdir(config_dir)

    args = build_parser().parse_args(["auth", "status", "--provider", "github"])
    assert cmd_auth_status(args) == 0
    output = capsys.readouterr().out
    assert "tokens" in output
    assert token_path.name in output


def test_instance_add_and_register(tmp_path, git_repo, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)

    assert cmd_instance_add(argparse.Namespace(name="my-repo", config=config_path)) == 0
    assert "Registered instance" in capsys.readouterr().out
    assert load_registry() == {"my-repo": str(config_path.resolve())}


def test_instance_add_invalid_name(tmp_path, git_repo, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)

    assert cmd_instance_add(argparse.Namespace(name="bad name", config=config_path)) == 1
    assert "invalid instance name" in capsys.readouterr().out
    assert load_registry() == {}


def test_instance_add_duplicate(tmp_path, git_repo, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    assert cmd_instance_add(argparse.Namespace(name="my-repo", config=config_path)) == 0
    capsys.readouterr()

    assert cmd_instance_add(argparse.Namespace(name="my-repo", config=config_path)) == 1
    assert "already registered" in capsys.readouterr().out


def test_instance_add_missing_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    assert (
        cmd_instance_add(argparse.Namespace(name="x", config=tmp_path / "missing.yaml"))
        == 1
    )
    assert "No such file" in capsys.readouterr().out


def test_instance_rm_unregisters(tmp_path, git_repo, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    assert cmd_instance_add(argparse.Namespace(name="my-repo", config=config_path)) == 0
    capsys.readouterr()

    assert cmd_instance_rm(argparse.Namespace(name="my-repo")) == 0
    assert "Unregistered" in capsys.readouterr().out
    assert load_registry() == {}

    assert cmd_instance_rm(argparse.Namespace(name="my-repo")) == 1
    assert "Unknown instance" in capsys.readouterr().out


def test_instance_rm_never_touches_config(tmp_path, git_repo, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    before = config_path.read_text()
    assert cmd_instance_add(argparse.Namespace(name="my-repo", config=config_path)) == 0

    assert cmd_instance_rm(argparse.Namespace(name="my-repo")) == 0
    assert config_path.exists()
    assert config_path.read_text() == before


def test_instance_list_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    assert cmd_instance_list(argparse.Namespace()) == 0
    assert "No registered instances" in capsys.readouterr().out


def test_instance_list_table_shows_state(tmp_path, git_repo, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    assert cmd_instance_add(argparse.Namespace(name="my-repo", config=config_path)) == 0
    backlog = tmp_path / "backlog.json"
    backlog.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "TASK-001",
                        "title": "Do it",
                        "description": "Do the thing.",
                        "status": "OPEN",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    RunRecorder(backlog.with_name("runs.jsonl")).append(
        RunRecord(
            started_at=datetime(2026, 8, 1, 1, 0, tzinfo=UTC),
            finished_at=datetime(2026, 8, 1, 1, 0, 5, tzinfo=UTC),
            kind=RunKind.TASK,
            task_id="TASK-001",
            task_title="Do it",
            outcome=RunOutcome.SUCCESS,
            agent_exit_code=0,
            duration_seconds=5.0,
        )
    )

    capsys.readouterr()
    assert cmd_instance_list(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "my-repo" in out
    assert "SUCCESS" in out
    assert "stopped" in out
    assert str(config_path) not in out
    assert str(git_repo) not in out
    assert "OPEN=" not in out


@requires_posix
def test_instance_list_reports_daemon_running(tmp_path, git_repo, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    assert cmd_instance_add(argparse.Namespace(name="my-repo", config=config_path)) == 0
    lock = acquire_run_lock(tmp_path / "backlog.lock")
    assert lock is not None
    try:
        assert cmd_instance_list(argparse.Namespace()) == 0
        assert "running" in capsys.readouterr().out
    finally:
        lock.close()


def test_instance_dispatch(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    assert cmd_instance(argparse.Namespace(instance_action="list")) == 0
    assert "No registered instances" in capsys.readouterr().out


def test_main_list_alias(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    assert main(["list"]) == 0
    assert "No registered instances" in capsys.readouterr().out


def test_status_resolves_name_from_registry(tmp_path, git_repo, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    assert cmd_instance_add(argparse.Namespace(name="my-repo", config=config_path)) == 0

    assert cmd_status(argparse.Namespace(config=DEFAULT_CONFIG, name="my-repo")) == 0
    assert "name: test-forgeo" in capsys.readouterr().out


def test_once_resolves_name_from_registry(tmp_path, git_repo, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    assert cmd_instance_add(argparse.Namespace(name="my-repo", config=config_path)) == 0
    fake = FakeForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: fake)

    assert cmd_once(argparse.Namespace(config=DEFAULT_CONFIG, name="my-repo")) == 0
    assert fake.cycles == 1
    assert "Cycle finished: task" in capsys.readouterr().out


def test_run_resolves_name_from_registry(tmp_path, git_repo, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    assert cmd_instance_add(argparse.Namespace(name="my-repo", config=config_path)) == 0
    fake = FakeForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: fake)

    assert (
        cmd_run(argparse.Namespace(config=DEFAULT_CONFIG, name="my-repo", task="SELF-012"))
        == 0
    )
    assert fake.cycles == 1
    assert fake.run_task_ids == ["SELF-012"]
    assert "Cycle finished: task" in capsys.readouterr().out


def test_stop_resolves_name_from_registry(tmp_path, git_repo, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    assert cmd_instance_add(argparse.Namespace(name="my-repo", config=config_path)) == 0

    assert cmd_stop(argparse.Namespace(config=DEFAULT_CONFIG, name="my-repo")) == 1
    assert "not running" in capsys.readouterr().out


@requires_posix
def test_restart_resolves_name_from_registry(tmp_path, git_repo, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path, interval_minutes=600)
    assert cmd_instance_add(argparse.Namespace(name="my-repo", config=config_path)) == 0

    assert (
        cmd_restart(argparse.Namespace(config=DEFAULT_CONFIG, name="my-repo", timeout=30.0))
        == 0
    )
    try:
        out = capsys.readouterr().out
        assert "restarted" in out
        assert "interval 600 min" in out
        assert is_lock_held(tmp_path / "backlog.lock")
    finally:
        cmd_stop(stop_args(config_path))


@requires_posix
def test_two_instances_stay_fully_independent(
    git_repo, git_template, tmp_path, monkeypatch, capsys
):
    """Two registered instances with configs in different directories keep every
    lock file, log, backlog, and runs.jsonl fully independent, and concurrent
    status/once calls never interfere."""
    registry = tmp_path / "registry.yaml"
    monkeypatch.setenv("FORGEO_REGISTRY", str(registry))

    repo_b = tmp_path / "repo-b"
    shutil.copytree(git_template, repo_b)

    dir_a = tmp_path / "inst-a"
    dir_b = tmp_path / "inst-b"
    dir_a.mkdir()
    dir_b.mkdir()

    config_a = write_config_in(
        dir_a,
        git_repo,
        tmp_path,
        name="inst-a",
        backlog=dir_a / "backlog.json",
        blocker_file=dir_a / "BLOCKER.md",
        agent_command="echo done > done-a.txt",
        interval_minutes=600,
    )
    config_b = write_config_in(
        dir_b,
        repo_b,
        tmp_path,
        name="inst-b",
        backlog=dir_b / "backlog.json",
        blocker_file=dir_b / "BLOCKER.md",
        agent_command="echo done > done-b.txt",
        interval_minutes=600,
    )
    assert cmd_instance_add(argparse.Namespace(name="inst-a", config=config_a)) == 0
    assert cmd_instance_add(argparse.Namespace(name="inst-b", config=config_b)) == 0
    assert set(list_instances_names()) == {"inst-a", "inst-b"}

    args_a = argparse.Namespace(config=DEFAULT_CONFIG, name="inst-a")
    args_b = argparse.Namespace(config=DEFAULT_CONFIG, name="inst-b")

    # Lock files are independent: holding A's lock leaves B's lock free.
    lock_a = acquire_run_lock(dir_a / "backlog.lock")
    assert lock_a is not None
    lock_b = acquire_run_lock(dir_b / "backlog.lock")
    assert lock_b is not None
    lock_b.close()
    assert is_lock_held(dir_a / "backlog.lock")
    assert not is_lock_held(dir_b / "backlog.lock")

    # status --name reports each instance's own daemon state.
    capsys.readouterr()
    assert cmd_status(args_a) == 0
    assert "daemon: running" in capsys.readouterr().out
    assert cmd_status(args_b) == 0
    out_b = capsys.readouterr().out
    assert "name: inst-b" in out_b
    assert "daemon: not running" in out_b
    lock_a.close()

    # Backlogs and runs.jsonl stay at each instance's own paths.
    (dir_a / "backlog.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "A-1",
                        "title": "Alpha task",
                        "description": "Do the thing.",
                        "status": "OPEN",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (dir_b / "backlog.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "B-1",
                        "title": "Beta task",
                        "description": "Do the thing.",
                        "status": "OPEN",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    RunRecorder(dir_a / "runs.jsonl").append(
        RunRecord(
            started_at=datetime(2026, 8, 1, 1, 0, tzinfo=UTC),
            finished_at=datetime(2026, 8, 1, 1, 0, 5, tzinfo=UTC),
            kind=RunKind.TASK,
            task_id="A-1",
            task_title="Alpha task",
            outcome=RunOutcome.SUCCESS,
            agent_exit_code=0,
            commit_sha="aaaa",
            duration_seconds=5.0,
        )
    )
    RunRecorder(dir_b / "runs.jsonl").append(
        RunRecord(
            started_at=datetime(2026, 8, 1, 2, 0, tzinfo=UTC),
            finished_at=datetime(2026, 8, 1, 2, 0, 5, tzinfo=UTC),
            kind=RunKind.TASK,
            task_id="B-1",
            task_title="Beta task",
            outcome=RunOutcome.ERROR,
            agent_exit_code=3,
            duration_seconds=5.0,
        )
    )

    assert cmd_status(args_a) == 0
    out_a = capsys.readouterr().out
    assert "OPEN=1" in out_a
    assert "A-1 — Alpha task" in out_a
    assert "last outcome: SUCCESS" in out_a
    assert "B-1" not in out_a

    assert cmd_status(args_b) == 0
    out_b = capsys.readouterr().out
    assert "OPEN=1" in out_b
    assert "B-1 — Beta task" in out_b
    assert "last outcome: ERROR" in out_b
    assert "A-1" not in out_b

    # Concurrent `forgeo status --name` subprocesses never interfere.
    env = {**os.environ, "FORGEO_REGISTRY": str(registry)}
    status_procs = [
        subprocess.Popen(
            [sys.executable, "-m", "forgeo", "status", "--name", "inst-a"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ),
        subprocess.Popen(
            [sys.executable, "-m", "forgeo", "status", "--name", "inst-b"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ),
    ]
    status_outputs: list[str] = []
    for proc in status_procs:
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, f"status failed: {err}\n{out}"
        status_outputs.append(out)
    assert "inst-a" in status_outputs[0] and "A-1" in status_outputs[0]
    assert "B-1" not in status_outputs[0]
    assert "inst-b" in status_outputs[1] and "B-1" in status_outputs[1]
    assert "A-1" not in status_outputs[1]

    # Concurrent `forgeo once --name` cycles run on separate locks/repos.
    cycle_procs = [
        subprocess.Popen(
            [sys.executable, "-m", "forgeo", "once", "--name", "inst-a"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ),
        subprocess.Popen(
            [sys.executable, "-m", "forgeo", "once", "--name", "inst-b"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ),
    ]
    for proc in cycle_procs:
        out, err = proc.communicate(timeout=90)
        assert proc.returncode == 0, f"once failed: {err}\n{out}"

    # Each instance's log, backlog, and runs.jsonl were updated independently.
    log_a = (dir_a / "forgeo.log").read_text(encoding="utf-8")
    log_b = (dir_b / "forgeo.log").read_text(encoding="utf-8")
    assert str(config_a.resolve()) in log_a
    assert str(config_b.resolve()) in log_b
    assert str(config_b.resolve()) not in log_a
    assert str(config_a.resolve()) not in log_b

    backlog_a = json.loads((dir_a / "backlog.json").read_text(encoding="utf-8"))
    backlog_b = json.loads((dir_b / "backlog.json").read_text(encoding="utf-8"))
    assert [task["id"] for task in backlog_a["tasks"]] == ["A-1"]
    assert [task["id"] for task in backlog_b["tasks"]] == ["B-1"]
    assert backlog_a["tasks"][0]["status"] == "COMPLETED"
    assert backlog_b["tasks"][0]["status"] == "COMPLETED"

    runs_a = RunRecorder(dir_a / "runs.jsonl").read()
    runs_b = RunRecorder(dir_b / "runs.jsonl").read()
    assert any(record.task_id == "A-1" for record in runs_a)
    assert any(record.task_id == "B-1" for record in runs_b)
    assert all(record.task_id in (None, "A-1") for record in runs_a)
    assert all(record.task_id in (None, "B-1") for record in runs_b)

    # The registry now lists both instances.
    infos = list_instances()
    assert {info.name for info in infos} == {"inst-a", "inst-b"}


def list_instances_names() -> list[str]:
    return [info.name for info in list_instances()]


def task_add_args(
    config_path: Path,
    title: str = "Build the thing",
    description: str | None = "Do the work.",
    task_id: str | None = None,
    acceptance: list[str] | None = None,
    depends_on: list[str] | None = None,
    run_at: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        config=config_path,
        title=title,
        description=description,
        id=task_id,
        acceptance=acceptance,
        depends_on=depends_on,
        run_at=run_at,
    )


def task_list_args(
    config_path: Path, status: str | None = None, limit: int | None = None
) -> argparse.Namespace:
    return argparse.Namespace(config=config_path, status=status, limit=limit)


def task_reopen_args(config_path: Path, task_id: str) -> argparse.Namespace:
    return argparse.Namespace(config=config_path, task=task_id)


def write_backlog(tmp_path: Path, tasks: list) -> Path:
    backlog = tmp_path / "backlog.json"
    backlog.write_text(
        json.dumps({"tasks": [task.model_dump(mode="json") for task in tasks]}),
        encoding="utf-8",
    )
    return backlog


def read_backlog_tasks(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / "backlog.json").read_text(encoding="utf-8"))["tasks"]


def test_task_add_creates_first_task_with_auto_id(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)

    assert cmd_task_add(task_add_args(config_path)) == 0
    tasks = read_backlog_tasks(tmp_path)
    assert [task["id"] for task in tasks] == ["TASK-001"]
    assert tasks[0]["status"] == "OPEN"
    out = capsys.readouterr().out
    assert "Created task TASK-001" in out
    assert "forgeo run --task TASK-001" in out


def test_task_add_increments_past_highest_task_id(git_repo, tmp_path):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001"), make_task(id="TASK-007")])

    assert cmd_task_add(task_add_args(config_path)) == 0
    assert [task["id"] for task in read_backlog_tasks(tmp_path)] == [
        "TASK-001",
        "TASK-007",
        "TASK-008",
    ]


def test_task_add_honors_explicit_id_and_extras(git_repo, tmp_path):
    config_path = write_config(git_repo, tmp_path)

    args = task_add_args(
        config_path,
        task_id="CUSTOM-1",
        acceptance=["pytest passes"],
        depends_on=["TASK-001"],
    )
    assert cmd_task_add(args) == 0
    tasks = read_backlog_tasks(tmp_path)
    assert tasks[0]["id"] == "CUSTOM-1"
    assert tasks[0]["acceptance_criteria"] == ["pytest passes"]
    assert tasks[0]["dependencies"] == ["TASK-001"]


def test_task_add_refuses_duplicate_id(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001")])

    assert cmd_task_add(task_add_args(config_path, task_id="TASK-001")) == 1
    assert "already exists" in capsys.readouterr().out


def test_task_add_refuses_blank_title(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)

    assert cmd_task_add(task_add_args(config_path, title="   ")) == 1
    assert "must not be blank" in capsys.readouterr().out


def test_task_add_defaults_description_to_title(git_repo, tmp_path):
    config_path = write_config(git_repo, tmp_path)

    assert cmd_task_add(task_add_args(config_path, description=None)) == 0
    tasks = read_backlog_tasks(tmp_path)
    assert tasks[0]["title"] == "Build the thing"
    assert tasks[0]["description"] == "Build the thing"


def test_task_add_refuses_explicit_blank_description(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)

    assert cmd_task_add(task_add_args(config_path, description="   ")) == 1
    assert "must not be blank" in capsys.readouterr().out


def test_task_add_missing_config(tmp_path, capsys):
    assert cmd_task_add(task_add_args(tmp_path / "missing.yaml")) == 1
    assert "not found" in capsys.readouterr().out


def test_task_list_shows_tasks(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(
        tmp_path,
        [
            make_task(id="TASK-001", title="First"),
            make_task(id="TASK-002", title="Second", status="BLOCKED"),
        ],
    )

    assert cmd_task_list(task_list_args(config_path)) == 0
    out = capsys.readouterr().out
    assert "TASK-001" in out
    assert "TASK-002" in out
    assert "First" in out


def test_task_list_status_filter(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(
        tmp_path,
        [
            make_task(id="TASK-001", status="OPEN"),
            make_task(id="TASK-002", status="BLOCKED"),
        ],
    )

    assert cmd_task_list(task_list_args(config_path, status="open")) == 0
    out = capsys.readouterr().out
    assert "TASK-001" in out
    assert "TASK-002" not in out


def test_task_list_empty_backlog_hints_at_add(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)

    assert cmd_task_list(task_list_args(config_path)) == 0
    out = capsys.readouterr().out
    assert "No tasks yet" in out
    assert "forgeo task add" in out


def test_task_list_rejects_bad_limit(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)

    assert cmd_task_list(task_list_args(config_path, limit=0)) == 1
    assert "--limit" in capsys.readouterr().out


def task_next_args(config_path: Path) -> argparse.Namespace:
    return argparse.Namespace(config=config_path)


def test_task_next_picks_oldest_runnable_and_explains_queue(
    git_repo, tmp_path, capsys
):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(
        tmp_path,
        [
            make_task(id="TASK-001", title="First"),
            make_task(id="TASK-002", title="Second"),
        ],
    )

    assert cmd_task_next(task_next_args(config_path)) == 0
    out = capsys.readouterr().out
    assert "next: TASK-001 — First" in out
    assert "oldest runnable" in out
    assert "skipped: TASK-002" in out
    assert "queued behind TASK-001" in out
    assert "forgeo run --task TASK-001" in out


def test_task_next_prefers_due_run_at(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(
        tmp_path,
        [
            make_task(id="TASK-001", title="First"),
            make_task(
                id="TASK-002",
                title="Due",
                run_at="2000-01-01T00:00:00Z",
            ),
        ],
    )

    assert cmd_task_next(task_next_args(config_path)) == 0
    out = capsys.readouterr().out
    assert "next: TASK-002 — Due" in out
    assert "due since" in out
    assert "skipped: TASK-001" in out


def test_task_next_none_runnable_explains_waiting(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(
        tmp_path,
        [
            make_task(
                id="TASK-001", title="Waits", dependencies=["TASK-009"]
            ),
            make_task(
                id="TASK-002",
                title="Later",
                run_at="2999-01-01T00:00:00Z",
            ),
        ],
    )

    assert cmd_task_next(task_next_args(config_path)) == 0
    out = capsys.readouterr().out
    assert "next: (none)" in out
    assert "waiting: TASK-001" in out
    assert "waiting on TASK-009 (missing)" in out
    assert "waiting: TASK-002" in out
    assert "scheduled for" in out
    assert "earliest scheduled" in out


def test_task_next_paused_while_blocked(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(
        tmp_path,
        [
            make_task(id="TASK-001", title="Ready"),
            make_task(id="TASK-002", title="Stuck", status="BLOCKED"),
        ],
    )

    assert cmd_task_next(task_next_args(config_path)) == 0
    out = capsys.readouterr().out
    assert "next: (paused)" in out
    assert "TASK-002" in out
    assert "forgeo task reopen" in out


def test_task_next_empty_backlog_hints_at_add(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)

    assert cmd_task_next(task_next_args(config_path)) == 0
    out = capsys.readouterr().out
    assert "next: (none)" in out
    assert "no OPEN tasks" in out
    assert "forgeo task add" in out


def test_task_next_matches_scheduler_pick(git_repo, tmp_path):
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)
    tasks = [
        make_task(
            id="TASK-001",
            title="Old",
            created_at=now - timedelta(days=2),
        ),
        make_task(
            id="TASK-002",
            title="Due",
            created_at=now - timedelta(days=1),
            run_at=(now - timedelta(hours=1)).isoformat(),
        ),
        make_task(
            id="TASK-003",
            title="Future",
            created_at=now - timedelta(days=3),
            run_at=(now + timedelta(days=1)).isoformat(),
        ),
    ]
    out = render_task_next(tasks, now=now)
    assert "next: TASK-002 — Due" in out
    assert "skipped: TASK-001" in out
    assert "skipped: TASK-003" in out
    assert "scheduled for" in out


def test_task_next_parser_and_dispatch(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001")])

    args = build_parser().parse_args(
        ["task", "next", "--config", str(config_path)]
    )
    assert args.task_action == "next"
    assert cmd_task(args) == 0
    assert "next: TASK-001" in capsys.readouterr().out

    assert main(["task", "next", "--config", str(config_path)]) == 0
    assert "next: TASK-001" in capsys.readouterr().out


def test_task_reopen_blocked_task(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001", status="BLOCKED")])

    assert cmd_task_reopen(task_reopen_args(config_path, "TASK-001")) == 0
    assert read_backlog_tasks(tmp_path)[0]["status"] == "OPEN"
    out = capsys.readouterr().out
    assert "Reopened task TASK-001" in out
    assert "was BLOCKED" in out


def test_task_reopen_failed_task(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001", status="FAILED")])

    assert cmd_task_reopen(task_reopen_args(config_path, "TASK-001")) == 0
    assert read_backlog_tasks(tmp_path)[0]["status"] == "OPEN"
    assert "now OPEN" in capsys.readouterr().out


def test_task_reopen_unknown_task(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)

    assert cmd_task_reopen(task_reopen_args(config_path, "TASK-999")) == 1
    assert "Unknown task" in capsys.readouterr().out


def test_task_reopen_refuses_open_task(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001", status="OPEN")])

    assert cmd_task_reopen(task_reopen_args(config_path, "TASK-001")) == 1
    assert "already OPEN" in capsys.readouterr().out


def test_task_reopen_refuses_completed_task(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001", status="COMPLETED")])

    assert cmd_task_reopen(task_reopen_args(config_path, "TASK-001")) == 1
    assert "cannot be reopened" in capsys.readouterr().out


def test_task_group_dispatches_to_subcommands(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001")])

    args = argparse.Namespace(config=config_path, task_action="list", status=None, limit=None)
    assert cmd_task(args) == 0
    assert "TASK-001" in capsys.readouterr().out


def test_task_main_entrypoint_lists_tasks(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001")])

    assert main(["task", "list", "--config", str(config_path)]) == 0
    assert "TASK-001" in capsys.readouterr().out


def task_show_args(config_path: Path, task_id: str) -> argparse.Namespace:
    return argparse.Namespace(config=config_path, task=task_id)


def test_task_show_full_detail(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(
        tmp_path,
        [
            make_task(
                id="TASK-001",
                title="First",
                description="Build it with [brackets] intact.",
                acceptance_criteria=["pytest passes"],
                dependencies=["TASK-000"],
            )
        ],
    )

    assert cmd_task_show(task_show_args(config_path, "TASK-001")) == 0
    out = capsys.readouterr().out
    assert "id: TASK-001" in out
    assert "status: OPEN" in out
    assert "title: First" in out
    assert "Build it with [brackets] intact." in out
    assert "pytest passes" in out
    assert "TASK-000" in out
    assert "forgeo run --task TASK-001" in out


def test_task_show_minimal_task_omits_empty_sections(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001")])

    assert cmd_task_show(task_show_args(config_path, "TASK-001")) == 0
    out = capsys.readouterr().out
    assert "acceptance criteria:" not in out
    assert "blocker reason:" not in out
    assert "failure reason:" not in out


def test_task_show_blocked_reason_and_reopen_hint(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(
        tmp_path,
        [
            make_task(
                id="TASK-003",
                status="BLOCKED",
                blocker_reason=["Needs human: pick the color."],
            )
        ],
    )

    assert cmd_task_show(task_show_args(config_path, "TASK-003")) == 0
    out = capsys.readouterr().out
    assert "status: BLOCKED" in out
    assert "Needs human: pick the color." in out
    assert "forgeo task reopen --task TASK-003" in out


def test_task_show_unknown_task(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)

    assert cmd_task_show(task_show_args(config_path, "TASK-999")) == 1
    assert "Unknown task" in capsys.readouterr().out


def test_task_show_missing_config(tmp_path, capsys):
    assert cmd_task_show(task_show_args(tmp_path / "missing.yaml", "TASK-001")) == 1
    assert "not found" in capsys.readouterr().out


def test_task_show_main_entrypoint(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001", description="Shown via main.")])

    assert main(["task", "show", "--task", "TASK-001", "--config", str(config_path)]) == 0
    assert "Shown via main." in capsys.readouterr().out


def test_task_show_positional_id(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001", description="Shown positionally.")])

    assert main(["task", "show", "TASK-001", "--config", str(config_path)]) == 0
    assert "Shown positionally." in capsys.readouterr().out


def test_task_show_refuses_doubled_id(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    args = argparse.Namespace(config=config_path, task="TASK-001", task_id="TASK-001")
    assert cmd_task_show(args) == 2
    assert "not both" in capsys.readouterr().out


def test_task_show_refuses_missing_id(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    args = argparse.Namespace(config=config_path, task=None, task_id=None)
    assert cmd_task_show(args) == 2
    assert "Missing task id" in capsys.readouterr().out


def test_task_add_positional_title(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    args = argparse.Namespace(
        config=config_path,
        title=None,
        title_pos="Positional title",
        description=None,
        description_file=None,
        id=None,
        acceptance=None,
        depends_on=None,
        run_at=None,
    )
    assert cmd_task_add(args) == 0
    tasks = read_backlog_tasks(tmp_path)
    assert tasks[0]["title"] == "Positional title"
    assert tasks[0]["description"] == "Positional title"


def test_task_add_refuses_doubled_title(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    args = argparse.Namespace(
        config=config_path,
        title="Flag title",
        title_pos="Positional title",
        description=None,
        description_file=None,
        id=None,
        acceptance=None,
        depends_on=None,
        run_at=None,
    )
    assert cmd_task_add(args) == 2
    assert "not both" in capsys.readouterr().out


def test_task_add_parser_positional_title():
    args = build_parser().parse_args(["task", "add", "Hello"])
    assert args.title is None
    assert args.title_pos == "Hello"


def task_edit_args(config_path: Path, task_id: str, **overrides) -> argparse.Namespace:
    params = {
        "config": config_path,
        "task": task_id,
        "title": None,
        "description": None,
        "acceptance": None,
        "depends_on": None,
        "files": None,
        "run_at": None,
        "clear_acceptance": False,
        "clear_depends_on": False,
        "clear_files": False,
        "clear_run_at": False,
    }
    params.update(overrides)
    return argparse.Namespace(**params)


def test_task_edit_updates_description(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-003", status="BLOCKED")])

    assert (
        cmd_task_edit(
            task_edit_args(config_path, "TASK-003", description="Pick blue; see brand guide.")
        )
        == 0
    )
    assert read_backlog_tasks(tmp_path)[0]["description"] == "Pick blue; see brand guide."
    assert "Updated task TASK-003" in capsys.readouterr().out


def test_task_edit_replaces_lists_and_clears(git_repo, tmp_path):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001")])

    assert (
        cmd_task_edit(
            task_edit_args(
                config_path,
                "TASK-001",
                acceptance=["pytest passes"],
                depends_on=["TASK-009"],
                files=["src/a.py"],
            )
        )
        == 0
    )
    task = read_backlog_tasks(tmp_path)[0]
    assert task["acceptance_criteria"] == ["pytest passes"]
    assert task["dependencies"] == ["TASK-009"]
    assert task["files_to_modify"] == ["src/a.py"]

    assert (
        cmd_task_edit(task_edit_args(config_path, "TASK-001", clear_acceptance=True))
        == 0
    )
    assert read_backlog_tasks(tmp_path)[0]["acceptance_criteria"] == []


def test_task_edit_unknown_task(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)

    assert cmd_task_edit(task_edit_args(config_path, "TASK-999", title="New")) == 1
    assert "Unknown task" in capsys.readouterr().out


def test_task_edit_requires_a_field(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001")])

    assert cmd_task_edit(task_edit_args(config_path, "TASK-001")) == 1
    assert "Nothing to update" in capsys.readouterr().out


def test_task_edit_refuses_blank_title(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001")])

    assert cmd_task_edit(task_edit_args(config_path, "TASK-001", title="   ")) == 1
    assert "must not be blank" in capsys.readouterr().out


def test_task_edit_refuses_conflicting_flags(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001")])

    assert (
        cmd_task_edit(
            task_edit_args(
                config_path, "TASK-001", acceptance=["x"], clear_acceptance=True
            )
        )
        == 1
    )
    assert "not both" in capsys.readouterr().out


def test_task_add_with_run_at_iso(git_repo, tmp_path):
    config_path = write_config(git_repo, tmp_path)

    assert (
        cmd_task_add(task_add_args(config_path, run_at="2026-10-01T09:00:00Z")) == 0
    )
    tasks = read_backlog_tasks(tmp_path)
    assert tasks[0]["run_at"] == "2026-10-01T09:00:00Z"


def test_task_add_with_run_at_now_is_due(git_repo, tmp_path):
    from forgeo.backlog import oldest_open_task
    from forgeo.models import Task

    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001")])

    before = datetime.now(UTC) - timedelta(seconds=5)
    assert cmd_task_add(task_add_args(config_path, run_at="now")) == 0
    tasks = read_backlog_tasks(tmp_path)
    assert len(tasks) == 2
    run_at = Task.model_validate(tasks[1]).run_at
    assert run_at is not None and run_at >= before
    picked = oldest_open_task([Task.model_validate(entry) for entry in tasks])
    assert picked is not None and picked.id == "TASK-002"


def test_task_add_refuses_bad_run_at(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)

    assert cmd_task_add(task_add_args(config_path, run_at="not-a-date")) == 1
    assert "Invalid task" in capsys.readouterr().out


def test_parser_parses_run_flag_for_task_add():
    args = build_parser().parse_args(["task", "add", "--title", "Quick", "--run"])
    assert args.task_action == "add"
    assert args.run is True


def test_task_add_run_creates_and_runs_immediately(
    git_repo, tmp_path, monkeypatch, capsys
):
    config_path = write_config(git_repo, tmp_path)
    fake = FakeForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: fake)

    args = task_add_args(config_path)
    args.run = True
    assert cmd_task_add(args) == 0
    tasks = read_backlog_tasks(tmp_path)
    assert [task["id"] for task in tasks] == ["TASK-001"]
    assert fake.run_task_ids == ["TASK-001"]
    out = capsys.readouterr().out
    assert "Created task TASK-001" in out
    assert "Cycle finished: task" in out


def test_task_add_run_refuses_with_run_at(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)

    args = task_add_args(config_path, run_at="2026-10-01T09:00:00Z")
    args.run = True
    assert cmd_task_add(args) == 1
    assert "not both" in capsys.readouterr().out
    assert not (tmp_path / "backlog.json").exists()


@requires_posix
def test_task_add_run_refuses_while_lock_held(git_repo, tmp_path, monkeypatch, capsys):
    config_path = write_config(git_repo, tmp_path)
    fake = FakeForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: fake)
    lock = acquire_run_lock(tmp_path / "backlog.lock")
    assert lock is not None

    args = task_add_args(config_path)
    args.run = True
    assert cmd_task_add(args) == 1
    assert fake.cycles == 0
    out = capsys.readouterr().out
    assert "already running" in out
    # The task is still created, so the next cycle picks it up.
    assert [task["id"] for task in read_backlog_tasks(tmp_path)] == ["TASK-001"]

    lock.close()


def test_task_edit_sets_and_clears_run_at(git_repo, tmp_path):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001")])

    assert (
        cmd_task_edit(
            task_edit_args(config_path, "TASK-001", run_at="2026-10-01T09:00:00Z")
        )
        == 0
    )
    assert read_backlog_tasks(tmp_path)[0]["run_at"] == "2026-10-01T09:00:00Z"

    assert (
        cmd_task_edit(task_edit_args(config_path, "TASK-001", clear_run_at=True))
        == 0
    )
    assert read_backlog_tasks(tmp_path)[0]["run_at"] is None


def test_task_edit_run_at_now_jumps_queue(git_repo, tmp_path):
    from forgeo.backlog import oldest_open_task
    from forgeo.models import Task

    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001"), make_task(id="TASK-002")])

    assert cmd_task_edit(task_edit_args(config_path, "TASK-002", run_at="now")) == 0
    tasks = [Task.model_validate(entry) for entry in read_backlog_tasks(tmp_path)]
    picked = oldest_open_task(tasks)
    assert picked is not None and picked.id == "TASK-002"


def test_task_edit_refuses_run_at_conflict(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001")])

    assert (
        cmd_task_edit(
            task_edit_args(config_path, "TASK-001", run_at="now", clear_run_at=True)
        )
        == 1
    )
    assert "not both" in capsys.readouterr().out


def test_task_add_run_at_via_main_parser(git_repo, tmp_path):
    config_path = write_config(git_repo, tmp_path)

    assert (
        main(
            [
                "task",
                "add",
                "--title",
                "Scheduled",
                "--description",
                "Later.",
                "--run-at",
                "2026-10-01T09:00:00Z",
                "--config",
                str(config_path),
            ]
        )
        == 0
    )
    assert read_backlog_tasks(tmp_path)[0]["run_at"] == "2026-10-01T09:00:00Z"


def test_task_edit_main_entrypoint(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001")])

    assert (
        main(
            [
                "task",
                "edit",
                "--task",
                "TASK-001",
                "--description",
                "Edited via main.",
                "--config",
                str(config_path),
            ]
        )
        == 0
    )
    assert "Updated task TASK-001" in capsys.readouterr().out


def test_task_show_blocked_hint_mentions_edit(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(
        tmp_path,
        [make_task(id="TASK-003", status="BLOCKED", blocker_reason=["Needs human."])],
    )

    assert cmd_task_show(task_show_args(config_path, "TASK-003")) == 0
    assert "forgeo task edit --task TASK-003" in capsys.readouterr().out


def task_rm_args(config_path: Path, task_id: str) -> argparse.Namespace:
    return argparse.Namespace(config=config_path, task=task_id)


def test_task_rm_removes_task(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(
        tmp_path, [make_task(id="TASK-001"), make_task(id="TASK-002", title="Keep me")]
    )

    assert cmd_task_rm(task_rm_args(config_path, "TASK-001")) == 0
    assert [task["id"] for task in read_backlog_tasks(tmp_path)] == ["TASK-002"]
    out = capsys.readouterr().out
    assert "Removed task TASK-001" in out


def test_task_rm_unknown_task(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)

    assert cmd_task_rm(task_rm_args(config_path, "TASK-999")) == 1
    assert "Unknown task" in capsys.readouterr().out


def test_task_rm_warns_about_dependents(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(
        tmp_path,
        [
            make_task(id="TASK-001"),
            make_task(id="TASK-002", dependencies=["TASK-001"]),
        ],
    )

    assert cmd_task_rm(task_rm_args(config_path, "TASK-001")) == 0
    out = capsys.readouterr().out
    assert "Removed task TASK-001" in out
    assert "TASK-002" in out
    assert "depend" in out


def test_task_rm_main_entrypoint(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001")])

    assert main(["task", "rm", "--task", "TASK-001", "--config", str(config_path)]) == 0
    assert read_backlog_tasks(tmp_path) == []
    assert "Removed task TASK-001" in capsys.readouterr().out


def test_task_rm_missing_config(tmp_path, capsys):
    assert cmd_task_rm(task_rm_args(tmp_path / "missing.yaml", "TASK-001")) == 1
    assert "not found" in capsys.readouterr().out


def task_review_args(config_path: Path, task_id: str) -> argparse.Namespace:
    return argparse.Namespace(config=config_path, task=task_id)


def test_task_complete_review_marks_completed(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(
        tmp_path,
        [
            make_task(
                id="TASK-003",
                status="REVIEW",
                review_branch="forgeo/review/TASK-003",
                review_commit_sha="abc123",
            )
        ],
    )

    assert cmd_task_complete_review(task_review_args(config_path, "TASK-003")) == 0
    task = read_backlog_tasks(tmp_path)[0]
    assert task["status"] == "COMPLETED"
    assert task["review_branch"] is None
    assert "Completed task TASK-003" in capsys.readouterr().out


def test_task_request_changes_reopens(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(
        tmp_path,
        [
            make_task(
                id="TASK-003",
                status="REVIEW",
                review_branch="forgeo/review/TASK-003",
                review_commit_sha="abc123",
            )
        ],
    )

    assert cmd_task_request_changes(task_review_args(config_path, "TASK-003")) == 0
    task = read_backlog_tasks(tmp_path)[0]
    assert task["status"] == "OPEN"
    assert task["review_branch"] is None
    assert "Sent back task TASK-003" in capsys.readouterr().out


def test_task_complete_review_refuses_non_review(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001", status="OPEN")])

    assert cmd_task_complete_review(task_review_args(config_path, "TASK-001")) == 1
    assert "not REVIEW" in capsys.readouterr().out
    assert read_backlog_tasks(tmp_path)[0]["status"] == "OPEN"


def test_task_request_changes_refuses_non_review(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-001", status="BLOCKED")])

    assert cmd_task_request_changes(task_review_args(config_path, "TASK-001")) == 1
    assert "not REVIEW" in capsys.readouterr().out


def test_task_complete_review_unknown_task(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)

    assert cmd_task_complete_review(task_review_args(config_path, "TASK-999")) == 1
    assert "Unknown task" in capsys.readouterr().out


def test_task_request_changes_unknown_task(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)

    assert cmd_task_request_changes(task_review_args(config_path, "TASK-999")) == 1
    assert "Unknown task" in capsys.readouterr().out


def test_task_complete_review_main_entrypoint(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-003", status="REVIEW")])

    assert (
        main(
            [
                "task",
                "complete-review",
                "--task",
                "TASK-003",
                "--config",
                str(config_path),
            ]
        )
        == 0
    )
    assert read_backlog_tasks(tmp_path)[0]["status"] == "COMPLETED"
    assert "Completed task TASK-003" in capsys.readouterr().out


def test_task_request_changes_main_entrypoint(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-003", status="REVIEW")])

    assert (
        main(
            [
                "task",
                "request-changes",
                "--task",
                "TASK-003",
                "--config",
                str(config_path),
            ]
        )
        == 0
    )
    assert read_backlog_tasks(tmp_path)[0]["status"] == "OPEN"


def test_task_show_review_hint_mentions_review_commands(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-003", status="REVIEW")])

    assert cmd_task_show(task_show_args(config_path, "TASK-003")) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "forgeo task complete-review --task TASK-003" in out
    assert "forgeo task request-changes --task TASK-003" in out


def test_expand_task_id_shorthand_forms():
    assert _expand_task_id_shorthand("3") == "TASK-003"
    assert _expand_task_id_shorthand("003") == "TASK-003"
    assert _expand_task_id_shorthand("#3") == "TASK-003"
    assert _expand_task_id_shorthand("TASK-3") == "TASK-003"
    assert _expand_task_id_shorthand("task-3") == "TASK-003"
    assert _expand_task_id_shorthand("TASK-003") == "TASK-003"
    assert _expand_task_id_shorthand("PROJ-42") == "PROJ-42"
    assert _expand_task_id_shorthand("my-task") == "my-task"


def test_task_show_accepts_short_id(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-003", description="Shown via short id.")])

    assert cmd_task_show(task_show_args(config_path, "3")) == 0
    assert "Shown via short id." in capsys.readouterr().out


def test_task_show_accepts_hash_and_prefix_shorthand(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-003", description="Shown via prefix.")])

    assert cmd_task_show(task_show_args(config_path, "#3")) == 0
    assert "Shown via prefix." in capsys.readouterr().out
    assert cmd_task_show(task_show_args(config_path, "TASK-3")) == 0
    assert "Shown via prefix." in capsys.readouterr().out


def test_task_reopen_accepts_short_id(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-003", status="BLOCKED")])

    assert cmd_task_reopen(task_reopen_args(config_path, "3")) == 0
    assert read_backlog_tasks(tmp_path)[0]["status"] == "OPEN"
    assert "Reopened task TASK-003" in capsys.readouterr().out


def test_task_rm_accepts_short_id(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-003"), make_task(id="TASK-004")])

    assert cmd_task_rm(task_rm_args(config_path, "3")) == 0
    assert [task["id"] for task in read_backlog_tasks(tmp_path)] == ["TASK-004"]
    assert "Removed task TASK-003" in capsys.readouterr().out


def test_task_edit_accepts_short_id(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-003", status="BLOCKED")])

    assert cmd_task_edit(task_edit_args(config_path, "3", title="New title")) == 0
    assert read_backlog_tasks(tmp_path)[0]["title"] == "New title"
    assert "Updated task TASK-003" in capsys.readouterr().out


def test_task_complete_review_accepts_short_id(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task(id="TASK-003", status="REVIEW")])

    assert cmd_task_complete_review(task_review_args(config_path, "3")) == 0
    assert read_backlog_tasks(tmp_path)[0]["status"] == "COMPLETED"


def test_exact_id_wins_over_shorthand(git_repo, tmp_path, capsys):
    config_path = write_config(git_repo, tmp_path)
    write_backlog(
        tmp_path,
        [make_task(id="3", title="Native three"), make_task(id="TASK-003")],
    )

    assert cmd_task_show(task_show_args(config_path, "3")) == 0
    assert "Native three" in capsys.readouterr().out


async def test_run_accepts_short_id(git_repo, tmp_path):
    config_path = write_config(git_repo, tmp_path)
    config = load_config(config_path)
    backlog = open_backlog(config)
    created = await backlog.create_task(make_task(id="TASK-003", title="Short run"))
    assert created.id == "TASK-003"

    actual_id, task = await _resolve_backlog_task_id(backlog, "3")
    assert actual_id == "TASK-003"
    assert task is not None and task.id == "TASK-003"


def test_resolved_config_path_uses_cwd_file(tmp_path, monkeypatch):
    from forgeo.cli import _resolved_config_path

    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "forgeo.yaml").write_text("x", encoding="utf-8")

    resolved = _resolved_config_path(argparse.Namespace(config=DEFAULT_CONFIG))
    assert resolved == Path("forgeo.yaml")


def test_resolved_config_path_discovers_parent(tmp_path, monkeypatch):
    from forgeo.cli import _resolved_config_path

    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    root = tmp_path / "proj"
    sub = root / "docs" / "sub"
    sub.mkdir(parents=True)
    (root / "forgeo.yaml").write_text("x", encoding="utf-8")
    monkeypatch.chdir(sub)

    resolved = _resolved_config_path(argparse.Namespace(config=DEFAULT_CONFIG))
    assert resolved == root / "forgeo.yaml"


def test_resolved_config_path_explicit_missing_is_kept(tmp_path, monkeypatch):
    from forgeo.cli import _resolved_config_path

    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    monkeypatch.chdir(tmp_path)

    missing = tmp_path / "custom.yaml"
    resolved = _resolved_config_path(argparse.Namespace(config=missing))
    assert resolved == missing


def test_resolved_config_path_single_instance_fallback(tmp_path, monkeypatch, git_repo):
    from forgeo.cli import _resolved_config_path
    from forgeo.instances import add_instance

    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    config_path = write_config_in(cfg_dir, git_repo, tmp_path)
    add_instance("only", config_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)

    resolved = _resolved_config_path(argparse.Namespace(config=DEFAULT_CONFIG))
    assert resolved == config_path.resolve()


def test_resolved_config_path_no_fallback_with_two_instances(
    tmp_path, monkeypatch, git_repo
):
    from forgeo.cli import _resolved_config_path
    from forgeo.instances import add_instance

    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    cfg1 = tmp_path / "cfg1"
    cfg1.mkdir()
    cfg2 = tmp_path / "cfg2"
    cfg2.mkdir()
    add_instance("one", write_config_in(cfg1, git_repo, tmp_path))
    add_instance(
        "two", write_config_in(cfg2, git_repo, tmp_path, name="other-forgeo")
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)

    resolved = _resolved_config_path(argparse.Namespace(config=DEFAULT_CONFIG))
    assert resolved == DEFAULT_CONFIG


def test_cmd_status_works_from_subdirectory(git_repo, tmp_path, monkeypatch, capsys):
    write_config(git_repo, tmp_path)
    write_backlog(tmp_path, [make_task()])
    sub = tmp_path / "src" / "pkg"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))

    assert cmd_status(argparse.Namespace(config=DEFAULT_CONFIG)) == 0
    assert "test-forgeo" in capsys.readouterr().out
