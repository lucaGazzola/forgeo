"""OAuth / browser-assisted authentication for GitHub.

GitHub traditionally uses a PAT stored in an environment variable
(``token_env``).  Browser login adds an OAuth alternative:

* **Device flow** (preferred for CLI): no client secret, no redirect
  server.  The CLI asks ``https://github.com/login/device/code`` for a
  ``user_code``/``verification_uri``, prints them, polls
  ``https://github.com/login/oauth/access_token`` until the user
  approves in the browser.

* **Browser (auth-code+PKCE) flow**: opens
  ``https://github.com/login/oauth/authorize`` in the user's browser,
  listens on a loopback ``http://127.0.0.1:0/callback`` for the code,
  exchanges it for a token.

Both flows persist the token to a file outside ``forgeo.yaml`` (``0600``)
and are read by :class:`GithubClient` at request time, mirroring the
``oauth.py`` client-credentials provider but file-backed.
"""

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

# Give up on the device-flow poll after this long.
DEFAULT_DEVICE_POLL_TIMEOUT_SECONDS = 300.0
# Poll interval returned by GitHub, fallback.
DEFAULT_DEVICE_POLL_INTERVAL = 5.0

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GithubOAuthError(RuntimeError):
    """A browser/device login step failed; message is user-facing."""


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

DEFAULT_GITHUB_TOKEN_DIR = Path.home() / ".config" / "forgeo" / "tokens"


def github_default_token_path(api_base: str | None = None) -> Path:
    """Default token file for a GitHub API base.

    ``https://api.github.com`` -> ``~/.config/forgeo/tokens/github.json``
    ``https://github.example.com/api/v3`` -> ``~/.config/forgeo/tokens/github_github.example.com.json``
    """
    base = (api_base or "https://api.github.com").rstrip("/")
    # Derive a host token: reuse logic similar to central._github_web_base
    # For GHE: https://github.example.com/api/v3 -> host github.example.com
    parsed = urlparse(base)
    host = parsed.hostname or "github"
    if host == "api.github.com":
        name = "github.json"
    else:
        # Use host, replacing dots for filename safety
        safe = host.replace(".", "_")
        name = f"github_{safe}.json"
    return DEFAULT_GITHUB_TOKEN_DIR / name


def github_oauth_base(api_base: str) -> str:
    """Derive the OAuth authorize/token base from a GitHub API base.

    Mirrors ``central._github_web_base``:
    * ``https://api.github.com`` -> ``https://github.com``
    * ``https://github.example.com/api/v3`` -> ``https://github.example.com``
    * otherwise strip trailing /api/v3 and any path.
    """
    base = api_base.rstrip("/")
    if base.endswith("/api/v3"):
        return base[:-7].rstrip("/")
    parsed = urlparse(base)
    if parsed.hostname == "api.github.com":
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://github.com{port}"
    return base


