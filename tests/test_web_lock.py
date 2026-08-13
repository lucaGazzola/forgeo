"""Central-dashboard lock lifecycle tests: acquire/refuse/takeover/release,
``forgeo web -d`` detach, ``forgeo web stop``, and ``forgeo web status``."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from forgeo.central import (
    AUTOGENERATE_TOKEN,
    WebLock,
    WebLockError,
    load_web_token,
    resolve_web_token,
    save_web_token,
    stop_web,
    web_lock_path,
    web_token_path,
)
from forgeo.cli import build_parser, cmd_web, cmd_web_status, cmd_web_stop
from forgeo.daemon import read_lock_pid


def wait_for(predicate: Callable[[], bool], timeout: float = 15.0) -> bool:
    """Poll ``predicate`` until it holds; False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def free_port() -> int:
    """A port that is free right now (small race, fine for tests)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def web_args(*argv: str):
    return build_parser().parse_args(["web", *argv])


# --------------------------------------------------------------------------- #
# Lock path                                                                   #
# --------------------------------------------------------------------------- #


def test_web_lock_default_path():
    assert web_lock_path() == Path.home() / ".config" / "forgeo" / "web.lock"


def test_web_lock_honors_config_dir_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    assert web_lock_path() == tmp_path / "web.lock"


# --------------------------------------------------------------------------- #
# Token file + bearer auth resolution                                         #
# --------------------------------------------------------------------------- #


def test_web_token_default_path():
    assert web_token_path() == Path.home() / ".config" / "forgeo" / "web.toml"


def test_web_token_honors_config_dir_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    assert web_token_path() == tmp_path / "web.toml"


def test_load_web_token_absent_or_blank_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    assert load_web_token() is None
    (tmp_path / "web.toml").write_text("", encoding="utf-8")
    assert load_web_token() is None
    (tmp_path / "web.toml").write_text("token = \"\"", encoding="utf-8")
    assert load_web_token() is None


def test_save_and_load_web_token_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    save_web_token("abc-123")
    assert load_web_token() == "abc-123"
    assert (tmp_path / "web.toml").read_text(encoding="utf-8") == (
        'token = "abc-123"\n'
    )


def test_resolve_web_token_no_auth_default(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    token, generated = resolve_web_token(None)
    assert token is None
    assert generated is False
    assert not (tmp_path / "web.toml").exists()


def test_resolve_web_token_reuses_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    save_web_token("existing-token")
    token, generated = resolve_web_token(None)
    assert token == "existing-token"
    assert generated is False


def test_resolve_web_token_explicit_value_is_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    token, generated = resolve_web_token("my-secret")
    assert token == "my-secret"
    assert generated is False
    assert load_web_token() == "my-secret"


def test_resolve_web_token_autogenerates_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    token, generated = resolve_web_token(AUTOGENERATE_TOKEN)
    assert generated is True
    assert token
    assert load_web_token() == token


def test_resolve_web_token_autogenerates_when_file_without_token(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    (tmp_path / "web.toml").write_text("", encoding="utf-8")
    token, generated = resolve_web_token(None)
    assert generated is True
    assert token
    assert load_web_token() == token


# --------------------------------------------------------------------------- #
# Lock lifecycle: acquire / refuse / takeover / release                       #
# --------------------------------------------------------------------------- #


def test_web_lock_acquire_release(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    lock = WebLock()
    assert not lock.is_held()
    lock.acquire(host="127.0.0.1", port=9000)
    assert lock.is_held()
    assert lock.pid == os.getpid()
    assert lock.host == "127.0.0.1"
    assert lock.port == 9000
    assert read_lock_pid(lock.lock_path) == os.getpid()
    lock.release()
    assert not lock.is_held()
    assert not lock.lock_path.exists()


def test_web_lock_written_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    lock = WebLock()
    lock.acquire(host="0.0.0.0", port=8790)
    try:
        text = lock.lock_path.read_text(encoding="utf-8")
        assert text == f"pid={os.getpid()}\nhost=0.0.0.0\nport=8790\n"
    finally:
        lock.release()


def test_web_lock_refuses_second(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    first = WebLock()
    first.acquire()
    try:
        with pytest.raises(WebLockError):
            WebLock().acquire()
        assert first.is_held()
    finally:
        first.release()


def test_web_lock_takes_over_stale(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    lock_path = tmp_path / "web.lock"
    lock_path.write_text("pid=999999999\nhost=0.0.0.0\nport=8790\n", encoding="utf-8")
    lock = WebLock()
    with caplog.at_level(logging.WARNING, logger="forgeo.central"):
        lock.acquire()
    assert "stale" in caplog.text.lower()
    assert lock.is_held()
    assert lock.pid == os.getpid()
    lock.release()


def test_web_lock_takes_over_corrupt(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    (tmp_path / "web.lock").write_text("not a lock file\n", encoding="utf-8")
    lock = WebLock()
    lock.acquire()
    try:
        assert lock.pid == os.getpid()
    finally:
        lock.release()


# --------------------------------------------------------------------------- #
# forgeo web status                                                           #
# --------------------------------------------------------------------------- #


def test_web_status_not_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    assert cmd_web_status(web_args("status")) == 0
    assert "not running" in capsys.readouterr().out


def test_web_status_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    lock = WebLock()
    lock.acquire(host="127.0.0.1", port=9000)
    try:
        assert cmd_web_status(web_args("status")) == 0
        out = capsys.readouterr().out
        assert "running" in out
        assert str(os.getpid()) in out
        assert "127.0.0.1" in out
        assert "9000" in out
    finally:
        lock.release()


# --------------------------------------------------------------------------- #
# forgeo web stop                                                             #
# --------------------------------------------------------------------------- #


def test_web_stop_not_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    assert cmd_web_stop(web_args("stop")) == 1
    assert "not running" in capsys.readouterr().out


def test_web_stop_with_dead_pid(tmp_path, monkeypatch, capsys):
    """A lock recording a dead PID means the dashboard is not running."""
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    (tmp_path / "web.lock").write_text("pid=999999999\n", encoding="utf-8")
    assert cmd_web_stop(web_args("stop")) == 1
    assert "not running" in capsys.readouterr().out


def test_web_stop_terminates_running_dashboard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    port = free_port()
    env = {**os.environ, "FORGEO_CONFIG_DIR": str(tmp_path)}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "forgeo",
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    lock_path = tmp_path / "web.lock"
    try:
        assert wait_for(lambda: WebLock().is_held())
        assert cmd_web_stop(web_args("stop", "--timeout", "15")) == 0
        assert "stopped" in capsys.readouterr().out
        assert wait_for(lambda: proc.poll() is not None)
        assert not lock_path.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_stop_web_survives_process_lookup_error(tmp_path, monkeypatch):
    """A pid that dies between the alive-check and the SIGTERM is fine."""
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    lock = WebLock()
    lock.acquire()
    try:
        def boom(_pid: int, _sig: int) -> None:
            raise ProcessLookupError()

        monkeypatch.setattr("forgeo.central.os.kill", boom)
        state = {"calls": 0}

        def fake_is_held(_self: WebLock) -> bool:
            state["calls"] += 1
            return state["calls"] == 1

        monkeypatch.setattr(WebLock, "is_held", fake_is_held)
        stop_web()
    finally:
        lock.release()


def test_stop_web_permission_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    lock = WebLock()
    lock.acquire()
    try:
        def boom(_pid: int, _sig: int) -> None:
            raise PermissionError()

        monkeypatch.setattr("forgeo.central.os.kill", boom)
        with pytest.raises(WebLockError):
            stop_web()
    finally:
        lock.release()


# --------------------------------------------------------------------------- #
# forgeo web -d (detach)                                                      #
# --------------------------------------------------------------------------- #


class _FakeProc:
    """A stand-in Popen: writes the dashboard lock when running, and either
    stays up (``poll() -> None``) or exits immediately."""

    def __init__(self, pid: int, lock_path: Path, *, running: bool) -> None:
        self.pid = pid
        self.lock_path = lock_path
        self.running = running
        if running:
            self.lock_path.write_text(
                f"pid={pid}\nhost=127.0.0.1\nport=8790\n", encoding="utf-8"
            )

    def poll(self) -> int | None:
        return None if self.running else 0


def test_web_detach_starts_fake_server(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))

    def fake_popen(*_args, **_kwargs) -> _FakeProc:
        return _FakeProc(os.getpid(), tmp_path / "web.lock", running=True)

    monkeypatch.setattr("forgeo.central.subprocess.Popen", fake_popen)
    try:
        args = web_args(
            "-d", "--host", "127.0.0.1", "--port", "8790", "--timeout", "5"
        )
        assert cmd_web(args) == 0
        out = capsys.readouterr().out
        assert "started in the background" in out
        assert str(os.getpid()) in out
        assert "127.0.0.1" in out
        assert WebLock().is_held()
    finally:
        WebLock().release()


def test_web_detach_refuses_while_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    lock = WebLock()
    lock.acquire(host="127.0.0.1", port=8790)
    try:
        assert cmd_web(web_args("-d")) == 1
        assert "already running" in capsys.readouterr().out
    finally:
        lock.release()


def test_web_detach_generates_and_prints_token_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))

    def fake_popen(*_args, **_kwargs) -> _FakeProc:
        return _FakeProc(os.getpid(), tmp_path / "web.lock", running=True)

    monkeypatch.setattr("forgeo.central.subprocess.Popen", fake_popen)
    try:
        assert cmd_web(web_args("-d", "--token")) == 0
        out = capsys.readouterr().out
        assert "Web token:" in out
        assert "saved to web.toml" in out
        assert load_web_token() is not None
    finally:
        WebLock().release()


def test_web_detach_reuses_file_token_without_print(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    save_web_token("known-token")

    def fake_popen(*_args, **_kwargs) -> _FakeProc:
        return _FakeProc(os.getpid(), tmp_path / "web.lock", running=True)

    monkeypatch.setattr("forgeo.central.subprocess.Popen", fake_popen)
    try:
        assert cmd_web(web_args("-d")) == 0
        assert "Web token:" not in capsys.readouterr().out
    finally:
        WebLock().release()


def test_web_detach_explicit_token_flag_is_persisted(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))

    def fake_popen(*_args, **_kwargs) -> _FakeProc:
        return _FakeProc(os.getpid(), tmp_path / "web.lock", running=True)

    monkeypatch.setattr("forgeo.central.subprocess.Popen", fake_popen)
    try:
        assert cmd_web(web_args("-d", "--token", "explicit-token")) == 0
        assert "Web token:" not in capsys.readouterr().out
        assert load_web_token() == "explicit-token"
    finally:
        WebLock().release()


def test_web_detach_warns_on_stale_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))
    (tmp_path / "web.lock").write_text("pid=999999999\n", encoding="utf-8")

    def fake_popen(*_args, **_kwargs) -> _FakeProc:
        return _FakeProc(os.getpid(), tmp_path / "web.lock", running=True)

    monkeypatch.setattr("forgeo.central.subprocess.Popen", fake_popen)
    try:
        assert cmd_web(web_args("-d", "--timeout", "5")) == 0
        out = capsys.readouterr().out
        assert "Stale dashboard lock" in out
        assert "taking over" in out
    finally:
        WebLock().release()


def test_web_detach_fails_when_server_never_binds(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))

    def fake_popen(*_args, **_kwargs) -> _FakeProc:
        return _FakeProc(12345, tmp_path / "web.lock", running=False)

    monkeypatch.setattr("forgeo.central.subprocess.Popen", fake_popen)
    assert cmd_web(web_args("-d", "--timeout", "2")) == 1
    assert "did not start" in capsys.readouterr().out


def test_web_parser_flags():
    args = build_parser().parse_args(["web", "-d", "--timeout", "7"])
    assert args.detach is True
    assert args.timeout == 7.0
    assert args.web_action is None

    stop = build_parser().parse_args(["web", "stop", "--timeout", "5"])
    assert stop.web_action == "stop"
    assert stop.timeout == 5.0

    assert build_parser().parse_args(["web", "status"]).web_action == "status"


def test_web_parser_token_flag():
    assert build_parser().parse_args(["web"]).token is None
    assert build_parser().parse_args(["web", "--token", "abc"]).token == "abc"
    assert (
        build_parser().parse_args(["web", "--token"]).token is AUTOGENERATE_TOKEN
    )


def _run_foreground_noop(monkeypatch, tmp_path):
    """Patch the foreground server plumbing so ``run_foreground`` returns
    immediately after printing its banner instead of serving forever."""
    monkeypatch.setenv("FORGEO_CONFIG_DIR", str(tmp_path))

    async def fake_serve_forever(server, host, stop_requested):
        return None

    monkeypatch.setattr("forgeo.central._serve_forever", fake_serve_forever)
    monkeypatch.setattr("forgeo.central.signal.signal", lambda *a, **k: None)
    monkeypatch.setattr("forgeo.central.CentralWebServer.start", lambda self: True)


def test_run_foreground_prints_generated_token_once(tmp_path, monkeypatch, capsys):
    from forgeo.central import run_foreground

    _run_foreground_noop(monkeypatch, tmp_path)
    rc = run_foreground(host="127.0.0.1", port=0, token=AUTOGENERATE_TOKEN)
    assert rc == 0
    err = capsys.readouterr().err
    assert "Web token:" in err
    assert load_web_token() is not None
    assert not WebLock().is_held()


def test_run_foreground_reuses_file_token_without_print(tmp_path, monkeypatch, capsys):
    from forgeo.central import run_foreground

    _run_foreground_noop(monkeypatch, tmp_path)
    save_web_token("known-token")
    rc = run_foreground(host="127.0.0.1", port=0)
    assert rc == 0
    assert "Web token:" not in capsys.readouterr().err
    assert not WebLock().is_held()


def test_run_foreground_no_token_stays_open(tmp_path, monkeypatch, capsys):
    from forgeo.central import run_foreground

    _run_foreground_noop(monkeypatch, tmp_path)
    rc = run_foreground(host="127.0.0.1", port=0)
    assert rc == 0
    assert "Web token:" not in capsys.readouterr().err
    assert not (tmp_path / "web.toml").exists()
    assert not WebLock().is_held()
