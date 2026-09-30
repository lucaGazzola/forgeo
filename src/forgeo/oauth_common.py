"""Shared helpers for OAuth / browser login (GitHub, GitLab, Jira).

Extracted to avoid duplication across ``oauth_github``, ``oauth_gitlab`` and
``oauth_jira``. Each provider still has its own TokenStore/Provider with
provider-specific defaults, but the PKCE, loopback, token-file and HTTP
helpers are shared.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
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


def begin_browser_login(
    handler_cls: type[CallbackHandler],
    error_cls: type[Exception],
    callback_port: int | None,
) -> tuple[str, str, str, HTTPServer, str]:
    """Start a PKCE browser login: PKCE pair, state, loopback server, redirect URI."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    server = bind_loopback(handler_cls, callback_port, error_cls)
    addr = server.server_address
    host: str = str(addr[0])
    port: int = int(addr[1])
    redirect_uri = f"http://{host}:{port}/callback"
    return verifier, challenge, state, server, redirect_uri


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
            with contextlib.suppress(Exception):
                detail = exc.read().decode("utf-8", errors="replace")
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
        with contextlib.suppress(Exception):
            webbrowser.open(verification_uri)
            print(f"(opened browser to {verification_uri})")
    if expires_in:
        print(f"Code expires in {expires_in}s")
    print("Waiting for approval...", flush=True)
    return device_code, interval


def run_device_login(
    *,
    request_fn: Callable[..., dict[str, Any]],
    poll_fn: Callable[..., dict[str, Any]],
    error_cls: type[Exception],
    provider_label: str,
    client_id: str,
    oauth_base: str,
    scope: str | None = None,
    open_browser: bool = True,
    timeout: float = 300.0,
    extra_url_keys: tuple[str, ...] = (),
    default_interval: float = 5.0,
) -> dict[str, Any]:
    """Run the full device flow: request code, prompt user, poll.

    ``request_fn``/``poll_fn`` are the provider's ``request_device_code``/
    ``poll_device_token`` (``(client_id, oauth_base, scope=...)`` and
    ``(client_id, device_code, oauth_base, interval=..., timeout=...)``).
    Shared so GitHub/GitLab don't duplicate the request/announce/poll dance.
    """
    logger.info("Requesting %s device code for client %r", provider_label, client_id)
    data = request_fn(client_id, oauth_base, scope=scope)
    device_code, interval = announce_device_code(
        data,
        error_cls,
        open_browser=open_browser,
        extra_url_keys=extra_url_keys,
        default_interval=default_interval,
    )
    return poll_fn(client_id, device_code, oauth_base, interval=interval, timeout=timeout)


