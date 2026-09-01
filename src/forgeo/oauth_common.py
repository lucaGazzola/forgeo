"""Shared helpers for OAuth / browser login (GitHub, GitLab, Jira).

Extracted to avoid duplication across ``oauth_github``, ``oauth_gitlab`` and
``oauth_jira``. Each provider still has its own TokenStore/Provider with
provider-specific defaults, but the PKCE, loopback, token-file and HTTP
helpers are shared.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

logger = logging.getLogger(__name__)


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def post_form(url: str, fields: dict[str, str], timeout: float = 30.0, error_cls: type[Exception] = RuntimeError) -> dict[str, Any]:
    body = urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise error_cls(f"Unexpected response from {url}: not a JSON object")
            if data.get("error"):
                desc = data.get("error_description") or data.get("error") or ""
                raise error_cls(f"OAuth error at {url}: {data.get('error')}: {desc}")
            return data
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
            if detail:
                detail = f": {detail[:500]}"
        except Exception:
            pass
        raise error_cls(f"OAuth request to {url} failed with HTTP {exc.code}{detail}") from exc
    except OSError as exc:
        raise error_cls(f"OAuth request to {url} failed: {exc}") from exc


class CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    state: str | None = None
    error: str | None = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        code_vals = qs.get("code")
        state_vals = qs.get("state")
        error_vals = qs.get("error")
        self.code = code_vals[0] if code_vals else None
        self.state = state_vals[0] if state_vals else None
        self.error = error_vals[0] if error_vals else None
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if self.error:
            self.wfile.write(
                f"<html><body><h1>Forgeo login failed</h1><p>{self.error}</p><p>You may close this window.</p></body></html>".encode()
            )
        elif self.code:
            self.wfile.write(
                b"<html><body><h1>Forgeo login succeeded</h1><p>You may close this window and return to the terminal.</p></body></html>"
            )
        else:
            self.wfile.write(b"<html><body><h1>Forgeo login</h1><p>No code received.</p></body></html>")

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("oauth callback %s", format % args)


def run_loopback(state: str, timeout: float = 300.0) -> tuple[str | None, str | None, str | None]:
    """Run loopback server and capture code/state/error."""
    server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
    addr = server.server_address
    host: str = str(addr[0])
    port: int = int(addr[1])
    redirect_uri = f"http://{host}:{port}/callback"
    server.timeout = timeout
    last_handler: list[CallbackHandler] = []

    def _finish(request: Any, client_address: Any) -> None:
        handler_inst = CallbackHandler(request, client_address, server)
        last_handler.append(handler_inst)

    server.finish_request = _finish  # type: ignore[method-assign]
    start = time.monotonic()
    code: str | None = None
    received_state: str | None = None
    error: str | None = None
    while time.monotonic() - start < timeout:
        server.handle_request()
        if last_handler:
            h = last_handler[-1]
            code = h.code
            received_state = h.state
            error = h.error
            if code or error:
                break
    server.server_close()
    return code, received_state, error  # caller checks state & error; redirect_uri is derived externally


class FileTokenStore:
    """Generic 0600 JSON token file store."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()

    def load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not data.get("access_token"):
            return None
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def clear(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False
