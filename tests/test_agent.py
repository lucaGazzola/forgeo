"""Shell agent tests: exit-code mapping, env delivery, timeout."""

from __future__ import annotations

import os
import sys
import types
from collections import deque

import pytest

from forgeo.agent import (
    DockerSandboxAgent,
    SandboxUnavailableError,
    ShellAgent,
    _kill_process_group,
)
from forgeo.models import ExecutionStatus, RepoContext
from tests.conftest import make_task

TASK = make_task(id="TASK-001", title="Add retries", description="Implement retry logic.")


async def test_exit_zero_is_success():
    agent = ShellAgent("true")
    result = await agent.run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.SUCCESS


async def test_blocked_exit_code_is_blocked():
    agent = ShellAgent("echo need input; exit 2")
    result = await agent.run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.BLOCKED
    assert any("need input" in line for line in result.questions)


async def test_other_exit_code_is_error():
    agent = ShellAgent("exit 1")
    result = await agent.run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.ERROR
    assert "exit code 1" in (result.error or "")


async def test_no_changes_exit_code_is_success_with_no_changes():
    agent = ShellAgent("exit 3")
    result = await agent.run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.SUCCESS
    assert result.no_changes is True
    assert result.exit_code == 3


async def test_no_changes_exit_code_is_configurable():
    agent = ShellAgent("exit 5", no_changes_exit_code=5)
    result = await agent.run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.SUCCESS
    assert result.no_changes is True


async def test_missing_command_is_error():
    agent = ShellAgent("/nonexistent/binary --flag")
    result = await agent.run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.ERROR


async def test_argv_missing_binary_is_error():
    """An argv-list command whose binary does not exist surfaces a clear error."""
    agent = ShellAgent(["/nonexistent/binary"])
    result = await agent.run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.ERROR
    assert "command not found" in (result.error or "")


async def test_timeout_is_error():
    agent = ShellAgent("sleep 5", timeout_seconds=0.2)
    result = await agent.run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.ERROR
    assert "timed out" in (result.error or "")


async def test_timeout_includes_streamed_output():
    """Lines printed before a timeout must appear in output_logs."""
    agent = ShellAgent(
        "echo pre-timeout-marker; sleep 5",
        timeout_seconds=0.5,
    )
    result = await agent.run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.ERROR
    assert "timed out" in (result.error or "")
    assert any("pre-timeout-marker" in line for line in result.output_logs)


async def test_timeout_kills_whole_process_group(tmp_path):
    """Timeout must reap the entire process tree, not just the shell.

    Regression: ``proc.kill()`` killed only the direct child, orphaning
    grandchildren (the real agent) that kept the output pipes open and hung
    the forgeo forever.
    """
    marker = tmp_path / "sleep.pid"
    agent = ShellAgent(
        f"sleep 60 & echo $! > {marker}; wait",
        timeout_seconds=0.3,
    )
    result = await agent.run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.ERROR
    assert "timed out" in (result.error or "")
    sleep_pid = int(marker.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(sleep_pid, 0)


async def test_timeout_does_not_hang_when_grandchild_escapes_group():
    """A descendant that leaves the process group must not hang the forgeo.

    The drain is bounded, so even when an escaped grandchild (daemonized
    agent, docker container) keeps the output pipes open, the run returns.
    """
    agent = ShellAgent(
        f'"{sys.executable}" -c "import os, time; os.setsid(); time.sleep(60)" & wait',
        timeout_seconds=0.2,
        drain_timeout_seconds=1.0,
    )
    result = await agent.run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.ERROR
    assert "timed out" in (result.error or "")


async def test_output_log_window_is_bounded():
    """A chatty agent must not retain more than the last 1000 process lines."""
    # 1500 numbered lines; only the trailing window should remain.
    agent = ShellAgent("python3 -c \"import sys; [print(i) for i in range(1500)]\"")
    result = await agent.run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.SUCCESS
    stream = [line for line in result.output_logs if line.startswith(("[stdout]", "[stderr]"))]
    assert len(stream) == 1000
    assert stream[0] == "[stdout] 500"
    assert stream[-1] == "[stdout] 1499"


async def test_no_timeout_runs_to_completion():
    agent = ShellAgent("sleep 0.2", timeout_seconds=None)
    result = await agent.run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.SUCCESS


async def test_argv_list_command(tmp_path):
    agent = ShellAgent(["sh", "-c", "exit 2"])
    result = await agent.run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.BLOCKED


async def test_task_instruction_via_env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "out.txt").write_text("", encoding="utf-8")
    agent = ShellAgent('echo "$FORGEO_TASK" > out.txt')
    await agent.run_task(TASK, RepoContext(repo_path=repo, branch="main"))
    output = (repo / "out.txt").read_text(encoding="utf-8")
    assert "Add retries" in output
    assert "Implement retry logic." in output


