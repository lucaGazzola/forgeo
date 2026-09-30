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

from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from forgeo.oauth_common import (
    DEFAULT_DEVICE_POLL_INTERVAL,
    DEFAULT_DEVICE_POLL_TIMEOUT_SECONDS,
    DEFAULT_TOKEN_DIR,
    EXPIRY_MARGIN_SECONDS,
    CachedFileTokenProvider,
    github_web_base,
    host_token_path,
    make_callback_handler,
    make_device_flow,
    make_post_form,
    make_token_store,
    poll_device_grant,
    run_pkce_browser_login,
)


class GithubOAuthError(RuntimeError):
    """A browser/device login step failed; message is user-facing."""


DEFAULT_GITHUB_TOKEN_DIR = DEFAULT_TOKEN_DIR


def github_default_token_path(api_base: str | None = None) -> Path:
    """Default token file for a GitHub API base."""
    return host_token_path(
        "github",
        api_base,
        default_base="https://api.github.com",
        plain_host="api.github.com",
        strip_suffixes=("/api/v3",),
    )


def github_oauth_base(api_base: str) -> str:
    """Derive the OAuth authorize/token base from a GitHub API base."""
    return github_web_base(api_base)


GithubTokenStore = make_token_store(github_default_token_path)


class GithubOAuthTokenProvider(CachedFileTokenProvider):
    """File-backed, cached token for ``GithubClient``."""

    error_cls = GithubOAuthError
    missing_message = (
        "GitHub OAuth token not found at {path}; run `forgeo auth login --provider github` or set a PAT."
    )


_post_form = make_post_form(GithubOAuthError, "GitHub OAuth")


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


run_device_flow = make_device_flow(
    request_device_code,
    poll_device_token,
    GithubOAuthError,
    "GitHub",
)


_CallbackHandler = make_callback_handler("GitHub")


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
    """Open browser for GitHub OAuth and exchange code for token."""

    def _authorize_url(redirect_uri: str, state: str, challenge: str) -> str:
        params: dict[str, str] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope or "repo",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{oauth_base.rstrip('/')}/login/oauth/authorize?{urlencode(params)}"

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
        handler_cls=_CallbackHandler,
        error_cls=GithubOAuthError,
        provider_label="GitHub",
        build_authorize_url=_authorize_url,
        build_token_fields=_token_fields,
        post_fn=_post_form,
        token_url=f"{oauth_base.rstrip('/')}/login/oauth/access_token",
        open_browser=open_browser,
        callback_port=callback_port,
        timeout=timeout,
    )


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
