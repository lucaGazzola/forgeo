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
    apply_terminal_transition,
    build_task,
    claim_cutoff,
    embed_engine_state,
    extract_engine_state,
    extract_issue_labels,
    extract_issue_number,
    is_claim_stale,
    parse_datetime,
    parse_numeric_issue_id,
    parse_optional_datetime,
    task_engine_state,
    transition_label_update,
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
    client_cls: Any = None
    # ``update_issue`` payloads closing/reopening an issue, e.g.
    # ``{"state": "closed"}`` (GitHub) or ``{"state_event": "close"}``
    # (GitLab). Subclasses set both; the shared ``_close_issue`` /
    # ``_reopen_issue`` below send a copy.
    close_update: dict[str, Any] | None = None
    reopen_update: dict[str, Any] | None = None

    def __init__(self, url: str, config: Any, *, output_cap: int | None = None, client: Any = None) -> None:
        super().__init__(output_cap=output_cap)
        self.url = url.rstrip("/")
        self.config = config
        if client is None and self.client_cls is not None:
            client = self.client_cls(url, config)
        self.client = client
        self._pending_comments: list[tuple[Any, str]] = []

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.url!r})"

    # --- provider primitives (override in subclasses) ---

    def _body_of(self, issue: dict[str, Any]) -> str:
        body = issue.get(self.body_key)
        return body if isinstance(body, str) else ""

    async def _fetch_issue(self, numeric_id: int) -> Any:
        return await self._call(self.client.get_issue, numeric_id)

    async def _search_page(self, *, page: int, per_page: int) -> Any:
        # ``state="all"`` lists open and closed issues; the GitLab client
        # accepts (and ignores) it so both providers share this method.
        return await self._call(self.client.search_issues, page=page, per_page=per_page, state="all")

    async def _apply_update(self, numeric_id: int, fields: dict[str, Any]) -> Any:
        return await self._call(self.client.update_issue, numeric_id, fields)

    async def _post_comment(self, numeric_id: int, body: str) -> None:
        await self._call(self.client.add_comment, numeric_id, body)

    async def _delete_issue(self, numeric_id: int) -> None:
        await self._call(self.client.delete_issue, numeric_id)

    async def _close_issue(self, numeric_id: int) -> None:
        if self.close_update is None:
            raise NotImplementedError
        await self._call(self.client.update_issue, numeric_id, dict(self.close_update))

    def _created_id(self, created: Any) -> str | None:
        number = extract_issue_number(created if isinstance(created, dict) else {})
        return str(number) if number is not None else None

    def _create_fields(self, task: Task, engine: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": task.title,
            self.body_key: embed_engine_state(task.description, engine),
            "labels": [self.config.label_prefix],
        }

    def _update_body_field(self, candidate: Task, state: dict[str, Any]) -> dict[str, Any]:
        return {self.body_key: embed_engine_state(candidate.description, state)}

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
            fetched = await self._locked_issue_task(task.id, require_status=TaskStatus.OPEN)
            if fetched is None:
                return None
            issue, current = fetched
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

    async def _to_open(self, issue: dict[str, Any], task_id: str) -> None:
        """Reopen ``task_id`` (marker providers share one open state)."""
        del issue
        await self._transition_state(task_id, self.open_state)

    async def _to_closed(self, issue: dict[str, Any], task_id: str) -> None:
        """Close ``task_id`` (marker providers share one closed state)."""
        del issue
        await self._transition_state(task_id, self.close_state)

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
        if self.reopen_update is None:
            raise NotImplementedError
        await self._call(self.client.update_issue, numeric_id, dict(self.reopen_update))

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
        apply_terminal_transition(state, status, reason)
        add, remove = transition_label_update(status, self._labels)
        if status is TaskStatus.COMPLETED:
            await self._transition_state(issue_id, self.close_state)
        elif status is TaskStatus.OPEN:
            await self._transition_state(issue_id, self.open_state)
        await self._update_labels(issue_id, add=add, remove=remove)
        await self.put_engine_state(issue_id, state)
        if status is TaskStatus.BLOCKED:
            self._comment(number, "BLOCKED", reason or [])
        elif status not in (TaskStatus.COMPLETED, TaskStatus.OPEN):
            self._comment(number, "FAILED", reason or [])
        return await self.get_task(issue_id)

    async def _flush_comments(self) -> None:
        comments = self._take_pending_comments()
        error_cls: Any = self.request_error_cls
        for numeric_id, body in comments:
            try:
                await self._post_comment(numeric_id, body)
            except error_cls as exc:
                logger.warning("Could not add %s comment to %s: %s", self.provider_label, numeric_id, exc)

    async def delete_task(self, task_id: str) -> Task | None:
        async with self._lock:
            fetched = await self._locked_issue_task(task_id)
            if fetched is None:
                return None
            issue, task = fetched
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
            fetched = await self._locked_issue_task(task_id)
            if fetched is None:
                return None
            issue, current = fetched
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
