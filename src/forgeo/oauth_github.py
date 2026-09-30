"""OAuth / browser-assisted authentication for GitHub."""

from __future__ import annotations

from pathlib import Path

from forgeo.oauth_common import (
    github_web_base,
    host_token_path,
    make_browser_flow,
    make_callback_handler,
    make_device_code_request,
    make_device_flow,
    make_file_token_provider,
    make_poll_device_token,
    make_post_form,
    make_token_store,
)


class GithubOAuthError(RuntimeError):
    """A browser/device login step failed; message is user-facing."""


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


GithubOAuthTokenProvider = make_file_token_provider(
    GithubOAuthError,
    "GitHub OAuth token not found at {path}; run `forgeo auth login --provider github` or set a PAT.",
)


_post_form = make_post_form(GithubOAuthError, "GitHub OAuth")


request_device_code = make_device_code_request(GithubOAuthError, "GitHub", "/login/device/code")


poll_device_token = make_poll_device_token(GithubOAuthError, "/login/oauth/access_token")


run_device_flow = make_device_flow(
    request_device_code,
    poll_device_token,
    GithubOAuthError,
    "GitHub",
)


run_browser_flow = make_browser_flow(
    make_callback_handler("GitHub"),
    GithubOAuthError,
    "GitHub",
    _post_form,
    authorize_path="/login/oauth/authorize",
    token_path="/login/oauth/access_token",
    default_scope="repo",
)


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
