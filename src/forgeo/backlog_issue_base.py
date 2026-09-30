"""Shared helpers for issue-backed providers (Jira, GitHub, GitLab)."""

from __future__ import annotations

import contextlib
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

if TYPE_CHECKING:
    from forgeo.models import Task, TaskStatus


def plain_text_to_adf(text: str) -> dict[str, Any]:
    paragraphs: list[dict[str, Any]] = []
    for line in text.splitlines() or [""]:
        if line:
            paragraphs.append(
                {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            )
        else:
            paragraphs.append({"type": "paragraph"})
    return {"version": 1, "type": "doc", "content": paragraphs}


def adf_to_plain_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    lines: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        node_type = node.get("type")
        if node_type == "text" and isinstance(node.get("text"), str):
            lines.append(node["text"])
            return
        if node_type in {"hardBreak", "paragraph", "heading", "listItem", "blockquote"}:
            if node_type != "hardBreak":
                before = len(lines)
                visit(node.get("content", []))
                if len(lines) > before and lines[-1] != "\n":
                    lines.append("\n")
            else:
                lines.append("\n")
            return
        visit(node.get("content", []))

    visit(value.get("content", []))
    return "".join(lines).strip()


def _parse_iso_datetime(value: str) -> datetime | None:
    normalized = value.replace("Z", "+00:00")
    if normalized.endswith("+0000"):
        normalized = normalized[:-5] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, str):
        parsed = _parse_iso_datetime(value)
        if parsed is not None:
            return parsed
    return datetime.now(UTC)


def parse_optional_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    return _parse_iso_datetime(value)


