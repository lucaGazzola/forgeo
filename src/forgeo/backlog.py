"""The backlog: the list of tasks Forgeo works through.

Forgeo pulls the oldest ``OPEN`` task whose dependencies are all ``COMPLETED``
from here (an optional ``run_at`` one-shot schedule overrides the oldest-first
order). The tasks live in a single JSON document — ``{"tasks": [...]}`` — which
is either a local file you can edit by hand (add, remove, or set a ``BLOCKED``
task back to ``OPEN`` once you have provided the input it needed) or an HTTP
endpoint owned by another application, see :mod:`forgeo.backlog_http`. Issue
providers (Jira, GitHub, GitLab) implement the same task-level contract through
issue operations.

Everything above the document-storage layer is shared: :class:`BacklogStore`
holds the task manipulation and the asyncio lock that serializes writes. File
and HTTP document providers implement load/store; issue providers override the
task-level operations that cannot be represented as a whole-document write.

A file backlog is the single source of truth, so it is guarded against bad
writes: before every agent run (and on daemon startup) a rotating snapshot is
written next to it (``backlog.json.bak``, ``backlog.json.bak.1``, ...) and a
read that finds a corrupt store restores the newest valid snapshot in place
instead of silently starting from an empty one. A remote backlog is owned by
the provider, which keeps its own history, so it is never snapshotted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from forgeo.io import atomic_write_text
from forgeo.models import ExecutionResult, ForgeoConfig, Task, TaskStatus

logger = logging.getLogger(__name__)

#: How many rotating backlog snapshots to keep (default 2: ``.bak``, ``.bak.1``).
DEFAULT_SNAPSHOT_COUNT = 2

#: The task fields the web console may edit through ``update_task``.
EDITABLE_TASK_FIELDS = frozenset(
    {
        "title",
        "description",
        "acceptance_criteria",
        "dependencies",
        "files_to_modify",
        "agent_command",
        "agent_timeout_seconds",
        "retries_left",
        "run_at",
    }
)


def validate_task_updates(updates: dict[str, Any]) -> None:
    """Validate fields accepted by the task-editing API.

    Providers use the same validation before translating updates into their
    native API. Keeping it here prevents a remote provider from accepting a
    task shape that a local backlog would reject.
    """
    unknown = set(updates) - EDITABLE_TASK_FIELDS
    if unknown:
        raise ValueError(f"unknown task field(s): {', '.join(sorted(unknown))}")
    for field in ("title", "description"):
        if field in updates and not isinstance(updates[field], str):
            raise ValueError(f"{field} must be a string")
    if "title" in updates and not updates["title"].strip():
        raise ValueError("title must be a non-blank string")
    if "description" in updates and not updates["description"].strip():
        raise ValueError("description must be a non-blank string")
    for field in ("acceptance_criteria", "dependencies", "files_to_modify"):
        if field in updates and (
            not isinstance(updates[field], list)
            or not all(isinstance(item, str) for item in updates[field])
        ):
            raise ValueError(f"{field} must be a list of strings")
    if "retries_left" in updates and updates["retries_left"] is not None and (
        not isinstance(updates["retries_left"], int)
        or isinstance(updates["retries_left"], bool)
        or updates["retries_left"] < 0
    ):
        raise ValueError("retries_left must be a non-negative integer or null")
    if "run_at" in updates and updates["run_at"] is not None and not isinstance(
        updates["run_at"], str
    ):
        raise ValueError("run_at must be an ISO-8601 datetime string or null")


def _join_output_logs(result: ExecutionResult, cap: int | None = None) -> str | None:
    """The agent's output as one newline-joined string, ``None`` when empty.

    ``BacklogStore`` persists agent output as a single string field, so the
    agent's ``list[str]`` (its ``[stdout]``/``[stderr]``-prefixed lines) is
    flattened here, prefixes stripped. An empty result means "nothing to
    record" and must stay ``None`` — the caller then leaves any previously
    stored response untouched rather than wiping it. ``cap`` bounds the number
    of kept lines, mirroring ``agent_response_lines`` (``None`` = unbounded;
    ``0``/negative = persist nothing).
    """

    prefixes = ("[stdout]", "[stderr]")
    out = [
        line[len(prefix) + 1 :]
        for line in result.output_logs
        for prefix in prefixes
        if line.startswith(prefix)
    ]
    if cap is not None:
        if cap <= 0:
            return None
        out = out[-cap:]
    return "\n".join(out) if out else None


def unsatisfied_dependencies(tasks: list[Task], task: Task) -> list[dict[str, str]]:
    """The dependencies of ``task`` that are not yet satisfied.

    A dependency is satisfied only when a task with that id exists in the
    backlog and its status is ``COMPLETED``. Each returned entry carries the
    dependency id and its current status (``missing`` when no task with that
    id exists), in ``task.dependencies`` order.
    """
    status_by_id = {t.id: t.status for t in tasks}
    unmet: list[dict[str, str]] = []
    for dep_id in task.dependencies:
        status = status_by_id.get(dep_id)
        if status is TaskStatus.COMPLETED:
            continue
        unmet.append(
            {
                "id": dep_id,
                "status": status.value if status is not None else "missing",
            }
        )
    return unmet


def _runnable_open_tasks(tasks: list[Task]) -> list[Task]:
    """The OPEN tasks whose dependencies are all COMPLETED.

    A task is only runnable when every id in its ``dependencies`` exists and
    is ``COMPLETED``; tasks referencing missing or still-pending tasks are
    skipped. Tasks without dependencies are always runnable when OPEN.
    """
    status_by_id = {t.id: t.status for t in tasks}
    return [
        task
        for task in tasks
        if task.status is TaskStatus.OPEN
        and all(
            status_by_id.get(dep_id) is TaskStatus.COMPLETED
            for dep_id in task.dependencies
        )
    ]


def oldest_open_task(tasks: list[Task], *, now: datetime | None = None) -> Task | None:
    """Return the runnable OPEN task Forgeo should pick next.

    The default is the oldest ``created_at`` task whose dependencies are all
    ``COMPLETED``, exactly as before. An optional one-shot ``run_at`` changes
    the order:

    * A runnable OPEN task whose ``run_at`` is in the past (or equal to
      ``now``) is picked *before* every task without ``run_at`` — the
      "run this after deploy" case. Among due tasks the one with the earliest
      ``run_at`` (most overdue) is picked first, ties broken by ``created_at``.
    * A runnable OPEN task whose ``run_at`` is in the future is skipped until
      that moment arrives, so it never displaces an already-eligible task.

    Returns ``None`` when no runnable OPEN task exists (e.g. an empty backlog,
    a cycle of tasks all waiting on each other, or only future-``run_at``
    tasks).
    """
    if now is None:
        now = datetime.now(UTC)
    runnable = _runnable_open_tasks(tasks)
    due = [task for task in runnable if task.run_at is not None and task.run_at <= now]
    if due:
        return min(due, key=lambda task: (task.run_at, task.created_at))
    normal = [task for task in runnable if task.run_at is None]
    if not normal:
        return None
    return min(normal, key=lambda task: task.created_at)


def next_due_run_at(tasks: list[Task], *, now: datetime | None = None) -> datetime | None:
    """The earliest ``run_at`` among runnable OPEN tasks, or ``None``.

    The daemon uses this to wake early: when a scheduled task's ``run_at`` is
    sooner than the next interval (or already in the past), it sleeps only
    until that moment instead of the full interval, so a one-shot task fires
    promptly rather than waiting for the next scheduled pick. Tasks that are
    not runnable (not ``OPEN``, or blocked on an uncompleted dependency) and
    tasks without ``run_at`` are ignored.
    """
    run_at_times = [
        task.run_at
        for task in _runnable_open_tasks(tasks)
        if task.run_at is not None
    ]
    if not run_at_times:
        return None
    return min(run_at_times)


def backlog_status_counts(tasks: list[Task]) -> dict[str, int]:
    """Count tasks by status; always includes every known status key."""
    counts = {status.value: 0 for status in TaskStatus}
    counts.update(Counter(task.status.value for task in tasks))
    return counts


def snapshot_paths_for(
    path: str | Path, *, count: int = DEFAULT_SNAPSHOT_COUNT
) -> list[Path]:
    """The rotating snapshot paths for ``path``, newest first.

    The newest snapshot lives at ``<name>.bak``; older snapshots gain an
    index, ``<name>.bak.1``, ``<name>.bak.2``, ... up to ``<name>.bak.{count-1}``.
    A ``count`` of ``0`` disables snapshotting entirely.
    """
    base = Path(path)
    return [
        base.with_name(
            base.name + ".bak" if index == 0 else f"{base.name}.bak.{index}"
        )
        for index in range(max(0, count))
    ]


class BacklogUnavailableError(RuntimeError):
    """The backlog document could not be retrieved or stored.

    Raised by a storage backend that cannot reach the backlog at all, which
    is categorically different from a backlog that holds no tasks: callers
    must never let the two look the same.
    """


def normalize_store(data: Any) -> dict[str, Any]:
    """Coerce a decoded backlog document into the internal store shape.

    Deliberately forgiving about *shape* (anything that is not an object with
    a list of tasks reads as an empty backlog) and silent about it, because
    the document is hand-editable. It says nothing about whether the document
    could be *retrieved*: a storage backend must raise for that, so an
    unreachable backlog is never mistaken for an empty one.
    """
    if not isinstance(data, dict):
        return {"tasks": []}
    tasks = data.get("tasks")
    return {"tasks": tasks if isinstance(tasks, list) else []}


class BacklogStore(ABC):
    """Abstract task backlog.

    Holds the shared lock, output cap, and the task-level contract.
    Document and issue providers subclass via :class:`DocumentBacklogStore`
    and :class:`IssueBacklogBase`.
    """

    def __init__(self, *, output_cap: int | None = None) -> None:
        self._lock = asyncio.Lock()
        self._output_cap = output_cap

    @abstractmethod
    async def list_tasks(self) -> list[Task]:
        """Return all tasks, in the order they were created."""

    @abstractmethod
    async def get_task(self, task_id: str) -> Task | None:
        """Return a task by id, or ``None`` if it does not exist."""

    async def claim_task(self, task: Task) -> Task | None:
        """Claim ``task`` immediately before an agent starts working on it.

        A local document is protected by Forgeo's per-instance run lock, so a
        claim only needs to re-check that the task is still ``OPEN``. Remote
        providers override this with an atomic or workflow-backed claim.
        Returning ``None`` means another worker won the race.
        """
        # default: no remote claim state
        tasks = await self.list_tasks()
        for t in tasks:
            if t.id == task.id and t.status is TaskStatus.OPEN:
                return t
        return None

    async def recover_claims(self) -> None:
        """Recover stale remote claims before a new cycle starts."""

    async def validate_connection(self) -> None:
        """Verify that this provider can be read without mutating it."""
        await self.list_tasks()

    async def snapshot(self) -> None:
        """Take a rollback copy before the backlog is written to."""

    @abstractmethod
    async def create_task(self, task: Task) -> Task:
        """Persist ``task``, rejecting duplicate ids."""

    @abstractmethod
    async def update_status(
        self, task_id: str, status: TaskStatus, result: ExecutionResult
    ) -> Task | None:
        """Transition a task's status."""

    @abstractmethod
    async def set_blocked(
        self, task_id: str, reason: list[str], result: ExecutionResult
    ) -> Task | None:
        """Mark a task ``BLOCKED``."""

    @abstractmethod
    async def set_failed(
        self, task_id: str, reason: list[str], result: ExecutionResult
    ) -> Task | None:
        """Mark a task ``FAILED``."""

    @abstractmethod
    async def bump_failed_wait(self, task_id: str) -> Task | None:
        """Increment a retry-eligible ``FAILED`` task's wait counter."""

    @abstractmethod
    async def retry_task(self, task_id: str) -> Task | None:
        """Move a retry-eligible ``FAILED`` task back to ``OPEN``."""

    @abstractmethod
    async def reopen_task(self, task_id: str) -> Task | None:
        """Reopen a blocked task."""

    @abstractmethod
    async def delete_task(self, task_id: str) -> Task | None:
        """Remove a task from the backlog."""

    @abstractmethod
    async def update_task(
        self, task_id: str, updates: dict[str, Any]
    ) -> Task | None:
        """Update a task's editable fields."""

    @staticmethod
    def _to_task(entry: dict[str, Any]) -> Task:
        """Validate a stored dictionary back into a Task."""
        try:
            return Task.model_validate(entry)
        except ValidationError:
            return Task(
                id=str(entry.get("id", "<unknown>")),
                title="<unparsable task>",
                description="<unparsable task>",
                status=TaskStatus.FAILED,
            )


