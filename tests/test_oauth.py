"""Client-credentials token provider: caching, renewal, and failure messages."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from forgeo.models import BacklogAuth
from forgeo.oauth import (
    EXPIRY_MARGIN_SECONDS,
    ClientCredentialsTokenProvider,
    TokenError,
)

SECRET_ENV = "FORGEO_TEST_CLIENT_SECRET"


def make_auth(**overrides) -> BacklogAuth:
    defaults = {
        "token_url": "https://keycloak.test/realms/dev/protocol/openid-connect/token",
        "client_id": "forgeo",
        "client_secret_env": SECRET_ENV,
    }
    defaults.update(overrides)
    return BacklogAuth(**defaults)


class FakeTokenEndpoint:
    """Records every token request and replies with a scripted token."""

    def __init__(self, token: str = "tok-1", expires_in: int | None = 300) -> None:
        self.token = token
        self.expires_in = expires_in
        self.requests: list[dict[str, str]] = []

    def __call__(self, request, timeout=None):
        from urllib.parse import parse_qs

        body = parse_qs(request.data.decode("utf-8"))
        self.requests.append({k: v[0] for k, v in body.items()})
        payload: dict[str, object] = {"access_token": self.token, "token_type": "Bearer"}
        if self.expires_in is not None:
            payload["expires_in"] = self.expires_in
        return _response(payload)


class _Ctx:
    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)

    def __enter__(self):
        return self._body

    def __exit__(self, *exc):
        return False


def _response(payload: object) -> _Ctx:
    return _Ctx(json.dumps(payload).encode("utf-8"))


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "s3cr3t")


def patch_urlopen(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    monkeypatch.setattr("forgeo.oauth.urllib.request.urlopen", handler)


def test_token_is_requested_with_the_client_credentials_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = FakeTokenEndpoint()
    patch_urlopen(monkeypatch, endpoint)

    assert ClientCredentialsTokenProvider(make_auth()).token() == "tok-1"
    assert endpoint.requests == [
        {
            "grant_type": "client_credentials",
            "client_id": "forgeo",
            "client_secret": "s3cr3t",
        }
    ]


def test_scope_is_sent_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = FakeTokenEndpoint()
    patch_urlopen(monkeypatch, endpoint)

    ClientCredentialsTokenProvider(make_auth(scope="forgeo-backlog")).token()
    assert endpoint.requests[0]["scope"] == "forgeo-backlog"


def test_token_is_cached_until_it_nears_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = FakeTokenEndpoint(expires_in=300)
    patch_urlopen(monkeypatch, endpoint)
    provider = ClientCredentialsTokenProvider(make_auth())

    assert [provider.token() for _ in range(3)] == ["tok-1"] * 3
    assert len(endpoint.requests) == 1


def test_token_is_renewed_once_the_margin_is_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = FakeTokenEndpoint(expires_in=300)
    patch_urlopen(monkeypatch, endpoint)
    clock = [1000.0]
    monkeypatch.setattr("forgeo.oauth.time.monotonic", lambda: clock[0])
    provider = ClientCredentialsTokenProvider(make_auth())

    assert provider.token() == "tok-1"
    clock[0] += 300 - EXPIRY_MARGIN_SECONDS - 1
    assert len(endpoint.requests) == 1, "still inside the safe window"

    endpoint.token = "tok-2"
    clock[0] += 2
    assert provider.token() == "tok-2"
    assert len(endpoint.requests) == 2


def test_invalidate_forces_a_fresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = FakeTokenEndpoint()
    patch_urlopen(monkeypatch, endpoint)
    provider = ClientCredentialsTokenProvider(make_auth())

    provider.token()
    endpoint.token = "tok-2"
    provider.invalidate()
    assert provider.token() == "tok-2"
    assert len(endpoint.requests) == 2


def test_response_without_expires_in_is_still_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_urlopen(monkeypatch, FakeTokenEndpoint(expires_in=None))
    assert ClientCredentialsTokenProvider(make_auth()).token() == "tok-1"


def test_missing_secret_names_the_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SECRET_ENV, raising=False)
    with pytest.raises(TokenError, match=SECRET_ENV):
        ClientCredentialsTokenProvider(make_auth()).token()


def test_http_error_surfaces_the_providers_own_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error":"invalid_client"}'),
        )

    patch_urlopen(monkeypatch, boom)
    with pytest.raises(TokenError, match="invalid_client"):
        ClientCredentialsTokenProvider(make_auth()).token()


def test_network_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request, timeout=None):
        raise OSError("connection refused")

    patch_urlopen(monkeypatch, boom)
    with pytest.raises(TokenError, match="connection refused"):
        ClientCredentialsTokenProvider(make_auth()).token()


def test_response_without_access_token_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_urlopen(monkeypatch, lambda request, timeout=None: _response({"nope": 1}))
    with pytest.raises(TokenError, match="no access_token"):
        ClientCredentialsTokenProvider(make_auth()).token()