def as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        elif isinstance(item, dict):
            found = False
            for key in ("value", "name", "key"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    result.append(candidate.strip())
                    found = True
                    break
            if not found:
                text = adf_to_plain_text(item)
                if text:
                    result.extend(line.strip() for line in text.splitlines() if line.strip())
    return result


def as_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def as_optional_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def as_nonnegative_int(value: Any) -> int:
    parsed = as_optional_int(value)
    if parsed is None:
        return 0
    return max(0, parsed)


# ------------------------------------------------------------------ #
# Engine-state hidden marker (GitHub/GitLab)                          #
# ------------------------------------------------------------------ #

FORGEO_MARKER_RE = re.compile(r"<!--\s*forgeo:\s*(\{.*?\})\s*-->", re.DOTALL)


def embed_engine_state(body: str | None, state: dict[str, Any]) -> str:
    """Return body with hidden forgeo JSON block embedded."""
    marker = f"<!-- forgeo: {json.dumps(state, ensure_ascii=False)} -->"
    if not body:
        return marker
    if FORGEO_MARKER_RE.search(body):
        return FORGEO_MARKER_RE.sub(marker, body)
    return body.rstrip() + "\n\n" + marker


def extract_engine_state(body: str | None) -> tuple[dict[str, Any], str]:
    """Extract engine state from body, returning (state, visible_body)."""
    if not body or not isinstance(body, str):
        return {}, body or ""
    match = FORGEO_MARKER_RE.search(body)
    if not match:
        return {}, body
    try:
        state = json.loads(match.group(1))
        if not isinstance(state, dict):
            state = {}
    except json.JSONDecodeError:
        state = {}
    visible = body[: match.start()].rstrip() + body[match.end() :].rstrip()
    # remove extra blank lines left by marker removal
    visible = visible.strip()
    return state, visible


# ------------------------------------------------------------------ #
# Shared helpers for GitHub / GitLab issue providers                  #
# ------------------------------------------------------------------ #


def parse_numeric_issue_id(issue_id: str) -> int | None:
    """Parse a numeric issue id, handling ``WEB-123`` style prefixes."""
    try:
        return int(issue_id)
    except ValueError:
        try:
            return int(issue_id.split("-")[-1])
        except ValueError:
            return None


def format_state_comment(state: str, reason: list[str]) -> str:
    """One-line comment body the forgeo leaves on a blocked/failed issue."""
    text = "\n".join(reason[-20:]) if reason else "No reason was provided."
    return f"[forgeo] {state}\n{text}"


def bump_state_counter(state: dict[str, Any], key: str) -> int:
    """Increment ``state[key]`` as a non-negative int and return the new value."""
    current = as_optional_int(state.get(key)) or 0
    new_value = current + 1
    state[key] = new_value
    return new_value


def next_retry_state(state: dict[str, Any]) -> dict[str, Any]:
    """Mutate ``state`` for a retry transition and return it."""
    state.update(
        {
            "retry_count": (as_optional_int(state.get("retry_count")) or 0) + 1,
            "failed_wait_cycles": 0,
            "failure_reason": [],
        }
    )
    return state


def next_reopen_state(state: dict[str, Any]) -> dict[str, Any]:
    """Mutate ``state`` for a reopen transition and return it."""
    state.update({"blocker_reason": [], "failure_reason": []})
    return state


def apply_terminal_transition(
    state: dict[str, Any],
    status: TaskStatus,
    reason: list[str] | None = None,
    *,
    previous: str | None = None,
) -> dict[str, Any]:
    """Mutate engine ``state`` for a COMPLETED/OPEN/BLOCKED/FAILED transition.

    Shared by the marker (GitHub/GitLab) and Jira ``_transition_metadata``
    implementations, which mutated the state dict identically apart from
    Jira's retry-counter reset when the previous state was FAILED.
    """
    from forgeo.models import TaskStatus

    state["state"] = status.value
    if status is TaskStatus.COMPLETED:
        state.pop("claimed_at", None)
        state["failure_reason"] = []
        state["blocker_reason"] = []
        if previous == TaskStatus.FAILED.value:
            state["retry_count"] = 0
            state["failed_wait_cycles"] = 0
    elif status is TaskStatus.OPEN:
        state["failure_reason"] = []
        if previous == TaskStatus.FAILED.value:
            state["retry_count"] = 0
            state["failed_wait_cycles"] = 0
        state.pop("claimed_at", None)
    elif status is TaskStatus.BLOCKED:
        state["blocker_reason"] = list(reason or [])
        bump_state_counter(state, "blocked_count")
        state["failure_reason"] = []
        state.pop("claimed_at", None)
    else:
        state["failure_reason"] = list(reason or [])
        state["failed_wait_cycles"] = 0
        state.pop("claimed_at", None)
    return state


def transition_label_update(status: TaskStatus, labels: dict[str, str]) -> tuple[list[str], list[str]]:
    """Return the ``(add, remove)`` label update for a terminal transition.

    Shared by the marker (GitHub/GitLab) and Jira ``_transition_metadata``
    implementations, which applied identical label changes per status.
    """
    from forgeo.models import TaskStatus

    if status is TaskStatus.BLOCKED:
        return [labels["blocked"]], [labels["running"], labels["failed"]]
    if status is TaskStatus.FAILED:
        return [labels["failed"]], [labels["running"], labels["blocked"]]
    return [], list(labels.values())


def claim_cutoff(timeout_seconds: float) -> datetime:
    """Expiry instant for a claim lease."""
    return datetime.now(UTC) - timedelta(seconds=timeout_seconds)


def is_claim_stale(
    state: dict[str, Any], issue: dict[str, Any], cutoff: datetime
) -> bool:
    """True when the ``claimed_at`` in ``state`` (or the issue's update time) is stale."""
    claimed_at = parse_optional_datetime(state.get("claimed_at"))
    if claimed_at is None:
        # Fallback to the issue's last update — covers claims from older forgeo versions
        # or providers that never wrote ``claimed_at``.
        claimed_at = parse_datetime(issue.get("updated_at") or issue.get("updated") or issue.get("fields", {}).get("updated"))
    return claimed_at <= cutoff


def forgeo_labels(prefix: str) -> dict[str, str]:
    """Standard forgeo labels for a prefix (running / blocked / failed / review)."""
    return {
        "running": f"{prefix}-running",
        "blocked": f"{prefix}-blocked",
        "failed": f"{prefix}-failed",
        "review": f"{prefix}-review",
    }


def extract_issue_labels(issue: dict[str, Any]) -> list[str]:
    """Extract label names from a GitHub or GitLab issue dict.

    GitHub may return labels as ``[{\"name\": \"x\"}, ...]`` or ``[\"x\"]``;
    GitLab returns ``[\"x\"]`` and nests under ``fields.labels`` for Jira;
    this handles the GitHub/GitLab shapes (plain ``labels`` key).
    """
    labels = issue.get("labels", [])
    if isinstance(labels, list):
        result: list[str] = []
        for label in labels:
            if isinstance(label, str):
                result.append(label)
            elif isinstance(label, dict) and isinstance(label.get("name"), str):
                result.append(label["name"])
        return result
    return []


def extract_issue_number(issue: dict[str, Any]) -> int | None:
    """Extract a numeric issue number/iid from a GitHub or GitLab issue dict."""
    for key in ("number", "iid", "id"):
        value = issue.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


ENGINE_STATE_FIELDS: frozenset[str] = frozenset(
    {
        "description",
        "acceptance_criteria",
        "dependencies",
        "files_to_modify",
        "agent_command",
        "agent_timeout_seconds",
        "run_at",
        "retries_left",
        "review_required",
        "review_branch",
        "review_commit_sha",
    }
)


def build_task(
    *,
    issue_id: str,
    title: str | None,
    description: str,
    status: TaskStatus,
    created: Any,
    updated: Any,
    run_at: Any,
    state: dict[str, Any],
) -> Task:
    """Build a :class:`Task` from provider fields plus decoded engine ``state``.

    Unifies the 22-kwarg ``Task(...)`` block duplicated across the GitHub,
    GitLab and Jira providers. ``title`` falls back to ``issue_id`` when blank.
    """
    from forgeo.models import Task

    clean_title = title.strip() if isinstance(title, str) and title.strip() else issue_id
    clean_description = description.strip() if description.strip() else clean_title
    agent_command_value = state.get("agent_command")
    if isinstance(agent_command_value, str):
        agent_command: str | list[str] | None = (
            agent_command_value if agent_command_value.strip() else None
        )
    elif (
        isinstance(agent_command_value, list)
        and agent_command_value
        and all(isinstance(item, str) for item in agent_command_value)
    ):
        agent_command = agent_command_value
    else:
        agent_command = None
    agent_response = state.get("agent_response")
    review_branch = state.get("review_branch")
    review_commit_sha = state.get("review_commit_sha")
    review_required = state.get("review_required")
    return Task(
        id=issue_id,
        title=clean_title,
        description=clean_description,
        dependencies=as_string_list(state.get("dependencies")),
        acceptance_criteria=as_string_list(state.get("acceptance_criteria")),
        files_to_modify=as_string_list(state.get("files_to_modify")),
        status=status,
        created_at=created,
        updated_at=updated,
        run_at=run_at,
        agent_command=agent_command,
        agent_timeout_seconds=as_optional_float(state.get("agent_timeout_seconds")),
        blocker_reason=as_string_list(state.get("blocker_reason")),
        blocked_count=as_nonnegative_int(state.get("blocked_count")),
        failure_reason=as_string_list(state.get("failure_reason")),
        agent_response=agent_response if isinstance(agent_response, str) else None,
        retries_left=as_optional_int(state.get("retries_left")),
        retry_count=as_nonnegative_int(state.get("retry_count")),
        failed_wait_cycles=as_nonnegative_int(state.get("failed_wait_cycles")),
        review_branch=review_branch if isinstance(review_branch, str) else None,
        review_commit_sha=review_commit_sha if isinstance(review_commit_sha, str) else None,
        review_required=review_required if isinstance(review_required, bool) else None,
    )


def task_engine_state(task: Any) -> dict[str, Any]:
    """The author-controlled task fields stored in the hidden engine-state marker.

    These are the per-task customization fields the human sets (as opposed to
    engine-managed runtime state such as ``retry_count`` or ``agent_response``).
    GitHub and GitLab issues expose no portable custom-field mechanism on their
    REST issues API, so these fields travel in the hidden ``<!-- forgeo: ... -->``
    marker; Jira maps them to custom fields instead. ``None`` values are kept
    so a marker written by ``create_task`` and one refreshed by ``update_task``
    stay identical, and absent values read back as ``None`` either way.
    """
    return {
        "acceptance_criteria": task.acceptance_criteria,
        "dependencies": task.dependencies,
        "files_to_modify": task.files_to_modify,
        "agent_command": task.agent_command,
        "agent_timeout_seconds": task.agent_timeout_seconds,
        "run_at": task.run_at.isoformat() if task.run_at is not None else None,
        "retries_left": task.retries_left,
        "review_required": task.review_required,
        "review_branch": task.review_branch,
        "review_commit_sha": task.review_commit_sha,
    }


# ------------------------------------------------------------------ #
# Shared HTTP / auth helpers                                          #
# ------------------------------------------------------------------ #

ERROR_DETAIL_LIMIT = 500


def require_env_token(token_env: str, label: str, error_cls: type) -> str:
    """Return the env token or raise ``error_cls`` with a standard message."""
    token = os.environ.get(token_env)
    if not token:
        raise error_cls(f"{label} token environment variable {token_env!r} is not set")
    return token


def execute_rest_with_oauth_retry(
    *,
    build_request: Callable[[], urllib.request.Request],
    timeout: float,
    error_cls: type[Exception],
    method: str,
    has_oauth: bool,
    get_cached_provider: Callable[[], Any | None],
) -> Any:
    """Run ``build_request()`` with one OAuth retry on 401/403 (GitHub/GitLab)."""
    for attempt in (0, 1):
        request = build_request()
        try:
            return execute_json_request(request, timeout, error_cls, method)
        except error_cls as exc:
            provider = get_cached_provider()
            if (
                attempt != 0
                or getattr(exc, "status", None) not in (401, 403)
                or not has_oauth
                or provider is None
            ):
                raise
            with contextlib.suppress(Exception):  # noqa: BLE001 - invalidation is best-effort
                provider.invalidate()
            continue


class RestTransportBase:
    """Authenticated REST transport shared by the Jira/GitHub/GitLab clients.

    All three clients did the same dance — lazy OAuth provider,
    ``_api_url`` and ``_request`` with one OAuth retry — differing only in
    header shape and URL prefix. Subclasses provide
    :meth:`_oauth_components` (plus :meth:`_extra_headers`/
    :meth:`_token_headers`/:meth:`_oauth_provider_kwargs`/:meth:`_api_base`
    when the defaults don't fit) and inherit the rest. Jira additionally
    overrides :meth:`_auth_headers` (basic auth) and :meth:`_request`
    (non-object bodies are errors). Used via ``asyncio.to_thread``.
    """

    request_error_cls: type[Exception] = Exception
    provider_label: str = "issue"
    api_prefix: str = ""

    def __init__(self, base_url: str, config: Any) -> None:
        self.base_url = base_url.rstrip("/")
        self.config = config
        self._oauth_provider: Any | None = None

    def _oauth_components(self) -> tuple[Any, Any, type[Exception]]:
        """Return ``(store_cls, provider_cls, oauth_error_cls)`` (lazy import)."""
        raise NotImplementedError

    def _oauth_provider_kwargs(self) -> dict[str, Any]:
        """Extra ``provider_cls(store, ...)`` kwargs (Jira refresh needs client ids)."""
        return {}

    def _token_headers(self, token: str) -> dict[str, str]:
        """Auth headers for a resolved ``token`` (Bearer by default)."""
        return {"Authorization": f"Bearer {token}"}

    def _auth_headers(self) -> dict[str, str]:
        """Resolve the PAT/OAuth token and shape it via :meth:`_token_headers`.

        Shared by the issue clients, which differed only in the
        header shape (GitLab additionally sends ``PRIVATE-TOKEN``) and in
        the provider label/error classes used for messages.
        """
        auth = self.config.auth
        if auth.token_env is not None:
            token = require_env_token(auth.token_env, self.provider_label, self.request_error_cls)
            return self._token_headers(token)
        if auth.oauth is not None:
            provider = self._oauth_token_provider()
            assert provider is not None
            _, _, oauth_error_cls = self._oauth_components()
            try:
                token = str(provider.token())
            except Exception as exc:
                if isinstance(exc, self.request_error_cls):
                    raise
                if isinstance(exc, oauth_error_cls):
                    raise self.request_error_cls(str(exc)) from exc
                raise self.request_error_cls(str(exc)) from exc
            return self._token_headers(token)
        raise self.request_error_cls(
            f"{self.provider_label} auth is not configured (token_env or oauth required)"
        )

    def _extra_headers(self) -> dict[str, str]:
        return {}

    def _oauth_token_provider(self) -> Any | None:
        """Lazy ``store``/``provider`` pair, cached on the client."""
        if self._oauth_provider is not None:
            return self._oauth_provider
        auth = self.config.auth
        if auth.oauth is None:
            return None
        store_cls, provider_cls, _ = self._oauth_components()
        token_file = auth.oauth.token_file
        store = (
            store_cls(path=token_file, api_base=self.base_url)
            if token_file is not None
            else store_cls(api_base=self.base_url)
        )
        provider = provider_cls(store, **self._oauth_provider_kwargs())
        self._oauth_provider = provider
        return provider

    def _api_base(self) -> str:
        """API host before ``api_prefix`` (Jira swaps it for OAuth ``cloud_id``)."""
        return self.base_url

    def _api_url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = f"{self._api_base()}{self.api_prefix}{path}"
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
        def _build_request() -> urllib.request.Request:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
            headers = {**self._auth_headers(), **self._extra_headers()}
            if body is not None:
                headers["Content-Type"] = "application/json"
            return urllib.request.Request(
                self._api_url(path, query), data=body, method=method, headers=headers
            )

        return execute_rest_with_oauth_retry(
            build_request=_build_request,
            timeout=self.config.timeout_seconds,
            error_cls=self.request_error_cls,
            method=method,
            has_oauth=self.config.auth.oauth is not None,
            get_cached_provider=lambda: self._oauth_provider,
        )


class RestIssueClientBase(RestTransportBase):
    """Integer-id issue CRUD over :class:`RestTransportBase` (GitHub/GitLab).

    Jira uses string keys and JQL search, so it inherits the transport only.
    Subclasses provide :meth:`_collection_path` and :meth:`_comment_path`
    (plus :meth:`_search_query` when the defaults don't fit) and inherit
    the rest.
    """

    update_method: str = "PATCH"

    def _collection_path(self) -> str:
        raise NotImplementedError

    def _comment_path(self, issue_id: int) -> str:
        raise NotImplementedError

    def _search_query(self, page: int, per_page: int, state: str) -> dict[str, Any]:
        del state
        return {"per_page": per_page, "page": page}

    def _item_path(self, issue_id: int) -> str:
        return f"{self._collection_path()}/{issue_id}"

    def search_issues(
        self, *, page: int = 1, per_page: int = 30, state: str = "all"
    ) -> list[dict[str, Any]]:
        data = self._request("GET", self._collection_path(), query=self._search_query(page, per_page, state))
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    def get_issue(self, issue_id: int) -> dict[str, Any]:
        return self._request("GET", self._item_path(issue_id))  # type: ignore[no-any-return]

    def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", self._collection_path(), payload=fields)  # type: ignore[no-any-return]

    def update_issue(self, issue_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        return self._request(self.update_method, self._item_path(issue_id), payload=fields)  # type: ignore[no-any-return]

    def add_comment(self, issue_id: int, body: str) -> None:
        self._request("POST", self._comment_path(issue_id), payload={"body": body})

    def delete_issue(self, issue_id: int) -> None:
        self._request("DELETE", self._item_path(issue_id))


def execute_json_request(
    request: urllib.request.Request,
    timeout: float,
    error_cls: type[Exception],
    method: str,
) -> Any:
    """Execute ``request`` and decode its JSON body, mapping errors to ``error_cls``.

    Shared by GitHub, GitLab and Jira clients: a successful empty body yields
    ``{}``, a non-JSON body raises ``error_cls``, and HTTP/IO errors are
    wrapped with the request URL and status. Callers that require an object
    payload validate the returned type themselves.
    """
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except OSError:
            detail = ""
        suffix = f" {detail[:ERROR_DETAIL_LIMIT]}" if detail else ""
        try:
            raise error_cls(  # type: ignore[call-arg]
                f"{method} {request.full_url} failed with HTTP {exc.code} {exc.reason}.{suffix}",
                status=exc.code,
            ) from exc
        except TypeError:
            raise error_cls(
                f"{method} {request.full_url} failed with HTTP {exc.code} {exc.reason}.{suffix}",
            ) from exc
    except OSError as exc:
        raise error_cls(f"{method} {request.full_url} failed: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise error_cls(
            f"{method} {request.full_url} returned a body that is not JSON: {exc}"
        ) from exc



