"""OAuth2 access tokens for an HTTP backlog, via the client-credentials grant.

Forgeo authenticates to a backlog endpoint as a *service*, not as a person:
an identity provider (Keycloak, or anything else speaking OAuth2) issues an
access token for a confidential client, and that token is sent as a bearer on
every backlog request. No human credentials are involved, and the provider can
revoke Forgeo's access on its own.

The client secret never appears in ``forgeo.yaml`` — the config only names the
environment variable holding it, which is read at request time. That keeps the
secret out of the file the web console serves to the browser, and lets the same
config be committed or copied between machines.

Tokens are cached in memory until shortly before they expire. Nothing is ever
written to disk: a restarted daemon simply asks for a new token.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from forgeo.models import BacklogAuth

logger = logging.getLogger(__name__)

#: Renew this many seconds before the provider's stated expiry, so a token
#: never expires in flight between being read from the cache and being used.
EXPIRY_MARGIN_SECONDS = 30.0

#: Lifetime assumed when the provider does not state one.
_FALLBACK_LIFETIME_SECONDS = 60.0


class TokenError(RuntimeError):
    """A token could not be obtained; the message is user-facing."""


class ClientCredentialsTokenProvider:
    """Fetches and caches an access token for one confidential client.

    Thread-safe: the central web dashboard serves requests from several
    threads, and they share one provider per backlog.
    """

    def __init__(self, auth: BacklogAuth) -> None:
        self.auth = auth
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    def token(self) -> str:
        """A valid access token, from the cache or freshly requested."""
        with self._lock:
            if self._token is not None and time.monotonic() < self._expires_at:
                return self._token
            token, lifetime = self._request_token()
            self._token = token
            self._expires_at = time.monotonic() + max(
                lifetime - EXPIRY_MARGIN_SECONDS, 0.0
            )
            return token

    def invalidate(self) -> None:
        """Drop the cached token, so the next :meth:`token` asks for a new one.

        Called when the endpoint rejects the token we believed to be valid —
        the provider may have revoked it or rotated its keys early.
        """
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _client_secret(self) -> str:
        """The client secret, from the environment variable the config names."""
        secret = os.environ.get(self.auth.client_secret_env)
        if not secret:
            raise TokenError(
                f"Environment variable {self.auth.client_secret_env} is not set; "
                f"it must hold the client secret of {self.auth.client_id!r} "
                f"(backlog_auth.client_secret_env)."
            )
        return secret

    def _request_token(self) -> tuple[str, float]:
        """Perform the client-credentials request; returns token and lifetime."""
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.auth.client_id,
            "client_secret": self._client_secret(),
        }
        if self.auth.scope:
            payload["scope"] = self.auth.scope
        request = urllib.request.Request(
            self.auth.token_url,
            data=urllib.parse.urlencode(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.auth.timeout_seconds
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # The provider's own message ("invalid_client", ...) is the most
            # useful thing we can show, so it is surfaced rather than swallowed.
            detail = _error_detail(exc)
            raise TokenError(
                f"Token request to {self.auth.token_url} failed with HTTP "
                f"{exc.code}{detail}."
            ) from exc
        except (OSError, ValueError) as exc:
            raise TokenError(
                f"Token request to {self.auth.token_url} failed: {exc}"
            ) from exc

        if not isinstance(body, dict) or not body.get("access_token"):
            raise TokenError(
                f"Token response from {self.auth.token_url} carries no access_token."
            )
        lifetime = body.get("expires_in")
        if not isinstance(lifetime, int | float) or lifetime <= 0:
            logger.debug(
                "Token response states no usable expires_in; assuming %ss.",
                _FALLBACK_LIFETIME_SECONDS,
            )
            lifetime = _FALLBACK_LIFETIME_SECONDS
        logger.debug(
            "Obtained an access token for %r (valid %ss).",
            self.auth.client_id,
            lifetime,
        )
        return str(body["access_token"]), float(lifetime)


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """The provider's error body, trimmed, when it says anything useful."""
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001 - the detail is a nicety, never a failure
        return ""
    return f": {body[:200]}" if body else ""