def run_pkce_browser_login(
    *,
    handler_cls: type[CallbackHandler],
    error_cls: type[Exception],
    provider_label: str,
    build_authorize_url: Callable[[str, str, str], str],
    build_token_fields: Callable[[str, str, str], dict[str, str]],
    post_fn: Callable[[str, dict[str, str]], dict[str, Any]],
    token_url: str,
    open_browser: bool = True,
    callback_port: int | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Run the PKCE browser flow: loopback server, authorize URL, code exchange.

    ``build_authorize_url(redirect_uri, state, challenge)`` returns the
    provider's authorize URL; ``build_token_fields(code, redirect_uri,
    verifier)`` returns the token-exchange fields (including
    ``client_secret`` when the caller wants it). Shared so GitHub/GitLab
    don't duplicate the begin/open/wait/exchange dance.
    """
    verifier, challenge, state, server, redirect_uri = begin_browser_login(
        handler_cls, error_cls, callback_port
    )
    auth_url = build_authorize_url(redirect_uri, state, challenge)
    open_authorize_url(auth_url, provider_label, open_browser=open_browser)
    code, redirect_uri = wait_for_callback(
        server, handler_cls, state, timeout, error_cls, provider_label
    )
    return post_fn(token_url, build_token_fields(code, redirect_uri, verifier))


def stamp_issued_at(data: dict[str, Any]) -> dict[str, Any]:
    """Copy ``data`` adding ``issued_at=now`` when it carries ``expires_in``.

    Lets file-backed providers judge absolute expiry (``issued_at`` +
    ``expires_in``) instead of only the in-memory cache clock, so a token
    file re-read after a restart still expires on time.
    """
    data = dict(data)
    if isinstance(data.get("expires_in"), int | float) and "issued_at" not in data:
        data["issued_at"] = time.time()
    return data


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
        from forgeo.io import atomic_write_private

        atomic_write_private(self.path, json.dumps(data, indent=2) + "\n")

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

    Subclasses set :attr:`error_cls` and :attr:`missing_message`. Providers
    with a refresh grant (e.g. Jira) override :meth:`_refresh_data`; the base
    never refreshes and just reuses the stored access token.
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
        self._refresh_requested = False

    def _missing_error(self) -> Exception:
        return self.error_cls(self.missing_message.format(path=self.store.path))

    def _refresh_data(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Try to renew an expired/invalidated token; return new data or ``None``."""
        del data
        return None

    def _stored_expired(self, data: dict[str, Any]) -> bool:
        """Whether the stored ``data`` is past its absolute expiry (needs ``issued_at``)."""
        expires_in = data.get("expires_in")
        issued_at = data.get("issued_at")
        return (
            isinstance(expires_in, int | float)
            and isinstance(issued_at, int | float)
            and time.time() >= float(issued_at) + float(expires_in) - self.expiry_margin
        )

    def _expires_at_for(self, data: dict[str, Any]) -> float:
        """In-memory cache deadline for ``data`` (``issued_at``-aware when stamped)."""
        lifetime = data.get("expires_in")
        if isinstance(lifetime, int | float) and lifetime > 0:
            remaining = float(lifetime) - self.expiry_margin
            issued_at = data.get("issued_at")
            if isinstance(issued_at, int | float):
                remaining = float(issued_at) + float(lifetime) - time.time() - self.expiry_margin
            return time.monotonic() + max(remaining, 0.0)
        return float("inf")

    def token(self) -> str:
        with self._lock:
            if self._token is not None and time.monotonic() < self._expires_at:
                return self._token
            data = self.store.load()
            if data is None or not data.get("access_token"):
                raise self._missing_error()
            if self._refresh_requested or self._stored_expired(data):
                refreshed = self._refresh_data(data)
                if refreshed:
                    data = refreshed
            self._refresh_requested = False
            access = str(data["access_token"])
            self._expires_at = self._expires_at_for(data)
            self._token = access
            return access

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0
            self._refresh_requested = True

    def save_token(self, data: dict[str, Any]) -> None:
        """Persist ``data`` (stamping ``issued_at``) and prime the cache."""
        data = stamp_issued_at(data)
        self.store.save(data)
        with self._lock:
            self._refresh_requested = False
            self._token = str(data["access_token"]) if data.get("access_token") else None
            self._expires_at = self._expires_at_for(data)


# Shared provider boilerplate (token paths, stores, callbacks, post, device).
DEFAULT_TOKEN_DIR = Path.home() / ".config" / "forgeo" / "tokens"
DEFAULT_DEVICE_POLL_TIMEOUT_SECONDS = 300.0
DEFAULT_DEVICE_POLL_INTERVAL = 5.0


def strip_url_suffix(base: str, suffixes: tuple[str, ...]) -> str:
    """Strip the first matching suffix from ``base``."""
    for suffix in suffixes:
        if base.endswith(suffix):
            return base[: -len(suffix)].rstrip("/")
    return base


def host_token_path(
    provider: str,
    api_base: str | None,
    *,
    default_base: str,
    plain_host: str,
    strip_suffixes: tuple[str, ...] = (),
) -> Path:
    """Default ``<provider>[_<host>].json`` token file for an API base."""
    base = (api_base or default_base).rstrip("/")
    if strip_suffixes:
        base = strip_url_suffix(base, strip_suffixes)
    host = urlparse(base).hostname or provider
    if host == plain_host:
        return DEFAULT_TOKEN_DIR / f"{provider}.json"
    return DEFAULT_TOKEN_DIR / f"{provider}_{host.replace('.', '_')}.json"


def github_web_base(api_base: str) -> str:
    """Derive the web base from a GitHub API base URL."""
    base = api_base.rstrip("/")
    if base.endswith("/api/v3"):
        return base[:-7].rstrip("/")
    parsed = urlparse(base)
    if parsed.hostname == "api.github.com":
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://github.com{port}"
    return base


def make_token_store(default_path_fn: Callable[[str | None], Path]) -> Any:
    """Build a ``FileTokenStore`` subclass bound to ``default_path_fn``."""

    class _ProviderTokenStore(FileTokenStore):
        def __init__(self, path: Path | str | None = None, *, api_base: str | None = None) -> None:
            super().__init__(Path(path).expanduser() if path is not None else default_path_fn(api_base))

    return _ProviderTokenStore