class DocumentBacklogStore(BacklogStore):
    """A backlog persisted as a whole document (file or HTTP).

    Implements task operations via serialized whole-document reads/writes.
    """

    @abstractmethod
    async def _read(self) -> dict[str, Any]:
        """Load the whole document as ``{"tasks": [...]}``."""

    @abstractmethod
    async def _write(self, store: dict[str, Any]) -> None:
        """Persist the whole document."""

    async def list_tasks(self) -> list[Task]:
        store = await self._read()
        return [self._to_task(entry) for entry in store["tasks"]]

    async def claim_task(self, task: Task) -> Task | None:
        async with self._lock:
            store = await self._read()
            entry = self._entry_by_id(store, task.id)
            if entry is None:
                return None
            current = self._to_task(entry)
            if current.status is not TaskStatus.OPEN:
                return None
            return current

    async def get_task(self, task_id: str) -> Task | None:
        store = await self._read()
        entry = self._entry_by_id(store, task_id)
        return self._to_task(entry) if entry is not None else None

    async def create_task(self, task: Task) -> Task:
        async with self._lock:
            store = await self._read()
            if self._entry_by_id(store, task.id) is not None:
                raise ValueError(f"Task id already exists in backlog: {task.id!r}")
            store["tasks"].append(task.model_dump(mode="json"))
            await self._write(store)
        return task

    async def update_status(
        self, task_id: str, status: TaskStatus, result: ExecutionResult
    ) -> Task | None:
        def mutate(entry: dict[str, Any]) -> None:
            leaving_failed = (
                entry.get("status") == TaskStatus.FAILED.value
                and status is not TaskStatus.FAILED
            )
            entry["status"] = status.value
            joined = _join_output_logs(result, self._output_cap)
            if joined is not None:
                entry["agent_response"] = joined
            if status is not TaskStatus.FAILED:
                entry["failure_reason"] = []
                if leaving_failed:
                    entry["retry_count"] = 0
                    entry["failed_wait_cycles"] = 0

        return await self._update_entry(task_id, mutate)

    async def set_blocked(
        self, task_id: str, reason: list[str], result: ExecutionResult
    ) -> Task | None:
        def mutate(entry: dict[str, Any]) -> None:
            entry["status"] = TaskStatus.BLOCKED.value
            entry["blocker_reason"] = list(reason)
            entry["blocked_count"] = int(entry.get("blocked_count", 0)) + 1
            entry["failure_reason"] = []
            joined = _join_output_logs(result, self._output_cap)
            if joined is not None:
                entry["agent_response"] = joined

        return await self._update_entry(task_id, mutate)

    async def set_failed(
        self, task_id: str, reason: list[str], result: ExecutionResult
    ) -> Task | None:
        def mutate(entry: dict[str, Any]) -> None:
            entry["status"] = TaskStatus.FAILED.value
            entry["failure_reason"] = list(reason)
            entry["failed_wait_cycles"] = 0
            joined = _join_output_logs(result, self._output_cap)
            if joined is not None:
                entry["agent_response"] = joined

        return await self._update_entry(task_id, mutate)

    async def bump_failed_wait(self, task_id: str) -> Task | None:
        def mutate(entry: dict[str, Any]) -> None:
            entry["failed_wait_cycles"] = int(entry.get("failed_wait_cycles", 0)) + 1

        return await self._update_entry(task_id, mutate)

    async def retry_task(self, task_id: str) -> Task | None:
        def mutate(entry: dict[str, Any]) -> None:
            entry["status"] = TaskStatus.OPEN.value
            entry["retry_count"] = int(entry.get("retry_count", 0)) + 1
            entry["failed_wait_cycles"] = 0
            entry["failure_reason"] = []

        return await self._update_entry(task_id, mutate)

    async def reopen_task(self, task_id: str) -> Task | None:
        def mutate(entry: dict[str, Any]) -> None:
            entry["status"] = TaskStatus.OPEN.value
            entry["blocker_reason"] = []
            entry["failure_reason"] = []

        return await self._update_entry(task_id, mutate)

    async def delete_task(self, task_id: str) -> Task | None:
        async with self._lock:
            store = await self._read()
            entry = self._entry_by_id(store, task_id)
            if entry is None:
                return None
            store["tasks"] = [
                task for task in store["tasks"] if task["id"] != task_id
            ]
            deleted = self._to_task(entry)
            await self._write(store)
        return deleted

    async def update_task(
        self, task_id: str, updates: dict[str, Any]
    ) -> Task | None:
        if not isinstance(updates, dict):
            raise TypeError("updates must be a dict of task fields")
        validate_task_updates(updates)

        async with self._lock:
            store = await self._read()
            entry = self._entry_by_id(store, task_id)
            if entry is None:
                return None
            candidate = dict(entry)
            candidate.update(updates)
            candidate["updated_at"] = datetime.now(UTC).isoformat()
            try:
                task = Task.model_validate(candidate)
            except ValidationError as exc:
                raise ValueError(f"invalid task field(s): {exc}") from exc
            normalized = task.model_dump(mode="json")
            for field in updates:
                entry[field] = normalized[field]
            entry["updated_at"] = normalized["updated_at"]
            await self._write(store)
            return task

    async def _update_entry(
        self, task_id: str, mutate: Callable[[dict[str, Any]], None]
    ) -> Task | None:
        async with self._lock:
            store = await self._read()
            entry = self._entry_by_id(store, task_id)
            if entry is None:
                return None
            mutate(entry)
            entry["updated_at"] = datetime.now(UTC).isoformat()
            updated = self._to_task(entry)
            await self._write(store)
        return updated

    @staticmethod
    def _entry_by_id(store: dict[str, Any], task_id: str) -> Any:
        for entry in store["tasks"]:
            if entry["id"] == task_id:
                return entry
        return None