async def test_instruction_override_replaces_task_instruction(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "out.txt").write_text("", encoding="utf-8")
    agent = ShellAgent('echo "$FORGEO_TASK" > out.txt')
    await agent.run_task(
        TASK,
        RepoContext(repo_path=repo, branch="main"),
        instruction="# Project context\n\nSome overview.\n\n# Task\n\nAdd retries",
    )
    output = (repo / "out.txt").read_text(encoding="utf-8")
    assert "Project context" in output
    assert "Add retries" in output
    assert "Implement retry logic." not in output


async def test_per_task_command_override(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "marker.txt").write_text("", encoding="utf-8")
    agent = ShellAgent("exit 1")
    result = await agent.run_task(
        TASK,
        RepoContext(repo_path=repo, branch="main"),
        command='echo "$FORGEO_TASK" > marker.txt',
    )
    assert result.status is ExecutionStatus.SUCCESS
    output = (repo / "marker.txt").read_text(encoding="utf-8")
    assert "Add retries" in output


async def test_falls_back_to_configured_command():
    agent = ShellAgent("exit 1")
    result = await agent.run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.ERROR


async def test_per_task_timeout_override():
    agent = ShellAgent("sleep 5")
    result = await agent.run_task(TASK, RepoContext(), timeout_seconds=0.2)
    assert result.status is ExecutionStatus.ERROR
    assert "timed out after 0.2s" in (result.error or "")


async def test_per_task_timeout_null_uses_configured_timeout():
    agent = ShellAgent("sleep 0.2", timeout_seconds=5)
    result = await agent.run_task(TASK, RepoContext(), timeout_seconds=None)
    assert result.status is ExecutionStatus.SUCCESS


async def test_per_task_argv_list_override():
    agent = ShellAgent("exit 1")
    result = await agent.run_task(
        TASK,
        RepoContext(),
        command=["sh", "-c", "exit 2"],
    )
    assert result.status is ExecutionStatus.BLOCKED


class _FakeStream:
    """Stand-in for asyncio.StreamReader that serves a fixed chunk."""

    def __init__(self, data: bytes) -> None:
        self._lines = iter(data.splitlines(keepends=True))

    async def readline(self) -> bytes:
        try:
            return next(self._lines)
        except StopIteration:
            return b""


class _FakeProcess:
    """Stand-in for an asyncio subprocess: fixed exit code, no real I/O."""

    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        pass


def fake_docker_exec(captured: dict, returncode: int = 0) -> object:
    """A ``create_subprocess_exec`` fake recording the docker argv."""

    async def _run(*args, **kwargs):
        captured["args"] = list(args)
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return _FakeProcess(returncode=returncode, stdout=b"docker output\n")

    return _run


def docker_agent(**overrides) -> DockerSandboxAgent:
    defaults = {"command": "opencode run", "image": "forgeo-agent:latest"}
    defaults.update(overrides)
    return DockerSandboxAgent(**defaults)


async def test_docker_missing_binary_raises(monkeypatch):
    monkeypatch.setattr("forgeo.agent.shutil.which", lambda _cmd: None)
    with pytest.raises(SandboxUnavailableError):
        docker_agent()


def test_docker_rejects_blank_image():
    with pytest.raises(ValueError):
        DockerSandboxAgent("true", image="  ")


async def test_docker_runs_in_container_with_repo_mounted(monkeypatch, tmp_path):
    captured: dict = {}
    monkeypatch.setattr("forgeo.agent.asyncio.create_subprocess_exec", fake_docker_exec(captured))
    repo = tmp_path / "repo"
    repo.mkdir()
    agent = docker_agent()
    result = await agent.run_task(TASK, RepoContext(repo_path=repo, branch="main"))

    assert result.status is ExecutionStatus.SUCCESS
    args = captured["args"]
    assert args[0] == "docker"
    assert "--rm" in args
    assert "-w" in args
    assert args[args.index("-w") + 1] == str(repo)
    assert "-v" in args
    assert f"{repo}:{repo}" in args
    assert "--network" in args
    assert args[args.index("--network") + 1] == "none"
    assert "forgeo-agent:latest" in args
    assert args[args.index("-c") + 1] == "opencode run"
    assert captured["cwd"] == str(repo)


async def test_docker_forwards_task_env(monkeypatch, tmp_path):
    captured: dict = {}
    monkeypatch.setattr("forgeo.agent.asyncio.create_subprocess_exec", fake_docker_exec(captured))
    repo = tmp_path / "repo"
    repo.mkdir()
    await docker_agent().run_task(TASK, RepoContext(repo_path=repo, branch="feature"))

    args = captured["args"]
    assert "-e" in args
    task_env = next(a for a in args if a.startswith("FORGEO_TASK="))
    assert "Add retries" in task_env
    assert "Implement retry logic." in task_env
    assert next(a for a in args if a.startswith("FORGEO_REPO=")) == f"FORGEO_REPO={repo}"
    assert "FORGEO_BRANCH=feature" in args


