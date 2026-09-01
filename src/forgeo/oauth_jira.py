"""OAuth / browser-assisted authentication for Jira Cloud (Atlassian 3LO)."""

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


class JiraOAuthError(RuntimeError):
    """A Jira OAuth step failed; message is user-facing."""


DEFAULT_JIRA_TOKEN_DIR = Path.home() / ".config" / "forgeo" / "tokens"
ATLASSIAN_AUTH_BASE = "https://auth.atlassian.com"
ATLASSIAN_API_BASE = "https://api.atlassian.com"


def jira_default_token_path(api_base: str | None = None) -> Path:
    """Default token file for a Jira base."""
    base = (api_base or "https://jira.example.com").rstrip("/")
    parsed = urlparse(base)
    host = parsed.hostname or "jira"
    if "atlassian.net" in host or "atlassian.com" in host:
        # Use host prefix to avoid collisions, but keep generic name for many Atlassian sites
        safe = host.replace(".", "_")
        name = f"jira_{safe}.json"
    else:
        safe = host.replace(".", "_")
        name = f"jira_{safe}.json"
    # Fallback generic
    if name == "jira_jira.example.com.json":
        name = "jira.json"
    return DEFAULT_JIRA_TOKEN_DIR / name


def jira_oauth_base(api_base: str | None = None) -> str:
    """Jira OAuth base is always Atlassian auth; api_base not used but kept for symmetry."""
    return ATLASSIAN_AUTH_BASE


class JiraTokenStore:
    def __init__(self, path: Path | str | None = None, *, api_base: str | None = None) -> None:
        if path is not None:
            self.path = Path(path).expanduser()
        else:
            self.path = jira_default_token_path(api_base)

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


EXPIRY_MARGIN_SECONDS = 60.0


class JiraOAuthTokenProvider:
    """File-backed cached token with refresh support."""

    def __init__(self, store: JiraTokenStore, *, client_id: str | None = None, client_secret_env: str | None = None) -> None:
        self.store = store
        self.client_id = client_id
        self.client_secret_env = client_secret_env
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    def token(self) -> str:
        with self._lock:
            if self._token is not None and time.monotonic() < self._expires_at:
                return self._token
            data = self.store.load()
            if data is None or not data.get("access_token"):
                raise JiraOAuthError(
                    f"Jira OAuth token not found at {self.store.path}; run `forgeo auth login --provider jira`."
                )
            # Check expiry, attempt refresh if needed and possible
            expires_in = data.get("expires_in")
            # If we have a stored expires_at, use it; otherwise compute from expires_in if present
            # We store expires_at as absolute monotonic? Instead store expires_at timestamp? Simpler: use expires_in as lifetime and check if near expiry
            # For now, if expires_in present and we have issued_at, compute remaining
            # If token is considered expired, try refresh
            # We will treat if we have refresh_token and expires_in indicates near expiry, we refresh
            # For simplicity, if token has expires_in and we are past margin, try refresh
            # But we don't have issued time; we approximate that load time is close to issue time and use _expires_at cache
            # If _expires_at is inf (no expiry), just return
            # If we are here, either _token was None or expired ( _expires_at <= now ), so we need to maybe refresh
            if data.get("refresh_token") and isinstance(expires_in, int | float):
                # We don't know when token was issued; if we have no cached expiry, we try to use it as is unless we know it's expired
                # If we previously cached expiry and now it's expired, we are here because _expires_at <= now
                # So we should attempt refresh if we have refresh_token
                # Check if we should refresh: if _expires_at != inf and time.monotonic() >= _expires_at
                if self._expires_at != float("inf") and self._token is not None:
                    # We were cached and now expired -> refresh
                    refreshed = self._refresh(data)
                    if refreshed:
                        data = refreshed
            # Now set cache
            access = str(data["access_token"])
            lifetime = data.get("expires_in")
            if isinstance(lifetime, int | float) and lifetime > 0:
                self._expires_at = time.monotonic() + max(float(lifetime) - EXPIRY_MARGIN_SECONDS, 0.0)
            else:
                self._expires_at = float("inf")
            self._token = access
            return access

    def _refresh(self, data: dict[str, Any]) -> dict[str, Any] | None:
        refresh = data.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            return None
        if not self.client_id or not self.client_secret_env:
            return None
        secret = os.environ.get(self.client_secret_env) if self.client_secret_env else None
        if not secret:
            return None
        try:
            new_data = _refresh_token(self.client_id, secret, refresh)
            # Preserve cloud_id if not in new_data
            if "cloud_id" not in new_data and data.get("cloud_id"):
                new_data["cloud_id"] = data["cloud_id"]
            self.store.save(new_data)
            return new_data
        except Exception as exc:  # noqa: BLE001
            logger.warning("Jira token refresh failed: %s", exc)
            return None

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
                raise JiraOAuthError(f"Unexpected response from {url}: not a JSON object")
            if data.get("error"):
                desc = data.get("error_description") or data.get("error") or ""
                raise JiraOAuthError(f"Jira OAuth error at {url}: {data.get('error')}: {desc}")
            return data
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
            if detail:
                detail = f": {detail[:500]}"
        except Exception:
            pass
        raise JiraOAuthError(f"Jira OAuth request to {url} failed with HTTP {exc.code}{detail}") from exc
    except OSError as exc:
        raise JiraOAuthError(f"Jira OAuth request to {url} failed: {exc}") from exc


