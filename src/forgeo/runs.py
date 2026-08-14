"""Durable run history: one JSON line per finished cycle in ``runs.jsonl``.

Forgeo, the CLI, and the web API all read the same records; where the file
lives is decided by :func:`forgeo.paths.runs_path`. Reading tolerates a
missing file and skips corrupt lines with a warning, so a broken
``runs.jsonl`` never breaks a cycle or the API.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import ValidationError

from forgeo.models import RunRecord

logger = logging.getLogger(__name__)


class RunRecorder:
    """Appends :class:`RunRecord` rows to a JSON-lines file and reads them back."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, record: RunRecord) -> None:
        """Append ``record`` as one JSON line.

        A write failure is logged and never raised, so recording can never
        break a Forgeo cycle.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(record.model_dump_json() + "\n")
        except OSError as exc:
            logger.error("Could not write run record to %s: %s", self.path, exc)

    def read(self, limit: int | None = None) -> list[RunRecord]:
        """Return the newest ``limit`` records, newest first.

        A missing file yields an empty list; corrupt lines are skipped with a
        warning. ``limit=None`` returns every readable record.
        """
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        records: list[RunRecord] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append(RunRecord.model_validate_json(line))
            except ValidationError:
                logger.warning(
                    "Skipping corrupt run record in %s on line %s", self.path, line_no
                )
        records.sort(key=lambda record: record.finished_at, reverse=True)
        if limit is None:
            return records
        return records[: max(0, limit)]

    def read_last(self) -> RunRecord | None:
        """Return the most recent record, or ``None`` when none exists."""
        records = self.read(limit=1)
        return records[0] if records else None