class IssueBacklogBase(BacklogStore):
    """Base for issue-level providers (Jira, GitHub, GitLab).

    Issue providers store engine state via an abstract interface so that Jira
    (issue property) and GitHub/GitLab (hidden body marker) can share the same
    retry/blocked lease logic.
    """

    @abstractmethod
    async def get_engine_state(self, issue_id: str) -> dict[str, Any]:
        """Return engine-managed state for ``issue_id`` (may be empty)."""

    @abstractmethod
    async def put_engine_state(self, issue_id: str, state: dict[str, Any]) -> None:
        """Persist engine-managed state for ``issue_id``."""


class JSONBacklog(DocumentBacklogStore):
    """A backlog stored in a single JSON document on disk."""

    def __init__(
        self,
        path: str | Path,
        *,
        snapshot_count: int = DEFAULT_SNAPSHOT_COUNT,
        output_cap: int | None = None,
    ) -> None:
        super().__init__(output_cap=output_cap)
        self.path = Path(path)
        self.snapshot_count = max(0, snapshot_count)

    @property
    def snapshot_paths(self) -> list[Path]:
        """The rotating snapshot paths for this backlog, newest (``.bak``) first."""
        return snapshot_paths_for(self.path, count=self.snapshot_count)

    async def snapshot(self) -> None:
        """Copy the current store to a rotating snapshot (``backlog.json.bak``)."""
        if not self.path.exists() or self.snapshot_count <= 0:
            return
        async with self._lock:
            try:
                store = await self._read()
                self._rotate_snapshots()
                self._write_snapshot(store)
                logger.info("Backlog snapshot written to %s", self.snapshot_paths[0])
            except OSError as exc:
                logger.warning("Could not snapshot backlog at %s: %s", self.path, exc)

    async def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"tasks": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            restored = await self._restore_from_snapshot()
            if restored is not None:
                return restored
            if data is None:
                self._preserve_corrupt_file()
            return {"tasks": []}
        return {"tasks": data["tasks"]}

    async def _restore_from_snapshot(self) -> dict[str, Any] | None:
        for snapshot in self.snapshot_paths:
            if not snapshot.is_file():
                continue
            try:
                data = json.loads(snapshot.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
                continue
            store = {"tasks": data["tasks"]}
            self._preserve_corrupt_file()
            try:
                self._write_store(self.path, store)
            except OSError as exc:
                logger.warning(
                    "Restored backlog could not be persisted to %s: %s", self.path, exc
                )
            logger.warning(
                "Corrupt backlog at %s restored from snapshot %s", self.path, snapshot
            )
            return store
        return None

    def _preserve_corrupt_file(self) -> None:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        corrupt_path = self.path.with_name(f"{self.path.name}.corrupt-{timestamp}")
        try:
            self.path.rename(corrupt_path)
            logger.warning("Corrupt backlog at %s renamed to %s", self.path, corrupt_path)
        except OSError:
            logger.warning("Corrupt backlog at %s could not be preserved", self.path)

    def _rotate_snapshots(self) -> None:
        paths = self.snapshot_paths
        for index in range(len(paths) - 1, 0, -1):
            if paths[index - 1].exists():
                os.replace(paths[index - 1], paths[index])

    def _write_snapshot(self, store: dict[str, Any]) -> None:
        self._write_store(self.snapshot_paths[0], store)

    async def _write(self, store: dict[str, Any]) -> None:
        self._write_store(self.path, store)

    @staticmethod
    def _write_store(path: Path, store: dict[str, Any]) -> None:
        atomic_write_text(
            path,
            json.dumps(store, indent=2, ensure_ascii=False) + "\n",
        )


def _provider_factory(config: ForgeoConfig) -> BacklogStore:
    provider = config.effective_backlog_provider
    if provider == "jira":
        from forgeo.backlog_jira import JiraBacklog

        assert config.jira is not None
        return JiraBacklog(str(config.backlog), config.jira, output_cap=config.agent_response_lines)
    if provider == "github":
        from forgeo.backlog_github import GithubBacklog

        assert config.github is not None
        return GithubBacklog(str(config.backlog), config.github, output_cap=config.agent_response_lines)
    if provider == "gitlab":
        from forgeo.backlog_gitlab import GitlabBacklog

        assert config.gitlab is not None
        return GitlabBacklog(str(config.backlog), config.gitlab, output_cap=config.agent_response_lines)
    if provider == "http":
        from forgeo.backlog_http import HttpBacklog

        return HttpBacklog(
            str(config.backlog), auth=config.backlog_auth, output_cap=config.agent_response_lines
        )
    return JSONBacklog(Path(config.backlog), output_cap=config.agent_response_lines)


def open_backlog(config: ForgeoConfig) -> BacklogStore:
    """The task provider selected by ``config`` via registry."""
    return _provider_factory(config)
