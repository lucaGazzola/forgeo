"""GitLab-backed task provider."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlencode

from forgeo.backlog import BacklogUnavailableError
from forgeo.backlog_issue_base import embed_engine_state, execute_json_request, require_env_token
from forgeo.backlog_marker import MarkerIssueBacklog
from forgeo.models import GitlabBacklogConfig, Task

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
        if self._oauth_provider is not None:
            return self._oauth_provider
        auth = self.config.auth
        if auth.oauth is None:
            return None
        from forgeo.oauth_gitlab import GitlabOAuthTokenProvider, GitlabTokenStore

        token_file = auth.oauth.token_file
        store = GitlabTokenStore(path=token_file, api_base=self.base_url) if token_file is not None else GitlabTokenStore(api_base=self.base_url)
        provider = GitlabOAuthTokenProvider(store)
        self._oauth_provider = provider
        return provider

    def _auth_headers(self) -> dict[str, str]:
        auth = self.config.auth
        if auth.token_env is not None:
            token = require_env_token(auth.token_env, "GitLab", GitlabRequestError)
            # GitLab prefers PRIVATE-TOKEN, but also accepts Bearer
            return {"PRIVATE-TOKEN": token, "Authorization": f"Bearer {token}"}
        if auth.oauth is not None:
            provider = self._oauth_token_provider()
            assert provider is not None
            try:
                token = provider.token()
            except Exception as exc:
                if isinstance(exc, GitlabRequestError):
                    raise
                from forgeo.oauth_gitlab import GitlabOAuthError

                if isinstance(exc, GitlabOAuthError):
                    raise GitlabRequestError(str(exc)) from exc
                raise GitlabRequestError(str(exc)) from exc
            return {"Authorization": f"Bearer {token}"}
        raise GitlabRequestError("GitLab auth is not configured (token_env or oauth required)")

    def _project_path(self) -> str:
        # GitLab API expects URL-encoded project path or numeric id
        return quote(self.config.repo, safe="")

    def _api_url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url}/api/v4{path}"
        if query:
            url += "?" + urlencode(query, doseq=True)
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        for attempt in (0, 1):
            body = None
            if payload is not None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {
                "Accept": "application/json",
                **self._auth_headers(),
                **({"Content-Type": "application/json"} if body is not None else {}),
            }
            request = urllib.request.Request(
                self._api_url(path, query), data=body, method=method, headers=headers
            )
            try:
                return execute_json_request(
                    request, self.config.timeout_seconds, GitlabRequestError, method
                )
            except GitlabRequestError as exc:
                if (
                    attempt == 0
                    and exc.status in (401, 403)
                    and self.config.auth.oauth is not None
                    and self._oauth_provider is not None
                ):
                    try:
                        self._oauth_provider.invalidate()  # noqa: BLE001
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                raise

    def search_issues(self, *, page: int = 1, per_page: int = 20) -> list[dict[str, Any]]:
        path = f"/projects/{self._project_path()}/issues"
        query: dict[str, Any] = {"per_page": per_page, "page": page, "scope": "all", "state": "all"}
        data = self._request("GET", path, query=query)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

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

    def __init__(
        self,
        url: str,
        config: GitlabBacklogConfig,
        *,
        output_cap: int | None = None,
        client: GitlabClient | None = None,
    ) -> None:
        super().__init__(url, config, output_cap=output_cap, client=client or GitlabClient(url, config))

    def __repr__(self) -> str:
        return f"GitlabBacklog({self.url!r})"

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

    def _create_fields(self, task: Task, engine: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": task.title,
            "description": embed_engine_state(task.description, engine),
            "labels": [self.config.label_prefix],
        }

    def _update_body_field(self, candidate: Task, state: dict[str, Any]) -> dict[str, Any]:
        return {"description": embed_engine_state(candidate.description, state)}
