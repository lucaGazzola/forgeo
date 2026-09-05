"""Tests for the instance registry (src/forgeo/instances.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from forgeo.daemon import acquire_run_lock
from forgeo.instances import (
    add_instance,
    ensure_registered,
    list_instances,
    load_registry,
    registry_path,
    remove_instance,
    resolve_instance,
    save_registry,
)
from tests.conftest import requires_posix


def write_config(tmp_path: Path, subdir: str) -> Path:
    """A minimal valid forgeo.yaml in its own directory; returns its path."""
    config_dir = tmp_path / subdir
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "forgeo.yaml"
    path.write_text(
        f"name: {subdir}\n"
        "repo: .\n"
        f"backlog: {config_dir / 'backlog.json'}\n"
        f"blocker_file: {config_dir / 'BLOCKER.md'}\n"
        "agent_command: echo hi\n",
        encoding="utf-8",
    )
    return path


def test_registry_path_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "reg" / "instances.yaml"))
    assert registry_path() == tmp_path / "reg" / "instances.yaml"


def test_registry_path_default(monkeypatch):
    monkeypatch.delenv("FORGEO_REGISTRY", raising=False)
    assert registry_path() == Path.home() / ".config" / "forgeo" / "instances.yaml"


def test_load_registry_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "nope" / "instances.yaml"))
    assert load_registry() == {}


def test_add_instance_creates_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(tmp_path, "a")

    assert add_instance("alpha", config_path) == "alpha"
    assert (tmp_path / "instances.yaml").exists()
    assert load_registry() == {"alpha": str(config_path.resolve())}


def test_add_instance_stores_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(tmp_path, "a")

    add_instance("alpha", config_path)
    assert load_registry()["alpha"] == str(config_path.resolve())
    assert Path(load_registry()["alpha"]).is_absolute()


def test_add_instance_rejects_invalid_names(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(tmp_path, "a")
    for bad in ("has space", "bad/name", "bad:name", "name?", "name#", ""):
        with pytest.raises(ValueError, match="invalid instance name"):
            add_instance(bad, config_path)
    assert load_registry() == {}


def test_add_instance_accepts_valid_names(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(tmp_path, "a")
    for good in ("my-repo", "my_repo", "repo.one", "Repo1"):
        add_instance(good, config_path)
    assert set(load_registry()) == {"my-repo", "my_repo", "repo.one", "Repo1"}


def test_add_instance_rejects_duplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(tmp_path, "a")
    add_instance("alpha", config_path)
    with pytest.raises(ValueError, match="already registered"):
        add_instance("alpha", config_path)


def test_add_instance_rejects_missing_config(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    with pytest.raises(FileNotFoundError):
        add_instance("alpha", tmp_path / "missing" / "forgeo.yaml")
    assert load_registry() == {}


def test_add_instance_rejects_invalid_config(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nrepo: .\n", encoding="utf-8")
    with pytest.raises(ValueError):
        add_instance("alpha", bad)
    assert load_registry() == {}


def test_ensure_registered_registers_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(tmp_path, "a")

    assert ensure_registered("alpha", config_path) is True
    assert load_registry() == {"alpha": str(config_path.resolve())}


def test_ensure_registered_is_noop_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(tmp_path, "a")
    add_instance("alpha", config_path)

    assert ensure_registered("alpha", config_path) is False
    assert load_registry() == {"alpha": str(config_path.resolve())}


def test_ensure_registered_noop_when_name_taken_by_other_config(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    other = write_config(tmp_path, "b")
    add_instance("alpha", other)

    assert ensure_registered("alpha", write_config(tmp_path, "a")) is False
    assert load_registry() == {"alpha": str(other.resolve())}


def test_ensure_registered_skips_invalid_name(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(tmp_path, "a")

    assert ensure_registered("bad name", config_path) is False
    assert load_registry() == {}


def test_ensure_registered_skips_missing_config(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))

    assert ensure_registered("alpha", tmp_path / "missing" / "forgeo.yaml") is False
    assert load_registry() == {}


def test_ensure_registered_skips_unloadable_config(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nrepo: .\n", encoding="utf-8")

    assert ensure_registered("alpha", bad) is False
    assert load_registry() == {}


def test_resolve_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(tmp_path, "a")
    add_instance("alpha", config_path)

    assert resolve_instance("alpha") == config_path.resolve()
    assert resolve_instance("nope") is None


def test_remove_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    add_instance("alpha", write_config(tmp_path, "a"))

    assert remove_instance("alpha") is True
    assert load_registry() == {}
    assert remove_instance("alpha") is False


def test_remove_instance_never_touches_config(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(tmp_path, "a")
    add_instance("alpha", config_path)
    before = config_path.read_text()

    assert remove_instance("alpha") is True
    assert config_path.exists()
    assert config_path.read_text() == before


def test_save_registry_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    save_registry({"a": "/x/forgeo.yaml", "b": "/y/forgeo.yaml"})

    assert load_registry() == {"a": "/x/forgeo.yaml", "b": "/y/forgeo.yaml"}
    leftover = [
        p.name for p in tmp_path.iterdir() if p.name != "instances.yaml"
    ]
    assert leftover == []


def test_save_registry_overwrites_previous(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    save_registry({"a": "/x", "b": "/y"})
    save_registry({"a": "/z"})
    assert load_registry() == {"a": "/z"}


def test_list_instances_reports_name_config_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(tmp_path, "a")
    add_instance("alpha", config_path)

    infos = list_instances()
    assert len(infos) == 1
    info = infos[0]
    assert info.name == "alpha"
    assert info.config_path == config_path.resolve()
    assert info.repo == (tmp_path / "a").resolve()
    assert info.daemon_running is False
    assert info.config is not None


@requires_posix
def test_list_instances_reports_daemon_running(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    config_path = write_config(tmp_path, "a")
    add_instance("alpha", config_path)
    lock = acquire_run_lock(tmp_path / "a" / "backlog.lock")
    assert lock is not None
    try:
        infos = list_instances()
        assert infos[0].daemon_running is True
    finally:
        lock.close()


def test_list_instances_sorted_and_tolerant(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    add_instance("zeta", write_config(tmp_path, "z"))
    add_instance("alpha", write_config(tmp_path, "a"))

    names = [info.name for info in list_instances()]
    assert names == ["alpha", "zeta"]


def test_list_instances_tolerates_unloadable_config(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_REGISTRY", str(tmp_path / "instances.yaml"))
    save_registry({"ghost": str(tmp_path / "gone" / "forgeo.yaml")})

    infos = list_instances()
    assert len(infos) == 1
    info = infos[0]
    assert info.name == "ghost"
    assert info.repo is None
    assert info.daemon_running is False
    assert info.config is None
