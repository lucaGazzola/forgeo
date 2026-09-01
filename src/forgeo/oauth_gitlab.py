"""OAuth / browser-assisted authentication for GitLab."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

logger = logging.getLogger(__name__)

DEFAULT_DEVICE_POLL_TIMEOUT_SECONDS = 300.0
DEFAULT_DEVICE_POLL_INTERVAL = 5.0


class GitlabOAuthError(RuntimeError):
    """A browser/device login step failed; message is user-facing."""


DEFAULT_GITLAB_TOKEN_DIR = Path.home() / ".config" / "forgeo" / "tokens"


def gitlab_default_token_path(api_base: str | None = None) -> Path:
    """Default token file for a GitLab base."""
    base = (api_base or "https://gitlab.com").rstrip("/")
    # api_base may be https://gitlab.com or https://gitlab.example.com/api/v4 or https://gitlab.example.com
    # Strip /api/v4 suffix for host derivation
    if base.endswith("/api/v4"):
        base = base[: -len("/api/v4")]
    parsed = urlparse(base)
    host = parsed.hostname or "gitlab"
    if host == "gitlab.com":
        name = "gitlab.json"
    else:
        safe = host.replace(".", "_")
        name = f"gitlab_{safe}.json"
    return DEFAULT_GITLAB_TOKEN_DIR / name


def gitlab_oauth_base(api_base: str) -> str:
    """Derive OAuth base from a GitLab API base."""
    base = api_base.rstrip("/")
    if base.endswith("/api/v4"):
        base = base[: -len("/api/v4")]
    # Also strip possible trailing /api
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return base.rstrip("/")


class GitlabTokenStore:
    """Read/write a GitLab OAuth token file (0600, atomic)."""

    def __init__(self, path: Path | str | None = None, *, api_base: str | None = None) -> None:
        if path is not None:
            self.path = Path(path).expanduser()
        else:
            self.path = gitlab_default_token_path(api_base)

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

    def token(self) -> str | None:
        data = self.load()
        if data is None:
            return None
        token = data.get("access_token")
        return token if isinstance(token, str) and token else None


EXPIRY_MARGIN_SECONDS = 30.0


class GitlabOAuthTokenProvider:
    """File-backed, cached token for GitlabClient."""

    def __init__(self, store: GitlabTokenStore) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    def token(self) -> str:
        with self._lock:
            if self._token is not None and time.monotonic() < self._expires_at:
                return self._token
            data = self.store.load()
            if data is None or not data.get("access_token"):
                raise GitlabOAuthError(
                    f"GitLab OAuth token not found at {self.store.path}; run `forgeo auth login --provider gitlab` or set a PAT."
                )
            access = str(data["access_token"])
            lifetime = data.get("expires_in")
            if isinstance(lifetime, int | float) and lifetime > 0:
                self._expires_at = time.monotonic() + max(float(lifetime) - EXPIRY_MARGIN_SECONDS, 0.0)
            else:
                self._expires_at = float("inf")
            self._token = access
            return access

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def save_token(self, data: dict[str, Any]) -> None:
        self.store.save(data)
        with self._lock:
            self._token = str(data["access_token"]) if data.get("access_token") else None
            lifetime = data.get("expires_in")
            if isinstance(lifetime, int | float) and lifetime > 0:
                self._expires_at = time.monotonic() + max(float(lifetime) - EXPIRY_MARGIN_SECONDS, 0.0)
            else:
                self._expires_at = float("inf")


def _post_form(url: str, fields: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
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
                raise GitlabOAuthError(f"Unexpected response from {url}: not a JSON object")
            if data.get("error"):
                desc = data.get("error_description") or data.get("error") or ""
                raise GitlabOAuthError(f"GitLab OAuth error at {url}: {data.get('error')}: {desc}")
            return data
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
            if detail:
                detail = f": {detail[:500]}"
        except Exception:
            pass
        raise GitlabOAuthError(f"GitLab OAuth request to {url} failed with HTTP {exc.code}{detail}") from exc
    except OSError as exc:
        raise GitlabOAuthError(f"GitLab OAuth request to {url} failed: {exc}") from exc


def request_device_code(
    client_id: str, oauth_base: str, scope: str | None = None, *, timeout: float = 30.0
) -> dict[str, Any]:
    # GitLab device flow endpoint: /oauth/authorize_device (if enabled) or fallback to /oauth/device/code
    # Try standard RFC8628 endpoint first: /oauth/device/code
    urls = [
        f"{oauth_base.rstrip('/')}/oauth/device/code",
        f"{oauth_base.rstrip('/')}/oauth/authorize_device",
    ]
    last: Exception | None = None
    for url in urls:
        try:
            fields: dict[str, str] = {"client_id": client_id}
            if scope:
                fields["scope"] = scope
            return _post_form(url, fields, timeout=timeout)
        except GitlabOAuthError as exc:
            last = exc
            # try next url on 404
            if "404" in str(exc):
                continue
            raise
    raise GitlabOAuthError(f"GitLab device flow not available at {oauth_base}: {last}") from last


def poll_device_token(
    client_id: str,
    device_code: str,
    oauth_base: str,
    interval: float = DEFAULT_DEVICE_POLL_INTERVAL,
    timeout: float = DEFAULT_DEVICE_POLL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    url = f"{oauth_base.rstrip('/')}/oauth/token"
    deadline = time.monotonic() + timeout
    current_interval = max(interval, 1.0)
    while True:
        if time.monotonic() > deadline:
            raise GitlabOAuthError("Device login timed out; run `forgeo auth login` again.")
        time.sleep(current_interval)
        fields = {
            "client_id": client_id,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }
        body = urlencode(fields).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise GitlabOAuthError(f"Device poll failed with HTTP {exc.code}: {detail[:500]}") from exc
        except OSError as exc:
            raise GitlabOAuthError(f"Device poll failed: {exc}") from exc
        if not isinstance(data, dict):
            raise GitlabOAuthError("Device poll returned non-object JSON")
        error = data.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            current_interval += 5.0
            continue
        if error == "expired_token":
            raise GitlabOAuthError("Device code expired; run `forgeo auth login` again.")
        if error:
            desc = data.get("error_description") or error
            raise GitlabOAuthError(f"Device flow error: {error}: {desc}")
        if not data.get("access_token"):
            raise GitlabOAuthError("Device flow succeeded but no access_token was returned")
        return data


def run_device_flow(
    client_id: str,
    oauth_base: str,
    scope: str | None = None,
    *,
    open_browser: bool = True,
    timeout: float = DEFAULT_DEVICE_POLL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    logger.info("Requesting GitLab device code for client %r", client_id)
    data = request_device_code(client_id, oauth_base, scope=scope)
    device_code = data.get("device_code")
    user_code = data.get("user_code")
    verification_uri = data.get("verification_uri") or data.get("verification_url")
    expires_in = data.get("expires_in")
    interval = float(data.get("interval", DEFAULT_DEVICE_POLL_INTERVAL))
    if not isinstance(device_code, str) or not isinstance(user_code, str) or not isinstance(verification_uri, str):
        raise GitlabOAuthError(f"Device code response missing fields: {data}")
    print(f"\nOpen {verification_uri} in your browser and enter code: {user_code}\n")
    if verification_uri and open_browser:
        try:
            webbrowser.open(verification_uri)
            print(f"(opened browser to {verification_uri})")
        except Exception:
            pass
    if expires_in:
        print(f"Code expires in {expires_in}s")
    print("Waiting for approval...", flush=True)
    token = poll_device_token(client_id, device_code, oauth_base, interval=interval, timeout=timeout)
    return token


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
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
                f"<html><body><h1>Forgeo GitLab login failed</h1><p>{self.error}</p><p>You may close this window.</p></body></html>".encode()
            )
        elif self.code:
            self.wfile.write(
                b"<html><body><h1>Forgeo GitLab login succeeded</h1><p>You may close this window and return to the terminal.</p></body></html>"
            )
        else:
            self.wfile.write(b"<html><body><h1>Forgeo GitLab login</h1><p>No code received.</p></body></html>")

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("oauth callback %s", format % args)


def run_browser_flow(
    client_id: str,
    oauth_base: str,
    scope: str | None = None,
    *,
    client_secret: str | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    addr = server.server_address
    host: str = str(addr[0])
    port: int = int(addr[1])
    redirect_uri = f"http://{host}:{port}/callback"
    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope or "api",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if not scope:
        params["scope"] = "api"
    auth_url = f"{oauth_base.rstrip('/')}/oauth/authorize?{urlencode(params)}"
    print(f"\nOpening browser for GitLab login:\n  {auth_url}\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        print(f"Could not open browser automatically; please open:\n  {auth_url}")
    server.timeout = timeout
    last_handler: list[_CallbackHandler] = []

    def _finish(request: Any, client_address: Any) -> None:
        handler_inst = _CallbackHandler(request, client_address, server)
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
    if error:
        raise GitlabOAuthError(f"GitLab OAuth authorize error: {error}")
    if not code:
        raise GitlabOAuthError("Browser login timed out waiting for GitLab callback.")
    if received_state != state:
        raise GitlabOAuthError("OAuth state mismatch (possible CSRF); try again.")
    token_url = f"{oauth_base.rstrip('/')}/oauth/token"
    fields: dict[str, str] = {
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
    }
    if client_secret:
        fields["client_secret"] = client_secret
    return _post_form(token_url, fields)
