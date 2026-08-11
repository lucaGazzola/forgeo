"""Tests for the release-pipeline Homebrew formula renderer."""

from __future__ import annotations

import pathlib

import pytest

from scripts.render_homebrew_formula import render, sha256

ASSETS = [
    "forgeo-darwin-arm64",
    "forgeo-darwin-amd64",
    "forgeo-linux-amd64",
]


def _assets(tmp_path: pathlib.Path) -> pathlib.Path:
    for name in ASSETS:
        (tmp_path / name).write_bytes(b"fake-binary-" + name.encode())
    return tmp_path


def _line_counts(formula: str) -> tuple[int, int]:
    """(open blocks, closing ends) counted on do/if/def/test/class lines."""
    opens = 0
    for line in formula.splitlines():
        stripped = line.strip()
        if stripped.startswith(("on_", "def install", "test do", "if ", "else")):
            opens += 1
        if stripped == "end":
            opens -= 1
    return opens, 0


def test_render_embeds_urls_and_hashes(tmp_path: pathlib.Path) -> None:
    assets = _assets(tmp_path)

    formula = render("0.4.0", assets)

    assert "class Forgeo < Formula" in formula
    assert 'version "0.4.0"' in formula
    for name in ASSETS:
        url = f"https://github.com/lucaGazzola/forgeo/releases/download/v0.4.0/{name}"
        assert f'url "{url}"' in formula
        assert f'sha256 "{sha256(assets / name)}"' in formula
    assert "v0.4.0" in formula


def test_main_strips_v_from_version(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib
    import io

    from scripts.render_homebrew_formula import main

    monkeypatch.setattr(
        "sys.argv", ["render_homebrew_formula.py", "v0.4.0", str(_assets(tmp_path))]
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main() == 0

    formula = out.getvalue()
    assert 'version "0.4.0"' in formula
    assert "v0.4.0" in formula


def test_render_blocks_are_balanced(tmp_path: pathlib.Path) -> None:
    formula = render("0.4.0", _assets(tmp_path))

    opens, _ = _line_counts(formula)
    assert opens == 0


def test_render_covers_all_platforms(tmp_path: pathlib.Path) -> None:
    formula = render("0.4.0", _assets(tmp_path))

    assert "on_macos do" in formula
    assert "on_linux do" in formula
    assert "Hardware::CPU.arm?" in formula
    assert "Hardware::CPU.intel?" in formula