def _refresh_token(client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
    url = f"{ATLASSIAN_AUTH_BASE}/oauth/token"
    fields = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }
    return _post_form(url, fields)


def _fetch_accessible_resources(access_token: str) -> list[dict[str, Any]]:
    url = f"{ATLASSIAN_API_BASE}/oauth/token/accessible-resources"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
            return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not fetch accessible-resources: %s", exc)
        return []


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
                f"<html><body><h1>Forgeo Jira login failed</h1><p>{self.error}</p><p>You may close this window.</p></body></html>".encode()
            )
        elif self.code:
            self.wfile.write(
                b"<html><body><h1>Forgeo Jira login succeeded</h1><p>You may close this window and return to the terminal.</p></body></html>"
            )
        else:
            self.wfile.write(b"<html><body><h1>Forgeo Jira login</h1><p>No code received.</p></body></html>")

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("oauth callback %s", format % args)


def run_browser_flow(
    client_id: str,
    oauth_base: str | None = None,
    scope: str | None = None,
    *,
    client_secret: str | None = None,
    cloud_id: str | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Run Atlassian OAuth 3LO browser flow and return token data including cloud_id."""
    del oauth_base  # Atlassian base is fixed
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    addr = server.server_address
    host: str = str(addr[0])
    port: int = int(addr[1])
    redirect_uri = f"http://{host}:{port}/callback"
    # Atlassian scopes: offline_access required for refresh, plus Jira scopes
    # Default scope for Forgeo: read:jira-user read:jira-work offline_access
    eff_scope = scope or "offline_access read:jira-user read:jira-work"
    if "offline_access" not in eff_scope:
        eff_scope = eff_scope + " offline_access"
    params: dict[str, str] = {
        "audience": "api.atlassian.com",
        "client_id": client_id,
        "scope": eff_scope,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{ATLASSIAN_AUTH_BASE}/authorize?{urlencode(params)}"
    print(f"\nOpening browser for Jira login:\n  {auth_url}\n")
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
        raise JiraOAuthError(f"Jira OAuth authorize error: {error}")
    if not code:
        raise JiraOAuthError("Browser login timed out waiting for Jira callback.")
    if received_state != state:
        raise JiraOAuthError("OAuth state mismatch (possible CSRF); try again.")
    token_url = f"{ATLASSIAN_AUTH_BASE}/oauth/token"
    fields: dict[str, str] = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    if client_secret:
        fields["client_secret"] = client_secret
    token_data = _post_form(token_url, fields)
    # Fetch cloudId if not provided
    access = token_data.get("access_token")
    if not isinstance(access, str):
        raise JiraOAuthError("Token response missing access_token")
    if not cloud_id:
        resources = _fetch_accessible_resources(access)
        if resources:
            # If multiple, pick first or ask? For now pick first
            cloud_id = resources[0].get("id") if isinstance(resources[0].get("id"), str) else None
            if cloud_id:
                token_data["cloud_id"] = cloud_id
                # Also store site url for reference
                site_url = resources[0].get("url")
                if isinstance(site_url, str):
                    token_data["site_url"] = site_url
    else:
        token_data["cloud_id"] = cloud_id
    return token_data
