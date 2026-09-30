"""GitHub-backed task provider."""

from __future__ import annotations

import logging
import urllib.request
from typing import Any
from urllib.parse import quote

from forgeo.backlog import BacklogUnavailableError
from forgeo.backlog_issue_base import (
    build_api_url,
    clean_issue_list,
    encode_json_body,
    execute_rest_with_oauth_retry,
    oauth_access_token,
    require_env_token,
    resolve_cached_oauth_provider,
)
from forgeo.backlog_marker import MarkerIssueBacklog
from forgeo.models import GithubBacklogConfig

logger = logging.getLogger(__name__)


class GithubRequestError(BacklogUnavailableError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GithubClient:
    """Blocking GitHub REST client used via asyncio.to_thread."""

    def __init__(self, base_url: str, config: GithubBacklogConfig) -> None:
        self.base_url = base_url.rstrip("/")
        self.config = config
        self._oauth_provider: Any | None = None  # lazy GithubOAuthTokenProvider when oauth is used

    def _oauth_token_provider(self) -> Any | None:
        from forgeo.oauth_github import GithubOAuthTokenProvider, GithubTokenStore

        return resolve_cached_oauth_provider(self, GithubTokenStore, GithubOAuthTokenProvider)

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

    def _api_url(self, path: str, query: dict[str, Any] | None = None) -> str:
        return build_api_url(self.base_url, path, query)

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        # Retry once when GitHub rejects an OAuth token (revoked/expired)
        def _build_request() -> urllib.request.Request:
            body = encode_json_body(payload)
            auth_header = self._auth_header()
            return urllib.request.Request(
                self._api_url(path, query),
                data=body,
                method=method,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": auth_header,
                    "X-GitHub-Api-Version": "2022-11-28",
                    **({"Content-Type": "application/json"} if body is not None else {}),
                },
            )

        return execute_rest_with_oauth_retry(
            build_request=_build_request,
            timeout=self.config.timeout_seconds,
            error_cls=GithubRequestError,
            method=method,
            has_oauth=self.config.auth.oauth is not None,
            get_cached_provider=lambda: self._oauth_provider,
        )

    def _repo_path(self) -> str:
        # GitHub API expects owner/repo as two separate path segments;
        # encode each segment but keep the slash between them.
        return "/".join(quote(part, safe="") for part in self.config.repo.split("/"))

    def search_issues(
        self,
        *,
        page: int = 1,
        per_page: int = 30,
        state: str = "all",
    ) -> list[dict[str, Any]]:
        path = f"/repos/{self._repo_path()}/issues"
        query: dict[str, Any] = {"state": state, "per_page": per_page, "page": page}
        return clean_issue_list(self._request("GET", path, query=query))

    def get_issue(self, issue_number: int) -> dict[str, Any]:
        path = f"/repos/{self._repo_path()}/issues/{issue_number}"
        return self._request("GET", path)  # type: ignore[no-any-return]

    def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        path = f"/repos/{self._repo_path()}/issues"
        return self._request("POST", path, payload=fields)  # type: ignore[no-any-return]

    def update_issue(self, issue_number: int, fields: dict[str, Any]) -> dict[str, Any]:
        path = f"/repos/{self._repo_path()}/issues/{issue_number}"
        return self._request("PATCH", path, payload=fields)  # type: ignore[no-any-return]

    def add_comment(self, issue_number: int, body: str) -> None:
        path = f"/repos/{self._repo_path()}/issues/{issue_number}/comments"
        self._request("POST", path, payload={"body": body})

    def delete_issue(self, issue_number: int) -> None:
        path = f"/repos/{self._repo_path()}/issues/{issue_number}"
        self._request("PATCH", path, payload={"state": "closed"})


class GithubBacklog(MarkerIssueBacklog):
    """Task provider backed by GitHub issues."""

    body_key = "body"
    open_state = "open"
    close_state = "closed"
    provider_label = "GitHub"
    request_error_cls = GithubRequestError
    client_cls = GithubClient

    async def _search_page(self, *, page: int, per_page: int) -> Any:
        return await self._call(self.client.search_issues, page=page, per_page=per_page, state="all")

    async def _post_comment(self, numeric_id: int, body: str) -> None:
        await self._call(self.client.add_comment, numeric_id, body)

    async def _close_issue(self, numeric_id: int) -> None:
        await self._call(self.client.update_issue, numeric_id, {"state": "closed"})

    async def _reopen_issue(self, numeric_id: int) -> None:
        await self._call(self.client.update_issue, numeric_id, {"state": "open"})

    def _created_id(self, created: Any) -> str | None:
        number = created.get("number") if isinstance(created, dict) else None
        return str(number) if isinstance(number, int) else None
