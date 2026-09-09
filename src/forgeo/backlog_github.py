"""GitHub-backed task provider."""

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
from forgeo.models import GithubBacklogConfig, Task

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
        if self._oauth_provider is not None:
            return self._oauth_provider
        auth = self.config.auth
        if auth.oauth is None:
            return None
        from forgeo.oauth_github import GithubOAuthTokenProvider, GithubTokenStore

        token_file = auth.oauth.token_file
        store = GithubTokenStore(path=token_file, api_base=self.base_url) if token_file is not None else GithubTokenStore(api_base=self.base_url)
        provider = GithubOAuthTokenProvider(store)
        self._oauth_provider = provider
        return provider

    def _auth_header(self) -> str:
        auth = self.config.auth
        if auth.token_env is not None:
            token = require_env_token(auth.token_env, "GitHub", GithubRequestError)
            return f"Bearer {token}"
        if auth.oauth is not None:
            provider = self._oauth_token_provider()
            assert provider is not None
            try:
                token = provider.token()
            except Exception as exc:
                # Wrap file errors as GithubRequestError so callers surface a clear message
                if isinstance(exc, GithubRequestError):
                    raise
                from forgeo.oauth_github import GithubOAuthError

                if isinstance(exc, GithubOAuthError):
                    raise GithubRequestError(str(exc)) from exc
                raise GithubRequestError(str(exc)) from exc
            return f"Bearer {token}"
        raise GithubRequestError("GitHub auth is not configured (token_env or oauth required)")

    def _api_url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url}{path}"
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
        # Retry once when GitHub rejects an OAuth token (revoked/expired)
        for attempt in (0, 1):
            body = None
            if payload is not None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            auth_header = self._auth_header()
            request = urllib.request.Request(
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
            try:
                return execute_json_request(
                    request, self.config.timeout_seconds, GithubRequestError, method
                )
            except GithubRequestError as exc:
                # On 401/403 with OAuth, invalidate cache and retry once so a freshly
                # written token (e.g. after `forgeo auth login`) is picked up.
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
        data = self._request("GET", path, query=query)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

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

    def __init__(
        self,
        url: str,
        config: GithubBacklogConfig,
        *,
        output_cap: int | None = None,
        client: GithubClient | None = None,
    ) -> None:
        super().__init__(url, config, output_cap=output_cap, client=client or GithubClient(url, config))

    def __repr__(self) -> str:
        return f"GithubBacklog({self.url!r})"

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

    def _create_fields(self, task: Task, engine: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": task.title,
            "body": embed_engine_state(task.description, engine),
            "labels": [self.config.label_prefix],
        }

    def _update_body_field(self, candidate: Task, state: dict[str, Any]) -> dict[str, Any]:
        return {"body": embed_engine_state(candidate.description, state)}