class GithubTokenStore:
    """Read/write a GitHub OAuth token file (``0600``, atomic)."""

    def __init__(self, path: Path | str | None = None, *, api_base: str | None = None) -> None:
        if path is not None:
            self.path = Path(path).expanduser()
        else:
            self.path = github_default_token_path(api_base)

    def load(self) -> dict[str, Any] | None:
        """Load token data, or ``None`` when missing/corrupt."""
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
        """Persist ``data`` atomically with ``0600``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # atomic via temp file + replace
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
        """Remove the stored token; returns True when deleted."""
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def token(self) -> str | None:
        """The access token, or ``None``."""
        data = self.load()
        if data is None:
            return None
        token = data.get("access_token")
        return token if isinstance(token, str) and token else None


# ---------------------------------------------------------------------------
# Cached provider (file-backed, thread-safe, in-memory expiry)
# ---------------------------------------------------------------------------

EXPIRY_MARGIN_SECONDS = 30.0


class GithubOAuthTokenProvider:
    """File-backed, cached token for ``GithubClient``.

    Mirrors ``oauth.ClientCredentialsTokenProvider`` but reads from a file
    that browser login wrote. Thread-safe.
    """

    def __init__(self, store: GithubTokenStore) -> None:
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
                raise GithubOAuthError(
                    f"GitHub OAuth token not found at {self.store.path}; run `forgeo auth login --provider github` or set a PAT."
                )
            access = str(data["access_token"])
            # GitHub OAuth tokens typically don't expire, but handle expires_in if present
            lifetime = data.get("expires_in")
            if isinstance(lifetime, int | float) and lifetime > 0:
                self._expires_at = time.monotonic() + max(float(lifetime) - EXPIRY_MARGIN_SECONDS, 0.0)
            else:
                # No expiry -> cache forever until invalidate()
                self._expires_at = float("inf")
            self._token = access
            return access

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def save_token(self, data: dict[str, Any]) -> None:
        """Persist ``data`` and prime the cache."""
        self.store.save(data)
        with self._lock:
            self._token = str(data["access_token"]) if data.get("access_token") else None
            lifetime = data.get("expires_in")
            if isinstance(lifetime, int | float) and lifetime > 0:
                self._expires_at = time.monotonic() + max(float(lifetime) - EXPIRY_MARGIN_SECONDS, 0.0)
            else:
                self._expires_at = float("inf")


# ---------------------------------------------------------------------------
# Device flow
# ---------------------------------------------------------------------------


def _post_form(url: str, fields: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    """POST application/x-www-form-urlencoded and decode JSON."""
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
                raise GithubOAuthError(f"Unexpected response from {url}: not a JSON object")
            if data.get("error"):
                # Include error_description for user-facing detail
                desc = data.get("error_description") or data.get("error") or ""
                raise GithubOAuthError(f"GitHub OAuth error at {url}: {data.get('error')}: {desc}")
            return data
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
            if detail:
                detail = f": {detail[:500]}"
        except Exception:
            pass
        raise GithubOAuthError(f"GitHub OAuth request to {url} failed with HTTP {exc.code}{detail}") from exc
    except OSError as exc:
        raise GithubOAuthError(f"GitHub OAuth request to {url} failed: {exc}") from exc


def request_device_code(
    client_id: str, oauth_base: str, scope: str | None = None, *, timeout: float = 30.0
) -> dict[str, Any]:
    """Ask GitHub for a device code; returns the JSON payload."""
    url = f"{oauth_base.rstrip('/')}/login/device/code"
    fields: dict[str, str] = {"client_id": client_id}
    if scope:
        fields["scope"] = scope
    return _post_form(url, fields, timeout=timeout)


def poll_device_token(
    client_id: str,
    device_code: str,
    oauth_base: str,
    interval: float = DEFAULT_DEVICE_POLL_INTERVAL,
    timeout: float = DEFAULT_DEVICE_POLL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Poll until the user approves the device code; returns token JSON."""
    url = f"{oauth_base.rstrip('/')}/login/oauth/access_token"
    deadline = time.monotonic() + timeout
    current_interval = max(interval, 1.0)
    while True:
        if time.monotonic() > deadline:
            raise GithubOAuthError("Device login timed out; run `forgeo auth login` again.")
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
            # GitHub returns 200 with error JSON, not HTTP error for pending; treat HTTP errors as fatal
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise GithubOAuthError(f"Device poll failed with HTTP {exc.code}: {detail[:500]}") from exc
        except OSError as exc:
            raise GithubOAuthError(f"Device poll failed: {exc}") from exc
        if not isinstance(data, dict):
            raise GithubOAuthError("Device poll returned non-object JSON")
        error = data.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            current_interval += 5.0
            continue
        if error == "expired_token":
            raise GithubOAuthError("Device code expired; run `forgeo auth login` again.")
        if error:
            desc = data.get("error_description") or error
            raise GithubOAuthError(f"Device flow error: {error}: {desc}")
        if not data.get("access_token"):
            raise GithubOAuthError("Device flow succeeded but no access_token was returned")
        return data


