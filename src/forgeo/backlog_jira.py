"""Jira-backed task provider.

Jira is deliberately implemented as an issue-level provider rather than as a
special case of :mod:`forgeo.backlog_http`. The HTTP backlog exchanges one
complete document on every mutation; Jira has independent issue fields and
workflow transitions, so pretending that it is a replaceable JSON document
would make concurrent edits and status transitions unsafe.

The provider uses ordinary Jira REST APIs and no third-party SDK. Forgeo's
engine-managed state is kept in a Jira issue property named by configuration,
while labels make the current Forgeo state visible and queryable. This avoids
requiring a Jira administrator to create a set of custom fields just to run a
factory. Optional custom-field mappings cover task-specific fields such as
acceptance criteria and dependencies.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
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
    adf_to_plain_text,
    as_nonnegative_int,
    as_optional_float,
    as_optional_int,
    as_string_list,
    bump_state_counter,
    execute_json_request,
    format_state_comment,
    next_reopen_state,
    next_retry_state,
    parse_datetime,
    parse_optional_datetime,
    plain_text_to_adf,
    require_env_token,
)
from forgeo.models import (
    ExecutionResult,
    JiraBacklogConfig,
    Task,
    TaskStatus,
)

logger = logging.getLogger(__name__)

DEFAULT_API_VERSION = 3


class JiraRequestError(BacklogUnavailableError):
    """A Jira request failed or returned an unusable response."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class JiraClient:
    """Small blocking Jira REST client used through ``asyncio.to_thread``."""

    def __init__(
        self,
        base_url: str,
        config: JiraBacklogConfig,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.config = config
        self.api_version = config.api_version or DEFAULT_API_VERSION

    def _api_url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url}/rest/api/{self.api_version}{path}"
        if query:
            url += "?" + urlencode(query, doseq=True)
        return url

    def _auth_header(self) -> str:
        token = require_env_token(self.config.auth.token_env, "Jira", JiraRequestError)
        if self.config.auth.scheme == "bearer":
            return f"Bearer {token}"
        username = self.config.auth.username
        if self.config.auth.username_env is not None:
            username = os.environ.get(self.config.auth.username_env)
        if not username:
            source = self.config.auth.username_env or "username"
            raise JiraRequestError(f"Jira username {source!r} is not set")
        encoded = base64.b64encode(f"{username}:{token}".encode()).decode("ascii")
        return f"Basic {encoded}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform one authenticated request and decode its JSON response."""
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._api_url(path, query),
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": self._auth_header(),
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        data = execute_json_request(
            request, self.config.timeout_seconds, JiraRequestError, method
        )
        if not isinstance(data, dict):
            raise JiraRequestError(f"{method} {request.full_url} returned a non-object JSON body")
        return data

    def search_issues(
        self,
        jql: str,
        *,
        start_at: int = 0,
        max_results: int = 50,
        fields: list[str],
        next_page_token: str | None = None,
    ) -> dict[str, Any]:
        """Search a page of issues using Jira's JQL endpoint."""
        if self.api_version >= 3:
            path = "/search/jql"
            query: dict[str, Any] = {
                "jql": jql,
                "maxResults": max_results,
                "fields": list(dict.fromkeys(fields)),
            }
            if next_page_token is not None:
                query["nextPageToken"] = next_page_token
        else:
            path = "/search"
            query = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": max_results,
                "fields": list(dict.fromkeys(fields)),
            }
        return self._request(
            "GET",
            path,
            query=query,
        )

    def get_issue(self, issue_key: str, *, fields: list[str]) -> dict[str, Any]:
        """Fetch one issue by key."""
        return self._request(
            "GET",
            f"/issue/{quote(issue_key, safe='')}",
            query={"fields": list(dict.fromkeys(fields))},
        )

    def get_transitions(self, issue_key: str) -> list[dict[str, Any]]:
        """Return workflow transitions currently available to the caller."""
        data = self._request(
            "GET",
            f"/issue/{quote(issue_key, safe='')}/transitions",
        )
        transitions = data.get("transitions", [])
        return transitions if isinstance(transitions, list) else []

    def transition(self, issue_key: str, transition_id: str) -> None:
        """Apply one workflow transition."""
        self._request(
            "POST",
            f"/issue/{quote(issue_key, safe='')}/transitions",
            payload={"transition": {"id": transition_id}},
        )

    def update_issue(
        self,
        issue_key: str,
        *,
        fields: dict[str, Any] | None = None,
        update: dict[str, Any] | None = None,
    ) -> None:
        """Update native or custom issue fields."""
        payload: dict[str, Any] = {}
        if fields:
            payload["fields"] = fields
        if update:
            payload["update"] = update
        self._request(
            "PUT",
            f"/issue/{quote(issue_key, safe='')}",
            payload=payload,
        )

    def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Create a Jira issue and return Jira's response."""
        return self._request("POST", "/issue", payload={"fields": fields})

    def delete_issue(self, issue_key: str) -> None:
        """Delete an issue."""
        self._request("DELETE", f"/issue/{quote(issue_key, safe='')}")

    def add_comment(self, issue_key: str, body: str) -> None:
        """Add a human-readable transition comment."""
        comment_body: Any = body
        if self.api_version >= 3:
            comment_body = plain_text_to_adf(body)
        self._request(
            "POST",
            f"/issue/{quote(issue_key, safe='')}/comment",
            payload={"body": comment_body},
        )

    def get_property(self, issue_key: str, property_key: str) -> dict[str, Any]:
        """Read an issue property; a missing property is an empty object."""
        try:
            return self._request(
                "GET",
                f"/issue/{quote(issue_key, safe='')}/properties/{quote(property_key, safe='')}",
            )
        except JiraRequestError as exc:
            if exc.status == 404:
                return {}
            raise

    def set_property(self, issue_key: str, property_key: str, value: dict[str, Any]) -> None:
        """Replace one issue property."""
        self._request(
            "PUT",
            f"/issue/{quote(issue_key, safe='')}/properties/{quote(property_key, safe='')}",
            payload={"value": value},
        )


