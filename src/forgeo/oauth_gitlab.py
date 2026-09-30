"""OAuth / browser-assisted authentication for GitLab."""

from __future__ import annotations

from pathlib import Path

from forgeo.oauth_common import (
    DEFAULT_DEVICE_POLL_INTERVAL,
    DEFAULT_DEVICE_POLL_TIMEOUT_SECONDS,
    DEFAULT_TOKEN_DIR,
    EXPIRY_MARGIN_SECONDS,
    host_token_path,
    make_browser_flow,
    make_callback_handler,
    make_device_code_request,
    make_device_flow,
    make_file_token_provider,
    make_poll_device_token,
    make_post_form,
    make_token_store,
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


GitlabOAuthTokenProvider = make_file_token_provider(
    GitlabOAuthError,
    "GitLab OAuth token not found at {path}; run `forgeo auth login --provider gitlab` or set a PAT.",
)


_post_form = make_post_form(GitlabOAuthError, "GitLab OAuth")


request_device_code = make_device_code_request(
    GitlabOAuthError, "GitLab", "/oauth/device/code", "/oauth/authorize_device"
)


poll_device_token = make_poll_device_token(GitlabOAuthError, "/oauth/token")


run_device_flow = make_device_flow(
    request_device_code,
    poll_device_token,
    GitlabOAuthError,
    "GitLab",
    extra_url_keys=("verification_url",),
)


run_browser_flow = make_browser_flow(
    make_callback_handler("GitLab"),
    GitlabOAuthError,
    "GitLab",
    _post_form,
    authorize_path="/oauth/authorize",
    token_path="/oauth/token",
    default_scope="api",
    extra_authorize_params={"response_type": "code"},
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