def run_device_flow(
    client_id: str,
    oauth_base: str,
    scope: str | None = None,
    *,
    open_browser: bool = True,
    timeout: float = DEFAULT_DEVICE_POLL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run the full device flow: request code, prompt user, poll.

    Returns the token JSON (with ``access_token``).
    """
    logger.info("Requesting GitHub device code for client %r", client_id)
    data = request_device_code(client_id, oauth_base, scope=scope)
    device_code = data.get("device_code")
    user_code = data.get("user_code")
    verification_uri = data.get("verification_uri")
    expires_in = data.get("expires_in")
    interval = float(data.get("interval", DEFAULT_DEVICE_POLL_INTERVAL))
    if not isinstance(device_code, str) or not isinstance(user_code, str) or not isinstance(verification_uri, str):
        raise GithubOAuthError(f"Device code response missing fields: {data}")
    # Show instructions
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


# ---------------------------------------------------------------------------
# Browser (authorization code + PKCE) flow
# ---------------------------------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    """Capture ``code``/``state`` from the loopback redirect."""

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
                f"<html><body><h1>Forgeo GitHub login failed</h1><p>{self.error}</p><p>You may close this window.</p></body></html>".encode()
            )
        elif self.code:
            self.wfile.write(
                b"<html><body><h1>Forgeo GitHub login succeeded</h1><p>You may close this window and return to the terminal.</p></body></html>"
            )
        else:
            self.wfile.write(b"<html><body><h1>Forgeo GitHub login</h1><p>No code received.</p></body></html>")

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("oauth callback %s", format % args)


def run_browser_flow(
    client_id: str,
    oauth_base: str,
    scope: str | None = None,
    *,
    client_secret: str | None = None,
    open_browser: bool = True,
    callback_port: int | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Open browser for GitHub OAuth and exchange code for token.

    Uses PKCE (S256) for public clients; falls back to client_secret for
    confidential clients when provided.
    """
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    if callback_port is not None and not 1 <= callback_port <= 65535:
        raise GithubOAuthError("OAuth callback port must be between 1 and 65535")
    # Use an ephemeral port by default; a fixed port can be supplied when the
    # provider requires an exact callback URL to be registered.
    try:
        server = HTTPServer(("127.0.0.1", callback_port or 0), _CallbackHandler)
    except OSError as exc:
        raise GithubOAuthError(f"Could not bind OAuth callback port: {exc}") from exc
    addr = server.server_address
    host: str = str(addr[0])
    port: int = int(addr[1])
    redirect_uri = f"http://{host}:{port}/callback"
    # Build authorize URL
    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope or "repo",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    # Clean empty scope handling
    if not scope:
        params["scope"] = "repo"
    auth_url = f"{oauth_base.rstrip('/')}/login/oauth/authorize?{urlencode(params)}"
    if open_browser:
        print(f"\nOpening browser for GitHub login:\n  {auth_url}\n")
        try:
            webbrowser.open(auth_url)
        except Exception:
            print(f"Could not open browser automatically; please open:\n  {auth_url}")
    else:
        print(f"\nOpen this URL in your browser to authorize Forgeo:\n  {auth_url}\n")
    # Wait for callback
    server.timeout = timeout

    # Custom server to capture handler instance
    # Monkey-patch: wrap handler class to stash last instance
    last_handler: list[_CallbackHandler] = []

    def _finish(request: Any, client_address: Any) -> None:
        # Instantiate handler manually to capture
        handler_inst = _CallbackHandler(request, client_address, server)
        last_handler.append(handler_inst)
        # Do not call original_finish which would instantiate second time

    server.finish_request = _finish  # type: ignore[method-assign]
    # Wait for one request or timeout
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
        raise GithubOAuthError(f"GitHub OAuth authorize error: {error}")
    if not code:
        raise GithubOAuthError("Browser login timed out waiting for GitHub callback.")
    if received_state != state:
        raise GithubOAuthError("OAuth state mismatch (possible CSRF); try again.")
    # Exchange code for token
    token_url = f"{oauth_base.rstrip('/')}/login/oauth/access_token"
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
