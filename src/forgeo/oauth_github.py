"""OAuth / browser-assisted authentication for GitHub."""

from __future__ import annotations

from pathlib import Path

from forgeo.oauth_common import build_simple_oauth, github_web_base


class GithubOAuthError(RuntimeError):
    """A browser/device login step failed; message is user-facing."""


_provider = build_simple_oauth(
    GithubOAuthError, "github", "GitHub",
    default_base="https://api.github.com", plain_host="api.github.com",
    strip_suffixes=("/api/v3",), oauth_base_fn=github_web_base,
    device_paths=("/login/device/code",),
    token_path="/login/oauth/access_token",
    authorize_path="/login/oauth/authorize", default_scope="repo",
)


def github_default_token_path(api_base: str | None = None) -> Path:
    """Default token file for a GitHub API base."""
    return _provider.default_token_path(api_base)


def github_oauth_base(api_base: str) -> str:
    """Derive the OAuth authorize/token base from a GitHub API base."""
    return _provider.oauth_base(api_base)


GithubTokenStore = _provider.TokenStore
GithubOAuthTokenProvider = _provider.TokenProvider
request_device_code = _provider.request_device_code
poll_device_token = _provider.poll_device_token
run_device_flow = _provider.run_device_flow
run_browser_flow = _provider.run_browser_flow


__all__ = [
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
