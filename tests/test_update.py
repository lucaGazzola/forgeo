"""Tests for the best-effort update check (:mod:`forgeo.update`)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

import pytest

from forgeo import __version__
from forgeo.update import (
    UpdateState,
    check_for_update,
    fetch_latest_version,
    installed_version,
    update_state_path,
    upgrade_notice,
    version_is_newer,
)


@pytest.fixture(autouse=True)
def _update_check_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-enable the check for these tests (conftest disables it globally)."""
    monkeypatch.delenv("FORGEO_UPDATE_CHECK", raising=False)


class FakeResponse:
    """A minimal ``urllib`` response whose payload is a canned JSON body."""

    def __init__(self, payload: object) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _pypi_response(version: str) -> FakeResponse:
    return FakeResponse({"info": {"version": version}})


def test_version_is_newer_compares_dotted_versions() -> None:
    assert version_is_newer("0.5.0", "0.4.0")
    assert version_is_newer("1.0.0", "0.9.9")
    assert not version_is_newer("0.4.0", "0.4.0")
    assert not version_is_newer("0.4.0", "0.5.0")


def test_version_is_newer_ignores_suffixes() -> None:
    assert version_is_newer("1.2.0rc1", "1.1.0")
    assert not version_is_newer("1.2.0rc1", "1.2.0")


def test_upgrade_notice_names_version_and_command() -> None:
    notice = upgrade_notice("0.5.0", "0.4.0")
    assert "0.4.0" in notice
    assert "0.5.0" in notice
    assert "install.sh" in notice
    assert "pipx upgrade forgeo-cli" in notice
    assert "pip install --user --upgrade forgeo-cli" in notice


def test_check_notifies_when_outdated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        "forgeo.update.urllib.request.urlopen",
        lambda *a, **k: calls.append(a) or _pypi_response("999.0.0"),
    )
    state = update_state_path(tmp_path / "backlog.json")

    notice = check_for_update(state)

    assert notice is not None
    assert "999.0.0" in notice
    assert "pipx upgrade forgeo-cli" in notice
    assert len(calls) == 1


def test_check_prints_notice_when_outdated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "forgeo.update.urllib.request.urlopen",
        lambda *a, **k: _pypi_response("999.0.0"),
    )
    state = update_state_path(tmp_path / "backlog.json")

    check_for_update(state, print_fn=print)

    out = capsys.readouterr().out
    assert "999.0.0" in out
    assert "pipx upgrade forgeo-cli" in out


def test_check_silent_when_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        "forgeo.update.urllib.request.urlopen",
        lambda *a, **k: calls.append(a) or _pypi_response(__version__),
    )
    state = update_state_path(tmp_path / "backlog.json")

    assert check_for_update(state) is None
    assert len(calls) == 1


def test_check_network_failure_is_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*_a: object, **_k: object) -> FakeResponse:
        raise OSError("no network")

    monkeypatch.setattr("forgeo.update.urllib.request.urlopen", boom)
    state = update_state_path(tmp_path / "backlog.json")

    assert check_for_update(state) is None


def test_check_malformed_response_is_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "forgeo.update.urllib.request.urlopen",
        lambda *a, **k: FakeResponse({"unexpected": True}),
    )
    state = update_state_path(tmp_path / "backlog.json")

    assert check_for_update(state) is None


def test_fetch_latest_version_non_json_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "forgeo.update.urllib.request.urlopen",
        lambda *a, **k: FakeResponse("not json"),
    )
    assert fetch_latest_version() is None


def test_check_network_at_most_once_per_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        "forgeo.update.urllib.request.urlopen",
        lambda *a, **k: calls.append(a) or _pypi_response("999.0.0"),
    )
    state = update_state_path(tmp_path / "backlog.json")

    assert check_for_update(state) is not None
    assert check_for_update(state) is None
    assert len(calls) == 1


def test_state_due_after_interval(tmp_path: Path) -> None:
    state = UpdateState(tmp_path / "update.json")
    now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    assert state.due(now)
    state.mark(now)
    assert not state.due(now)
    assert not state.due(now + timedelta(hours=23))
    assert state.due(now + timedelta(days=1, seconds=1))


def test_state_survives_corrupt_file(tmp_path: Path) -> None:
    state = UpdateState(tmp_path / "update.json")
    state.path.write_text("garbage", encoding="utf-8")
    assert state.due()


def test_state_path_next_to_backlog(tmp_path: Path) -> None:
    assert update_state_path(tmp_path / "backlog.json") == tmp_path / "backlog.update.json"


def test_installed_version_available() -> None:
    assert installed_version() == __version__


def test_check_disabled_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEO_UPDATE_CHECK", "0")
    calls: list[object] = []
    monkeypatch.setattr(
        "forgeo.update.urllib.request.urlopen",
        lambda *a, **k: calls.append(a) or _pypi_response("999.0.0"),
    )
    state = update_state_path(tmp_path / "backlog.json")

    assert check_for_update(state) is None
    assert calls == []
