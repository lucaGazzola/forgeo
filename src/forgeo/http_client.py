"""Shared HTTP plumbing for backlog providers.

Centralises request building, JSON decode, and error translation so
GitHub/GitLab/Jira/HTTP document backends do not each reinvent urllib
boilerplate. Stdlib only, short timeouts, asyncio.to_thread for blocking.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from forgeo.backlog import BacklogUnavailableError


class HttpClientError(BacklogUnavailableError):
    """A request failed or returned unusable JSON."""


def build_auth_header(token_env: str, *, scheme: str = "Bearer") -> str:
    """Return an Authorization header value from an env var.

    Raises HttpClientError when the env var is missing.
    """
    token = os.environ.get(token_env)
    if not token:
        raise HttpClientError(f"Token environment variable {token_env!r} is not set")
    if scheme.lower() == "basic":
        # caller handles basic encoding separately
        return token
    return f"{scheme} {token}"


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | list[Any] | None = None,
    timeout: float = 30.0,
) -> Any:
    """Perform one HTTP request and return decoded JSON.

    Raises HttpClientError (subclass of BacklogUnavailableError) on failure.
    Empty body returns {}.
    """
    body: bytes | None = None
    req_headers: dict[str, str] = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            # also expose response headers for pagination helpers if needed
            # caller can inspect via alternative helper if needed
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except OSError:
            detail = ""
        suffix = f" {detail[:500]}" if detail else ""
        raise HttpClientError(
            f"{method} {request.full_url} failed with HTTP {exc.code} {exc.reason}.{suffix}",
        ) from exc
    except OSError as exc:
        raise HttpClientError(f"{method} {request.full_url} failed: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HttpClientError(
            f"{method} {request.full_url} returned a body that is not JSON: {exc}"
        ) from exc


def request_text(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: bytes | None = None,
    timeout: float = 30.0,
) -> str:
    """Perform one HTTP request and return body as text."""
    req_headers: dict[str, str] = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        req_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=payload, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return str(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except OSError:
            detail = ""
        suffix = f" {detail[:500]}" if detail else ""
        raise HttpClientError(
            f"{method} {request.full_url} failed with HTTP {exc.code} {exc.reason}.{suffix}",
        ) from exc
    except OSError as exc:
        raise HttpClientError(f"{method} {request.full_url} failed: {exc}") from exc
