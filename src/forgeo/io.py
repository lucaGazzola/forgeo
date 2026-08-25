"""Small filesystem helpers shared across Forgeo modules."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: str | Path, content: str) -> None:
    """Atomically write ``content`` to ``path`` (temp file + rename).

    A crash mid-write never leaves a partial file at ``path``: the content is
    first written to a temporary file in the same directory and then moved
    over ``path`` with ``os.replace``. The parent directory is created when
    missing. The temporary file is cleaned up if the write fails.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
