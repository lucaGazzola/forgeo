"""Shared lifecycle for marker-backed issue providers (GitHub, GitLab).

Both providers store engine state in a hidden ``<!-- forgeo: {...} -->`` marker
in the issue body and share the same claim / transition / review lifecycle.
Subclasses only supply provider-specific field names and client calls.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from forgeo.backlog import IssueBacklogBase, _join_output_logs, validate_task_updates
from forgeo.backlog_issue_base import (
    ENGINE_STATE_FIELDS,
    build_task,
    bump_state_counter,
    claim_cutoff,
    embed_engine_state,
    extract_engine_state,
    extract_issue_labels,
    extract_issue_number,
    format_state_comment,
    is_claim_stale,
    next_reopen_state,
    next_retry_state,
    parse_datetime,
    parse_numeric_issue_id,
    parse_optional_datetime,
    task_engine_state,
)
from forgeo.models import ExecutionResult, Task, TaskStatus

logger = logging.getLogger(__name__)


class MarkerIssueBacklog(IssueBacklogBase):
    """Issue backlog storing engine state in the issue body marker."""

    body_key: str = "body"
    open_state: str = "open"
    close_state: str = "closed"
    provider_label: str = "issue"
    request_error_cls: type[Exception] = Exception

    def __init__(self, url: str, config: Any, *, output_cap: int | None = None, client: Any = None) -> None:
        super().__init__(output_cap=output_cap)
        self.url = url.rstrip("/")
        self.config = config
        self.client = client
        self._pending_comments: list[tuple[int, str]] = []

    # --- provider primitives (override in subclasses) ---

    def _body_of(self, issue: dict[str, Any]) -> str:
        body = issue.get(self.body_key)
        return body if isinstance(body, str) else ""

    async def _fetch_issue(self, numeric_id: int) -> Any:
        return await self._call(self.client.get_issue, numeric_id)

    async def _search_page(self, *, page: int, per_page: int) -> Any:
        return await self._call(self.client.search_issues, page=page, per_page=per_page)

    async def _apply_update(self, numeric_id: int, fields: dict[str, Any]) -> Any:
        return await self._call(self.client.update_issue, numeric_id, fields)

    async def _post_comment(self, numeric_id: int, body: str) -> None:
        raise NotImplementedError

    async def _delete_issue(self, numeric_id: int) -> None:
        await self._call(self.client.delete_issue, numeric_id)

    async def _close_issue(self, numeric_id: int) -> None:
        raise NotImplementedError

    def _created_id(self, created: Any) -> str | None:
        raise NotImplementedError

    def _create_fields(self, task: Task, engine: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _update_body_field(self, candidate: Task, state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _not_found(self, issue_id: str) -> Exception:
        return self.request_error_cls(f"{self.provider_label} issue {issue_id} response was not an object")

    # --- shared lifecycle ---

    async def _get_issue(self, issue_id: str) -> dict[str, Any] | None:
        number = parse_numeric_issue_id(issue_id)
        if number is None:
            return None
        try:
            issue = await self._fetch_issue(number)
        except self.request_error_cls as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise
        if not isinstance(issue, dict):
            raise self._not_found(issue_id)
        return issue

    async def _search_all(self) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        page = 1
        while len(issues) < self.config.max_issues:
            remaining = self.config.max_issues - len(issues)
            per_page = min(self.config.page_size, remaining)
            page_issues = await self._search_page(page=page, per_page=per_page)
            if not isinstance(page_issues, list):
                raise self.request_error_cls(f"{self.provider_label} search response did not contain a list")
            if not page_issues:
                break
            issues.extend(item for item in page_issues if isinstance(item, dict))
            if len(page_issues) < per_page:
                break
            page += 1
        return issues[: self.config.max_issues]

    async def get_engine_state(self, issue_id: str) -> dict[str, Any]:
        issue = await self._get_issue(issue_id)
        if issue is None:
            return {}
        state, _ = extract_engine_state(self._body_of(issue))
        return state

    async def put_engine_state(self, issue_id: str, state: dict[str, Any]) -> None:
        issue = await self._get_issue(issue_id)
        if issue is None:
            return
        _, visible = extract_engine_state(self._body_of(issue))
        new_body = embed_engine_state(visible, state)
        number = extract_issue_number(issue)
        if number is None:
            try:
                number = int(issue_id)
            except ValueError:
                return
        await self._apply_update(number, {self.body_key: new_body})

    def _state_from_issue(self, issue: dict[str, Any]) -> TaskStatus | None:
        labels = set(extract_issue_labels(issue))
        cfg = self._labels
        if cfg["blocked"] in labels:
            return TaskStatus.BLOCKED
        if cfg["failed"] in labels:
            return TaskStatus.FAILED
        if cfg["review"] in labels:
            return TaskStatus.REVIEW
        if cfg["running"] in labels:
            return None
        state = issue.get("state")
        if state == "closed":
            return TaskStatus.COMPLETED
        if state in ("open", "opened"):
            return TaskStatus.OPEN
        return None

    async def _task_from_issue(self, issue: dict[str, Any]) -> Task | None:
        number = extract_issue_number(issue)
        if number is None:
            return None
        status = self._state_from_issue(issue)
        if status is None:
            return None
        issue_id = str(number)
        state, visible_body = extract_engine_state(self._body_of(issue))
        return build_task(
            issue_id=issue_id,
            title=issue.get("title"),
            description=visible_body,
            status=status,
            created=parse_datetime(issue.get("created_at")),
            updated=parse_datetime(issue.get("updated_at")),
            run_at=parse_optional_datetime(state.get("run_at")),
            state=state,
        )

    async def claim_task(self, task: Task) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task.id)
            if issue is None:
                return None
            current = await self._task_from_issue(issue)
            if current is None or current.status is not TaskStatus.OPEN:
                return None
            number = extract_issue_number(issue)
            assert number is not None
            labels = extract_issue_labels(issue)
            if self._labels["running"] not in labels:
                await self._update_issue_labels(
                    task.id,
                    add=[self._labels["running"]],
                    remove=[self._labels["blocked"], self._labels["failed"]],
                )
            state = await self.get_engine_state(task.id)
            state.update({"claimed_at": datetime.now(UTC).isoformat()})
            await self.put_engine_state(task.id, state)
            return current

    async def recover_claims(self) -> None:
        issues = await self._search_all()
        cutoff = claim_cutoff(self.config.claim_timeout_seconds)
        for issue in issues:
            labels = extract_issue_labels(issue)
            if self._labels["running"] not in labels:
                continue
            number = extract_issue_number(issue)
            if number is None:
                continue
            issue_id = str(number)
            state = await self.get_engine_state(issue_id)
            if not is_claim_stale(state, issue, cutoff):
                continue
            await self._update_issue_labels(issue_id, add=[], remove=[self._labels["running"]])
            state.pop("claimed_at", None)
            await self.put_engine_state(issue_id, state)
            logger.warning("Recovered stale %s claim for task %s.", self.provider_label, issue_id)

    async def _update_labels(self, issue_id: str, *, add: list[str], remove: list[str]) -> None:
        await self._update_issue_labels(issue_id, add=add, remove=remove)

    async def _transition_state(self, issue_id: str, state: str) -> None:
        issue = await self._get_issue(issue_id)
        if issue is None:
            return
        number = extract_issue_number(issue)
        assert number is not None
        if issue.get("state") == state:
            return
        if state == self.close_state:
            await self._close_issue(number)
        else:
            await self._reopen_issue(number)

    async def _reopen_issue(self, numeric_id: int) -> None:
        raise NotImplementedError

    async def _transition_metadata(
        self,
        issue: dict[str, Any],
        status: TaskStatus,
        result: ExecutionResult,
        *,
        reason: list[str] | None = None,
    ) -> Task | None:
        number = extract_issue_number(issue)
        if number is None:
            return None
        issue_id = str(number)
        state = await self.get_engine_state(issue_id)
        joined = _join_output_logs(result, self._output_cap)
        if joined is not None:
            state["agent_response"] = joined
        state["state"] = status.value
        if status is TaskStatus.COMPLETED:
            state.pop("claimed_at", None)
            state["failure_reason"] = []
            state["blocker_reason"] = []
            await self._transition_state(issue_id, self.close_state)
            await self._update_labels(issue_id, add=[], remove=list(self._labels.values()))
            await self.put_engine_state(issue_id, state)
            return await self.get_task(issue_id)
        if status is TaskStatus.OPEN:
            state["failure_reason"] = []
            state.pop("claimed_at", None)
            await self._transition_state(issue_id, self.open_state)
            await self._update_labels(issue_id, add=[], remove=list(self._labels.values()))
            await self.put_engine_state(issue_id, state)
            return await self.get_task(issue_id)
        if status is TaskStatus.BLOCKED:
            state["blocker_reason"] = list(reason or [])
            bump_state_counter(state, "blocked_count")
            state["failure_reason"] = []
            state.pop("claimed_at", None)
            await self._update_labels(
                issue_id,
                add=[self._labels["blocked"]],
                remove=[self._labels["running"], self._labels["failed"]],
            )
            await self.put_engine_state(issue_id, state)
            self._comment(number, "BLOCKED", reason or [])
            return await self.get_task(issue_id)
        state["failure_reason"] = list(reason or [])
        state["failed_wait_cycles"] = 0
        state.pop("claimed_at", None)
        await self._update_labels(
            issue_id,
            add=[self._labels["failed"]],
            remove=[self._labels["running"], self._labels["blocked"]],
        )
        await self.put_engine_state(issue_id, state)
        self._comment(number, "FAILED", reason or [])
        return await self.get_task(issue_id)

    def _comment(self, numeric_id: int, state: str, reason: list[str]) -> None:
        self._pending_comments.append((numeric_id, format_state_comment(state, reason)))

    async def _flush_comments(self) -> None:
        comments = getattr(self, "_pending_comments", [])
        self._pending_comments = []
        error_cls: Any = self.request_error_cls
        for numeric_id, body in comments:
            try:
                await self._post_comment(numeric_id, body)
            except error_cls as exc:
                logger.warning("Could not add %s comment to %s: %s", self.provider_label, numeric_id, exc)

    async def update_status(self, task_id: str, status: TaskStatus, result: ExecutionResult) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            updated = await self._transition_metadata(issue, status, result)
            await self._flush_comments()
            return updated

    async def set_blocked(self, task_id: str, reason: list[str], result: ExecutionResult) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            updated = await self._transition_metadata(issue, TaskStatus.BLOCKED, result, reason=reason)
            await self._flush_comments()
            return updated

    async def set_failed(self, task_id: str, reason: list[str], result: ExecutionResult) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            updated = await self._transition_metadata(issue, TaskStatus.FAILED, result, reason=reason)
            await self._flush_comments()
            return updated

    async def retry_task(self, task_id: str) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            task = await self._task_from_issue(issue)
            if task is None or task.status is not TaskStatus.FAILED:
                return None
            state = await self.get_engine_state(task_id)
            state["state"] = TaskStatus.OPEN.value
            next_retry_state(state)
            await self._transition_state(task_id, self.open_state)
            await self._update_labels(task_id, add=[], remove=list(self._labels.values()))
            await self.put_engine_state(task_id, state)
            return await self.get_task(task_id)

    async def set_review(self, task_id: str, branch: str, sha: str | None, result: ExecutionResult) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            state = await self.get_engine_state(task_id)
            joined = _join_output_logs(result, self._output_cap)
            if joined is not None:
                state["agent_response"] = joined
            state["state"] = TaskStatus.REVIEW.value
            state["review_branch"] = branch
            if sha is not None:
                state["review_commit_sha"] = sha
            state.pop("claimed_at", None)
            state["failure_reason"] = []
            state["blocker_reason"] = []
            await self._update_labels(
                task_id,
                add=[self._labels["review"]],
                remove=[self._labels["running"], self._labels["blocked"], self._labels["failed"]],
            )
            await self.put_engine_state(task_id, state)
            number = extract_issue_number(issue)
            assert number is not None
            self._comment(number, "REVIEW", [f"branch {branch}" + (f" sha {sha}" if sha else "")])
            await self._flush_comments()
            return await self.get_task(task_id)

    async def complete_review(self, task_id: str) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            task = await self._task_from_issue(issue)
            if task is None or task.status is not TaskStatus.REVIEW:
                return None
            state = await self.get_engine_state(task_id)
            state["state"] = TaskStatus.COMPLETED.value
            state.pop("review_branch", None)
            state.pop("review_commit_sha", None)
            state.pop("claimed_at", None)
            await self._transition_state(task_id, self.close_state)
            await self._update_labels(task_id, add=[], remove=list(self._labels.values()))
            await self.put_engine_state(task_id, state)
            return await self.get_task(task_id)

    async def request_changes(self, task_id: str) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            task = await self._task_from_issue(issue)
            if task is None or task.status is not TaskStatus.REVIEW:
                return None
            state = await self.get_engine_state(task_id)
            state["state"] = TaskStatus.OPEN.value
            state.pop("review_branch", None)
            state.pop("review_commit_sha", None)
            await self._transition_state(task_id, self.open_state)
            await self._update_labels(task_id, add=[], remove=list(self._labels.values()))
            await self.put_engine_state(task_id, state)
            return await self.get_task(task_id)

    async def reopen_task(self, task_id: str) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            task = await self._task_from_issue(issue)
            if task is None or task.status is not TaskStatus.BLOCKED:
                return None
            state = await self.get_engine_state(task_id)
            state["state"] = TaskStatus.OPEN.value
            next_reopen_state(state)
            await self._transition_state(task_id, self.open_state)
            await self._update_labels(task_id, add=[], remove=list(self._labels.values()))
            await self.put_engine_state(task_id, state)
            return await self.get_task(task_id)

    async def delete_task(self, task_id: str) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            task = await self._task_from_issue(issue)
            if task is None:
                return None
            number = extract_issue_number(issue)
            assert number is not None
            try:
                await self._delete_issue(number)
            except self.request_error_cls:
                await self._close_issue(number)
            return task

    async def create_task(self, task: Task) -> Task:
        engine: dict[str, Any] = {"state": TaskStatus.OPEN.value}
        engine.update(task_engine_state(task))
        fields = self._create_fields(task, engine)
        async with self._lock:
            created = await self._call(self.client.create_issue, fields)
            created_id = self._created_id(created)
            if created_id is None:
                raise self.request_error_cls(
                    f"{self.provider_label} create response did not contain an issue id"
                )
            # The full state (including author-set customization) is embedded at
            # creation; do not re-write the marker here.
            result = await self.get_task(created_id)
            if result is None:
                raise self.request_error_cls(
                    f"Created {self.provider_label} issue {created_id} could not be read back"
                )
            return result

    async def update_task(self, task_id: str, updates: dict[str, Any]) -> Task | None:
        if not isinstance(updates, dict):
            raise TypeError("updates must be a dict of task fields")
        validate_task_updates(updates)
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            current = await self._task_from_issue(issue)
            if current is None:
                return None
            candidate = current.model_copy(update=updates)
            try:
                candidate = Task.model_validate(candidate.model_dump(mode="python"))
            except ValidationError as exc:
                raise ValueError(f"invalid task field(s): {exc}") from exc
            number = extract_issue_number(issue)
            assert number is not None
            fields: dict[str, Any] = {}
            if "title" in updates:
                fields["title"] = candidate.title
            if not ENGINE_STATE_FIELDS.isdisjoint(updates):
                state = await self.get_engine_state(task_id)
                state.update(task_engine_state(candidate))
                fields.update(self._update_body_field(candidate, state))
            if fields:
                await self._apply_update(number, fields)
            return await self.get_task(task_id)
