"""Helpers for the central web dashboard (:mod:`forgeo.central`).

The dashboard speaks JSON and serves static files over the stdlib
``http.server``; these small, pure helpers keep the handler consistent
(same bounds, same static path guards, same serialization).
"""

from __future__ import annotations

import json
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

WEB_ROOT = Path(__file__).resolve().parent / "web"

DEFAULT_LOG_LINES = 100
MAX_LOG_LINES = 10_000
DEFAULT_RUN_LIMIT = 10
MAX_RUN_LIMIT = 10_000
DEFAULT_RUN_OFFSET = 0
MAX_RUN_OFFSET = 1_000_000


def json_bytes(payload: Any) -> bytes:
    """Serialize ``payload`` to pretty JSON bytes with a trailing newline."""
    return json.dumps(payload, indent=2, default=str).encode("utf-8") + b"\n"


def iso(value: datetime) -> str:
    """Render a datetime as an ISO-8601 string with an explicit UTC offset."""
    if value.tzinfo is None:
        return value.isoformat() + "Z"
    return value.isoformat()


def tail_lines(path: Path, n: int) -> list[str]:
    """Return the last ``n`` lines of ``path``, tolerating a missing file.

    A missing or unreadable file reads as an empty list.
    """
    if n <= 0 or not path.exists():
        return []
    try:
        from collections import deque

        with path.open(encoding="utf-8", errors="replace") as file:
            return [line.rstrip("\r\n") for line in deque(file, maxlen=n)]
    except OSError:
        return []


def clamp_query_int(
    query: dict[str, list[str]], key: str, default: int, maximum: int
) -> int:
    """Parse a bounded non-negative integer from a query parameter."""
    raw = query.get(key, [str(default)])[0]
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(0, min(value, maximum))


def safe_static_path(url_path: str, root: Path = WEB_ROOT) -> Path | None:
    """Resolve a URL path under ``root``, rejecting traversal.

    ``/`` and trailing slashes resolve to ``index.html``; a resolved path
    that escapes ``root`` or is not a regular file returns ``None``.
    """
    rel = unquote(url_path).lstrip("/")
    if not rel:
        rel = "index.html"
    elif rel.endswith("/"):
        rel = rel + "index.html"
    root = root.resolve()
    if not root.is_dir():
        return None
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    return None


def guess_content_type(path: Path) -> str:
    """A MIME type for a static file, falling back to ``application/octet-stream``."""
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"
