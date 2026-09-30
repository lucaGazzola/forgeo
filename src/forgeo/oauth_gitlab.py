"""OAuth / browser-assisted authentication for GitLab."""

from __future__ import annotations

from pathlib import Path

from forgeo.oauth_common import build_simple_oauth, strip_url_suffix


class GitlabOAuthError(RuntimeError):
    """A browser/device login step failed; message is user-facing."""


_provider = build_simple_oauth(
    GitlabOAuthError, "gitlab", "GitLab",
    default_base="https://gitlab.com", plain_host="gitlab.com",
    strip_suffixes=("/api/v4", "/api"),
    oauth_base_fn=lambda base: strip_url_suffix(base.rstrip("/"), ("/api/v4", "/api")),
    device_paths=("/oauth/device/code", "/oauth/authorize_device"),
    token_path="/oauth/token", authorize_path="/oauth/authorize",
    default_scope="api", extra_authorize_params={"response_type": "code"},
    extra_url_keys=("verification_url",),
)


def gitlab_default_token_path(api_base: str | None = None) -> Path:
    """Default token file for a GitLab base."""
    return _provider.default_token_path(api_base)


def gitlab_oauth_base(api_base: str) -> str:
    """Derive OAuth base from a GitLab API base."""
    return _provider.oauth_base(api_base)


GitlabTokenStore = _provider.TokenStore
GitlabOAuthTokenProvider = _provider.TokenProvider
request_device_code = _provider.request_device_code
poll_device_token = _provider.poll_device_token
run_device_flow = _provider.run_device_flow
run_browser_flow = _provider.run_browser_flow


__all__ = [
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
