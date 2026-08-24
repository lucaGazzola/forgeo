"""The coding agent: any shell command that implements the task.

The configured command is run with the repository as its working directory.
The task is delivered to the agent as the ``FORGEO_TASK`` environment
variable (title, description, acceptance criteria) so any CLI coding tool
(aider, claude, a custom script, ...) can consume it. The exit code decides
the outcome:

* ``0`` — SUCCESS, the work is committed and pushed,
* ``blocked_exit_code`` (default ``2``) — BLOCKED, the agent needs human
  input; its output ends up in the blocker file,
* ``no_changes_exit_code`` (default ``3``) — SUCCESS with no changes: the
  agent explicitly reports the task needs no code change, nothing is
  committed, and the task is completed without a commit,
* anything else — ERROR, the task fails and the agent's changes are
  discarded.

An agent that exits ``0`` without producing any changes is *not* a valid
no-op: Forgeo treats it as a FAILED task, because a silent no-change SUCCESS
is indistinguishable from an agent that simply did nothing.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from abc import ABC, abstractmethod
from collections import deque

from forgeo.models import ExecutionResult, ExecutionStatus, RepoContext, Task

# Keep only the most recent process output lines so a chatty agent cannot
# blow memory; header/footer status lines are added around this window.
_MAX_OUTPUT_LINES = 1000

# Hard cap on how long we wait for the output streams to reach EOF after the
# process is done (or killed). Grandchildren that escaped the process group
# (daemonized agents, docker containers) may hold the pipes open forever;
# Forgeo must never hang on that.
_DEFAULT_DRAIN_TIMEOUT_SECONDS = 30.0


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the whole process group of a spawned agent.

    ``proc.kill()`` only kills the direct child (for a string command, the
    shell); the real agent often runs as a grandchild, which would survive
    as an orphan and keep the output pipes open. The agent is spawned in its
    own session (``start_new_session=True``), so ``proc.pid`` is its process
    group id and killing the group reaps the entire process tree. Falls back
    to killing just the direct child when the group is already gone.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


class SandboxUnavailableError(RuntimeError):
    """The configured sandbox backend cannot run on this host."""


class BaseAgent(ABC):
    """Uniform interface for invoking a coding agent on a task."""

    name: str = "base"

    @abstractmethod
    async def run_task(
        self,
        task: Task,
        context: RepoContext,
        *,
        command: str | list[str] | None = None,
        timeout_seconds: float | None = None,
        instruction: str | None = None,
    ) -> ExecutionResult:
        """Execute one task and return its result.

        ``command`` and ``timeout_seconds`` optionally override the agent's
        configured values for this single task; ``None`` means "use the
        configured default". ``instruction`` optionally replaces the task's
        own instruction (e.g. a project-context preamble wrapped around it);
        ``None`` means "use ``task.instruction``".

        Implementations must never raise for expected agent failures — they
        should encode them as ``ExecutionResult(status=ERROR)``.
        """


class ShellAgent(BaseAgent):
    """Runs ``ForgeoConfig.agent_command`` in a subprocess."""

    name = "shell"

    def __init__(
        self,
        command: str | list[str],
        *,
        timeout_seconds: float | None = None,
        drain_timeout_seconds: float = _DEFAULT_DRAIN_TIMEOUT_SECONDS,
        env: dict[str, str] | None = None,
        blocked_exit_code: int = 2,
        no_changes_exit_code: int = 3,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.drain_timeout_seconds = drain_timeout_seconds
        self.env = dict(env or {})
        self.blocked_exit_code = blocked_exit_code
        self.no_changes_exit_code = no_changes_exit_code

    @staticmethod
    async def _drain_stream(
        stream: asyncio.StreamReader | None,
        prefix: str,
        lines: deque[str],
    ) -> None:
        """Read ``stream`` line-by-line into ``lines`` until EOF."""
        if stream is None:
            return
        while True:
            raw = await stream.readline()
            if not raw:
                break
            text = raw.decode(errors="replace").rstrip("\r\n")
            lines.append(f"[{prefix}] {text}")

    async def _spawn(
        self,
        command: str | list[str],
        cwd: str,
        env: dict[str, str],
    ) -> asyncio.subprocess.Process:
        """Start the agent process; returns a handle to it.

        Subclasses (e.g. sandboxes) override this to change *how* the command
        runs; the exit-code contract and output handling stay in ``run_task``.
        """
        if isinstance(command, str):
            return await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        return await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    async def run_task(
        self,
        task: Task,
        context: RepoContext,
        *,
        command: str | list[str] | None = None,
        timeout_seconds: float | None = None,
        instruction: str | None = None,
    ) -> ExecutionResult:
        """Run the configured command once for the task.

        ``command`` and ``timeout_seconds`` override this agent's configured
        values for this run when given; otherwise the configured defaults are
        used. ``instruction`` replaces ``task.instruction`` (e.g. a
        project-context preamble wrapped around it) when given.
        """
        command = command if command is not None else self.command
        timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        logs: list[str] = [f"[{self.name}] Running task {task.id} ({task.title})"]
        env = {
            **os.environ,
            **self.env,
            "FORGEO_TASK": instruction if instruction is not None else task.instruction,
            "FORGEO_REPO": str(context.repo_path),
            "FORGEO_BRANCH": context.branch,
        }

        try:
            proc = await self._spawn(command, str(context.repo_path), env)
        except FileNotFoundError as exc:
            logs.append(f"[{self.name}] Command not found: {exc}")
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                output_logs=logs,
                error=f"command not found: {exc}",
            )

        stream_lines: deque[str] = deque(maxlen=_MAX_OUTPUT_LINES)
        readers = asyncio.gather(
            self._drain_stream(proc.stdout, "stdout", stream_lines),
            self._drain_stream(proc.stderr, "stderr", stream_lines),
        )

        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            _kill_process_group(proc)
            # ``proc.wait()`` waits for both process exit *and* pipe closure
            # (see ``BaseSubprocessTransport._try_finish``).  A grandchild that
            # called ``setsid()`` and holds the write end of the pipe keeps it
            # open forever, so the wait would hang past the drain deadline.
            # Bound it by ``drain_timeout_seconds`` and force-close the
            # transport if it still hangs.
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.drain_timeout_seconds)
            except TimeoutError:
                try:
                    proc._transport.close()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001, S110 - close must not fail the task
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except TimeoutError:
                    logs.append(
                        f"[{self.name}] Process did not exit within "
                        f"{self.drain_timeout_seconds:g}s after kill; proceeding."
                    )
        # Always finish draining so lines already written (and any residual
        # after kill) are captured before we build the result. Bounded: a
        # grandchild that escaped the process group (daemonized agent, docker
        # container) may hold the pipes open, and Forgeo must never hang
        # waiting for EOF that will not come.
        try:
            await asyncio.wait_for(readers, timeout=self.drain_timeout_seconds)
        except TimeoutError:
            readers.cancel()
            try:
                await readers
            except asyncio.CancelledError:
                pass
            logs.append(
                f"[{self.name}] Output streams stayed open beyond "
                f"{self.drain_timeout_seconds:g}s; proceeding without them."
            )
        logs.extend(stream_lines)

        if timed_out:
            label = f" after {timeout:g}s" if timeout is not None else ""
            logs.append(f"[{self.name}] Execution timed out{label}; process killed.")
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                output_logs=logs,
                error=f"timed out{label}",
            )

        if proc.returncode == 0:
            logs.append(f"[{self.name}] Task {task.id} finished successfully (exit 0).")
            return ExecutionResult(
                status=ExecutionStatus.SUCCESS,
                output_logs=logs,
                exit_code=proc.returncode,
            )

        if proc.returncode == self.blocked_exit_code:
            logs.append(f"[{self.name}] Task {task.id} needs human input (exit {proc.returncode}).")
            return ExecutionResult(
                status=ExecutionStatus.BLOCKED,
                output_logs=logs,
                questions=[line for line in logs if line.startswith(("[stdout]", "[stderr]"))],
                exit_code=proc.returncode,
            )

        if proc.returncode == self.no_changes_exit_code:
            logs.append(
                f"[{self.name}] Task {task.id} reported no changes needed "
                f"(exit {proc.returncode})."
            )
            return ExecutionResult(
                status=ExecutionStatus.SUCCESS,
                output_logs=logs,
                exit_code=proc.returncode,
                no_changes=True,
            )

        logs.append(f"[{self.name}] Task {task.id} failed with exit code {proc.returncode}.")
        return ExecutionResult(
            status=ExecutionStatus.ERROR,
            output_logs=logs,
            error=f"exit code {proc.returncode}",
            exit_code=proc.returncode,
        )


# Environment variables set by ``run_task`` and forwarded into the container.
_SANDBOX_FORWARDED_ENV = ("FORGEO_TASK", "FORGEO_REPO", "FORGEO_BRANCH")


class DockerSandboxAgent(ShellAgent):
    """Runs the agent command inside a ``docker run --rm`` container.

    The repository is bind-mounted at its absolute path (so the agent's edits
    land on the host checkout), ``FORGEO_TASK`` and ``agent_env`` are passed
    through as environment variables, and networking is disabled
    (``--network none``) unless a network is configured explicitly. Agent
    credentials/config are only visible inside the container when listed in
    ``mounts`` (mounted read-only at the same path). The container exit code
    is mapped exactly like the shell agent's: ``0`` success,
    ``blocked_exit_code`` needs human input, ``no_changes_exit_code`` means
    no code change is needed, anything else is an error.

    The image is expected to contain the agent CLI (e.g. ``claude``) and a
    POSIX shell (``sh``) for string commands.
    """

    name = "docker"

    def __init__(
        self,
        command: str | list[str],
        *,
        image: str,
        network: str = "none",
        mounts: list[str] | None = None,
        timeout_seconds: float | None = None,
        env: dict[str, str] | None = None,
        blocked_exit_code: int = 2,
        no_changes_exit_code: int = 3,
    ) -> None:
        if not (image or "").strip():
            raise ValueError("docker sandbox requires an image")
        if shutil.which("docker") is None:
            raise SandboxUnavailableError(
                "agent_sandbox: docker is configured but the `docker` binary was "
                "not found on PATH."
            )
        super().__init__(
            command,
            timeout_seconds=timeout_seconds,
            env=env,
            blocked_exit_code=blocked_exit_code,
            no_changes_exit_code=no_changes_exit_code,
        )
        self.image = image
        self.network = network
        self.mounts = [mount for mount in (mounts or []) if mount]

    def _docker_args(
        self,
        command: str | list[str],
        cwd: str,
        env: dict[str, str],
    ) -> list[str]:
        """Build the ``docker run`` argv for one agent execution."""
        args = ["docker", "run", "--rm", "--network", self.network, "-w", cwd]
        args.extend(["-v", f"{cwd}:{cwd}"])
        forwarded = set(_SANDBOX_FORWARDED_ENV) | set(self.env)
        for key in sorted(forwarded):
            if key in env:
                args.extend(["-e", f"{key}={env[key]}"])
        for mount in self.mounts:
            args.extend(["-v", f"{mount}:{mount}:ro"])
        args.append(self.image)
        if isinstance(command, str):
            args.extend(["sh", "-c", command])
        else:
            args.extend(command)
        return args

    async def _spawn(
        self,
        command: str | list[str],
        cwd: str,
        env: dict[str, str],
    ) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *self._docker_args(command, cwd, env),
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
