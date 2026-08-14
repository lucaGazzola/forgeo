"""Daemon tests: scheduled cycles, stop handling, run lock, config reload."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

from forgeo.config import load_config
from forgeo.daemon import (
    MIN_WAKE_SLEEP_SECONDS,
    ForgeoDaemon,
    RunLock,
    acquire_run_lock,
    is_lock_held,
    read_lock_pid,
)
from forgeo.models import ExecutionResult, ExecutionStatus, TaskStatus
from tests.conftest import FakeForgeo, make_config, make_forgeo, make_task


def make_daemon(git_repo, tmp_path, interval=1, **overrides) -> ForgeoDaemon:
    config = make_config(git_repo, tmp_path, interval_minutes=interval, **overrides)
    return ForgeoDaemon(config, FakeForgeo())


async def test_daemon_runs_cycles_on_interval(git_repo, tmp_path):
    daemon = make_daemon(git_repo, tmp_path)
    daemon.interval_seconds = 0.01
    task = asyncio.create_task(daemon.run_forever())

    while daemon.forgeo.cycles == 0:
        await asyncio.sleep(0.01)
    daemon.stop()
    await asyncio.wait_for(task, timeout=5)

    assert daemon.forgeo.cycles >= 1


async def test_daemon_interval_seconds(git_repo, tmp_path):
    daemon = make_daemon(git_repo, tmp_path, interval=5)
    assert daemon.interval_seconds == 300.0


async def test_daemon_survives_crashed_cycle(git_repo, tmp_path, caplog):
    daemon = make_daemon(git_repo, tmp_path)
    daemon.interval_seconds = 0.01
    daemon.forgeo.crash = True
    daemon.forgeo.cycles = 0
    with caplog.at_level(logging.ERROR, logger="forgeo"):
        task = asyncio.create_task(daemon.run_forever())
        await asyncio.sleep(0.1)
        daemon.stop()
        await asyncio.wait_for(task, timeout=5)
    assert "boom" in caplog.text


def test_run_lock_is_exclusive(tmp_path):
    lock_path = tmp_path / "forgeo.lock"
    first = acquire_run_lock(lock_path)
    assert first is not None
    assert acquire_run_lock(lock_path) is None
    first.close()
    assert acquire_run_lock(lock_path) is not None


def test_is_lock_held_detects_holder_and_stale_file(tmp_path):
    lock_path = tmp_path / "forgeo.lock"
    assert is_lock_held(lock_path) is False
    held = acquire_run_lock(lock_path)
    assert held is not None
    assert is_lock_held(lock_path) is True
    held.close()
    assert is_lock_held(lock_path) is False


def test_run_lock_held_while_active(tmp_path):
    lock_path = tmp_path / "forgeo.run"
    first = RunLock(lock_path)
    second = RunLock(lock_path)
    with first.held() as acquired:
        assert acquired is True
        with second.held() as blocked:
            assert blocked is False
    with second.held() as again:
        assert again is True


def test_read_lock_pid(tmp_path):
    lock_path = tmp_path / "forgeo.lock"
    assert read_lock_pid(lock_path) is None
    held = acquire_run_lock(lock_path)
    try:
        assert read_lock_pid(lock_path) == os.getpid()
    finally:
        held.close()


def test_read_lock_pid_garbage(tmp_path):
    lock_path = tmp_path / "forgeo.lock"
    lock_path.write_text("garbage\npid=notanumber\n", encoding="utf-8")
    assert read_lock_pid(lock_path) is None


def test_failed_acquire_keeps_holders_pid(tmp_path):
    """A second, failing acquire must not wipe the running holder's PID."""
    lock_path = tmp_path / "forgeo.lock"
    held = acquire_run_lock(lock_path)
    try:
        assert acquire_run_lock(lock_path) is None
        assert read_lock_pid(lock_path) == os.getpid()
    finally:
        held.close()


async def test_daemon_skips_when_previous_run_active(git_repo, tmp_path):
    daemon = make_daemon(git_repo, tmp_path)
    daemon.interval_seconds = 0.01
    lock = RunLock(daemon.run_lock.lock_path)
    with lock.held():
        task = asyncio.create_task(daemon.run_forever())
        await asyncio.sleep(0.1)
        daemon.stop()
        await asyncio.wait_for(task, timeout=5)
    assert daemon.forgeo.cycles == 0


async def test_daemon_runs_after_run_lock_released(git_repo, tmp_path):
    daemon = make_daemon(git_repo, tmp_path)
    daemon.interval_seconds = 0.01
    task = asyncio.create_task(daemon.run_forever())
    while daemon.forgeo.cycles == 0:
        await asyncio.sleep(0.01)
    daemon.stop()
    await asyncio.wait_for(task, timeout=5)
    assert daemon.forgeo.cycles >= 1


