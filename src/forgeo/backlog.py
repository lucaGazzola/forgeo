"""The backlog: a single human-readable JSON file of tasks.

Forgeo pulls the oldest ``OPEN`` task whose dependencies are all ``COMPLETED``
from here. Edit this file directly to add, remove, or reopen tasks (e.g. set a
``BLOCKED`` task back to ``OPEN`` once the human input has been provided).
Writes are atomic and serialized through an asyncio lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from forgeo.io import atomic_write_text
from forgeo.models import Task, TaskStatus

logger = logging.getLogger(__name__)

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
    }
)


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


def oldest_open_task(tasks: list[Task]) -> Task | None:
    """Return the oldest OPEN task whose dependencies are all COMPLETED.

    A task is only runnable when every id in its ``dependencies`` exists and
    is ``COMPLETED``; tasks referencing missing or still-pending tasks are
    skipped. Tasks without dependencies behave exactly as before. Returns
    ``None`` when no runnable OPEN task exists (e.g. an empty backlog, or a
    cycle of tasks all waiting on each other).
    """
    status_by_id = {t.id: t.status for t in tasks}
    open_tasks = [
        task
        for task in tasks
        if task.status is TaskStatus.OPEN
        and all(
            status_by_id.get(dep_id) is TaskStatus.COMPLETED
            for dep_id in task.dependencies
        )
    ]
    if not open_tasks:
        return None
    return min(open_tasks, key=lambda task: task.created_at)


def backlog_status_counts(tasks: list[Task]) -> dict[str, int]:
    """Count tasks by status; always includes every known status key."""
    counts = {status.value: 0 for status in TaskStatus}
    counts.update(Counter(task.status.value for task in tasks))
    return counts


class JSONBacklog:
    """A backlog stored in a single JSON document on disk."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def list_tasks(self) -> list[Task]:
        """Return all tasks, in the order they were created."""
        store = await self._read()
        return [self._to_task(entry) for entry in store["tasks"]]

    async def fetch_next_task(self) -> Task | None:
        """Return the oldest OPEN task whose dependencies are all COMPLETED,
        or ``None`` when nothing is runnable."""
        return oldest_open_task(await self.list_tasks())

    async def get_task(self, task_id: str) -> Task | None:
        """Return a task by id, or ``None`` if it does not exist."""
        store = await self._read()
        entry = self._entry_by_id(store, task_id)
        return self._to_task(entry) if entry is not None else None

    async def create_task(self, task: Task) -> Task:
        """Persist ``task``, rejecting duplicate ids with a ``ValueError``."""
        async with self._lock:
            store = await self._read()
            if self._entry_by_id(store, task.id) is not None:
                raise ValueError(f"Task id already exists in backlog: {task.id!r}")
            store["tasks"].append(task.model_dump(mode="json"))
            await self._write(store)
        return task

    async def update_status(self, task_id: str, status: TaskStatus) -> Task | None:
        """Transition a task's status, bumping its ``updated_at`` timestamp.

        Any transition away from ``FAILED`` clears the persisted
        ``failure_reason``, so a task that stops being failed no longer shows
        the stale reason.
        """
        def mutate(entry: dict[str, Any]) -> None:
            entry["status"] = status.value
            if status is not TaskStatus.FAILED:
                entry["failure_reason"] = []

        return await self._update_entry(task_id, mutate)

    async def set_blocked(self, task_id: str, reason: list[str]) -> Task | None:
        """Mark a task ``BLOCKED``, persisting the agent's blocker reason.

        ``reason`` is stored on the task as ``blocker_reason`` (the source
        ``BLOCKER.md`` is derived from) and ``blocked_count`` is incremented
        every time a task transitions into ``BLOCKED``. An unknown ``task_id``
        returns ``None`` without writing anything.
        """
        def mutate(entry: dict[str, Any]) -> None:
            entry["status"] = TaskStatus.BLOCKED.value
            entry["blocker_reason"] = list(reason)
            entry["blocked_count"] = int(entry.get("blocked_count", 0)) + 1
            entry["failure_reason"] = []

        return await self._update_entry(task_id, mutate)

    async def set_failed(self, task_id: str, reason: list[str]) -> Task | None:
        """Mark a task ``FAILED``, persisting the failure reason.

        ``reason`` is stored on the task as ``failure_reason`` (shown in the
        web console's task modal) every time a task transitions into
        ``FAILED``. An unknown ``task_id`` returns ``None`` without writing
        anything.
        """
        def mutate(entry: dict[str, Any]) -> None:
            entry["status"] = TaskStatus.FAILED.value
            entry["failure_reason"] = list(reason)

        return await self._update_entry(task_id, mutate)

    async def reopen_task(self, task_id: str) -> Task | None:
        """Reopen a blocked task: status back to ``OPEN``, reason cleared.

        ``blocked_count`` is kept as history (the human should see how often
        the task blocked before deciding to retry, split, or drop it). An
        unknown ``task_id`` returns ``None`` without writing anything.
        """
        def mutate(entry: dict[str, Any]) -> None:
            entry["status"] = TaskStatus.OPEN.value
            entry["blocker_reason"] = []
            entry["failure_reason"] = []

        return await self._update_entry(task_id, mutate)

    async def delete_task(self, task_id: str) -> Task | None:
        """Remove a task from the backlog, returning the deleted task.

        An unknown ``task_id`` returns ``None`` without writing anything.
        Any other callers wishing to restrict deletion (e.g. only ``OPEN``
        tasks) must check the status before calling; this method deletes
        whatever is there. Writes go through the same lock and atomic-replace
        path as the other mutators.
        """
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
        """Update a task's editable fields, bumping its ``updated_at``.

        ``updates`` may contain any of :data:`EDITABLE_TASK_FIELDS`; ``id``,
        ``status``, ``created_at`` and ``updated_at`` are never replaced
        (except ``updated_at``, bumped to now). Unknown fields and invalid
        values raise a ``ValueError``; an unknown ``task_id`` returns ``None``
        (mirroring :meth:`update_status`). Writes go through the same lock
        and atomic-replace path as the other mutators.
        """
        if not isinstance(updates, dict):
            raise TypeError("updates must be a dict of task fields")
        unknown = set(updates) - EDITABLE_TASK_FIELDS
        if unknown:
            raise ValueError(
                f"unknown task field(s): {', '.join(sorted(unknown))}"
            )
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

    # ------------------------------------------------------------------ #
    # Internal persistence helpers                                        #
    # ------------------------------------------------------------------ #

    async def _update_entry(
        self, task_id: str, mutate: Callable[[dict[str, Any]], None]
    ) -> Task | None:
        """Mutate one stored task under the lock and persist it.

        ``mutate`` receives the stored dict for ``task_id`` and changes it in
        place; ``updated_at`` is bumped after it runs. An unknown ``task_id``
        returns ``None`` without writing anything.
        """
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
        """The stored dict for ``task_id``, or ``None`` when it is absent."""
        for entry in store["tasks"]:
            if entry["id"] == task_id:
                return entry
        return None

    async def _read(self) -> dict[str, Any]:
        """Load the store from disk, tolerating a missing or corrupt file."""
        if not self.path.exists():
            return {"tasks": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            corrupt_path = self.path.with_name(f"{self.path.name}.corrupt-{timestamp}")
            try:
                self.path.rename(corrupt_path)
                logger.warning(
                    "Corrupt backlog at %s renamed to %s; starting with empty store",
                    self.path,
                    corrupt_path,
                )
            except OSError:
                logger.warning(
                    "Corrupt backlog at %s could not be preserved; starting with empty store",
                    self.path,
                )
            return {"tasks": []}
        if not isinstance(data, dict):
            return {"tasks": []}
        tasks = data.get("tasks")
        return {"tasks": tasks if isinstance(tasks, list) else []}

    async def _write(self, store: dict[str, Any]) -> None:
        """Atomically persist the store (temp file + rename)."""
        atomic_write_text(
            self.path,
            json.dumps(store, indent=2, ensure_ascii=False) + "\n",
        )

    @staticmethod
    def _to_task(entry: dict[str, Any]) -> Task:
        """Validate a stored dictionary back into a Task, skipping corrupt rows."""
        try:
            return Task.model_validate(entry)
        except ValidationError:
            return Task(
                id=str(entry.get("id", "<unknown>")),
                title="<unparsable task>",
                description="<unparsable task>",
                status=TaskStatus.FAILED,
            )
