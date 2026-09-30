"""OAuth / browser-assisted authentication for GitLab."""

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
    host_token_path,
    make_callback_handler,
    make_device_flow,
    make_post_form,
    make_token_store,
    poll_device_grant,
    run_pkce_browser_login,
    strip_url_suffix,
)


class GitlabOAuthError(RuntimeError):
    """A browser/device login step failed; message is user-facing."""


DEFAULT_GITLAB_TOKEN_DIR = DEFAULT_TOKEN_DIR


def gitlab_default_token_path(api_base: str | None = None) -> Path:
    """Default token file for a GitLab base."""
    return host_token_path(
        "gitlab",
        api_base,
        default_base="https://gitlab.com",
        plain_host="gitlab.com",
        strip_suffixes=("/api/v4", "/api"),
    )


def gitlab_oauth_base(api_base: str) -> str:
    """Derive OAuth base from a GitLab API base."""
    return strip_url_suffix(api_base.rstrip("/"), ("/api/v4", "/api"))


GitlabTokenStore = make_token_store(gitlab_default_token_path)


class GitlabOAuthTokenProvider(CachedFileTokenProvider):
    """File-backed, cached token for GitlabClient."""

    error_cls = GitlabOAuthError
    missing_message = (
        "GitLab OAuth token not found at {path}; run `forgeo auth login --provider gitlab` or set a PAT."
    )


_post_form = make_post_form(GitlabOAuthError, "GitLab OAuth")


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


run_device_flow = make_device_flow(
    request_device_code,
    poll_device_token,
    GitlabOAuthError,
    "GitLab",
    extra_url_keys=("verification_url",),
)


_CallbackHandler = make_callback_handler("GitLab")


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
    def _authorize_url(redirect_uri: str, state: str, challenge: str) -> str:
        params: dict[str, str] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope or "api",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{oauth_base.rstrip('/')}/oauth/authorize?{urlencode(params)}"

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
        error_cls=GitlabOAuthError,
        provider_label="GitLab",
        build_authorize_url=_authorize_url,
        build_token_fields=_token_fields,
        post_fn=_post_form,
        token_url=f"{oauth_base.rstrip('/')}/oauth/token",
        open_browser=open_browser,
        callback_port=callback_port,
        timeout=timeout,
    )


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