async def test_compute_next_run_at_shortened_by_future_run_at(git_repo, tmp_path):
    forgeo, _agent, backlog = make_forgeo(git_repo, tmp_path)
    run_at = datetime.now(UTC) + timedelta(seconds=30)
    await backlog.create_task(make_task(run_at=run_at))
    daemon = ForgeoDaemon(forgeo.config, forgeo)
    daemon.interval_seconds = 3600
    target = await daemon._compute_next_run_at()
    assert abs((target - run_at).total_seconds()) < 1


async def test_compute_next_run_at_wakes_immediately_for_past_run_at(git_repo, tmp_path):
    forgeo, _agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task(run_at=datetime.now(UTC) - timedelta(minutes=5)))
    daemon = ForgeoDaemon(forgeo.config, forgeo)
    daemon.interval_seconds = 3600
    target = await daemon._compute_next_run_at()
    delay = (target - datetime.now(UTC)).total_seconds()
    assert 0 <= delay <= MIN_WAKE_SLEEP_SECONDS + 0.5


async def test_compute_next_run_at_ignores_run_at_beyond_interval(git_repo, tmp_path):
    forgeo, _agent, backlog = make_forgeo(git_repo, tmp_path)
    run_at = datetime.now(UTC) + timedelta(hours=3)
    await backlog.create_task(make_task(run_at=run_at))
    daemon = ForgeoDaemon(forgeo.config, forgeo)
    daemon.interval_seconds = 60
    target = await daemon._compute_next_run_at()
    assert abs((target - (datetime.now(UTC) + timedelta(seconds=60))).total_seconds()) < 1


async def test_compute_next_run_at_falls_back_to_interval_without_run_at(git_repo, tmp_path):
    forgeo, _agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    daemon = ForgeoDaemon(forgeo.config, forgeo)
    daemon.interval_seconds = 3600
    target = await daemon._compute_next_run_at()
    assert abs((target - (datetime.now(UTC) + timedelta(seconds=3600))).total_seconds()) < 1


