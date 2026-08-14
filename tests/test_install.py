"""Tests for the root install.sh one-liner installer.

The script is exercised with stub `uname`/`curl`/`wget`/`python3`/`pipx`/`pip`
binaries on PATH so no network access is needed. Each stub logs its argv to
``$STUB_LOG`` so the tests can assert exactly what the installer invoked.

Two code paths are covered:

* the **binary path** — the default when a prebuilt GitHub Release binary
  matches the host OS/arch: downloaded with curl (or wget) into
  ``~/.local/bin/forgeo``, no Python required;
* the **fallback path** — pipx then ``pip install --user``, used only when no
  prebuilt binary matches the platform (stubbed here with a foreign OS).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
PYPI_PACKAGE = "forgeo-cli"
VERSION = "0.6.0"
SH = shutil.which("sh")
assert SH, "sh must be available to run install.sh"

FAKE_BINARY = "#!/bin/sh\nprintf 'forgeo-binary-stub %s\\n' \"${1:-}\"\n"

PYTHON_STUB = """printf 'python3 %s\\n' "$*" >> "$STUB_LOG"
if [ "$1" = "-c" ]; then
    exit "${PY_VERSION_OK:-0}"
fi
if [ "$1" = "-m" ] && [ "$2" = "site" ]; then
    printf '%s\\n' "$STUB_USER_BASE"
    exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
    exit 0
fi
exit 0"""

GENERIC_STUB = """printf '%s %s\\n' "${0##*/}" "$*" >> "$STUB_LOG"
exit 0"""

UNAME_STUB = """if [ "$1" = "-s" ]; then
    printf '%s\\n' "${FORGEO_TEST_OS:-Linux}"
elif [ "$1" = "-m" ]; then
    printf '%s\\n' "${FORGEO_TEST_ARCH:-x86_64}"
fi
exit 0"""

CURL_STUB = """printf 'curl %s\\n' "$*" >> "$STUB_LOG"
dest=""
prev=""
for arg do
    if [ "$prev" = "-o" ]; then dest="$arg"; fi
    prev="$arg"
done
cp "$FORGEO_FAKE_BINARY" "$dest"
exit 0"""

WGET_STUB = """printf 'wget %s\\n' "$*" >> "$STUB_LOG"
dest=""
prev=""
for arg do
    if [ "$prev" = "-O" ]; then dest="$arg"; fi
    prev="$arg"
