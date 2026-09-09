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

import logging
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from forgeo.oauth_common import (
    EXPIRY_MARGIN_SECONDS,
    CachedFileTokenProvider,
    CallbackHandler,
    FileTokenStore,
    announce_device_code,
    bind_loopback,
    open_authorize_url,
    pkce_pair,
    poll_device_grant,
    post_form,
    wait_for_callback,
)

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


class GithubTokenStore(FileTokenStore):
    """Read/write a GitHub OAuth token file (``0600``, atomic)."""

    def __init__(self, path: Path | str | None = None, *, api_base: str | None = None) -> None:
        super().__init__(Path(path).expanduser() if path is not None else github_default_token_path(api_base))


# ---------------------------------------------------------------------------
# Cached provider (file-backed, thread-safe, in-memory expiry)
# ---------------------------------------------------------------------------


class GithubOAuthTokenProvider(CachedFileTokenProvider):
    """File-backed, cached token for ``GithubClient``.

    Mirrors ``oauth.ClientCredentialsTokenProvider`` but reads from a file
    that browser login wrote. Thread-safe.
    """

    error_cls = GithubOAuthError
    missing_message = (
        "GitHub OAuth token not found at {path}; run `forgeo auth login --provider github` or set a PAT."
    )


# ---------------------------------------------------------------------------
# Device flow
# ---------------------------------------------------------------------------


def _post_form(url: str, fields: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    """POST application/x-www-form-urlencoded and decode JSON."""
    return post_form(url, fields, timeout, GithubOAuthError, label="GitHub OAuth")


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
    return poll_device_grant(
        token_url=f"{oauth_base.rstrip('/')}/login/oauth/access_token",
        client_id=client_id,
        device_code=device_code,
        interval=interval,
        timeout=timeout,
        error_cls=GithubOAuthError,
    )


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
    device_code, interval = announce_device_code(
        data, GithubOAuthError, open_browser=open_browser, default_interval=DEFAULT_DEVICE_POLL_INTERVAL
    )
    return poll_device_token(client_id, device_code, oauth_base, interval=interval, timeout=timeout)


# ---------------------------------------------------------------------------
# Browser (authorization code + PKCE) flow
# ---------------------------------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    return pkce_pair()


class _CallbackHandler(CallbackHandler):
    """Capture ``code``/``state`` from the loopback redirect."""

    provider_label = "GitHub"


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
    server = bind_loopback(_CallbackHandler, callback_port, GithubOAuthError)
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
    open_authorize_url(auth_url, "GitHub", open_browser=open_browser)
    code, redirect_uri = wait_for_callback(server, _CallbackHandler, state, timeout, GithubOAuthError, "GitHub")
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


__all__ = [
    "DEFAULT_DEVICE_POLL_INTERVAL",
    "DEFAULT_DEVICE_POLL_TIMEOUT_SECONDS",
    "DEFAULT_GITHUB_TOKEN_DIR",
    "EXPIRY_MARGIN_SECONDS",
    "GithubOAuthError",
    "GithubOAuthTokenProvider",
    "GithubTokenStore",
    "github_default_token_path",
    "github_oauth_base",
    "poll_device_token",
    "request_device_code",
    "run_browser_flow",
    "run_device_flow",
]
