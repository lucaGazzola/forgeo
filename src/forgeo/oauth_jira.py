"""OAuth / browser-assisted authentication for Jira Cloud (Atlassian 3LO)."""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

from forgeo.oauth_common import (
    CachedFileTokenProvider,
    FileTokenStore,
    host_token_path,
    make_browser_flow,
    make_callback_handler,
    make_post_form,
    stamp_issued_at,
)

logger = logging.getLogger(__name__)


class JiraOAuthError(RuntimeError):
    """A Jira OAuth step failed; message is user-facing."""


ATLASSIAN_AUTH_BASE = "https://auth.atlassian.com"
ATLASSIAN_API_BASE = "https://api.atlassian.com"


def jira_default_token_path(api_base: str | None = None) -> Path:
    """Default token file for a Jira base."""
    return host_token_path(
        "jira",
        api_base,
        default_base="https://jira.example.com",
        plain_host="jira.example.com",
    )


def jira_oauth_base(api_base: str | None = None) -> str:
    """Jira OAuth base is always Atlassian auth; api_base not used but kept for symmetry."""
    return ATLASSIAN_AUTH_BASE


class JiraTokenStore(FileTokenStore):
    def __init__(self, path: Path | str | None = None, *, api_base: str | None = None) -> None:
        super().__init__(Path(path).expanduser() if path is not None else jira_default_token_path(api_base))

    def save(self, data: dict[str, Any]) -> None:
        super().save(stamp_issued_at(data))


EXPIRY_MARGIN_SECONDS = 60.0


class JiraOAuthTokenProvider(CachedFileTokenProvider):
    """File-backed cached token with refresh-grant support."""

    error_cls = JiraOAuthError
    missing_message = "Jira OAuth token not found at {path}; run `forgeo auth login --provider jira`."
    expiry_margin = EXPIRY_MARGIN_SECONDS

    def __init__(self, store: JiraTokenStore, *, client_id: str | None = None, client_secret_env: str | None = None) -> None:
        super().__init__(store)
        self.client_id = client_id
        self.client_secret_env = client_secret_env

    def _refresh_data(self, data: dict[str, Any]) -> dict[str, Any] | None:
        refresh = data.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            return None
        if not self.client_id or not self.client_secret_env:
            return None
        secret = os.environ.get(self.client_secret_env) if self.client_secret_env else None
        if not secret:
            return None
        try:
            new_data = _post_form(
                f"{ATLASSIAN_AUTH_BASE}/oauth/token",
                {
                    "grant_type": "refresh_token",
                    "client_id": self.client_id,
                    "client_secret": secret,
                    "refresh_token": refresh,
                },
            )
            # Preserve cloud_id if not in new_data
            if "cloud_id" not in new_data and data.get("cloud_id"):
                new_data["cloud_id"] = data["cloud_id"]
            if "refresh_token" not in new_data and refresh:
                new_data["refresh_token"] = refresh
            new_data = stamp_issued_at(new_data)
            self.store.save(new_data)
            return new_data
        except Exception as exc:  # noqa: BLE001
            logger.warning("Jira token refresh failed: %s", exc)
            return None


_post_form = make_post_form(JiraOAuthError, "Jira OAuth")


def _fetch_accessible_resources(access_token: str) -> list[dict[str, Any]]:
    url = f"{ATLASSIAN_API_BASE}/oauth/token/accessible-resources"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
            return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not fetch accessible-resources: %s", exc)
        return []


_CallbackHandler = make_callback_handler("Jira")

#: Shared PKCE browser dance (loopback server, authorize URL, code exchange);
#: Atlassian-only extras are the fixed authorize params and the default scope.
#: ``offline_access`` (refresh grant) is ensured per call in
#: :func:`run_browser_flow`, since a custom scope may omit it.
_browser_flow = make_browser_flow(
    _CallbackHandler,
    JiraOAuthError,
    "Jira",
    _post_form,
    authorize_path="/authorize",
    token_path="/oauth/token",
    default_scope="offline_access read:jira-user read:jira-work",
    extra_authorize_params={
        "audience": "api.atlassian.com",
        "response_type": "code",
        "prompt": "consent",
    },
)


def run_browser_flow(
    client_id: str,
    oauth_base: str | None = None,
    scope: str | None = None,
    *,
    client_secret: str | None = None,
    cloud_id: str | None = None,
    open_browser: bool = True,
    callback_port: int | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Run Atlassian OAuth 3LO browser flow and return token data including cloud_id."""
    del oauth_base  # Atlassian base is fixed
    # Atlassian scopes: offline_access required for refresh, plus Jira scopes
    eff_scope = scope or "offline_access read:jira-user read:jira-work"
    if "offline_access" not in eff_scope:
        eff_scope = eff_scope + " offline_access"
    token_data = _browser_flow(
        client_id,
        ATLASSIAN_AUTH_BASE,
        eff_scope,
        client_secret=client_secret,
        open_browser=open_browser,
        callback_port=callback_port,
        timeout=timeout,
    )
    # Fetch cloudId if not provided
    access = token_data.get("access_token")
    if not isinstance(access, str):
        raise JiraOAuthError("Token response missing access_token")
    if not cloud_id:
        resources = _fetch_accessible_resources(access)
        if resources:
            # If multiple, pick first or ask? For now pick first
            cloud_id = resources[0].get("id") if isinstance(resources[0].get("id"), str) else None
            if cloud_id:
                token_data["cloud_id"] = cloud_id
                # Also store site url for reference
                site_url = resources[0].get("url")
                if isinstance(site_url, str):
                    token_data["site_url"] = site_url
    else:
        token_data["cloud_id"] = cloud_id
    return token_data


__all__ = [
    "ATLASSIAN_API_BASE",
    "ATLASSIAN_AUTH_BASE",
    "EXPIRY_MARGIN_SECONDS",
    "JiraOAuthError",
    "JiraOAuthTokenProvider",
    "JiraTokenStore",
    "jira_default_token_path",
    "jira_oauth_base",
    "run_browser_flow",
]
