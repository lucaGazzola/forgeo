"""CLI tests for the ``forgeo once``/``status``/``stop``/``restart`` commands."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forgeo.cli import (
    DEFAULT_CONFIG,
    backlog_status_counts,
    build_parser,
    cmd_instance,
    cmd_instance_add,
    cmd_instance_list,
    cmd_instance_rm,
    cmd_once,
    cmd_restart,
    cmd_start,
    cmd_status,
    cmd_stop,
    cmd_validate,
    last_outcome_from_runs,
    main,
    render_status,
)
from forgeo.daemon import acquire_run_lock, is_lock_held, read_lock_pid
from forgeo.instances import list_instances, load_registry
from forgeo.models import RunKind, RunOutcome, RunRecord, TaskStatus
from forgeo.paths import lock_path, runs_path
from forgeo.runs import RunRecorder
from tests.conftest import FakeForgeo, git, make_config, make_task


def write_config(git_repo: Path, tmp_path: Path, **overrides) -> Path:
    """A config file wired to the fixture repo; returns its path."""
    config = make_config(git_repo, tmp_path, **overrides)
    path = tmp_path / "forgeo.yaml"
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


def once_args(config_path: Path) -> argparse.Namespace:
    return argparse.Namespace(config=config_path)


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


def wait_for(predicate: Callable[[], bool], timeout: float = 15.0) -> bool:
    """Poll ``predicate`` until it holds; False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


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
    assert "once" in capsys.readouterr().out


def test_parser_help_lists_status(capsys):
    build_parser().print_help()
    assert "status" in capsys.readouterr().out


def test_backlog_status_counts_empty():
    assert backlog_status_counts([]) == {
        "OPEN": 0,
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
# forgeo validate: read-only dry run                                          #
# --------------------------------------------------------------------------- #


def write_validate_config(path: Path, **overrides) -> None:
    """Write a minimal forgeo.yaml for validate tests (repo + agent command)."""
    fields = {"agent_command": "echo hi"}
    fields.update(overrides)
    body = "".join(f"{key}: {value}\n" for key, value in fields.items())
    path.write_text(body, encoding="utf-8")


def test_parser_help_lists_validate(capsys):
    build_parser().print_help()
    assert "validate" in capsys.readouterr().out


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


def test_validate_unknown_name_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    assert cmd_validate(argparse.Namespace(config=DEFAULT_CONFIG, name="nope")) == 1
    assert "Unknown instance" in capsys.readouterr().out


def test_parser_help_lists_stop_and_restart(capsys):
    build_parser().print_help()
    out = capsys.readouterr().out
    assert "stop" in out
    assert "restart" in out


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
    "command", ["start", "once", "status", "validate", "stop", "restart"]
)
def test_parser_accepts_name_for_commands(command):
    args = build_parser().parse_args([command, "--name", "my-repo"])
    assert getattr(args, "name", None) == "my-repo"


@pytest.mark.parametrize(
    "command", ["start", "once", "status", "validate", "stop", "restart"]
)
def test_parser_rejects_name_with_config(command):
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args([command, "--name", "x", "--config", "forgeo.yaml"])
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


def test_status_unknown_name_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    assert cmd_status(argparse.Namespace(config=DEFAULT_CONFIG, name="nope")) == 1
    assert "Unknown instance" in capsys.readouterr().out


def test_once_resolves_name_from_registry(tmp_path, git_repo, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    assert cmd_instance_add(argparse.Namespace(name="my-repo", config=config_path)) == 0
    fake = FakeForgeo()
    monkeypatch.setattr("forgeo.cli._make_forgeo", lambda config: fake)

    assert cmd_once(argparse.Namespace(config=DEFAULT_CONFIG, name="my-repo")) == 0
    assert fake.cycles == 1
    assert "Cycle finished: task" in capsys.readouterr().out


def test_once_unknown_name_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    assert cmd_once(argparse.Namespace(config=DEFAULT_CONFIG, name="nope")) == 1
    assert "Unknown instance" in capsys.readouterr().out


def test_stop_resolves_name_from_registry(tmp_path, git_repo, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(git_repo, tmp_path)
    assert cmd_instance_add(argparse.Namespace(name="my-repo", config=config_path)) == 0

    assert cmd_stop(argparse.Namespace(config=DEFAULT_CONFIG, name="my-repo")) == 1
    assert "not running" in capsys.readouterr().out


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


def test_two_instances_stay_fully_independent(
    git_repo, tmp_path, monkeypatch, capsys
):
    """Two registered instances with configs in different directories keep every
    lock file, log, backlog, and runs.jsonl fully independent, and concurrent
    status/once calls never interfere."""
    registry = tmp_path / "registry.yaml"
    monkeypatch.setenv("FORGEO_REGISTRY", str(registry))

    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    git(repo_b, "init", "-b", "main")
    git(repo_b, "config", "user.email", "forgeo@test.local")
    git(repo_b, "config", "user.name", "Forgeo Test")
    (repo_b / "app.py").write_text("def answer():\n    return 0\n", encoding="utf-8")
    git(repo_b, "add", "-A")
    git(repo_b, "commit", "-m", "initial")

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
