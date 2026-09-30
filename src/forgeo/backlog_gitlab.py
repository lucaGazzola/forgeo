"""GitLab-backed task provider."""

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
from forgeo.models import GitlabBacklogConfig

logger = logging.getLogger(__name__)


class GitlabRequestError(BacklogUnavailableError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitlabClient:
    """Blocking GitLab REST client via asyncio.to_thread."""

    def __init__(self, base_url: str, config: GitlabBacklogConfig) -> None:
        self.base_url = base_url.rstrip("/")
        self.config = config
        self._oauth_provider: Any | None = None

    def _oauth_token_provider(self) -> Any | None:
        from forgeo.oauth_gitlab import GitlabOAuthTokenProvider, GitlabTokenStore

        return resolve_cached_oauth_provider(self, GitlabTokenStore, GitlabOAuthTokenProvider)

    def _auth_headers(self) -> dict[str, str]:
        auth = self.config.auth
        if auth.token_env is not None:
            token = require_env_token(auth.token_env, "GitLab", GitlabRequestError)
            # GitLab prefers PRIVATE-TOKEN, but also accepts Bearer
            return {"PRIVATE-TOKEN": token, "Authorization": f"Bearer {token}"}
        if auth.oauth is not None:
            provider = self._oauth_token_provider()
            assert provider is not None
            from forgeo.oauth_gitlab import GitlabOAuthError

            return {"Authorization": f"Bearer {oauth_access_token(provider, oauth_error_cls=GitlabOAuthError, request_error_cls=GitlabRequestError)}"}
        raise GitlabRequestError("GitLab auth is not configured (token_env or oauth required)")

    def _project_path(self) -> str:
        # GitLab API expects URL-encoded project path or numeric id
        return quote(self.config.repo, safe="")

    def _api_url(self, path: str, query: dict[str, Any] | None = None) -> str:
        return build_api_url(self.base_url, path, query, api_prefix="/api/v4")

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        def _build_request() -> urllib.request.Request:
            body = encode_json_body(payload)
            headers = {
                "Accept": "application/json",
                **self._auth_headers(),
                **({"Content-Type": "application/json"} if body is not None else {}),
            }
            return urllib.request.Request(
                self._api_url(path, query), data=body, method=method, headers=headers
            )

        return execute_rest_with_oauth_retry(
            build_request=_build_request,
            timeout=self.config.timeout_seconds,
            error_cls=GitlabRequestError,
            method=method,
            has_oauth=self.config.auth.oauth is not None,
            get_cached_provider=lambda: self._oauth_provider,
        )

    def search_issues(self, *, page: int = 1, per_page: int = 20) -> list[dict[str, Any]]:
        path = f"/projects/{self._project_path()}/issues"
        query: dict[str, Any] = {"per_page": per_page, "page": page, "scope": "all", "state": "all"}
        return clean_issue_list(self._request("GET", path, query=query))

    def get_issue(self, iid: int) -> dict[str, Any]:
        path = f"/projects/{self._project_path()}/issues/{iid}"
        return self._request("GET", path)  # type: ignore[no-any-return]

    def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        path = f"/projects/{self._project_path()}/issues"
        return self._request("POST", path, payload=fields)  # type: ignore[no-any-return]

    def update_issue(self, iid: int, fields: dict[str, Any]) -> dict[str, Any]:
        path = f"/projects/{self._project_path()}/issues/{iid}"
        return self._request("PUT", path, payload=fields)  # type: ignore[no-any-return]

    def add_note(self, iid: int, body: str) -> None:
        path = f"/projects/{self._project_path()}/issues/{iid}/notes"
        self._request("POST", path, payload={"body": body})

    def delete_issue(self, iid: int) -> None:
        path = f"/projects/{self._project_path()}/issues/{iid}"
        self._request("DELETE", path)


class GitlabBacklog(MarkerIssueBacklog):
    """Task provider backed by GitLab issues."""

    body_key = "description"
    open_state = "opened"
    close_state = "closed"
    provider_label = "GitLab"
    request_error_cls = GitlabRequestError
    client_cls = GitlabClient

    async def _post_comment(self, numeric_id: int, body: str) -> None:
        await self._call(self.client.add_note, numeric_id, body)

    async def _close_issue(self, numeric_id: int) -> None:
        await self._call(self.client.update_issue, numeric_id, {"state_event": "close"})

    async def _reopen_issue(self, numeric_id: int) -> None:
        await self._call(self.client.update_issue, numeric_id, {"state_event": "reopen"})

    def _created_id(self, created: Any) -> str | None:
        if not isinstance(created, dict):
            return None
        iid = created.get("iid") or created.get("id")
        return str(iid) if isinstance(iid, int) else None