async def test_docker_forwards_agent_env(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("forgeo.agent.asyncio.create_subprocess_exec", fake_docker_exec(captured))
    agent = docker_agent(env={"MY_TOKEN": "secret", "MODEL": "claude"})
    await agent.run_task(TASK, RepoContext())

    args = captured["args"]
    assert "MY_TOKEN=secret" in args
    assert "MODEL=claude" in args


async def test_docker_network_configurable(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("forgeo.agent.asyncio.create_subprocess_exec", fake_docker_exec(captured))
    agent = docker_agent(network="bridge")
    await agent.run_task(TASK, RepoContext())

    args = captured["args"]
    assert args[args.index("--network") + 1] == "bridge"


async def test_docker_mounts_are_read_only(monkeypatch, tmp_path):
    captured: dict = {}
    monkeypatch.setattr("forgeo.agent.asyncio.create_subprocess_exec", fake_docker_exec(captured))
    creds = tmp_path / "creds"
    creds.mkdir()
    agent = docker_agent(mounts=[str(creds)])
    await agent.run_task(TASK, RepoContext())

    args = captured["args"]
    assert f"{creds}:{creds}:ro" in args


async def test_docker_preserves_blocked_exit_code(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "forgeo.agent.asyncio.create_subprocess_exec",
        fake_docker_exec(captured, returncode=2),
    )
    result = await docker_agent().run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.BLOCKED
    assert result.exit_code == 2


async def test_docker_preserves_other_exit_code_as_error(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "forgeo.agent.asyncio.create_subprocess_exec",
        fake_docker_exec(captured, returncode=1),
    )
    result = await docker_agent().run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.ERROR
    assert "exit code 1" in (result.error or "")


async def test_docker_no_changes_exit_code(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "forgeo.agent.asyncio.create_subprocess_exec",
        fake_docker_exec(captured, returncode=3),
    )
    result = await docker_agent().run_task(TASK, RepoContext())
    assert result.status is ExecutionStatus.SUCCESS
    assert result.no_changes is True
    assert result.exit_code == 3


async def test_docker_accepts_argv_list_command(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("forgeo.agent.asyncio.create_subprocess_exec", fake_docker_exec(captured))
    agent = docker_agent(command=["opencode", "run"])
    await agent.run_task(TASK, RepoContext())

    args = captured["args"]
    assert args[-2:] == ["opencode", "run"]
    assert "sh" not in args


def test_kill_process_group_falls_back_when_group_gone(monkeypatch):
    """When the process group is already gone, the direct child still dies."""
    proc = types.SimpleNamespace(pid=123)

    def fake_killpg(pid, sig):
        raise ProcessLookupError

    proc.kill = lambda: setattr(proc, "killed", True)
    monkeypatch.setattr("forgeo.agent.os.killpg", fake_killpg)

    _kill_process_group(proc)

    assert getattr(proc, "killed", False) is True


async def test_drain_stream_none_is_noop():
    """A missing stream (no stderr) is skipped without error."""
    lines = deque()
    await ShellAgent._drain_stream(None, "stderr", lines)
    assert lines == deque()


class _HangingStream:
    """A stream that never reaches EOF (as if a daemonized grandchild held it)."""

    async def readline(self) -> bytes:
        import asyncio

        await asyncio.sleep(3600)
        return b""


class _HangingProcess(_FakeProcess):
    """An exit-0 process whose output streams never finish."""

    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = _HangingStream()
        self.stderr = _HangingStream()


async def test_drain_timeout_proceeds_without_hanging():
    """Streams that never reach EOF must not hang the run; the run succeeds."""
    agent = ShellAgent("true", drain_timeout_seconds=0.2)

    async def fake_spawn(*args, **kwargs):
        return _HangingProcess()

    agent._spawn = fake_spawn  # type: ignore[method-assign]

    result = await agent.run_task(TASK, RepoContext())

    assert result.status is ExecutionStatus.SUCCESS
    assert any("Output streams stayed open" in line for line in result.output_logs)


def test_kill_process_group_sends_sigkill(monkeypatch):
    """The whole process group receives SIGKILL, not just the child."""
    proc = types.SimpleNamespace(pid=456)
    seen: list = []
    monkeypatch.setattr(
        "forgeo.agent.os.killpg",
        lambda pid, sig: seen.append((pid, sig)),
    )

    _kill_process_group(proc)

    assert seen == [(456, 9)]