async def test_daemon_picks_due_run_at_task_promptly(git_repo, tmp_path):
    """A past run_at is fired on the very next cycle: the task leaves OPEN
    without waiting for the (long) interval."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS, no_changes=True)
    await backlog.create_task(make_task(id="DUE", run_at=datetime.now(UTC) - timedelta(minutes=5)))
    daemon = ForgeoDaemon(forgeo.config, forgeo)
    daemon.interval_seconds = 3600
    task = asyncio.create_task(daemon.run_forever())
    for _ in range(300):
        current = await backlog.get_task("DUE")
        if current is not None and current.status is not TaskStatus.OPEN:
            break
        await asyncio.sleep(0.02)
    daemon.stop()
    await asyncio.wait_for(task, timeout=5)
    current = await backlog.get_task("DUE")
    assert current.status is not TaskStatus.OPEN


async def test_daemon_next_run_at_reflects_future_run_at(git_repo, tmp_path):
    """After a cycle the daemon's next_run_at is shortened to the scheduled
    run_at instead of the full interval."""
    forgeo, agent, backlog = make_forgeo(git_repo, tmp_path)
    agent.result = ExecutionResult(status=ExecutionStatus.SUCCESS, no_changes=True)
    run_at = datetime.now(UTC) + timedelta(seconds=10)
    await backlog.create_task(make_task(id="SOON", run_at=run_at))
    daemon = ForgeoDaemon(forgeo.config, forgeo)
    daemon.interval_seconds = 3600
    task = asyncio.create_task(daemon.run_forever())
    while daemon.next_run_at is None:
        await asyncio.sleep(0.02)
    daemon.stop()
    await asyncio.wait_for(task, timeout=5)
    delay = (daemon.next_run_at - datetime.now(UTC)).total_seconds()
    assert 0 <= delay <= 12


def test_state_file_written_on_start(git_repo, tmp_path):
    import json

    daemon = make_daemon(git_repo, tmp_path)
    assert not daemon.state_file.exists()
    daemon.write_state()
    payload = json.loads(daemon.state_file.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["started_at"]
    assert payload["last_outcome"] is None
    assert payload["next_run_at"] is None


async def test_state_file_tracks_runs(git_repo, tmp_path):
    import json

    daemon = make_daemon(git_repo, tmp_path)
    daemon.interval_seconds = 0.01
    task = asyncio.create_task(daemon.run_forever())
    while daemon.forgeo.cycles == 0:
        await asyncio.sleep(0.01)
    daemon.stop()
    await asyncio.wait_for(task, timeout=5)

    payload = json.loads(daemon.state_file.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["last_outcome"] == "task"
    assert payload["next_run_at"]


def test_state_file_path_next_to_backlog(git_repo, tmp_path):
    daemon = make_daemon(git_repo, tmp_path)
    assert daemon.state_file == tmp_path / "backlog.state.json"


async def test_daemon_snapshots_backlog_on_startup(git_repo, tmp_path):
    import json

    forgeo, _agent, backlog = make_forgeo(git_repo, tmp_path)
    await backlog.create_task(make_task())
    daemon = ForgeoDaemon(forgeo.config, forgeo)
    daemon.interval_seconds = 0.01
    task = asyncio.create_task(daemon.run_forever())
    while daemon.last_outcome is None:
        await asyncio.sleep(0.01)
    daemon.stop()
    await asyncio.wait_for(task, timeout=5)

    bak = tmp_path / "backlog.json.bak"
    assert bak.is_file()
    store = json.loads(bak.read_text(encoding="utf-8"))
    assert [entry["id"] for entry in store["tasks"]] == ["TASK-001"]


def _write_config(path, repo, interval_minutes=60, backlog="backlog.json") -> None:
    """Write a minimal forgeo.yaml for the reload tests."""
    path.write_text(
        f"name: reload\nrepo: {repo}\ninterval_minutes: {interval_minutes}\n"
        f"backlog: {backlog}\nblocker_file: BLOCKER.md\n"
        "agent_command: echo hi\n",
        encoding="utf-8",
    )


class ReloadRecorder:
    """A config-carrying stand-in for :class:`Forgeo` used by the reload tests."""

    def __init__(self, config) -> None:
        self.config = config
        self.cycles = 0

    async def run_cycle(self) -> str:
        self.cycles += 1
        return "task"


def _reload_daemon(tmp_path, git_repo):
    config_path = tmp_path / "forgeo.yaml"
    _write_config(config_path, git_repo, interval_minutes=60)
    config = load_config(config_path)
    daemon = ForgeoDaemon(
        config,
        ReloadRecorder(config),
        config_path=config_path,
        forgeo_factory=lambda c: ReloadRecorder(c),
    )
    return config_path, config, daemon


async def test_daemon_reloads_config_on_change(git_repo, tmp_path, caplog):
    config_path, config, daemon = _reload_daemon(tmp_path, git_repo)
    assert config.interval_minutes == 60
    daemon.interval_seconds = 0.02
    with caplog.at_level(logging.INFO, logger="forgeo"):
        task = asyncio.create_task(daemon.run_forever())
        while daemon.forgeo.cycles == 0:
            await asyncio.sleep(0.01)
        _write_config(config_path, git_repo, interval_minutes=30)
        for _ in range(200):
            if daemon.config.interval_minutes == 30:
                break
            await asyncio.sleep(0.01)
        daemon.stop()
        await asyncio.wait_for(task, timeout=5)

    assert daemon.config.interval_minutes == 30
    assert daemon.forgeo.config.interval_minutes == 30
    assert daemon.interval_seconds == 30 * 60
    assert "Config reloaded" in caplog.text
    assert "next cycle uses the new settings" in caplog.text


async def test_daemon_keeps_last_valid_config_on_invalid_change(git_repo, tmp_path, caplog):
    config_path, config, daemon = _reload_daemon(tmp_path, git_repo)
    assert config.interval_minutes == 60
    daemon.interval_seconds = 0.02
    with caplog.at_level(logging.WARNING, logger="forgeo"):
        task = asyncio.create_task(daemon.run_forever())
        while daemon.forgeo.cycles == 0:
            await asyncio.sleep(0.01)
        config_path.write_text("not: [valid", encoding="utf-8")
        await asyncio.sleep(0.2)
        daemon.stop()
        await asyncio.wait_for(task, timeout=5)

    assert daemon.config.interval_minutes == 60
    assert daemon.forgeo.config.interval_minutes == 60
    assert "Config change rejected" in caplog.text
    assert "keeping the previous config" in caplog.text


async def test_daemon_does_not_reload_unchanged_config(git_repo, tmp_path, caplog):
    _, _, daemon = _reload_daemon(tmp_path, git_repo)
    daemon.interval_seconds = 0.02
    with caplog.at_level(logging.INFO, logger="forgeo"):
        task = asyncio.create_task(daemon.run_forever())
        while daemon.forgeo.cycles < 2:
            await asyncio.sleep(0.01)
        daemon.stop()
        await asyncio.wait_for(task, timeout=5)

    assert daemon.config.interval_minutes == 60
    assert "Config reloaded" not in caplog.text


async def test_daemon_pins_paths_on_config_change(git_repo, tmp_path, caplog):
    config_path, config, daemon = _reload_daemon(tmp_path, git_repo)
    original_backlog = config.backlog
    daemon.interval_seconds = 0.02
    with caplog.at_level(logging.WARNING, logger="forgeo"):
        task = asyncio.create_task(daemon.run_forever())
        while daemon.forgeo.cycles == 0:
            await asyncio.sleep(0.01)
        _write_config(config_path, git_repo, interval_minutes=30, backlog="moved.json")
        for _ in range(200):
            if daemon.config.interval_minutes == 30:
                break
            await asyncio.sleep(0.01)
        daemon.stop()
        await asyncio.wait_for(task, timeout=5)

    assert daemon.config.interval_minutes == 30
    assert daemon.config.backlog == original_backlog
    assert daemon.forgeo.config.backlog == original_backlog
    assert daemon.run_lock.lock_path == original_backlog.with_suffix(".run")
    assert daemon.state_file == original_backlog.with_suffix(".state.json")
    assert "cannot relocate path" in caplog.text
