"""GitHub-backed task provider."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime
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
    ENGINE_STATE_FIELDS,
    as_nonnegative_int,
    as_optional_float,
    as_optional_int,
    as_string_list,
    bump_state_counter,
    claim_cutoff,
    embed_engine_state,
    execute_json_request,
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
    require_env_token,
)
from forgeo.models import ExecutionResult, GithubBacklogConfig, Task, TaskStatus

logger = logging.getLogger(__name__)


class GithubRequestError(BacklogUnavailableError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GithubClient:
    """Blocking GitHub REST client used via asyncio.to_thread."""

    def __init__(self, base_url: str, config: GithubBacklogConfig) -> None:
        self.base_url = base_url.rstrip("/")
        self.config = config

    def _auth_header(self) -> str:
        token = require_env_token(self.config.auth.token_env, "GitHub", GithubRequestError)
        # GitHub accepts Bearer or token; use Bearer for consistency
        return f"Bearer {token}"

    def _api_url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url}{path}"
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
        request = urllib.request.Request(
            self._api_url(path, query),
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": self._auth_header(),
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        return execute_json_request(
            request, self.config.timeout_seconds, GithubRequestError, method
        )

    def _repo_path(self) -> str:
        # GitHub API expects owner/repo as two separate path segments;
        # encode each segment but keep the slash between them.
        return "/".join(quote(part, safe="") for part in self.config.repo.split("/"))

    def search_issues(
        self,
        *,
        page: int = 1,
        per_page: int = 30,
        state: str = "all",
    ) -> list[dict[str, Any]]:
        path = f"/repos/{self._repo_path()}/issues"
        query: dict[str, Any] = {"state": state, "per_page": per_page, "page": page}
        data = self._request("GET", path, query=query)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    def get_issue(self, issue_number: int) -> dict[str, Any]:
        path = f"/repos/{self._repo_path()}/issues/{issue_number}"
        return self._request("GET", path)  # type: ignore[no-any-return]

    def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        path = f"/repos/{self._repo_path()}/issues"
        return self._request("POST", path, payload=fields)  # type: ignore[no-any-return]

    def update_issue(self, issue_number: int, fields: dict[str, Any]) -> dict[str, Any]:
        path = f"/repos/{self._repo_path()}/issues/{issue_number}"
        return self._request("PATCH", path, payload=fields)  # type: ignore[no-any-return]

    def add_comment(self, issue_number: int, body: str) -> None:
        path = f"/repos/{self._repo_path()}/issues/{issue_number}/comments"
        self._request("POST", path, payload={"body": body})

    def delete_issue(self, issue_number: int) -> None:
        path = f"/repos/{self._repo_path()}/issues/{issue_number}"
        self._request("PATCH", path, payload={"state": "closed"})


class GithubBacklog(IssueBacklogBase):
    """Task provider backed by GitHub issues."""

    def __init__(
        self,
        url: str,
        config: GithubBacklogConfig,
        *,
        output_cap: int | None = None,
        client: GithubClient | None = None,
    ) -> None:
        super().__init__(output_cap=output_cap)
        self.url = url.rstrip("/")
        self.config = config
        self.client = client or GithubClient(self.url, config)
        self._pending_comments: list[tuple[int, str]] = []

    def __repr__(self) -> str:
        return f"GithubBacklog({self.url!r})"

    async def _get_issue(self, issue_id: str) -> dict[str, Any] | None:
        number = parse_numeric_issue_id(issue_id)
        if number is None:
            return None
        try:
            issue = await self._call(self.client.get_issue, number)
        except GithubRequestError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(issue, dict):
            raise GithubRequestError(f"GitHub issue {issue_id} response was not an object")
        return issue

    async def _search_all(self) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        page = 1
        while len(issues) < self.config.max_issues:
            remaining = self.config.max_issues - len(issues)
            per_page = min(self.config.page_size, remaining)
            page_issues = await self._call(
                self.client.search_issues, page=page, per_page=per_page, state="all"
            )
            if not isinstance(page_issues, list):
                raise GithubRequestError("GitHub search response did not contain a list")
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
        body = issue.get("body") if isinstance(issue.get("body"), str) else ""
        state, _ = extract_engine_state(body)
        return state

    async def put_engine_state(self, issue_id: str, state: dict[str, Any]) -> None:
        issue = await self._get_issue(issue_id)
        if issue is None:
            return
        body = issue.get("body") if isinstance(issue.get("body"), str) else ""
        _, visible = extract_engine_state(body)
        new_body = embed_engine_state(visible, state)
        number = extract_issue_number(issue)
        if number is None:
            try:
                number = int(issue_id)
            except ValueError:
                return
        await self._call(self.client.update_issue, number, {"body": new_body})

    def _state_from_issue(self, issue: dict[str, Any]) -> TaskStatus | None:
        labels = set(extract_issue_labels(issue))
        cfg = self._labels
        if cfg["blocked"] in labels:
            return TaskStatus.BLOCKED
        if cfg["failed"] in labels:
            return TaskStatus.FAILED
        if cfg["running"] in labels:
            return None
        state = issue.get("state")
        if state == "closed":
            return TaskStatus.COMPLETED
        if state == "open":
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
        body = issue.get("body") if isinstance(issue.get("body"), str) else ""
        state, visible_body = extract_engine_state(body)
        # Only fetch extra metadata when needed? For GitHub, state is already extracted
        title = issue.get("title")
        title = title.strip() if isinstance(title, str) and title.strip() else issue_id
        description = visible_body.strip() if visible_body.strip() else title
        created = parse_datetime(issue.get("created_at"))
        updated = parse_datetime(issue.get("updated_at"))
        # Use engine state for retry/blocked counters
        run_at = parse_optional_datetime(state.get("run_at"))
        # If field mapping specifies run_at field, try to read from issue custom field? not needed
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
                await self._update_labels(task.id, add=[self._labels["running"]], remove=[self._labels["blocked"], self._labels["failed"]])
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
            await self._update_labels(issue_id, add=[], remove=[self._labels["running"]])
            state.pop("claimed_at", None)
            await self.put_engine_state(issue_id, state)
            logger.warning("Recovered stale GitHub claim for task %s.", issue_id)

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
        await self._call(self.client.update_issue, number, {"state": state})

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
            await self._transition_state(issue_id, "closed")
            await self._update_labels(issue_id, add=[], remove=list(self._labels.values()))
            await self.put_engine_state(issue_id, state)
            return await self.get_task(issue_id)
        if status is TaskStatus.OPEN:
            state["failure_reason"] = []
            state.pop("claimed_at", None)
            await self._transition_state(issue_id, "open")
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
            self._comment(number, "BLOCKED", reason or [])
            return await self.get_task(issue_id)
        state["failure_reason"] = list(reason or [])
        state["failed_wait_cycles"] = 0
        state.pop("claimed_at", None)
        await self._update_labels(issue_id, add=[self._labels["failed"]], remove=[self._labels["running"], self._labels["blocked"]])
        await self.put_engine_state(issue_id, state)
        self._comment(number, "FAILED", reason or [])
        return await self.get_task(issue_id)

    def _comment(self, issue_number: int, state: str, reason: list[str]) -> None:
        self._pending_comments.append((issue_number, format_state_comment(state, reason)))

    async def _flush_comments(self) -> None:
        comments = getattr(self, "_pending_comments", [])
        self._pending_comments = []
        for number, body in comments:
            try:
                await self._call(self.client.add_comment, number, body)
            except GithubRequestError as exc:
                logger.warning("Could not add GitHub comment to %s: %s", number, exc)

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
            await self._transition_state(task_id, "open")
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
            await self._transition_state(task_id, "open")
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
            # Try to delete via client; fallback to close if not supported
            try:
                await self._call(self.client.delete_issue, number)
            except GithubRequestError:
                await self._call(self.client.update_issue, number, {"state": "closed"})
            return task

    async def create_task(self, task: Task) -> Task:
        fields: dict[str, Any] = {
            "title": task.title,
            "body": embed_engine_state(
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
            number = created.get("number")
            if not isinstance(number, int):
                raise GithubRequestError("GitHub create response did not contain an issue number")
            issue_id = str(number)
            await self.put_engine_state(issue_id, {"state": TaskStatus.OPEN.value})
            result = await self.get_task(issue_id)
            if result is None:
                raise GithubRequestError(f"Created GitHub issue {issue_id} could not be read back")
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
                fields["body"] = embed_engine_state(candidate.description, state)
            if fields:
                await self._call(self.client.update_issue, number, fields)
            return await self.get_task(task_id)
