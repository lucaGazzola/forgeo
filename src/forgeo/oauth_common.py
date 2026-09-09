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


def post_form(
    url: str,
    fields: dict[str, str],
    timeout: float = 30.0,
    error_cls: type[Exception] = RuntimeError,
    *,
    label: str = "OAuth",
) -> dict[str, Any]:
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
                raise error_cls(f"{label} error at {url}: {data.get('error')}: {desc}")
            return data
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
            if detail:
                detail = f": {detail[:500]}"
        except Exception:
            pass
        raise error_cls(f"{label} request to {url} failed with HTTP {exc.code}{detail}") from exc
    except OSError as exc:
        raise error_cls(f"{label} request to {url} failed: {exc}") from exc


class CallbackHandler(BaseHTTPRequestHandler):
    """Capture ``code``/``state`` from the loopback redirect.

    Subclasses set :attr:`provider_label` (``"GitHub"`` …) for the HTML page;
    the default renders the generic ``"Forgeo login …"`` titles.
    """

    provider_label: str = ""
    code: str | None = None
    state: str | None = None
    error: str | None = None

    @property
    def _title(self) -> str:
        return f"Forgeo {self.provider_label} login" if self.provider_label else "Forgeo login"

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
                f"<html><body><h1>{self._title} failed</h1><p>{self.error}</p><p>You may close this window.</p></body></html>".encode()
            )
        elif self.code:
            self.wfile.write(
                f"<html><body><h1>{self._title} succeeded</h1><p>You may close this window and return to the terminal.</p></body></html>".encode()
            )
        else:
            self.wfile.write(f"<html><body><h1>{self._title}</h1><p>No code received.</p></body></html>".encode())

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("oauth callback %s", format % args)


def bind_loopback(handler_cls: type[CallbackHandler], callback_port: int | None, error_cls: type[Exception]) -> HTTPServer:
    """Bind a loopback ``/callback`` server (ephemeral port by default)."""
    if callback_port is not None and not 1 <= callback_port <= 65535:
        raise error_cls("OAuth callback port must be between 1 and 65535")
    try:
        return HTTPServer(("127.0.0.1", callback_port or 0), handler_cls)
    except OSError as exc:
        raise error_cls(f"Could not bind OAuth callback port: {exc}") from exc


def wait_for_callback(
    server: HTTPServer,
    handler_cls: type[CallbackHandler],
    state: str,
    timeout: float,
    error_cls: type[Exception],
    provider_label: str,
) -> tuple[str, str]:
    """Wait for one OAuth redirect, validating ``state``; return ``(code, redirect_uri)``."""
    addr = server.server_address
    host: str = str(addr[0])
    port: int = int(addr[1])
    redirect_uri = f"http://{host}:{port}/callback"
    last_handler: list[CallbackHandler] = []

    def _finish(request: Any, client_address: Any) -> None:
        last_handler.append(handler_cls(request, client_address, server))

    server.finish_request = _finish  # type: ignore[method-assign]
    server.timeout = timeout
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
        raise error_cls(f"{provider_label} OAuth authorize error: {error}")
    if not code:
        raise error_cls(f"Browser login timed out waiting for {provider_label} callback.")
    if received_state != state:
        raise error_cls("OAuth state mismatch (possible CSRF); try again.")
    return code, redirect_uri


def open_authorize_url(auth_url: str, provider_label: str, *, open_browser: bool) -> None:
    """Print (and optionally open) an OAuth authorize URL."""
    if open_browser:
        print(f"\nOpening browser for {provider_label} login:\n  {auth_url}\n")
        try:
            import webbrowser

            webbrowser.open(auth_url)
        except Exception:
            print(f"Could not open browser automatically; please open:\n  {auth_url}")
    else:
        print(f"\nOpen this URL in your browser to authorize Forgeo:\n  {auth_url}\n")


def poll_device_grant(
    *,
    token_url: str,
    client_id: str,
    device_code: str,
    interval: float,
    timeout: float,
    error_cls: type[Exception],
) -> dict[str, Any]:
    """Poll an RFC8628 device-grant token endpoint until approval; return token JSON."""
    import json as _json

    deadline = time.monotonic() + timeout
    current_interval = max(interval, 1.0)
    while True:
        if time.monotonic() > deadline:
            raise error_cls("Device login timed out; run `forgeo auth login` again.")
        time.sleep(current_interval)
        fields = {
            "client_id": client_id,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }
        body = urlencode(fields).encode("utf-8")
        req = urllib.request.Request(
            token_url,
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise error_cls(f"Device poll failed with HTTP {exc.code}: {detail[:500]}") from exc
        except OSError as exc:
            raise error_cls(f"Device poll failed: {exc}") from exc
        if not isinstance(data, dict):
            raise error_cls("Device poll returned non-object JSON")
        error = data.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            current_interval += 5.0
            continue
        if error == "expired_token":
            raise error_cls("Device code expired; run `forgeo auth login` again.")
        if error:
            desc = data.get("error_description") or error
            raise error_cls(f"Device flow error: {error}: {desc}")
        if not data.get("access_token"):
            raise error_cls("Device flow succeeded but no access_token was returned")
        return data


def announce_device_code(
    data: dict[str, Any],
    error_cls: type[Exception],
    *,
    open_browser: bool = True,
    extra_url_keys: tuple[str, ...] = (),
    default_interval: float = 5.0,
) -> tuple[str, float]:
    """Validate a device-code response, print user instructions, return ``(device_code, interval)``."""
    import webbrowser

    device_code = data.get("device_code")
    user_code = data.get("user_code")
    verification_uri: Any = data.get("verification_uri")
    for key in extra_url_keys:
        if not isinstance(verification_uri, str):
            verification_uri = data.get(key)
    expires_in = data.get("expires_in")
    interval = float(data.get("interval", default_interval))
    if not isinstance(device_code, str) or not isinstance(user_code, str) or not isinstance(verification_uri, str):
        raise error_cls(f"Device code response missing fields: {data}")
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
    return device_code, interval


def run_loopback(state: str, timeout: float = 300.0) -> tuple[str | None, str | None, str | None]:
    """Run loopback server and capture code/state/error."""
    server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
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

    def token(self) -> str | None:
        """The access token, or ``None``."""
        data = self.load()
        if data is None:
            return None
        token = data.get("access_token")
        return token if isinstance(token, str) and token else None


EXPIRY_MARGIN_SECONDS = 30.0


class CachedFileTokenProvider:
    """File-backed, cached token with in-memory expiry; thread-safe.

    Subclasses set :attr:`error_cls` and :attr:`missing_message`.
    """

    error_cls: type[Exception] = RuntimeError
    missing_message: str = "OAuth token not found; run `forgeo auth login`."
    expiry_margin: float = EXPIRY_MARGIN_SECONDS

    def __init__(self, store: FileTokenStore) -> None:
        from threading import Lock

        self.store = store
        self._lock = Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    def _missing_error(self) -> Exception:
        return self.error_cls(self.missing_message.format(path=self.store.path))

    def token(self) -> str:
        with self._lock:
            if self._token is not None and time.monotonic() < self._expires_at:
                return self._token
            data = self.store.load()
            if data is None or not data.get("access_token"):
                raise self._missing_error()
            access = str(data["access_token"])
            lifetime = data.get("expires_in")
            if isinstance(lifetime, int | float) and lifetime > 0:
                self._expires_at = time.monotonic() + max(float(lifetime) - self.expiry_margin, 0.0)
            else:
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
                self._expires_at = time.monotonic() + max(float(lifetime) - self.expiry_margin, 0.0)
            else:
                self._expires_at = float("inf")