class JiraBacklog(IssueBacklogBase):
    """A task provider backed by Jira issues and workflow transitions."""

    def __init__(
        self,
        url: str,
        config: JiraBacklogConfig,
        *,
        output_cap: int | None = None,
        client: JiraClient | None = None,
    ) -> None:
        super().__init__(output_cap=output_cap)
        self.url = url.rstrip("/")
        self.config = config
        self.client = client or JiraClient(self.url, config)
        self._pending_comments: list[tuple[str, str]] = []

    def __repr__(self) -> str:
        return f"JiraBacklog({self.url!r})"

    def _fields(self) -> list[str]:
        fields = [
            "summary",
            "description",
            "status",
            "created",
            "updated",
            "labels",
            "issuelinks",
            "duedate",
        ]
        mapping = self.config.fields
        fields.extend(
            field
            for field in (
                mapping.acceptance_criteria,
                mapping.dependencies,
                mapping.files_to_modify,
                mapping.agent_command,
                mapping.agent_timeout_seconds,
                mapping.run_at,
                mapping.retries_left,
            )
            if field is not None
        )
        return list(dict.fromkeys(fields))

    async def _get_issue(self, issue_key: str) -> dict[str, Any] | None:
        """Fetch an issue, translating Jira's not-found response to ``None``."""
        try:
            issue = await self._call(self.client.get_issue, issue_key, fields=self._fields())
        except JiraRequestError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(issue, dict):
            raise JiraRequestError(f"Jira issue {issue_key} response was not an object")
        return issue

    async def _search_all(self, jql: str | None = None) -> list[dict[str, Any]]:
        """Fetch all issues in the configured query, respecting pagination."""
        query = self.config.jql if jql is None else jql
        issues: list[dict[str, Any]] = []
        start_at = 0
        next_page_token: str | None = None
        while len(issues) < self.config.max_issues:
            request_size = min(
                self.config.page_size,
                self.config.max_issues - len(issues),
            )
            page = await self._call(
                self.client.search_issues,
                query,
                start_at=start_at,
                max_results=request_size,
                fields=self._fields(),
                next_page_token=next_page_token,
            )
            page_issues = page.get("issues", [])
            if not isinstance(page_issues, list):
                raise JiraRequestError("Jira search response did not contain an issues list")
            issues.extend(issue for issue in page_issues if isinstance(issue, dict))
            if self.config.api_version >= 3:
                token = page.get("nextPageToken")
                if not isinstance(token, str) or not token or token == next_page_token:
                    break
                next_page_token = token
            else:
                if len(page_issues) < request_size:
                    break
                total = as_optional_int(page.get("total"))
                start_at += len(page_issues)
                if total is not None and start_at >= total:
                    break
                if not page_issues:
                    break
        return issues[: self.config.max_issues]

    async def _metadata(self, issue_key: str) -> dict[str, Any]:
        data = await self._call(self.client.get_property, issue_key, self.config.property_key)
        value = data.get("value") if isinstance(data, dict) else None
        return dict(value) if isinstance(value, dict) else {}

    async def _save_metadata(self, issue_key: str, metadata: dict[str, Any]) -> None:
        await self._call(
            self.client.set_property,
            issue_key,
            self.config.property_key,
            metadata,
        )

    async def get_engine_state(self, issue_id: str) -> dict[str, Any]:
        return await self._metadata(issue_id)

    async def put_engine_state(self, issue_id: str, state: dict[str, Any]) -> None:
        await self._save_metadata(issue_id, state)

    @staticmethod
    def _issue_key(issue: dict[str, Any]) -> str | None:
        key = issue.get("key")
        return key if isinstance(key, str) and key else None

    @staticmethod
    def _status(issue: dict[str, Any]) -> dict[str, str]:
        raw = issue.get("fields", {}).get("status", {})
        if not isinstance(raw, dict):
            return {}
        return {
            key: value
            for key in ("id", "name")
            if isinstance(value := raw.get(key), str)
        }

    @staticmethod
    def _issue_labels(issue: dict[str, Any]) -> list[str]:
        labels = issue.get("fields", {}).get("labels", [])
        return [label for label in labels if isinstance(label, str)] if isinstance(labels, list) else []

    @staticmethod
    def _matches(reference: str | None, status: dict[str, str]) -> bool:
        if reference is None:
            return False
        return any(value.casefold() == reference.casefold() for value in status.values())

    def _state_from_issue(
        self,
        issue: dict[str, Any],
    ) -> TaskStatus | None:
        labels = set(self._issue_labels(issue))
        configured = self._labels
        if configured["blocked"] in labels:
            return TaskStatus.BLOCKED
        if configured["failed"] in labels:
            return TaskStatus.FAILED
        status = self._status(issue)
        workflow = self.config.workflow
        if configured["running"] in labels or self._matches(workflow.running_status, status):
            return None
        if self._matches(workflow.blocked_status, status):
            return TaskStatus.BLOCKED
        if self._matches(workflow.failed_status, status):
            return TaskStatus.FAILED
        if self._matches(workflow.completed_status, status):
            return TaskStatus.COMPLETED
        if any(self._matches(reference, status) for reference in workflow.open_statuses):
            return TaskStatus.OPEN
        return None

    async def _task_from_issue(
        self,
        issue: dict[str, Any],
        *,
        include_metadata: bool = False,
    ) -> Task | None:
        key = self._issue_key(issue)
        if key is None:
            return None
        status = self._state_from_issue(issue)
        if status is None:
            return None
        metadata = (
            await self._metadata(key)
            if include_metadata or status in (TaskStatus.BLOCKED, TaskStatus.FAILED)
            else {}
        )
        fields = issue.get("fields", {})
        if not isinstance(fields, dict):
            return None
        title = fields.get("summary")
        title = title.strip() if isinstance(title, str) and title.strip() else key
        description = adf_to_plain_text(fields.get("description"))
        if not description:
            description = title
        mapping = self.config.fields
        acceptance = as_string_list(fields.get(mapping.acceptance_criteria))
        dependencies = as_string_list(fields.get(mapping.dependencies))
        if mapping.dependencies is None:
            dependencies = self._issue_link_dependencies(fields.get("issuelinks"))
        files_to_modify = as_string_list(fields.get(mapping.files_to_modify))
        command_value = fields.get(mapping.agent_command)
        command: str | list[str] | None
        if (
            (isinstance(command_value, str) and command_value.strip())
            or (
                isinstance(command_value, list)
                and command_value
                and all(isinstance(item, str) for item in command_value)
            )
        ):
            command = command_value
        else:
            command = None
        timeout = as_optional_float(fields.get(mapping.agent_timeout_seconds))
        if timeout is not None and timeout <= 0:
            timeout = None
        run_at_field = mapping.run_at or "duedate"
        run_at = parse_optional_datetime(fields.get(run_at_field))
        retries_left = as_optional_int(fields.get(mapping.retries_left))
        if retries_left is not None and retries_left < 0:
            retries_left = None
        return Task(
            id=key,
            title=title,
            description=description,
            dependencies=dependencies,
            acceptance_criteria=acceptance,
            files_to_modify=files_to_modify,
            status=status,
            created_at=parse_datetime(fields.get("created")),
            updated_at=parse_datetime(fields.get("updated")),
            run_at=run_at,
            agent_command=command,
            agent_timeout_seconds=timeout,
            blocker_reason=as_string_list(metadata.get("blocker_reason")),
            blocked_count=as_nonnegative_int(metadata.get("blocked_count")),
            failure_reason=as_string_list(metadata.get("failure_reason")),
            agent_response=(
                metadata.get("agent_response")
                if isinstance(metadata.get("agent_response"), str)
                else None
            ),
            retries_left=retries_left,
            retry_count=as_nonnegative_int(metadata.get("retry_count")),
            failed_wait_cycles=as_nonnegative_int(metadata.get("failed_wait_cycles")),
        )

    @staticmethod
    def _issue_link_dependencies(value: Any) -> list[str]:
        """Use issues linked with a ``blocks`` relationship as dependencies."""
        if not isinstance(value, list):
            return []
        dependencies: list[str] = []
        for link in value:
            if not isinstance(link, dict):
                continue
            inward = link.get("inwardIssue")
            link_type = link.get("type", {})
            link_name = link_type.get("name") if isinstance(link_type, dict) else None
            if not isinstance(link_name, str) or link_name.casefold() != "blocks":
                continue
            # Jira places the issue that blocks the current issue in
            # ``inwardIssue``. An ``outwardIssue`` is one that the current
            # issue blocks, and therefore is not a dependency of the current
            # task.
            issue = inward if isinstance(inward, dict) else None
            key = issue.get("key") if isinstance(issue, dict) else None
            if isinstance(key, str) and key:
                dependencies.append(key)
        return dependencies

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
        return await self._task_from_issue(issue, include_metadata=True)

    async def validate_connection(self) -> None:
        await self._search_all()

    async def claim_task(self, task: Task) -> Task | None:
        """Move an open issue into the running state before the agent starts."""
        async with self._lock:
            issue = await self._get_issue(task.id)
            if issue is None:
                return None
            current = await self._task_from_issue(issue)
            if current is None or current.status is not TaskStatus.OPEN:
                return None
            workflow = self.config.workflow
            if workflow.running_status is not None:
                await self._transition_to(issue, workflow.running_status)
            await self._update_labels(
                task.id,
                add=[self._labels["running"]],
                remove=[self._labels["blocked"], self._labels["failed"]],
            )
            metadata = await self._metadata(task.id)
            metadata.update(
                {
                    "state": "RUNNING",
                    "claimed_at": datetime.now(UTC).isoformat(),
                }
            )
            await self._save_metadata(task.id, metadata)
            return current

    async def recover_claims(self) -> None:
        """Release claims older than the configured lease."""
        running_label = self._labels["running"]
        recovery_jql = f"({self.config.jql}) AND labels = \"{running_label}\""
        issues = await self._search_all(recovery_jql)
        cutoff = datetime.now(UTC) - timedelta(seconds=self.config.claim_timeout_seconds)
        for issue in issues:
            key = self._issue_key(issue)
            if key is None:
                continue
            metadata = await self._metadata(key)
            claimed_at = parse_optional_datetime(metadata.get("claimed_at"))
            if claimed_at is None:
                claimed_at = parse_datetime(issue.get("fields", {}).get("updated"))
            if claimed_at > cutoff:
                continue
            await self._update_labels(key, add=[], remove=[running_label])
            metadata.update({"state": TaskStatus.OPEN.value})
            metadata.pop("claimed_at", None)
            await self._save_metadata(key, metadata)
            workflow = self.config.workflow
            if workflow.open_status and self._matches(workflow.running_status, self._status(issue)):
                await self._transition_to(issue, workflow.open_status)
            logger.warning("Recovered stale Jira claim for task %s.", key)

    async def _transition_to(self, issue: dict[str, Any], target: str | None) -> None:
        """Find and apply a transition whose destination matches ``target``."""
        if target is None or self._matches(target, self._status(issue)):
            return
        key = self._issue_key(issue)
        if key is None:
            raise JiraRequestError("Jira issue response has no key")
        transitions = await self._call(self.client.get_transitions, key)
        for transition in transitions:
            destination = transition.get("to", {})
            if not isinstance(destination, dict):
                continue
            destination_status = {
                name: value
                for name in ("id", "name")
                if isinstance(value := destination.get(name), str)
            }
            if not self._matches(target, destination_status):
                continue
            transition_id = transition.get("id")
            if not isinstance(transition_id, str):
                continue
            await self._call(self.client.transition, key, transition_id)
            return
        raise JiraRequestError(
            f"No Jira workflow transition for {key} reaches status {target!r}"
        )

    async def _update_labels(
        self,
        issue_key: str,
        *,
        add: list[str],
        remove: list[str],
    ) -> None:
        """Apply label additions/removals without replacing unrelated labels."""
        update: list[dict[str, str]] = []
        update.extend({"add": label} for label in add if label)
        update.extend({"remove": label} for label in remove if label)
        if update:
            await self._call(
                self.client.update_issue,
                issue_key,
                update={"labels": update},
            )

    async def _transition_metadata(
        self,
        issue: dict[str, Any],
        status: TaskStatus,
        result: ExecutionResult,
        *,
        reason: list[str] | None = None,
    ) -> Task | None:
        """Persist a terminal state, labels, workflow transition, and output."""
        key = self._issue_key(issue)
        if key is None:
            return None
        metadata = await self._metadata(key)
        previous = metadata.get("state")
        joined = _join_output_logs(result, self._output_cap)
        if joined is not None:
            metadata["agent_response"] = joined
        metadata["state"] = status.value
        if status is TaskStatus.COMPLETED:
            metadata.pop("claimed_at", None)
            metadata["failure_reason"] = []
            metadata["blocker_reason"] = []
            if previous == TaskStatus.FAILED.value:
                metadata["retry_count"] = 0
                metadata["failed_wait_cycles"] = 0
            await self._transition_to(issue, self.config.workflow.completed_status)
            await self._update_labels(
                key,
                add=[],
                remove=list(self._labels.values()),
            )
            await self._save_metadata(key, metadata)
            return await self.get_task(key)
        if status is TaskStatus.OPEN:
            metadata["failure_reason"] = []
            if previous == TaskStatus.FAILED.value:
                metadata["retry_count"] = 0
                metadata["failed_wait_cycles"] = 0
            metadata.pop("claimed_at", None)
            await self._transition_to(issue, self.config.workflow.open_status)
            await self._update_labels(key, add=[], remove=list(self._labels.values()))
            await self._save_metadata(key, metadata)
            return await self.get_task(key)
        if status is TaskStatus.BLOCKED:
            metadata["blocker_reason"] = list(reason or [])
            bump_state_counter(metadata, "blocked_count")
            metadata["failure_reason"] = []
            metadata.pop("claimed_at", None)
            await self._update_labels(
                key,
                add=[self._labels["blocked"]],
                remove=[self._labels["running"], self._labels["failed"]],
            )
            await self._save_metadata(key, metadata)
            await self._transition_to(issue, self.config.workflow.blocked_status)
            self._comment(key, "BLOCKED", reason or [])
            return await self.get_task(key)
        metadata["failure_reason"] = list(reason or [])
        metadata["failed_wait_cycles"] = 0
        metadata.pop("claimed_at", None)
        await self._update_labels(
            key,
            add=[self._labels["failed"]],
            remove=[self._labels["running"], self._labels["blocked"]],
        )
        await self._save_metadata(key, metadata)
        target = self.config.workflow.failed_status
        if target is None and self._matches(self.config.workflow.running_status, self._status(issue)):
            target = self.config.workflow.open_status
        await self._transition_to(issue, target)
        self._comment(key, "FAILED", reason or [])
        return await self.get_task(key)

    def _comment(self, issue_key: str, state: str, reason: list[str]) -> None:
        """Add a bounded, clearly marked Jira comment without affecting the run."""
        self._pending_comments.append((issue_key, format_state_comment(state, reason)))

    async def _flush_comments(self) -> None:
        comments = getattr(self, "_pending_comments", [])
        self._pending_comments = []
        for issue_key, body in comments:
            try:
                await self._call(self.client.add_comment, issue_key, body)
            except JiraRequestError as exc:
                logger.warning("Could not add Jira comment to %s: %s", issue_key, exc)

    async def update_status(
        self, task_id: str, status: TaskStatus, result: ExecutionResult
    ) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            updated = await self._transition_metadata(issue, status, result)
            await self._flush_comments()
            return updated

    async def set_blocked(
        self, task_id: str, reason: list[str], result: ExecutionResult
    ) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            updated = await self._transition_metadata(
                issue, TaskStatus.BLOCKED, result, reason=reason
            )
            await self._flush_comments()
            return updated

    async def set_failed(
        self, task_id: str, reason: list[str], result: ExecutionResult
    ) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            updated = await self._transition_metadata(
                issue, TaskStatus.FAILED, result, reason=reason
            )
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
            metadata = await self._metadata(task_id)
            bump_state_counter(metadata, "failed_wait_cycles")
            await self._save_metadata(task_id, metadata)
            return await self.get_task(task_id)

    async def retry_task(self, task_id: str) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            task = await self._task_from_issue(issue)
            if task is None or task.status is not TaskStatus.FAILED:
                return None
            metadata = await self._metadata(task_id)
            metadata["state"] = TaskStatus.OPEN.value
            next_retry_state(metadata)
            await self._transition_to(issue, self.config.workflow.open_status)
            await self._update_labels(task_id, add=[], remove=list(self._labels.values()))
            await self._save_metadata(task_id, metadata)
            return await self.get_task(task_id)

    async def reopen_task(self, task_id: str) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            task = await self._task_from_issue(issue)
            if task is None or task.status is not TaskStatus.BLOCKED:
                return None
            metadata = await self._metadata(task_id)
            metadata["state"] = TaskStatus.OPEN.value
            next_reopen_state(metadata)
            await self._transition_to(issue, self.config.workflow.open_status)
            await self._update_labels(task_id, add=[], remove=list(self._labels.values()))
            await self._save_metadata(task_id, metadata)
            return await self.get_task(task_id)

    async def delete_task(self, task_id: str) -> Task | None:
        async with self._lock:
            issue = await self._get_issue(task_id)
            if issue is None:
                return None
            task = await self._task_from_issue(issue)
            if task is None:
                return None
            await self._call(self.client.delete_issue, task_id)
            return task

    async def create_task(self, task: Task) -> Task:
        project_key = self.config.project_key
        if project_key is None:
            raise ValueError("jira.project_key is required to create Jira tasks")
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "issuetype": {"name": self.config.issue_type},
            "summary": task.title,
            "description": (
                plain_text_to_adf(task.description)
                if self.config.api_version >= 3
                else task.description
            ),
            "labels": [self.config.label_prefix],
        }
        self._set_custom_fields(fields, task)
        async with self._lock:
            created = await self._call(self.client.create_issue, fields)
            key = created.get("key")
            if not isinstance(key, str) or not key:
                raise JiraRequestError("Jira create response did not contain an issue key")
            await self._save_metadata(key, {"state": TaskStatus.OPEN.value})
            task_result = await self.get_task(key)
            if task_result is None:
                raise JiraRequestError(f"Created Jira issue {key} could not be read back")
            return task_result

    def _set_custom_fields(self, fields: dict[str, Any], task: Task) -> None:
        mapping = self.config.fields
        values = {
            mapping.acceptance_criteria: task.acceptance_criteria,
            mapping.dependencies: task.dependencies,
            mapping.files_to_modify: task.files_to_modify,
            mapping.agent_command: task.agent_command,
            mapping.agent_timeout_seconds: task.agent_timeout_seconds,
            mapping.run_at: task.run_at.isoformat() if task.run_at is not None else None,
            mapping.retries_left: task.retries_left,
        }
        for field, value in values.items():
            if field is not None and value is not None:
                fields[field] = value
        if mapping.run_at is None and task.run_at is not None:
            fields["duedate"] = task.run_at.date().isoformat()

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
            updates = {
                field: getattr(candidate, field)
                for field in updates
            }
            fields: dict[str, Any] = {}
            if "title" in updates:
                fields["summary"] = updates["title"]
            if "description" in updates:
                fields["description"] = (
                    plain_text_to_adf(updates["description"])
                    if self.config.api_version >= 3
                    else updates["description"]
                )
            mapping = self.config.fields
            field_values = {
                "acceptance_criteria": updates.get("acceptance_criteria"),
                "dependencies": updates.get("dependencies"),
                "files_to_modify": updates.get("files_to_modify"),
                "agent_command": updates.get("agent_command"),
                "agent_timeout_seconds": updates.get("agent_timeout_seconds"),
                "run_at": updates.get("run_at"),
                "retries_left": updates.get("retries_left"),
            }
            for name, value in field_values.items():
                if name not in updates:
                    continue
                field = getattr(mapping, name)
                if name == "run_at" and isinstance(value, datetime):
                    value = value.isoformat()
                if name == "run_at" and mapping.run_at is None:
                    fields["duedate"] = value[:10] if isinstance(value, str) else value
                    continue
                if field is None:
                    raise ValueError(f"Jira field mapping is missing for {name}")
                fields[field] = value
            if fields:
                await self._call(self.client.update_issue, task_id, fields=fields)
            return await self.get_task(task_id)


