"""GitLab-backed task provider."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from forgeo.backlog import BacklogUnavailableError
from forgeo.backlog_issue_base import RestIssueClientBase
from forgeo.backlog_marker import MarkerIssueBacklog


class GitlabRequestError(BacklogUnavailableError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitlabClient(RestIssueClientBase):
    """Blocking GitLab REST client via asyncio.to_thread."""

    request_error_cls = GitlabRequestError
    provider_label = "GitLab"
    api_prefix = "/api/v4"
    update_method = "PUT"

    def _oauth_components(self) -> tuple[Any, Any, type[Exception]]:
        from forgeo.oauth_gitlab import GitlabOAuthError, GitlabOAuthTokenProvider, GitlabTokenStore

        return GitlabTokenStore, GitlabOAuthTokenProvider, GitlabOAuthError

    def _token_headers(self, token: str) -> dict[str, str]:
        # GitLab prefers PRIVATE-TOKEN, but also accepts Bearer.
        return {"PRIVATE-TOKEN": token, "Authorization": f"Bearer {token}"}

    def _extra_headers(self) -> dict[str, str]:
        return {"Accept": "application/json"}

    def _project_path(self) -> str:
        # GitLab API expects URL-encoded project path or numeric id
        return quote(self.config.repo, safe="")

    def _collection_path(self) -> str:
        return f"/projects/{self._project_path()}/issues"

    def _comment_path(self, iid: int) -> str:
        return f"{self._item_path(iid)}/notes"

    def _search_query(self, page: int, per_page: int, state: str) -> dict[str, Any]:
        del state
        return {"per_page": per_page, "page": page, "scope": "all", "state": "all"}

    add_note = RestIssueClientBase.add_comment


class GitlabBacklog(MarkerIssueBacklog):
    """Task provider backed by GitLab issues."""

    body_key = "description"
    open_state = "opened"
    close_state = "closed"
    provider_label = "GitLab"
    request_error_cls = GitlabRequestError
    client_cls = GitlabClient
    close_update: dict[str, Any] = {"state_event": "close"}
    reopen_update: dict[str, Any] = {"state_event": "reopen"}
