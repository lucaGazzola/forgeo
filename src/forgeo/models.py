"""The only data contracts Forgeo needs.

A task lives in the backlog, gets executed by the agent, and changes status
exactly once per run. A forgeo config describes one repository and how the
forgeo should work on it.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_REFACTOR_PROMPT = (
    "Review the codebase for improvement opportunities that do not change "
    "behavior: dead code, duplication, overly complex functions, missing "
    "tests, outdated comments. Apply the safe improvements you find and run "
    "the test suite to verify nothing broke. If nothing needs refactoring, "
    "make no changes."
)

#: The run outcomes the generic webhook notification can report.
WEBHOOK_EVENTS = ("blocked", "completed", "failed", "review")

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

#: Default number of finished runs kept in ``runs.jsonl``; overridden by the
#: ``run_history_keep`` config key. ``0`` disables retention.
DEFAULT_RUN_HISTORY_KEEP = 2000


#: URL schemes a ``backlog`` value may use to point at a remote endpoint.
#: Anything else is treated as a filesystem path.
_URL_SCHEMES = ("http://", "https://")

#: All backlog providers Forgeo knows about.
PROVIDER_CHOICES: tuple[str, ...] = ("file", "http", "jira", "github", "gitlab")
PROVIDER_LITERAL = Literal["auto", "file", "http", "jira", "github", "gitlab"]
REMOTE_PROVIDERS: frozenset[str] = frozenset({"http", "jira", "github", "gitlab"})
ISSUE_PROVIDERS: frozenset[str] = frozenset({"jira", "github", "gitlab"})


def is_url(value: object) -> bool:
    """True when ``value`` is a string pointing at an ``http(s)`` endpoint."""
    return isinstance(value, str) and value.startswith(_URL_SCHEMES)


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
    REVIEW = "REVIEW"
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
    SKIPPED = "SKIPPED"


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
    retry_count: int | None = Field(
        default=None,
        description="How many times the task had already been retried when "
        "this run happened (task runs only; ``None`` for refactor runs and "
        "records that predate the field).",
    )


class Task(BaseModel):
    """A unit of work Forgeo executes with the coding agent.

    ``blocker_reason`` and ``blocked_count`` are engine-managed: they record
    the last agent explanation when the task becomes ``BLOCKED`` and how many
    times that happened, respectively. ``failure_reason`` is likewise
    engine-managed: the agent's error when the task becomes ``FAILED``.
    ``agent_response`` is likewise engine-managed: the last agent
    stdout/stderr output persisted on a status transition. None of them are
    editable through the web console's ``PATCH`` endpoint.
    ``retries_left`` is a human-set per-task override of the retry budget
    (``failed_retry_max`` in the config); ``retry_count`` and
    ``failed_wait_cycles`` are engine-managed retry state.
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
    run_at: datetime | None = Field(
        default=None,
        description="Optional one-shot schedule: the earliest moment this task "
        "may be picked. A past value makes the task fire immediately on the "
        "next cycle (and the daemon wakes early for it); a future value keeps "
        "the task unpicked until then. ``None`` (the default) picks the task "
        "by oldest ``created_at`` as before.",
    )
    agent_command: str | list[str] | None = Field(default=None)
    agent_timeout_seconds: float | None = Field(default=None, gt=0)
    blocker_reason: list[str] = Field(default_factory=list)
    blocked_count: int = Field(default=0, ge=0)
    failure_reason: list[str] = Field(default_factory=list)
    agent_response: str | None = Field(
        default=None,
        description="Engine-managed: the agent's last stdout/stderr output, "
        "stripped of its stream prefixes and persisted on status transitions "
        "(capped at ``agent_response_lines`` lines when set). ``None`` when "
        "the transition carried no output. Intended for the backlog consumer; "
        "not editable via ``PATCH``.",
    )
    retries_left: int | None = Field(
        default=None,
        ge=0,
        description="Per-task override of the retry budget: how many times a "
        "FAILED task may be retried automatically. ``None`` falls back to the "
        "config's ``failed_retry_max``; ``0`` disables retries for this task.",
    )
    retry_count: int = Field(
        default=0,
        ge=0,
        description="Engine-managed: how many times this task has already "
        "been retried (shown in run records and the web console).",
    )
    failed_wait_cycles: int = Field(
        default=0,
        ge=0,
        description="Engine-managed: how many cycles this task has been FAILED "
        "awaiting a retry (backed off by ``failed_retry_wait_cycles``).",
    )
    review_branch: str | None = Field(
        default=None,
        description="Engine-managed: the feature branch created for a REVIEW task "
        "(set when the task moves to REVIEW, cleared when it leaves).",
    )
    review_commit_sha: str | None = Field(
        default=None,
        description="Engine-managed: the commit SHA pushed on the review branch.",
    )
    review_required: bool | None = Field(
        default=None,
        description="Per-task override of the review workflow: ``True`` forces a "
        "feature branch + REVIEW, ``False`` forces direct COMPLETED, ``None`` "
        "inherits from the config's ``review_mode``.",
    )

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

    @field_validator("run_at", mode="before")
    @classmethod
    def _run_at_type(cls, value: object) -> object:
        """Reject non-string/non-datetime ``run_at`` values.

        Without this, pydantic would silently coerce a bare number into an
        epoch timestamp (e.g. ``42`` becomes 1970-01-01) — never useful for a
        one-shot schedule, so it is refused with a clear message instead.
        """
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("run_at must be an ISO-8601 datetime string or null")
        return value

    @field_validator("run_at")
    @classmethod
    def _run_at_utc(cls, value: datetime | None) -> datetime | None:
        """Normalize ``run_at`` to an aware UTC datetime for comparison.

        The backlog is edited by hand and by the web console, so ``run_at``
        may arrive naive (assumed UTC, like the daemon's other timestamps)
        or in another offset; the pick and the daemon both compare against
        aware ``datetime.now(UTC)``, so it is normalized once here.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


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


class BacklogAuth(BaseModel):
    """OAuth2 *client credentials* access to an HTTP backlog endpoint.

    Forgeo authenticates as a service account, not as a human: an identity
    provider (Keycloak and anything else speaking OAuth2) issues an access
    token for a confidential client, and that token is sent as a bearer on
    every backlog request.

    The client secret is deliberately **not** a config value: only the name of
    the environment variable holding it is stored, so the secret never lands in
    ``forgeo.yaml`` (which the web console serves to the browser) nor in a
    backup of it.

    Attributes:
        token_url: The provider's token endpoint, e.g.
            ``https://keycloak.example.com/realms/<realm>/protocol/openid-connect/token``.
        client_id: The confidential client requesting the token.
        client_secret_env: Name of the environment variable holding that
            client's secret. Read from the process environment at request
            time; a missing variable is a hard error.
        scope: Optional scope requested alongside the token.
        timeout_seconds: Kill a token request after this many seconds.
    """

    token_url: str
    client_id: str
    client_secret_env: str
    scope: str | None = None
    timeout_seconds: float = Field(default=10, gt=0)

    @field_validator("token_url")
    @classmethod
    def _token_url_is_http(cls, value: str) -> str:
        if not is_url(value):
            raise ValueError("token_url must be an http:// or https:// URL")
        return value

    @field_validator("client_id", "client_secret_env")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class JiraOAuthConfig(BaseModel):
    """OAuth / browser login for Jira Cloud (Atlassian 3LO)."""

    client_id: str
    client_secret_env: str | None = None
    scope: str | None = None
    token_file: str | Path | None = None
    cloud_id: str | None = None
    flow: Literal["browser", "device"] = "browser"

    @field_validator("client_id")
    @classmethod
    def _client_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("client_id must not be blank")
        return value

    @field_validator("client_secret_env", "cloud_id")
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("value must not be blank")
        return value


class JiraAuth(BaseModel):
    """Authentication for a Jira REST API.

    Jira Cloud normally uses ``basic`` authentication with an email address
    and API token. Jira Server/Data Center installations commonly expose a
    bearer personal-access token instead. Secrets are always read from the
    environment; only the environment variable names are persisted in the
    config file.

    OAuth (browser login) is available for Jira Cloud via ``oauth``; exactly
    one of PAT (``token_env``/``scheme``) or ``oauth`` must be set.
    """

    scheme: Literal["basic", "bearer"] = "basic"
    token_env: str | None = None
    username: str | None = None
    username_env: str | None = None
    oauth: JiraOAuthConfig | None = None

    @field_validator("token_env", "username_env")
    @classmethod
    def _env_name_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("environment variable names must not be blank")
        return value

    @field_validator("username")
    @classmethod
    def _username_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("username must not be blank")
        return value

    @model_validator(mode="after")
    def _check_auth(self) -> JiraAuth:
        has_pat = self.token_env is not None
        has_oauth = self.oauth is not None
        if has_pat == has_oauth:
            raise ValueError("jira.auth must have exactly one of token_env or oauth")
        if has_pat and self.scheme == "basic" and self.username is None and self.username_env is None:
            raise ValueError("Jira basic authentication requires username or username_env")
        return self


class JiraWorkflow(BaseModel):
    """Jira status references used by the task provider.

    Values may be Jira status IDs or names. IDs are preferred because names
    can be changed or localized. ``open_statuses`` controls which issues are
    eligible for picking; ``open_status`` is the target used when reopening.
    """

    open_statuses: list[str] = Field(default_factory=lambda: ["To Do", "Open"])
    open_status: str = "To Do"
    running_status: str | None = "In Progress"
    blocked_status: str | None = None
    completed_status: str = "Done"
    failed_status: str | None = None

    @field_validator(
        "open_statuses",
        "open_status",
        "running_status",
        "blocked_status",
        "completed_status",
        "failed_status",
    )
    @classmethod
    def _status_references_not_blank(
        cls, value: list[str] | str | None
    ) -> list[str] | str | None:
        if isinstance(value, list):
            if not value or any(not item.strip() for item in value):
                raise ValueError("open_statuses must contain non-blank values")
        elif value is not None and not value.strip():
            raise ValueError("Jira status references must not be blank")
        return value


def _field_names_not_blank(value: str | None) -> str | None:
    if value is None:
        return None
    return _require_non_blank(value, "field names must not be blank")


_ISSUE_FIELD_NAMES: tuple[str, ...] = (
    "acceptance_criteria",
    "dependencies",
    "files_to_modify",
    "agent_command",
    "agent_timeout_seconds",
    "run_at",
    "retries_left",
)


class _IssueFieldMappingBase(BaseModel):
    """Shared optional field mappings for issue providers."""

    acceptance_criteria: str | None = None
    dependencies: str | None = None
    files_to_modify: str | None = None
    agent_command: str | None = None
    agent_timeout_seconds: str | None = None
    run_at: str | None = None
    retries_left: str | None = None

    @field_validator(*_ISSUE_FIELD_NAMES)
    @classmethod
    def _check_field_names(cls, value: str | None) -> str | None:
        return _field_names_not_blank(value)


class JiraFieldMapping(_IssueFieldMappingBase):
    """Optional Jira custom fields carrying Forgeo task attributes."""


class _IssueBacklogConfigBase(BaseModel):
    """Shared settings for issue backlogs."""

    label_prefix: str = "forgeo"
    property_key: str = "forgeo"
    page_size: int = Field(default=30, ge=1, le=100)
    max_issues: int = Field(default=1000, ge=1)
    timeout_seconds: float = Field(default=30, gt=0)
    claim_timeout_seconds: float = Field(default=86400, gt=0)

    @field_validator("label_prefix", "property_key")
    @classmethod
    def _safe_identifier(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("labels and property keys must not be blank")
        if any(character.isspace() for character in value):
            raise ValueError("labels and property keys must not contain whitespace")
        return value

class JiraBacklogConfig(_IssueBacklogConfigBase):
    """Jira-specific settings for a remote task provider."""

    auth: JiraAuth
    jql: str
    project_key: str | None = None
    issue_type: str = "Task"
    api_version: Literal[2, 3] = 3
    page_size: int = Field(default=50, ge=1, le=100)
    workflow: JiraWorkflow = Field(default_factory=JiraWorkflow)
    fields: JiraFieldMapping = Field(default_factory=JiraFieldMapping)

    @field_validator("jql", "issue_type")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Jira configuration values must not be blank")
        return value


# ------------------------------------------------------------------ #
# GitHub / GitLab shared base                                       #
# ------------------------------------------------------------------ #


class _PatAuthBase(BaseModel):
    """Shared PAT authentication for issue providers."""

    token_env: str

    @field_validator("token_env")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("token_env must not be blank")
        return value


class GithubOAuthConfig(BaseModel):
    """OAuth / browser login for GitHub."""

    client_id: str
    scope: str | None = None
    token_file: str | Path | None = None
    flow: Literal["device", "browser"] = "device"
    client_secret_env: str | None = None

    @field_validator("client_id")
    @classmethod
    def _client_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("client_id must not be blank")
        return value

    @field_validator("client_secret_env")
    @classmethod
    def _secret_env_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("client_secret_env must not be blank")
        return value


class GithubAuth(BaseModel):
    """PAT or OAuth authentication for GitHub REST API.

    Exactly one of ``token_env`` (PAT) or ``oauth`` (browser/device flow)
    must be set. ``token_env`` preserves the historical behaviour.
    """

    token_env: str | None = None
    oauth: GithubOAuthConfig | None = None

    @field_validator("token_env")
    @classmethod
    def _token_env_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("token_env must not be blank")
        return value

    @model_validator(mode="after")
    def _exactly_one_auth(self) -> GithubAuth:
        has_pat = self.token_env is not None
        has_oauth = self.oauth is not None
        if has_pat == has_oauth:
            raise ValueError("github.auth must have exactly one of token_env or oauth")
        return self



class GitlabOAuthConfig(BaseModel):
    """OAuth / browser login for GitLab."""

    client_id: str
    scope: str | None = None
    token_file: str | Path | None = None
    flow: Literal["device", "browser"] = "browser"
    client_secret_env: str | None = None

    @field_validator("client_id")
    @classmethod
    def _client_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("client_id must not be blank")
        return value

    @field_validator("client_secret_env")
    @classmethod
    def _secret_env_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("client_secret_env must not be blank")
        return value


class GitlabAuth(BaseModel):
    """PAT or OAuth authentication for GitLab REST API.

    Exactly one of ``token_env`` (PAT) or ``oauth`` (browser/device flow)
    must be set.
    """

    token_env: str | None = None
    oauth: GitlabOAuthConfig | None = None

    @field_validator("token_env")
    @classmethod
    def _token_env_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("token_env must not be blank")
        return value

    @model_validator(mode="after")
    def _exactly_one_auth(self) -> GitlabAuth:
        has_pat = self.token_env is not None
        has_oauth = self.oauth is not None
        if has_pat == has_oauth:
            raise ValueError("gitlab.auth must have exactly one of token_env or oauth")
        return self



class _IssueWorkflowBase(BaseModel):
    """Shared workflow mapping for issue providers."""

    open_statuses: list[str]
    open_status: str
    running_status: str | None = None
    blocked_status: str | None = None
    completed_status: str = "closed"
    failed_status: str | None = None


class GithubWorkflow(_IssueWorkflowBase):
    """Workflow mapping for GitHub issues.

    GitHub issues have states ``open`` / ``closed``; blocked/failed are
    represented by labels. ``open_statuses`` is kept for symmetry but not
    used; ``completed_status`` defaults to ``closed``.
    """

    open_statuses: list[str] = Field(default_factory=lambda: ["open"])
    open_status: str = "open"


class GitlabWorkflow(_IssueWorkflowBase):
    """Workflow mapping for GitLab issues.

    GitLab issues have states ``opened`` / ``closed``.
    """

    open_statuses: list[str] = Field(default_factory=lambda: ["opened"])
    open_status: str = "opened"


class GithubFieldMapping(_IssueFieldMappingBase):
    """Optional field mappings for GitHub issues."""


class GitlabFieldMapping(_IssueFieldMappingBase):
    """Optional field mappings for GitLab issues."""


def _require_non_blank(value: str, message: str) -> str:
    if not value.strip():
        raise ValueError(message)
    return value


class _RepoBacklogConfigBase(_IssueBacklogConfigBase):
    """Shared ``repo`` field for GitHub/GitLab issue providers."""

    repo: str

    @field_validator("repo")
    @classmethod
    def _repo_not_blank(cls, value: str) -> str:
        return _require_non_blank(value, "repo must not be blank")


class GithubBacklogConfig(_RepoBacklogConfigBase):
    """GitHub-specific settings for an issue backlog."""

    auth: GithubAuth
    workflow: GithubWorkflow = Field(default_factory=GithubWorkflow)
    fields: GithubFieldMapping = Field(default_factory=GithubFieldMapping)


class GitlabBacklogConfig(_RepoBacklogConfigBase):
    """GitLab-specific settings for an issue backlog."""

    auth: GitlabAuth
    workflow: GitlabWorkflow = Field(default_factory=GitlabWorkflow)
    fields: GitlabFieldMapping = Field(default_factory=GitlabFieldMapping)


class ForgeoConfig(BaseModel):
    """Everything needed to run one forgeo on one repository.

    Attributes:
        name: Display name of this forgeo (used in logs and commit messages).
        repo: Path of the git repository Forgeo works on.
        interval_minutes: How often a scheduled run happens.
        backlog: Where the backlog lives: the path of a JSON file (created on
            first use), an ``http(s)`` URL serving the Forgeo document, or a
            Jira base URL when ``backlog_provider`` is ``jira``.
        backlog_provider: ``auto`` preserves the historical file/HTTP
            inference. ``file``, ``http`` and ``jira`` explicitly select a
            provider. A Jira provider uses the separate ``jira`` settings.
        state_dir: Directory holding Forgeo's own runtime files (the locks,
            the daemon state, the run history, the update-check stamp). Only
            needed when ``backlog`` is remote, where there is no backlog file
            to put them next to; it then defaults to the directory of
            ``forgeo.yaml``. With a backlog file they always sit beside it.
        backlog_auth: Credentials for an ``http(s)`` backlog that requires
            them. Omit for a file backlog or an unauthenticated endpoint.
        jira: Jira REST API, workflow, and field mapping settings. Required
            when the effective ``backlog_provider`` is ``jira``.
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
        task_context: Optional path to a file (e.g. ``CONTEXT.md``) whose
            contents are prepended to every agent instruction, task and
            refactoring run alike. This gives an agent the high-level
            project overview before the task, instead of only the isolated
            task description. The file is re-read on every run, so the
            agent's own updates to it are picked up on the next cycle; a
            missing or unreadable file is logged and the cycle runs without
            it.
        log_file: Where the scheduled forgeo writes its log.
        run_history_keep: How many finished runs ``runs.jsonl`` keeps (oldest
            lines are trimmed atomically on append). ``0`` disables retention
            entirely so the file grows forever, exactly as before.
        run_output_lines: How many agent output lines each run record keeps
            in ``runs.jsonl`` (the bounded tail of the agent's stdout/stderr).
            ``0`` disables persisting agent output entirely.
        agent_response_lines: How many agent output lines the task's
            ``agent_response`` keeps when it transitions (the bounded tail of
            the agent's stdout/stderr). ``None`` (default) = unbounded;
            ``0`` disables persisting agent output on the task.
        failed_retry_max: How many times a ``FAILED`` task is retried
            automatically (``0`` = disabled: a task stays ``FAILED`` until a
            human reopens it, exactly as before). A task may override this
            budget per-task with ``retries_left``.
        failed_retry_wait_cycles: How many cycles a retry-eligible ``FAILED``
            task waits (backoff) before it is moved back to ``OPEN``.
        no_changes_retry_max: How many times a task whose agent exits ``0``
            without producing any code changes is re-run immediately, in the
            same cycle, before the task is marked ``BLOCKED`` for human
            review. ``0`` (default) = a silent no-change SUCCESS is marked
            ``BLOCKED`` on the first attempt.
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
    backlog: str | Path = Field(default=Path("backlog.json"))
    backlog_provider: PROVIDER_LITERAL = "auto"
    state_dir: Path | None = None
    backlog_auth: BacklogAuth | None = None
    jira: JiraBacklogConfig | None = None
    github: GithubBacklogConfig | None = None
    gitlab: GitlabBacklogConfig | None = None
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
    task_context: Path | None = None
    log_file: str = "forgeo.log"
    run_history_keep: int = Field(default=DEFAULT_RUN_HISTORY_KEEP, ge=0)
    run_output_lines: int = Field(default=DEFAULT_RUN_OUTPUT_LINES, ge=0)
    agent_response_lines: int | None = Field(
        default=None,
        ge=0,
        description="How many agent output lines the task's ``agent_response`` "
        "keeps on a status transition (the bounded tail of the agent's "
        "stdout/stderr). ``None`` (default) = unbounded; ``0`` disables "
        "persisting agent output on the task.",
    )
    failed_retry_max: int = Field(default=0, ge=0)
    failed_retry_wait_cycles: int = Field(default=1, ge=1)
    no_changes_retry_max: int = Field(default=0, ge=0)
    review_mode: Literal["off", "branch"] = Field(
        default="off",
        description="Whether completed tasks go to REVIEW on a feature branch. "
        "``off`` (default) commits directly to ``branch`` and marks COMPLETED; "
        "``branch`` creates ``review_branch_prefix + task.id`` and marks REVIEW.",
    )
    review_branch_prefix: str = Field(
        default="forgeo/review/",
        description="Prefix for feature branches created when ``review_mode`` is "
        "``branch``. The task id is appended, e.g. ``forgeo/review/TASK-001``.",
    )
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

    @field_validator("backlog", mode="before")
    @classmethod
    def _normalize_backlog(cls, value: Any) -> Any:
        """Keep a URL backlog a ``str``; coerce anything else to a ``Path``.

        Deciding the branch here (instead of leaving it to the union) is what
        makes the field predictable: ``Path("https://host/x")`` would silently
        collapse the double slash into ``https:/host/x``, so a URL must never
        reach the ``Path`` branch.
        """
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("backlog must not be blank")
            if is_url(value):
                return value
        if isinstance(value, str | Path):
            return Path(value)
        return value

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

    @field_validator("review_branch_prefix")
    @classmethod
    def _review_prefix_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("review_branch_prefix must not be blank")
        if any(ch.isspace() for ch in value):
            raise ValueError("review_branch_prefix must not contain whitespace")
        return value

    @property
    def backlog_is_url(self) -> bool:
        """True when the backlog value is a remote HTTP(S) URL."""
        return is_url(self.backlog)

    @property
    def effective_backlog_provider(self) -> Literal["file", "http", "jira", "github", "gitlab"]:
        """The provider selected by explicit config or legacy inference."""
        if self.backlog_provider != "auto":
            return self.backlog_provider
        if self.jira is not None:
            return "jira"
        if self.github is not None:
            return "github"
        if self.gitlab is not None:
            return "gitlab"
        value: Literal["file", "http", "jira", "github", "gitlab"] = (
            "http" if self.backlog_is_url else "file"
        )
        return value

    @property
    def backlog_is_jira(self) -> bool:
        """True when tasks are loaded from Jira rather than a document."""
        return self.effective_backlog_provider == "jira"

    @property
    def backlog_is_remote(self) -> bool:
        """True when the backlog has no local task document."""
        return self.effective_backlog_provider in REMOTE_PROVIDERS

    @property
    def backlog_is_issue_provider(self) -> bool:
        return self.effective_backlog_provider in ISSUE_PROVIDERS

    def _check_sandbox(self) -> None:
        if self.agent_sandbox is SandboxMode.DOCKER and not (self.agent_sandbox_image or "").strip():
            raise ValueError("agent_sandbox_image is required when agent_sandbox is 'docker'")
        if self.no_changes_exit_code == self.blocked_exit_code:
            raise ValueError("no_changes_exit_code must differ from blocked_exit_code")

    def _check_provider_url(self, provider: str) -> None:
        if provider in REMOTE_PROVIDERS and not self.backlog_is_url:
            raise ValueError(
                f"backlog must be an http:// or https:// URL when backlog_provider is "
                f"{provider!r}"
            )
        if provider == "file" and self.backlog_is_url:
            raise ValueError("backlog must be a filesystem path when backlog_provider is 'file'")
        if self.backlog_auth is not None and provider != "http":
            # Silently ignoring the credentials would hide a typo in the
            # backlog value behind a working (but local) forgeo.
            raise ValueError(
                "backlog_auth is only valid when backlog is an http:// or https:// URL "
                "and backlog_provider is 'http'"
            )

    def _check_provider_blocks(self, provider: str) -> None:
        for name, label in (("jira", "Jira"), ("github", "GitHub"), ("gitlab", "GitLab")):
            cfg = getattr(self, name)
            if provider == name and cfg is None:
                raise ValueError(f"{name} configuration is required when backlog_provider is {name!r}")
            if provider != name and cfg is not None:
                raise ValueError(
                    f"{name} configuration is only valid when backlog_provider is {name!r} "
                    f"or 'auto' with a {label} backlog"
                )

    def _check_review(self) -> None:
        if self.review_mode not in ("off", "branch"):
            raise ValueError("review_mode must be 'off' or 'branch'")

    @model_validator(mode="after")
    def _docker_requires_image(self) -> ForgeoConfig:
        self._check_sandbox()
        self._check_review()
        provider = self.effective_backlog_provider
        self._check_provider_url(provider)
        self._check_provider_blocks(provider)
        return self
