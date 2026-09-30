"""GitHub-backed task provider."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from forgeo.backlog import BacklogUnavailableError
from forgeo.backlog_issue_base import (
    RestIssueClientBase,
    oauth_access_token,
    require_env_token,
)
from forgeo.backlog_marker import MarkerIssueBacklog
from forgeo.models import GithubBacklogConfig


class GithubRequestError(BacklogUnavailableError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GithubClient(RestIssueClientBase):
    """Blocking GitHub REST client used via asyncio.to_thread."""

    request_error_cls = GithubRequestError
    provider_label = "GitHub"

    def __init__(self, base_url: str, config: GithubBacklogConfig) -> None:
        super().__init__(base_url, config)

    def _oauth_components(self) -> tuple[Any, Any, type[Exception]]:
        from forgeo.oauth_github import (
            GithubOAuthError,
            GithubOAuthTokenProvider,
            GithubTokenStore,
        )

        return GithubTokenStore, GithubOAuthTokenProvider, GithubOAuthError

    def _auth_header(self) -> str:
        auth = self.config.auth
        if auth.token_env is not None:
            token = require_env_token(auth.token_env, "GitHub", GithubRequestError)
            return f"Bearer {token}"
        if auth.oauth is not None:
            provider = self._oauth_token_provider()
            assert provider is not None
            from forgeo.oauth_github import GithubOAuthError

            return f"Bearer {oauth_access_token(provider, oauth_error_cls=GithubOAuthError, request_error_cls=GithubRequestError)}"
        raise GithubRequestError("GitHub auth is not configured (token_env or oauth required)")

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": self._auth_header()}

    def _extra_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _repo_path(self) -> str:
        # GitHub API expects owner/repo as two separate path segments;
        # encode each segment but keep the slash between them.
        return "/".join(quote(part, safe="") for part in self.config.repo.split("/"))

    def _collection_path(self) -> str:
        return f"/repos/{self._repo_path()}/issues"

    def _comment_path(self, issue_number: int) -> str:
        return f"{self._item_path(issue_number)}/comments"

    def _search_query(self, page: int, per_page: int, state: str) -> dict[str, Any]:
        return {"state": state, "per_page": per_page, "page": page}

    def delete_issue(self, issue_number: int) -> None:
        self._request("PATCH", self._item_path(issue_number), payload={"state": "closed"})


class GithubBacklog(MarkerIssueBacklog):
    """Task provider backed by GitHub issues."""

    body_key = "body"
    open_state = "open"
    close_state = "closed"
    provider_label = "GitHub"
    request_error_cls = GithubRequestError
    client_cls = GithubClient
    close_update: dict[str, Any] = {"state": "closed"}
    reopen_update: dict[str, Any] = {"state": "open"}