def make_callback_handler(provider_label: str) -> type[CallbackHandler]:
    """Build the loopback ``CallbackHandler`` subclass for ``provider_label``."""
    return type(f"_{provider_label}CallbackHandler", (CallbackHandler,), {"provider_label": provider_label})


def make_post_form(error_cls: type[Exception], label: str) -> Callable[..., dict[str, Any]]:
    """Build a ``(url, fields, timeout=30)`` POST helper bound to ``error_cls``."""

    def _post(url: str, fields: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
        return post_form(url, fields, timeout, error_cls, label=label)

    return _post


def make_browser_flow(
    handler_cls: type[CallbackHandler],
    error_cls: type[Exception],
    provider_label: str,
    post_fn: Callable[[str, dict[str, str]], dict[str, Any]],
    *,
    authorize_path: str,
    token_path: str,
    default_scope: str,
    extra_authorize_params: dict[str, str] | None = None,
) -> Callable[..., dict[str, Any]]:
    """Build a ``run_browser_flow`` wrapper around :func:`run_pkce_browser_login`.

    ``authorize_path``/``token_path`` are appended to the provider's
    ``oauth_base`` (e.g. ``"/login/oauth/authorize"`` for GitHub,
    ``"/oauth/authorize"`` for GitLab); ``default_scope`` fills in when the
    caller passes no ``scope``. Shared so GitHub/GitLab don't duplicate the
    authorize-URL/token-fields closures.
    """

    def run(
        client_id: str,
        oauth_base: str,
        scope: str | None = None,
        *,
        client_secret: str | None = None,
        open_browser: bool = True,
        callback_port: int | None = None,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        base = oauth_base.rstrip("/")

        def _authorize_url(redirect_uri: str, state: str, challenge: str) -> str:
            params: dict[str, str] = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": scope or default_scope,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
            if extra_authorize_params:
                params.update(extra_authorize_params)
            return f"{base}{authorize_path}?{urlencode(params)}"

        def _token_fields(code: str, redirect_uri: str, verifier: str) -> dict[str, str]:
            fields: dict[str, str] = {
                "client_id": client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
            }
            if client_secret:
                fields["client_secret"] = client_secret
            return fields

        return run_pkce_browser_login(
            handler_cls=handler_cls,
            error_cls=error_cls,
            provider_label=provider_label,
            build_authorize_url=_authorize_url,
            build_token_fields=_token_fields,
            post_fn=post_fn,
            token_url=f"{base}{token_path}",
            open_browser=open_browser,
            callback_port=callback_port,
            timeout=timeout,
        )

    return run


def make_poll_device_token(
    error_cls: type[Exception],
    token_path: str,
) -> Callable[..., dict[str, Any]]:
    """Build a ``poll_device_token`` polling ``oauth_base`` + ``token_path``."""


    def poll(
        client_id: str,
        device_code: str,
        oauth_base: str,
        interval: float = DEFAULT_DEVICE_POLL_INTERVAL,
        timeout: float = DEFAULT_DEVICE_POLL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return poll_device_grant(
            token_url=f"{oauth_base.rstrip('/')}{token_path}",
            client_id=client_id,
            device_code=device_code,
            interval=interval,
            timeout=timeout,
            error_cls=error_cls,
        )

    return poll


def make_device_flow(
    request_fn: Callable[..., dict[str, Any]],
    poll_fn: Callable[..., dict[str, Any]],
    error_cls: type[Exception],
    provider_label: str,
    *,
    extra_url_keys: tuple[str, ...] = (),
    default_interval: float = DEFAULT_DEVICE_POLL_INTERVAL,
) -> Callable[..., dict[str, Any]]:
    """Build a ``run_device_flow`` wrapper around :func:`run_device_login`."""

    def run(
        client_id: str,
        oauth_base: str,
        scope: str | None = None,
        *,
        open_browser: bool = True,
        timeout: float = DEFAULT_DEVICE_POLL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return run_device_login(
            request_fn=request_fn,
            poll_fn=poll_fn,
            error_cls=error_cls,
            provider_label=provider_label,
            client_id=client_id,
            oauth_base=oauth_base,
            scope=scope,
            open_browser=open_browser,
            timeout=timeout,
            extra_url_keys=extra_url_keys,
            default_interval=default_interval,
        )

    return run
