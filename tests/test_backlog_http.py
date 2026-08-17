"""The HTTP backlog backend: the wire protocol, and how failures behave.

The most important property under test is that an unreachable endpoint raises
instead of reading as an empty backlog — an empty backlog makes Forgeo run a
refactoring pass and then POST that emptiness back over the real task list.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from forgeo.backlog import open_backlog
from forgeo.backlog_http import BacklogUnavailableError, HttpBacklog
from forgeo.models import BacklogAuth, ForgeoConfig, TaskStatus
from tests.conftest import make_result, make_task

URL = "https://api.example.com/api/forgeo/backlog"
SECRET_ENV = "FORGEO_TEST_CLIENT_SECRET"


class FakeEndpoint:
    """An in-memory backlog endpoint: GET returns it, POST replaces it."""

    def __init__(self, document: object | None = None) -> None:
        self.document: object = {"tasks": []} if document is None else document
        self.calls: list[tuple[str, dict[str, str], object]] = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.calls.append((request.method, dict(request.headers), body))
        if request.method == "POST":
            self.document = body
            return _Ctx(b"")
        return _Ctx(json.dumps(self.document).encode("utf-8"))

    @property
    def methods(self) -> list[str]:
        return [method for method, _, _ in self.calls]


class _Ctx:
    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)

    def __enter__(self):
        return self._body

    def __exit__(self, *exc):
        return False


def patch_urlopen(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    monkeypatch.setattr("forgeo.backlog_http.urllib.request.urlopen", handler)


def http_error(code: int, reason: str = "Boom"):
    def raise_it(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, code, reason, {}, io.BytesIO(b""))

    return raise_it


# --------------------------------------------------------------------------- #
# The wire protocol                                                            #
# --------------------------------------------------------------------------- #


async def test_list_tasks_reads_the_document_with_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = make_task(id="TASK-001")
    endpoint = FakeEndpoint({"tasks": [json.loads(task.model_dump_json())]})
    patch_urlopen(monkeypatch, endpoint)

    tasks = await HttpBacklog(URL).list_tasks()

    assert [t.id for t in tasks] == ["TASK-001"]
    assert endpoint.methods == ["GET"]


async def test_a_mutation_posts_the_whole_document_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = FakeEndpoint()
    patch_urlopen(monkeypatch, endpoint)
    backlog = HttpBacklog(URL)

    await backlog.create_task(make_task(id="TASK-001"))
    await backlog.create_task(make_task(id="TASK-002"))

    assert endpoint.methods == ["GET", "POST", "GET", "POST"]
    assert [t["id"] for t in endpoint.document["tasks"]] == ["TASK-001", "TASK-002"]


async def test_status_transitions_round_trip_through_the_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = FakeEndpoint()
    patch_urlopen(monkeypatch, endpoint)
    backlog = HttpBacklog(URL)
    await backlog.create_task(make_task(id="TASK-001"))

    await backlog.set_blocked("TASK-001", ["needs a decision"], make_result())
    stored = endpoint.document["tasks"][0]
    assert stored["status"] == "BLOCKED"
    assert stored["blocker_reason"] == ["needs a decision"]

    reopened = await backlog.reopen_task("TASK-001")
    assert reopened is not None and reopened.status is TaskStatus.OPEN
    assert endpoint.document["tasks"][0]["blocker_reason"] == []


async def test_requests_declare_json(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = FakeEndpoint()
    patch_urlopen(monkeypatch, endpoint)

    await HttpBacklog(URL).create_task(make_task(id="TASK-001"))

    get_headers, post_headers = endpoint.calls[0][1], endpoint.calls[1][1]
    assert get_headers["Accept"] == "application/json"
    assert post_headers["Content-type"] == "application/json"


@pytest.mark.parametrize(
    "document",
    [
        {"tasks": []},
        {},
        {"tasks": "not a list"},
        [],
        "nonsense",
    ],
)
async def test_documents_without_usable_tasks_read_as_empty(
    monkeypatch: pytest.MonkeyPatch, document: object
) -> None:
    patch_urlopen(monkeypatch, FakeEndpoint(document))
    assert await HttpBacklog(URL).list_tasks() == []


# --------------------------------------------------------------------------- #
# Failures: never silently empty                                               #
# --------------------------------------------------------------------------- #


async def test_server_error_on_read_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_urlopen(monkeypatch, http_error(500, "Internal Server Error"))
    with pytest.raises(BacklogUnavailableError, match="500"):
        await HttpBacklog(URL).list_tasks()


async def test_network_failure_on_read_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(request, timeout=None):
        raise OSError("connection refused")

    patch_urlopen(monkeypatch, boom)
    with pytest.raises(BacklogUnavailableError, match="connection refused"):
        await HttpBacklog(URL).list_tasks()


async def test_non_json_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_urlopen(monkeypatch, lambda request, timeout=None: _Ctx(b"<html>oops</html>"))
    with pytest.raises(BacklogUnavailableError, match="not JSON"):
        await HttpBacklog(URL).list_tasks()


async def test_failure_on_write_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(request, timeout=None):
        calls.append(request.method)
        if request.method == "POST":
            raise urllib.error.HTTPError(URL, 503, "Unavailable", {}, io.BytesIO(b""))
        return _Ctx(b'{"tasks": []}')

    patch_urlopen(monkeypatch, handler)
    with pytest.raises(BacklogUnavailableError, match="503"):
        await HttpBacklog(URL).create_task(make_task(id="TASK-001"))
    assert calls == ["GET", "POST"]


# --------------------------------------------------------------------------- #
# Authentication                                                               #
# --------------------------------------------------------------------------- #


def make_auth(**overrides) -> BacklogAuth:
    defaults = {
        "token_url": "https://keycloak.test/realms/dev/protocol/openid-connect/token",
        "client_id": "forgeo",
        "client_secret_env": SECRET_ENV,
    }
    defaults.update(overrides)
    return BacklogAuth(**defaults)


def patch_token(monkeypatch: pytest.MonkeyPatch, tokens: list[str]) -> list[str]:
    """Serve ``tokens`` in order; returns the list of tokens handed out."""
    issued: list[str] = []

    def token(self) -> str:
        issued.append(tokens[min(len(issued), len(tokens) - 1)])
        return issued[-1]

    monkeypatch.setattr(
        "forgeo.oauth.ClientCredentialsTokenProvider.token", token, raising=True
    )
    return issued


async def test_bearer_token_is_sent_on_read_and_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = FakeEndpoint()
    patch_urlopen(monkeypatch, endpoint)
    patch_token(monkeypatch, ["tok-1"])

    await HttpBacklog(URL, auth=make_auth()).create_task(make_task(id="TASK-001"))

    assert [headers["Authorization"] for _, headers, _ in endpoint.calls] == [
        "Bearer tok-1",
        "Bearer tok-1",
    ]


async def test_rejected_token_is_refreshed_and_the_request_retried_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def handler(request, timeout=None):
        seen.append(request.headers["Authorization"])
        if len(seen) == 1:
            raise urllib.error.HTTPError(URL, 401, "Unauthorized", {}, io.BytesIO(b""))
        return _Ctx(b'{"tasks": []}')

    patch_urlopen(monkeypatch, handler)
    patch_token(monkeypatch, ["stale", "fresh"])

    assert await HttpBacklog(URL, auth=make_auth()).list_tasks() == []
    assert seen == ["Bearer stale", "Bearer fresh"]


async def test_a_second_rejection_is_not_retried_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    def handler(request, timeout=None):
        attempts.append(request.method)
        raise urllib.error.HTTPError(URL, 401, "Unauthorized", {}, io.BytesIO(b""))

    patch_urlopen(monkeypatch, handler)
    patch_token(monkeypatch, ["tok"])

    with pytest.raises(BacklogUnavailableError, match="401"):
        await HttpBacklog(URL, auth=make_auth()).list_tasks()
    assert attempts == ["GET", "GET"]


async def test_missing_secret_fails_the_request_not_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SECRET_ENV, raising=False)
    patch_urlopen(monkeypatch, FakeEndpoint())

    with pytest.raises(BacklogUnavailableError, match=SECRET_ENV):
        await HttpBacklog(URL, auth=make_auth()).list_tasks()


async def test_no_authorization_header_without_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = FakeEndpoint()
    patch_urlopen(monkeypatch, endpoint)

    await HttpBacklog(URL).list_tasks()
    assert "Authorization" not in endpoint.calls[0][1]


# --------------------------------------------------------------------------- #
# The factory                                                                  #
# --------------------------------------------------------------------------- #


def test_open_backlog_picks_the_backend_from_the_config(tmp_path) -> None:
    from forgeo.backlog import JSONBacklog

    file_backed = open_backlog(
        ForgeoConfig(agent_command="true", backlog=tmp_path / "backlog.json")
    )
    assert isinstance(file_backed, JSONBacklog)

    remote = open_backlog(ForgeoConfig(agent_command="true", backlog=URL))
    assert isinstance(remote, HttpBacklog)
    assert remote.url == URL


def test_open_backlog_passes_the_credentials_through() -> None:
    config = ForgeoConfig(agent_command="true", backlog=URL, backlog_auth=make_auth())
    remote = open_backlog(config)
    assert isinstance(remote, HttpBacklog)
    assert remote._tokens is not None
