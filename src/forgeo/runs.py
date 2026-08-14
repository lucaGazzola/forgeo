"""Durable run history: one JSON line per finished cycle in ``runs.jsonl``.

Forgeo, the CLI, and the web API all read the same records; where the file
lives is decided by :func:`forgeo.paths.runs_path`. Reading tolerates a
missing file and skips corrupt lines with a warning, so a broken
``runs.jsonl`` never breaks a cycle or the API.

The file is trimmed to ``run_history_keep`` records (default 2000) when
appending, so a busy Forgeo never accumulates run history forever. Trimming
is atomic (temp file + rename) and its failures are logged and skipped, so it
never breaks a cycle; ``keep=0`` disables retention entirely and the file
grows without bound, exactly as before.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import ValidationError

from forgeo.io import atomic_write_text
from forgeo.models import RunRecord

logger = logging.getLogger(__name__)

#: Default number of finished runs kept in ``runs.jsonl``; overridden by the
#: ``run_history_keep`` config key. ``0`` disables retention.
DEFAULT_RUN_HISTORY_KEEP = 2000


class RunRecorder:
    """Appends :class:`RunRecord` rows to a JSON-lines file and reads them back.

    When ``keep`` is positive, the file is trimmed to at most ``keep`` lines
    (oldest first) as part of the append, so the file never grows past the
    retention limit. Trimming is atomic: readers either see the old file or
    the fully trimmed one. ``keep=0`` disables retention entirely.
    """

    def __init__(self, path: str | Path, *, keep: int = DEFAULT_RUN_HISTORY_KEEP) -> None:
        self.path = Path(path)
        self.keep = keep

    def append(self, record: RunRecord) -> None:
        """Append ``record`` as one JSON line.

        Old lines are trimmed first when the file is at ``keep``, so the file
        never holds more than ``keep`` records. A write or trim failure is
        logged and never raised, so recording can never break a Forgeo cycle.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.keep > 0 and self._append_trimmed(record):
                return
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(record.model_dump_json() + "\n")
        except OSError as exc:
            logger.error("Could not write run record to %s: %s", self.path, exc)

    def _append_trimmed(self, record: RunRecord) -> bool:
        """Atomically trim the history to ``keep`` lines and append ``record``.

        Returns ``True`` when the trimmed write happened; ``False`` when it
        was skipped because there is nothing to trim yet (or it failed and
        was logged), leaving the caller to fall back to a plain append.
        """
        if not self.path.exists():
            return False
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) < self.keep:
                return False
            room = self.keep - 1
            lines = lines[-room:] if room > 0 else []
            lines.append(record.model_dump_json())
            atomic_write_text(self.path, "\n".join(lines) + "\n")
            return True
        except OSError as exc:
            logger.error(
                "Could not trim run history in %s: %s; appending without trimming",
                self.path,
                exc,
            )
            return False

    def read(self, limit: int | None = None, offset: int = 0) -> list[RunRecord]:
        """Return the newest records, newest first, skipping the first ``offset``.

        A missing file yields an empty list; corrupt lines are skipped with a
        warning. ``limit=None`` returns every readable record after ``offset``
        (``offset=0`` starts at the newest).
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
        if offset > 0:
            records = records[offset:]
        if limit is None:
            return records
        return records[: max(0, limit)]

    def total(self) -> int:
        """The number of readable records in the run history, or ``0``.

        A missing, empty, or corrupt-only file counts as zero; corrupt lines
        are skipped with a warning, matching :meth:`read`.
        """
        return len(self.read())

    def read_last(self) -> RunRecord | None:
        """Return the most recent record, or ``None`` when none exists."""
        records = self.read(limit=1)
        return records[0] if records else None
