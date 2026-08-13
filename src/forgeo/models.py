"""The only data contracts Forgeo needs.

A task lives in the backlog, gets executed by the agent, and changes status
exactly once per run. A forgeo config describes one repository and how the
forgeo should work on it.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_REFACTOR_PROMPT = (
    "Review the codebase for improvement opportunities that do not change "
    "behavior: dead code, duplication, overly complex functions, missing "
    "tests, outdated comments. Apply the safe improvements you find and run "
    "the test suite to verify nothing broke. If nothing needs refactoring, "
    "make no changes."
)

#: The run outcomes the generic webhook notification can report.
WEBHOOK_EVENTS = ("blocked", "completed", "failed")

#: Fallback shown to the human when a blocked agent gave no explanation.
NO_BLOCKER_REASON = "The agent did not explain what it needs."

#: Failure reason when a task's agent exits 0 but leaves the working tree
#: unchanged: the engine cannot tell "deliberately did nothing" from
#: "did nothing", so a no-change SUCCESS fails the task. To complete a task
#: without touching the code the agent must say so explicitly with
#: ``no_changes_exit_code`` (see the agent contract).
NO_CHANGES_REASON = "Agent exited 0 but produced no changes"

#: Reason recorded when an agent explicitly reports a task needs no code
#: change (exit ``no_changes_exit_code``) and the tree is clean.
NO_CHANGES_REPORTED_REASON = "Agent reported no changes needed"

#: Failure reason when an agent reports no changes but leaves the working tree
#: dirty — a contradiction that must not be silently accepted.
NO_CHANGES_DIRTY_REASON = "Agent reported no changes but left uncommitted changes"

#: Default number of agent output lines kept per run record in ``runs.jsonl``;
#: overridden by the ``run_output_lines`` config key. ``0`` disables
#: persisting agent output entirely.
DEFAULT_RUN_OUTPUT_LINES = 200


def _validate_agent_command(value: str | list[str] | None) -> str | list[str] | None:
    """Shared validation: an agent command must be a non-blank string or list."""
    if value is None:
        return value
    if isinstance(value, str) and not value.strip():
        raise ValueError("agent_command must not be blank")
    if isinstance(value, list) and not value:
        raise ValueError("agent_command must not be an empty list")
    return value


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TaskStatus(str, enum.Enum):
    OPEN = "OPEN"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ExecutionStatus(str, enum.Enum):
    SUCCESS = "SUCCESS"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


class SandboxMode(str, enum.Enum):
    """How the agent process is isolated from the host machine.

    ``NONE`` runs the agent directly on the host with the user's full
    privileges (the default, unchanged behavior); ``DOCKER`` runs the agent
    inside a ``docker run --rm`` container.
    """

    NONE = "none"
    DOCKER = "docker"


class RunKind(str, enum.Enum):
    """What kind of work a finished cycle performed."""

    TASK = "task"
    REFACTOR = "refactor"


class RunOutcome(str, enum.Enum):
    """The outcome of a finished cycle.

    ``SUCCESS``, ``BLOCKED`` and ``ERROR`` mirror the agent execution status;
    the remaining values cover cycles that paused or never ran the agent.
    """

    SUCCESS = "SUCCESS"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"
    PAUSED = "PAUSED"
    DIRTY = "DIRTY"


class RunRecord(BaseModel):
    """A durable, queryable record of one finished forgeo cycle.

    One JSON object per line in ``runs.jsonl``, next to the backlog.
    """

    started_at: datetime
    finished_at: datetime
    kind: RunKind | None = None
    task_id: str | None = None
    task_title: str | None = None
    outcome: RunOutcome
    agent_exit_code: int | None = None
    commit_sha: str | None = None
    duration_seconds: float
    reason: str | None = Field(
        default=None,
        description="Human-readable note surfacing a no-change SUCCESS "
        "(no commit was produced), so it is never a silent null commit_sha.",
    )
    output_logs: list[str] | None = Field(
        default=None,
        description="Bounded tail of the agent's stdout/stderr for this run "
        "(at most ``run_output_lines`` lines). ``None`` for runs that never "
        "reached the agent or that predate the field, so old records render "
        "as empty instead of erroring.",
    )


class Task(BaseModel):
    """A unit of work Forgeo executes with the coding agent.

    ``blocker_reason`` and ``blocked_count`` are engine-managed: they record
    the last agent explanation when the task becomes ``BLOCKED`` and how many
    times that happened, respectively. ``failure_reason`` is likewise
    engine-managed: the agent's error when the task becomes ``FAILED``. None
    of them are editable through the web console's ``PATCH`` endpoint.
    """

    id: str
    title: str
    description: str
    dependencies: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    files_to_modify: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.OPEN
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    agent_command: str | list[str] | None = Field(default=None)
    agent_timeout_seconds: float | None = Field(default=None, gt=0)
    blocker_reason: list[str] = Field(default_factory=list)
    blocked_count: int = Field(default=0, ge=0)
    failure_reason: list[str] = Field(default_factory=list)

    @property
    def instruction(self) -> str:
        """The full instruction handed to the agent for this task."""
        lines = [self.title, ""]
        if self.description:
            lines.append(self.description)
        if self.acceptance_criteria:
            lines.append("Acceptance criteria:")
            lines.extend(f"- {criterion}" for criterion in self.acceptance_criteria)
        return "\n".join(lines)

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("description must be a non-blank string")
        return value

    @field_validator("agent_command")
    @classmethod
    def _command_not_blank(cls, value: str | list[str] | None) -> str | list[str] | None:
        return _validate_agent_command(value)


class ExecutionResult(BaseModel):
    """The outcome of one agent run."""

    status: ExecutionStatus
    output_logs: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    error: str | None = None
    exit_code: int | None = None
    no_changes: bool = Field(
        default=False,
        description="Agent explicitly reported no code change is needed "
        "(exit no_changes_exit_code); the tree must be clean.",
    )

    @property
    def reason(self) -> list[str]:
        """The agent's human-readable explanation: its questions, else its output."""
        return self.questions or self.output_logs


