"""Unit tests for the pure helpers in :mod:`forgeo.web_common`.

The dashboard exercises these helpers indirectly through its HTTP endpoints;
these tests pin down the edge cases directly (boundaries, missing files,
traversal guards).
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from forgeo.web_common import (
    clamp_query_int,
    guess_content_type,
    iso,
    json_bytes,
    safe_static_path,
    tail_lines,
)
from tests.conftest import requires_posix


def test_json_bytes_pretty_with_newline() -> None:
    body = json_bytes({"a": 1})
    assert body == b'{\n  "a": 1\n}\n'


def test_json_bytes_uses_str_fallback() -> None:
    body = json_bytes({"when": datetime(2026, 8, 1, tzinfo=UTC)})
    assert b"2026-08-01 00:00:00+00:00" in body


def test_iso_naive_datetime_appends_z() -> None:
    value = datetime(2026, 8, 1, 1, 2, 3)  # noqa: DTZ001 (intentional naive input)
    assert iso(value) == "2026-08-01T01:02:03Z"


def test_iso_aware_datetime_keeps_offset() -> None:
    value = datetime(2026, 8, 1, 1, 2, 3, tzinfo=UTC)
    assert iso(value) == "2026-08-01T01:02:03+00:00"


def test_tail_lines_last_n(tmp_path: Path) -> None:
    path = tmp_path / "log.txt"
    path.write_text("\n".join(f"line {i}" for i in range(5)) + "\n", encoding="utf-8")
    assert tail_lines(path, 2) == ["line 3", "line 4"]


def test_tail_lines_all_when_requested_more(tmp_path: Path) -> None:
    path = tmp_path / "log.txt"
    path.write_text("a\nb\n", encoding="utf-8")
    assert tail_lines(path, 100) == ["a", "b"]


def test_tail_lines_missing_file(tmp_path: Path) -> None:
    assert tail_lines(tmp_path / "missing.log", 10) == []


def test_tail_lines_zero_or_negative(tmp_path: Path) -> None:
    path = tmp_path / "log.txt"
    path.write_text("a\nb\n", encoding="utf-8")
    assert tail_lines(path, 0) == []
    assert tail_lines(path, -3) == []


@requires_posix
def test_tail_lines_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "log.txt"
    path.write_text("a\nb\n", encoding="utf-8")
    os.chmod(path, 0)
    try:
        assert tail_lines(path, 10) == []
    finally:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def test_clamp_query_int_parses_and_bounds() -> None:
    assert clamp_query_int({"n": ["7"]}, "n", 10, 100) == 7
    assert clamp_query_int({"n": ["999"]}, "n", 10, 100) == 100
    assert clamp_query_int({"n": ["-5"]}, "n", 10, 100) == 0


def test_clamp_query_int_invalid_falls_back() -> None:
    assert clamp_query_int({"n": ["abc"]}, "n", 10, 100) == 10


def test_clamp_query_int_missing_uses_default() -> None:
    assert clamp_query_int({}, "n", 10, 100) == 10


def test_safe_static_path_root(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("hi", encoding="utf-8")
    assert safe_static_path("/", tmp_path) == (tmp_path / "index.html")
    assert safe_static_path("", tmp_path) == (tmp_path / "index.html")


def test_safe_static_path_subpath(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    assert safe_static_path("/a.txt", tmp_path) == (tmp_path / "a.txt")


def test_safe_static_path_trailing_slash_serves_index(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "index.html").write_text("hi", encoding="utf-8")
    assert safe_static_path("/sub/", tmp_path) == (sub / "index.html")


def test_safe_static_path_missing_file_returns_none(tmp_path: Path) -> None:
    assert safe_static_path("/nope.txt", tmp_path) is None


def test_safe_static_path_traversal_returns_none(tmp_path: Path) -> None:
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    assert safe_static_path("/../secret.txt", tmp_path) is None


def test_safe_static_path_escaped_url_returns_none(tmp_path: Path) -> None:
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    assert safe_static_path("/%2e%2e/secret.txt", tmp_path) is None


def test_safe_static_path_non_dir_root(tmp_path: Path) -> None:
    file_root = tmp_path / "file.txt"
    file_root.write_text("x", encoding="utf-8")
    assert safe_static_path("/", file_root) is None


def test_guess_content_type_falls_back(tmp_path: Path) -> None:
    assert guess_content_type(Path("x.css")) == "text/css"
    assert guess_content_type(Path("x.unknownext")) == "application/octet-stream"
