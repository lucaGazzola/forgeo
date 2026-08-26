"""GitLab-backed task provider."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from pydantic import ValidationError

from forgeo.backlog import (
    BacklogUnavailableError,
    IssueBacklogBase,
    _join_output_logs,
    validate_task_updates,
)
from forgeo.backlog_issue_base import (
    as_nonnegative_int,
    as_optional_float,
    as_optional_int,
    as_string_list,
    bump_state_counter,
    embed_engine_state,
    extract_engine_state,
    extract_issue_labels,
    extract_issue_number,
    forgeo_labels,
    format_state_comment,
    next_reopen_state,
    next_retry_state,
    parse_datetime,
    parse_numeric_issue_id,
    parse_optional_datetime,
)
from forgeo.models import ExecutionResult, GitlabBacklogConfig, Task, TaskStatus

logger = logging.getLogger(__name__)


class GitlabRequestError(BacklogUnavailableError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitlabClient:
    """Blocking GitLab REST client via asyncio.to_thread."""

    def __init__(self, base_url: str, config: GitlabBacklogConfig) -> None:
        self.base_url = base_url.rstrip("/")
        self.config = config

    def _auth_headers(self) -> dict[str, str]:
        token = os.environ.get(self.config.auth.token_env)
        if not token:
            raise GitlabRequestError(
                f"GitLab token environment variable {self.config.auth.token_env!r} is not set"
            )
        # GitLab prefers PRIVATE-TOKEN, but also accepts Bearer
        return {"PRIVATE-TOKEN": token, "Authorization": f"Bearer {token}"}

    def _project_path(self) -> str:
        # GitLab API expects URL-encoded project path or numeric id
        return quote(self.config.repo, safe="")

    def _api_url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url}/api/v4{path}"
        if query:
            url += "?" + urlencode(query, doseq=True)
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            **self._auth_headers(),
            **({"Content-Type": "application/json"} if body is not None else {}),
        }
        request = urllib.request.Request(
            self._api_url(path, query), data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except OSError:
                detail = ""
            suffix = f" {detail[:500]}" if detail else ""
            raise GitlabRequestError(
                f"{method} {request.full_url} failed with HTTP {exc.code} {exc.reason}.{suffix}",
                status=exc.code,
            ) from exc
        except OSError as exc:
            raise GitlabRequestError(f"{method} {request.full_url} failed: {exc}") from exc
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GitlabRequestError(
                f"{method} {request.full_url} returned a body that is not JSON: {exc}"
            ) from exc
        return data

    def search_issues(self, *, page: int = 1, per_page: int = 20) -> list[dict[str, Any]]:
        path = f"/projects/{self._project_path()}/issues"
        query: dict[str, Any] = {"per_page": per_page, "page": page, "scope": "all", "state": "all"}
        data = self._request("GET", path, query=query)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    def get_issue(self, iid: int) -> dict[str, Any]:
        path = f"/projects/{self._project_path()}/issues/{iid}"
        return self._request("GET", path)  # type: ignore[no-any-return]

    def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        path = f"/projects/{self._project_path()}/issues"
        return self._request("POST", path, payload=fields)  # type: ignore[no-any-return]

    def update_issue(self, iid: int, fields: dict[str, Any]) -> dict[str, Any]:
        path = f"/projects/{self._project_path()}/issues/{iid}"
        return self._request("PUT", path, payload=fields)  # type: ignore[no-any-return]

    def add_note(self, iid: int, body: str) -> None:
        path = f"/projects/{self._project_path()}/issues/{iid}/notes"
        self._request("POST", path, payload={"body": body})

    def delete_issue(self, iid: int) -> None:
        path = f"/projects/{self._project_path()}/issues/{iid}"
        self._request("DELETE", path)


class GitlabBacklog(IssueBacklogBase):
    """Task provider backed by GitLab issues."""

    def __init__(
        self,
        url: str,
        config: GitlabBacklogConfig,
        *,
        output_cap: int | None = None,
        client: GitlabClient | None = None,
    ) -> None:
        super().__init__(output_cap=output_cap)
        self.url = url.rstrip("/")
        self.config = config
        self.client = client or GitlabClient(self.url, config)
        self._pending_comments: list[tuple[int, str]] = []

    def __repr__(self) -> str:
        return f"GitlabBacklog({self.url!r})"

    @property
    def _labels(self) -> dict[str, str]:
        return forgeo_labels(self.config.label_prefix)

    async def _call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(function, *args, **kwargs)

    async def _get_issue(self, issue_id: str) -> dict[str, Any] | None:
        iid = parse_numeric_issue_id(issue_id)
        if iid is None:
            return None
        try:
            issue = await self._call(self.client.get_issue, iid)
        except GitlabRequestError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(issue, dict):
            raise GitlabRequestError(f"GitLab issue {issue_id} response was not an object")
        return issue

    async def _search_all(self) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        page = 1
        while len(issues) < self.config.max_issues:
            remaining = self.config.max_issues - len(issues)
            per_page = min(self.config.page_size, remaining)
            page_issues = await self._call(self.client.search_issues, page=page, per_page=per_page)
            if not isinstance(page_issues, list):
                raise GitlabRequestError("GitLab search response did not contain a list")
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
        body = issue.get("description") if isinstance(issue.get("description"), str) else ""
        state, _ = extract_engine_state(body)
        return state

    async def put_engine_state(self, issue_id: str, state: dict[str, Any]) -> None:
        issue = await self._get_issue(issue_id)
        if issue is None:
            return
        body = issue.get("description") if isinstance(issue.get("description"), str) else ""
        _, visible = extract_engine_state(body)
        new_body = embed_engine_state(visible, state)
        iid = int(issue.get("iid") or issue.get("id") or issue_id)
        await self._call(self.client.update_issue, iid, {"description": new_body})


    @staticmethod
    def _issue_iid(issue: dict[str, Any]) -> int | None:
        return extract_issue_number(issue)

    @staticmethod
    def _issue_labels(issue: dict[str, Any]) -> list[str]:
        return extract_issue_labels(issue)

    def _state_from_issue(self, issue: dict[str, Any]) -> TaskStatus | None:
        labels = set(self._issue_labels(issue))
        cfg = self._labels
        if cfg["blocked"] in labels:
            return TaskStatus.BLOCKED
        if cfg["failed"] in labels:
            return TaskStatus.FAILED
        if cfg["running"] in labels:
            return None
        state = issue.get("state")
        # GitLab uses opened/closed
        if state == "closed":
            return TaskStatus.COMPLETED
        if state in ("opened", "open"):
            return TaskStatus.OPEN
        return None

    async def _task_from_issue(self, issue: dict[str, Any]) -> Task | None:
        iid = self._issue_iid(issue)
        if iid is None:
            return None
        status = self._state_from_issue(issue)
        if status is None:
            return None
        issue_id = str(iid)
        body = issue.get("description") if isinstance(issue.get("description"), str) else ""
        state, visible = extract_engine_state(body)
        title = issue.get("title")
        title = title.strip() if isinstance(title, str) and title.strip() else issue_id
        description = visible.strip() if visible.strip() else title
        created = parse_datetime(issue.get("created_at"))
        updated = parse_datetime(issue.get("updated_at"))
        run_at = parse_optional_datetime(state.get("run_at"))
        return Task(
            id=issue_id,
            title=title,
            description=description,
            dependencies=as_string_list(state.get("dependencies")),
            acceptance_criteria=as_string_list(state.get("acceptance_criteria")),
            files_to_modify=as_string_list(state.get("files_to_modify")),
            status=status,
            created_at=created,
            updated_at=updated,
            run_at=run_at,
            agent_command=state.get("agent_command") if isinstance(state.get("agent_command"), str) else None,
            agent_timeout_seconds=as_optional_float(state.get("agent_timeout_seconds")),
            blocker_reason=as_string_list(state.get("blocker_reason")),
            blocked_count=as_nonnegative_int(state.get("blocked_count")),
            failure_reason=as_string_list(state.get("failure_reason")),
            agent_response=state.get("agent_response") if isinstance(state.get("agent_response"), str) else None,
            retries_left=as_optional_int(state.get("retries_left")),
            retry_count=as_nonnegative_int(state.get("retry_count")),
            failed_wait_cycles=as_nonnegative_int(state.get("failed_wait_cycles")),
        )

    async def list_tasks(self) -> list[Task]:
        issues = await self._search_all()
        tasks: list[Task] = []
        for issue in issues:
            task = await self._task_from_issue(issue)
            if task is not None:
                tasks.append(task)
        return tasks

    async def get_task(self, task_id: str) -> Task | None:
        issue = await self._get_issue(task_id)
        if issue is None:
            return None
        return await self._task_from_issue(issue)

    async def validate_connection(self) -> None:
        await self._search_all()

    async def claim_task(self, task: Task) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task.id)
            if issue is None:
                return None
            current = await self._task_from_issue(issue)
            if current is None or current.status is not TaskStatus.OPEN:
                return None
            iid = self._issue_iid(issue)
            assert iid is not None
            labels = self._issue_labels(issue)
            if self._labels["running"] not in labels:
                await self._update_labels(task.id, add=[self._labels["running"]], remove=[self._labels["blocked"], self._labels["failed"]])
            state = await self.get_engine_state(task.id)
            state.update({"claimed_at": datetime.now(UTC).isoformat()})
            await self.put_engine_state(task.id, state)
            return current

    async def recover_claims(self) -> None:
        issues = await self._search_all()
        cutoff = datetime.now(UTC) - timedelta(seconds=self.config.claim_timeout_seconds)
        for issue in issues:
            labels = self._issue_labels(issue)
            if self._labels["running"] not in labels:
                continue
            iid = self._issue_iid(issue)
            if iid is None:
                continue
            issue_id = str(iid)
            state = await self.get_engine_state(issue_id)
            claimed_at = parse_optional_datetime(state.get("claimed_at"))
            if claimed_at is None:
                claimed_at = parse_datetime(issue.get("updated_at"))
            if claimed_at > cutoff:
                continue
            await self._update_labels(issue_id, add=[], remove=[self._labels["running"]])
            state.pop("claimed_at", None)
            await self.put_engine_state(issue_id, state)
            logger.warning("Recovered stale GitLab claim for task %s.", issue_id)

    async def _update_labels(self, issue_id: str, *, add: list[str], remove: list[str]) -> None:
        issue = await self._get_issue(issue_id)
        if issue is None:
            return
        iid = self._issue_iid(issue)
        assert iid is not None
        labels = set(self._issue_labels(issue))
        for label in add:
            labels.add(label)
        for label in remove:
            labels.discard(label)
        await self._call(self.client.update_issue, iid, {"labels": list(labels)})

    async def _transition_state(self, issue_id: str, state: str) -> None:
        issue = await self._get_issue(issue_id)
        if issue is None:
            return
        iid = self._issue_iid(issue)
        assert iid is not None
        current = issue.get("state")
        if current == state:
            return
        # GitLab API expects state_event: close/reopen
        event = "close" if state == "closed" else "reopen"
        await self._call(self.client.update_issue, iid, {"state_event": event})

    async def _transition_metadata(
        self,
        issue: dict[str, Any],
        status: TaskStatus,
        result: ExecutionResult,
        *,
        reason: list[str] | None = None,
    ) -> Task | None:
        iid = self._issue_iid(issue)
        if iid is None:
            return None
        issue_id = str(iid)
        state = await self.get_engine_state(issue_id)
        joined = _join_output_logs(result, self._output_cap)
        if joined is not None:
            state["agent_response"] = joined
        state["state"] = status.value
        if status is TaskStatus.COMPLETED:
            state.pop("claimed_at", None)
            state["failure_reason"] = []
            state["blocker_reason"] = []
            await self._transition_state(issue_id, "closed")
            await self._update_labels(issue_id, add=[], remove=list(self._labels.values()))
            await self.put_engine_state(issue_id, state)
            return await self.get_task(issue_id)
        if status is TaskStatus.OPEN:
            state["failure_reason"] = []
            state.pop("claimed_at", None)
            await self._transition_state(issue_id, "opened")
            await self._update_labels(issue_id, add=[], remove=list(self._labels.values()))
            await self.put_engine_state(issue_id, state)
            return await self.get_task(issue_id)
        if status is TaskStatus.BLOCKED:
            state["blocker_reason"] = list(reason or [])
            bump_state_counter(state, "blocked_count")
            state["failure_reason"] = []
            state.pop("claimed_at", None)
            await self._update_labels(issue_id, add=[self._labels["blocked"]], remove=[self._labels["running"], self._labels["failed"]])
            await self.put_engine_state(issue_id, state)
            self._comment(iid, "BLOCKED", reason or [])
            return await self.get_task(issue_id)
        state["failure_reason"] = list(reason or [])
        state["failed_wait_cycles"] = 0
        state.pop("claimed_at", None)
        await self._update_labels(issue_id, add=[self._labels["failed"]], remove=[self._labels["running"], self._labels["blocked"]])
        await self.put_engine_state(issue_id, state)
        self._comment(iid, "FAILED", reason or [])
        return await self.get_task(issue_id)

    def _comment(self, iid: int, state: str, reason: list[str]) -> None:
        self._pending_comments.append((iid, format_state_comment(state, reason)))

    async def _flush_comments(self) -> None:
        comments = getattr(self, "_pending_comments", [])
        self._pending_comments = []
        for iid, body in comments:
            try:
                await self._call(self.client.add_note, iid, body)
            except GitlabRequestError as exc:
                logger.warning("Could not add GitLab note to %s: %s", iid, exc)

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

    async def bump_failed_wait(self, task_id: str) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            task = await self._task_from_issue(issue)
            if task is None:
                return None
            state = await self.get_engine_state(task_id)
            bump_state_counter(state, "failed_wait_cycles")
            await self.put_engine_state(task_id, state)
            return await self.get_task(task_id)

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
            await self._transition_state(task_id, "opened")
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
            await self._transition_state(task_id, "opened")
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
            iid = self._issue_iid(issue)
            assert iid is not None
            try:
                await self._call(self.client.delete_issue, iid)
            except GitlabRequestError:
                await self._call(self.client.update_issue, iid, {"state_event": "close"})
            return task

    async def create_task(self, task: Task) -> Task:
        fields: dict[str, Any] = {
            "title": task.title,
            "description": embed_engine_state(
                task.description,
                {
                    "state": TaskStatus.OPEN.value,
                    "acceptance_criteria": task.acceptance_criteria,
                    "dependencies": task.dependencies,
                    "files_to_modify": task.files_to_modify,
                },
            ),
            "labels": [self.config.label_prefix],
        }
        async with self._lock:
            created = await self._call(self.client.create_issue, fields)
            iid = created.get("iid") or created.get("id")
            if not isinstance(iid, int):
                raise GitlabRequestError("GitLab create response did not contain an issue iid")
            issue_id = str(iid)
            await self.put_engine_state(issue_id, {"state": TaskStatus.OPEN.value})
            result = await self.get_task(issue_id)
            if result is None:
                raise GitlabRequestError(f"Created GitLab issue {issue_id} could not be read back")
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
            iid = self._issue_iid(issue)
            assert iid is not None
            fields: dict[str, Any] = {}
            if "title" in updates:
                fields["title"] = candidate.title
            if any(k in updates for k in ("description", "acceptance_criteria", "dependencies", "files_to_modify", "agent_command", "agent_timeout_seconds", "run_at", "retries_left")):
                state = await self.get_engine_state(task_id)
                state.update(
                    {
                        "acceptance_criteria": candidate.acceptance_criteria,
                        "dependencies": candidate.dependencies,
                        "files_to_modify": candidate.files_to_modify,
                        "agent_command": candidate.agent_command,
                        "agent_timeout_seconds": candidate.agent_timeout_seconds,
                        "run_at": candidate.run_at.isoformat() if candidate.run_at else None,
                        "retries_left": candidate.retries_left,
                    }
                )
                fields["description"] = embed_engine_state(candidate.description, state)
            if fields:
                await self._call(self.client.update_issue, iid, fields)
            return await self.get_task(task_id)
