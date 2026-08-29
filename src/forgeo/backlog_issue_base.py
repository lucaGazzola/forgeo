"""Shared helpers for issue-backed providers (Jira, GitHub, GitLab)."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any


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


def forgeo_labels(prefix: str) -> dict[str, str]:
    """Standard forgeo labels for a prefix (running / blocked / failed)."""
    return {
        "running": f"{prefix}-running",
        "blocked": f"{prefix}-blocked",
        "failed": f"{prefix}-failed",
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
    }
)


# ------------------------------------------------------------------ #
# Shared HTTP / auth helpers                                          #
# ------------------------------------------------------------------ #

ERROR_DETAIL_LIMIT = 500


def http_error_detail_suffix(exc: urllib.error.HTTPError) -> str:
    """Truncated detail suffix for an HTTPError (empty when no detail)."""
    try:
        detail = exc.read().decode("utf-8", errors="replace")
    except OSError:
        detail = ""
    return f" {detail[:ERROR_DETAIL_LIMIT]}" if detail else ""


def require_env_token(token_env: str, label: str, error_cls: type) -> str:
    """Return the env token or raise ``error_cls`` with a standard message."""
    token = os.environ.get(token_env)
    if not token:
        raise error_cls(f"{label} token environment variable {token_env!r} is not set")
    return token


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
        suffix = http_error_detail_suffix(exc)
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



