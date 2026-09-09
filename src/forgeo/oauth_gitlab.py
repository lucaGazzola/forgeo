"""OAuth / browser-assisted authentication for GitLab."""

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


class GitlabTokenStore(FileTokenStore):
    """Read/write a GitLab OAuth token file (0600, atomic)."""

    def __init__(self, path: Path | str | None = None, *, api_base: str | None = None) -> None:
        super().__init__(Path(path).expanduser() if path is not None else gitlab_default_token_path(api_base))


class GitlabOAuthTokenProvider(CachedFileTokenProvider):
    """File-backed, cached token for GitlabClient."""

    error_cls = GitlabOAuthError
    missing_message = (
        "GitLab OAuth token not found at {path}; run `forgeo auth login --provider gitlab` or set a PAT."
    )


def _post_form(url: str, fields: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    return post_form(url, fields, timeout, GitlabOAuthError, label="GitLab OAuth")


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
    return poll_device_grant(
        token_url=f"{oauth_base.rstrip('/')}/oauth/token",
        client_id=client_id,
        device_code=device_code,
        interval=interval,
        timeout=timeout,
        error_cls=GitlabOAuthError,
    )


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
    device_code, interval = announce_device_code(
        data,
        GitlabOAuthError,
        open_browser=open_browser,
        extra_url_keys=("verification_url",),
        default_interval=DEFAULT_DEVICE_POLL_INTERVAL,
    )
    return poll_device_token(client_id, device_code, oauth_base, interval=interval, timeout=timeout)


def _pkce_pair() -> tuple[str, str]:
    return pkce_pair()


class _CallbackHandler(CallbackHandler):
    provider_label = "GitLab"


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
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    server = bind_loopback(_CallbackHandler, callback_port, GitlabOAuthError)
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
    open_authorize_url(auth_url, "GitLab", open_browser=open_browser)
    code, redirect_uri = wait_for_callback(server, _CallbackHandler, state, timeout, GitlabOAuthError, "GitLab")
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


__all__ = [
    "DEFAULT_DEVICE_POLL_INTERVAL",
    "DEFAULT_DEVICE_POLL_TIMEOUT_SECONDS",
    "DEFAULT_GITLAB_TOKEN_DIR",
    "EXPIRY_MARGIN_SECONDS",
    "GitlabOAuthError",
    "GitlabOAuthTokenProvider",
    "GitlabTokenStore",
    "gitlab_default_token_path",
    "gitlab_oauth_base",
    "poll_device_token",
    "request_device_code",
    "run_browser_flow",
    "run_device_flow",
]