done
cp "$FORGEO_FAKE_BINARY" "$dest"
exit 0"""


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _forwarding_stub(command: str) -> str:
    """A stub that forwards to the real ``command`` by absolute path, so the
    test stays hermetic (PATH contains only the stub dir) while install.sh can
    still use coreutils like mkdir/mv/chmod."""
    real = shutil.which(command)
    assert real, f"real '{command}' not found on host for forwarding stub"
    return f'#!/bin/sh\nexec {real} "$@"\n'


def _write_bin(bin_dir: Path, stubs: list[str]) -> None:
    bin_dir.mkdir(exist_ok=True)
    for command in ("mkdir", "chmod", "mv", "rm", "cp"):
        _write_stub(bin_dir, command, _forwarding_stub(command))
    for name in stubs:
        if name == "python3":
            _write_stub(bin_dir, name, PYTHON_STUB)
        elif name == "uname":
            _write_stub(bin_dir, name, UNAME_STUB)
        elif name == "curl":
            _write_stub(bin_dir, name, CURL_STUB)
        elif name == "wget":
            _write_stub(bin_dir, name, WGET_STUB)
        else:
            _write_stub(bin_dir, name, GENERIC_STUB)


def _run_install(
    tmp_path: Path,
    bin_dir: Path,
    *,
    stubs: list[str] | None = None,
    os_name: str = "Linux",
    arch: str = "x86_64",
    python_ok: bool = True,
    user_base_on_path: bool = False,
    home_bin_on_path: bool = False,
) -> subprocess.CompletedProcess:
    log = tmp_path / "calls.log"
    user_base = tmp_path / "userbase"
    path_dirs = [str(bin_dir)]
    if user_base_on_path:
        path_dirs.append(str(user_base / "bin"))
    if home_bin_on_path:
        path_dirs.append(str(tmp_path / ".local" / "bin"))
    env = {
        **os.environ,
        "PATH": ":".join(path_dirs),
        "STUB_LOG": str(log),
        "STUB_USER_BASE": str(user_base),
        "PY_VERSION_OK": "0" if python_ok else "1",
        "FORGEO_TEST_OS": os_name,
        "FORGEO_TEST_ARCH": arch,
        "FORGEO_FAKE_BINARY": str(tmp_path / "fake_binary"),
        "HOME": str(tmp_path),
    }
    (tmp_path / "fake_binary").write_text(FAKE_BINARY, encoding="utf-8")
    _write_bin(bin_dir, stubs or ["uname", "curl"])
    return subprocess.run(
        [SH, str(INSTALL_SH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    if not log.exists():
        return []
    return log.read_text(encoding="utf-8").splitlines()


def _installed_binary(tmp_path: Path) -> Path:
    return tmp_path / ".local" / "bin" / "forgeo"


# --- binary-download path ----------------------------------------------------


def test_binary_install_works_without_python(tmp_path):
    bin_dir = tmp_path / "bin"

    result = _run_install(tmp_path, bin_dir)

    assert result.returncode == 0, result.stderr
    forgeo = _installed_binary(tmp_path)
    assert forgeo.exists()
    assert os.access(forgeo, os.X_OK)
    assert "forgeo init" in result.stdout
    assert "forgeo start" in result.stdout
    assert "python3" not in " ".join(_calls(tmp_path))
    assert "pipx" not in " ".join(_calls(tmp_path))


def test_binary_download_uses_release_asset_url(tmp_path):
    bin_dir = tmp_path / "bin"

    result = _run_install(tmp_path, bin_dir)

    assert result.returncode == 0, result.stderr
    curl_lines = [line for line in _calls(tmp_path) if line.startswith("curl ")]
    assert curl_lines, "curl was not invoked"
    assert any(
        f"releases/download/v{VERSION}/forgeo-linux-amd64" in line for line in curl_lines
    ), curl_lines


def test_binary_rerun_is_idempotent(tmp_path):
    bin_dir = tmp_path / "bin"

    first = _run_install(tmp_path, bin_dir)
    second = _run_install(tmp_path, bin_dir)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert _installed_binary(tmp_path).exists()
    curl_lines = [line for line in _calls(tmp_path) if line.startswith("curl ")]
    assert len([l for l in curl_lines if "api.github.com" in l]) == 2
    assert len([l for l in curl_lines if "releases/download" in l]) == 2


def test_binary_warns_when_bin_dir_not_on_path(tmp_path):
    bin_dir = tmp_path / "bin"

    result = _run_install(tmp_path, bin_dir)

    assert result.returncode == 0, result.stderr
    assert "not on your PATH" in result.stderr
    assert str(tmp_path / ".local" / "bin") in result.stderr


def test_binary_silent_when_bin_dir_on_path(tmp_path):
    bin_dir = tmp_path / "bin"

    result = _run_install(tmp_path, bin_dir, home_bin_on_path=True)

    assert result.returncode == 0, result.stderr
    assert "not on your PATH" not in result.stderr


def test_binary_download_via_wget_when_no_curl(tmp_path):
    bin_dir = tmp_path / "bin"

    result = _run_install(tmp_path, bin_dir, stubs=["uname", "wget"])

    assert result.returncode == 0, result.stderr
    assert _installed_binary(tmp_path).exists()
    wget_lines = [line for line in _calls(tmp_path) if line.startswith("wget ")]
    assert any("forgeo-linux-amd64" in line for line in wget_lines)


def test_darwin_arm64_maps_to_binary(tmp_path):
    bin_dir = tmp_path / "bin"

    result = _run_install(tmp_path, bin_dir, os_name="Darwin", arch="arm64")

    assert result.returncode == 0, result.stderr
    curl_lines = [line for line in _calls(tmp_path) if line.startswith("curl ")]
    assert any("forgeo-darwin-arm64" in line for line in curl_lines)


def test_no_download_tool_with_matching_platform_fails(tmp_path):
    bin_dir = tmp_path / "bin"

    result = _run_install(tmp_path, bin_dir, stubs=["uname"], python_ok=True)

    assert result.returncode != 0
    assert "curl or wget" in result.stderr
    assert "pipx" not in " ".join(_calls(tmp_path))


# --- fallback path (no prebuilt binary matches the platform) -----------------


def test_pipx_fallback_when_no_binary_for_platform(tmp_path):
    bin_dir = tmp_path / "bin"

    result = _run_install(
        tmp_path,
        bin_dir,
        stubs=["uname", "curl", "python3", "pipx", "pip"],
        os_name="FreeBSD",
    )

    assert result.returncode == 0, result.stderr
    assert f"pipx install --force {PYPI_PACKAGE}" in _calls(tmp_path)
    assert not any("python3 -m pip" in line for line in _calls(tmp_path))
    assert "forgeo init" in result.stdout
    assert "forgeo start" in result.stdout


def test_pipx_rerun_upgrades_instead_of_failing(tmp_path):
    bin_dir = tmp_path / "bin"
    stubs = ["uname", "curl", "python3", "pipx"]

    first = _run_install(tmp_path, bin_dir, stubs=stubs, os_name="FreeBSD")
    second = _run_install(tmp_path, bin_dir, stubs=stubs, os_name="FreeBSD")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert _calls(tmp_path).count(f"pipx install --force {PYPI_PACKAGE}") == 2


def test_pip_fallback_warns_when_user_bin_not_on_path(tmp_path):
    bin_dir = tmp_path / "bin"

    result = _run_install(
        tmp_path, bin_dir, stubs=["uname", "curl", "python3", "pip"], os_name="FreeBSD"
    )

    assert result.returncode == 0, result.stderr
    assert f"python3 -m pip install --user --upgrade {PYPI_PACKAGE}" in _calls(tmp_path)
    assert "not on your PATH" in result.stderr
    assert "forgeo init" in result.stdout


def test_pip_fallback_silent_when_user_bin_on_path(tmp_path):
    bin_dir = tmp_path / "bin"

    result = _run_install(
        tmp_path,
        bin_dir,
        stubs=["uname", "curl", "python3"],
        os_name="FreeBSD",
        user_base_on_path=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"python3 -m pip install --user --upgrade {PYPI_PACKAGE}" in _calls(tmp_path)
    assert "not on your PATH" not in result.stderr


def test_missing_interpreter_and_no_binary_fails_with_actionable_error(tmp_path):
    bin_dir = tmp_path / "bin"

    result = _run_install(tmp_path, bin_dir, os_name="FreeBSD")

    assert result.returncode != 0
    assert "Python 3.11 or newer is required" in result.stderr
    assert "pipx" not in " ".join(_calls(tmp_path))


def test_too_old_python_and_no_binary_fails(tmp_path):
    bin_dir = tmp_path / "bin"

    result = _run_install(
        tmp_path, bin_dir, stubs=["uname", "curl", "python3"], os_name="FreeBSD", python_ok=False
    )

    assert result.returncode != 0
    assert "Python 3.11 or newer is required" in result.stderr