class RepoContext(BaseModel):
    """Where the agent works: the repository checkout and its branch."""

    repo_path: Path = Path(".")
    branch: str = "main"


class ForgeoConfig(BaseModel):
    """Everything needed to run one forgeo on one repository.

    Attributes:
        name: Display name of this forgeo (used in logs and commit messages).
        repo: Path of the git repository Forgeo works on.
        interval_minutes: How often a scheduled run happens.
        backlog: Path of the JSON backlog file (created on first use).
        blocker_file: Where ``BLOCKER.md`` is written when the agent needs
            human input. Keep it outside the repository so it is never
            committed.
        agent_command: Shell command (or argv list) that runs the coding
            agent. Exit 0 = success, ``blocked_exit_code`` = needs human
            input, anything else = error. The task is available to the
            process as the ``FORGEO_TASK`` environment variable.
        agent_timeout_seconds: Kill the agent process after this many seconds
            (``None`` = never; a run that overruns the interval simply makes
            the next iteration skip).
        agent_env: Extra environment variables for the agent process.
        agent_sandbox: Isolation mode for the agent process: ``none`` (the
            default, runs directly on the host) or ``docker`` (runs inside a
            container). See the README for the docker image expectations.
        agent_sandbox_image: Container image used when ``agent_sandbox`` is
            ``docker``. Required in that mode; it must contain the agent CLI
            and a POSIX shell.
        agent_sandbox_network: Docker network for the sandboxed agent
            (``--network``). Default ``none`` (networking disabled); set to
            e.g. ``bridge`` or ``host`` to re-enable it.
        agent_sandbox_mounts: Host paths mounted read-only into the sandboxed
            container at the same absolute path (agent credentials/config).
            Nothing is mounted unless listed here.
        blocked_exit_code: Exit code the agent uses to signal that it needs
            human input.
        no_changes_exit_code: Exit code the agent uses to signal that the task
            legitimately needs no code change. Exiting with this code completes
            the task without committing anything; exiting ``0`` with an empty
            working tree instead fails the task (see the agent contract).
        remote: Git remote to push to (e.g. ``origin``). When omitted the
            forgeo only commits locally.
        branch: Branch everything is committed to (default ``main``).
        git_timeout_seconds: Kill a git subprocess after this many seconds
            (default 120). Raise for slow remotes.
        refactor_prompt: Instruction used for the refactoring run that
            happens when the backlog has no runnable task.
        log_file: Where the scheduled forgeo writes its log.
        run_history_keep: How many finished runs ``runs.jsonl`` keeps (oldest
            lines are trimmed atomically on append). ``0`` disables retention
            entirely so the file grows forever, exactly as before.
        run_output_lines: How many agent output lines each run record keeps
            in ``runs.jsonl`` (the bounded tail of the agent's stdout/stderr).
            ``0`` disables persisting agent output entirely.
        telegram_bot_token: Telegram bot token for blocked-run
            notifications. Disabled unless ``telegram_chat_id`` is also set.
        telegram_chat_id: Chat ID that receives blocked-run notifications.
            Disabled unless ``telegram_bot_token`` is also set.
        notify_webhook_url: Vendor-neutral webhook URL that receives a JSON
            POST for run outcomes (Slack, Discord, ntfy, ...). Disabled when
            unset.
        notify_webhook_events: Which outcomes to report to
            ``notify_webhook_url``; a subset of ``blocked``, ``completed``,
            ``failed``. Defaults to ``blocked`` only.
    """

    name: str = "forgeo"
    repo: Path = Field(default=Path("."))
    interval_minutes: int = Field(default=60, ge=1)
    backlog: Path = Field(default=Path("backlog.json"))
    blocker_file: Path = Field(default=Path("BLOCKER.md"))
    agent_command: str | list[str]
    agent_timeout_seconds: float | None = Field(default=None, gt=0)
    agent_env: dict[str, str] = Field(default_factory=dict)
    agent_sandbox: SandboxMode = SandboxMode.NONE
    agent_sandbox_image: str | None = None
    agent_sandbox_network: str = "none"
    agent_sandbox_mounts: list[str] = Field(default_factory=list)
    blocked_exit_code: int = Field(default=2)
    no_changes_exit_code: int = Field(default=3)
    remote: str | None = None
    branch: str = "main"
    git_timeout_seconds: float = Field(default=120, gt=0)
    refactor_prompt: str = DEFAULT_REFACTOR_PROMPT
    log_file: str = "forgeo.log"
    run_history_keep: int = Field(default=2000, ge=0)
    run_output_lines: int = Field(default=DEFAULT_RUN_OUTPUT_LINES, ge=0)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    notify_webhook_url: str | None = None
    notify_webhook_events: list[str] = Field(
        default_factory=lambda: [WEBHOOK_EVENTS[0]]
    )

    @field_validator("agent_command")
    @classmethod
    def _command_not_blank(cls, value: str | list[str]) -> str | list[str] | None:
        return _validate_agent_command(value)

    @field_validator("notify_webhook_events")
    @classmethod
    def _webhook_events_valid(cls, value: list[str]) -> list[str]:
        unknown = [event for event in value if event not in WEBHOOK_EVENTS]
        if unknown:
            raise ValueError(
                "notify_webhook_events must be a subset of "
                f"{list(WEBHOOK_EVENTS)}, got {unknown}"
            )
        return list(dict.fromkeys(value))

    @field_validator("agent_sandbox_network")
    @classmethod
    def _network_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("agent_sandbox_network must not be blank")
        return value

    @field_validator("no_changes_exit_code")
    @classmethod
    def _no_changes_exit_code_not_success(cls, value: int) -> int:
        if value == 0:
            raise ValueError("no_changes_exit_code must not be 0 (reserved for SUCCESS)")
        return value

    @field_validator("agent_sandbox_mounts")
    @classmethod
    def _mounts_not_blank(cls, value: list[str]) -> list[str]:
        for mount in value:
            if not mount.strip():
                raise ValueError("agent_sandbox_mounts must not contain blank paths")
        return value

    @model_validator(mode="after")
    def _docker_requires_image(self) -> ForgeoConfig:
        if self.agent_sandbox is SandboxMode.DOCKER and not (self.agent_sandbox_image or "").strip():
            raise ValueError("agent_sandbox_image is required when agent_sandbox is 'docker'")
        if self.no_changes_exit_code == self.blocked_exit_code:
            raise ValueError(
                "no_changes_exit_code must differ from blocked_exit_code"
            )
        return self
