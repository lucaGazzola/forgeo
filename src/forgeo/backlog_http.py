"""A backlog owned by another application, reached over HTTP.

When ``backlog:`` is an ``http(s)`` URL, the task document lives in someone
else's system — typically the backend of an application that already knows how
to display and edit a backlog. Forgeo treats that endpoint exactly like it
treats a file:

* **GET** returns the whole document, ``{"tasks": [...]}``, on every read;
* **POST** sends the whole document back after every mutation.

So the endpoint replaces the list it holds with the body it receives; it must
not append. Sending the complete document (rather than per-task calls) is what
keeps the two backends interchangeable: the same
:class:`~forgeo.backlog.BacklogStore` logic drives both.

Failures are loud, and that is deliberate. A file backlog that does not exist
yet is legitimately empty, but an endpoint that cannot be *reached* says
nothing about how many tasks it holds. Reading it as empty would make Forgeo
run a refactoring pass over a backlog it never saw, and the POST at the end of
that cycle would overwrite the remote task list with nothing. So a failed
request raises, the cycle fails, the daemon logs it and retries on the next
interval — and the remote backlog is left untouched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from forgeo.backlog import BacklogStore, BacklogUnavailableError, normalize_store
from forgeo.models import BacklogAuth
from forgeo.oauth import ClientCredentialsTokenProvider, TokenError

logger = logging.getLogger(__name__)

#: Give up on a backlog request after this long. A stuck endpoint must not
#: hold a cycle open indefinitely — the next interval will try again.
REQUEST_TIMEOUT_SECONDS = 30.0

#: Statuses that mean "your token is not (or no longer) accepted": worth
#: dropping the cached token and trying once more.
_AUTH_STATUSES = (401, 403)


class HttpBacklog(BacklogStore):
    """A backlog document served over HTTP by another application."""

    def __init__(
        self,
        url: str,
        *,
        auth: BacklogAuth | None = None,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__()
        self.url = url
        self.timeout_seconds = timeout_seconds
        self._tokens = (
            ClientCredentialsTokenProvider(auth) if auth is not None else None
        )

    def __repr__(self) -> str:
        return f"HttpBacklog({self.url!r})"

    async def _read(self) -> dict[str, Any]:
        """GET the document; anything but a usable response raises."""
        body = await asyncio.to_thread(self._request, "GET", None)
        logger.info("GET %s returned body: %s", self.url, body)
        try:
            data = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError as exc:
            raise BacklogUnavailableError(
                f"Backlog endpoint {self.url} returned a body that is not JSON: {exc}"
            ) from exc
        return normalize_store(data)

    async def _write(self, store: dict[str, Any]) -> None:
        """POST the whole document back."""
        payload = json.dumps(store, indent=2, ensure_ascii=False).encode("utf-8")
        logger.info("POST %s %s", self.url, payload)
        await asyncio.to_thread(self._request, "POST", payload)

    # ------------------------------------------------------------------ #
    # HTTP                                                                #
    # ------------------------------------------------------------------ #

    def _request(self, method: str, payload: bytes | None) -> str:
        """One request, retried once when the endpoint rejects our token.

        Blocking on purpose: callers reach it through ``asyncio.to_thread``,
        so the daemon's event loop keeps running while the endpoint answers.
        """
        try:
            return self._send(method, payload)
        except BacklogUnavailableError as exc:
            if self._tokens is None or exc.__cause__ is None:
                raise
            cause = exc.__cause__
            if not isinstance(cause, urllib.error.HTTPError):
                raise
            if cause.code not in _AUTH_STATUSES:
                raise
            # The provider may have revoked or rotated the token early; a
            # single retry with a fresh one keeps a key rotation from costing
            # a whole cycle.
            logger.info(
                "Backlog endpoint rejected the access token (HTTP %s); "
                "requesting a new one and retrying once.",
                cause.code,
            )
            self._tokens.invalidate()
            return self._send(method, payload)

    def _send(self, method: str, payload: bytes | None) -> str:
        """Perform one HTTP request and return its body as text."""
        request = urllib.request.Request(
            self.url,
            data=payload,
            method=method,
            headers=self._headers(payload is not None),
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                return str(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            raise BacklogUnavailableError(
                f"{method} {self.url} failed with HTTP {exc.code} {exc.reason}."
            ) from exc
        except OSError as exc:
            raise BacklogUnavailableError(
                f"{method} {self.url} failed: {exc}"
            ) from exc

    def _headers(self, has_body: bool) -> dict[str, str]:
        """Request headers, including the bearer token when configured."""
        headers = {"Accept": "application/json"}
        if has_body:
            headers["Content-Type"] = "application/json"
        if self._tokens is not None:
            try:
                headers["Authorization"] = f"Bearer {self._tokens.token()}"
            except TokenError as exc:
                raise BacklogUnavailableError(str(exc)) from exc
        return headers
