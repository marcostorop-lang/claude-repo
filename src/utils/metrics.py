"""Structured JSON metrics emission.

The SQLite ``tick_stats`` table is already the authoritative store for
in-app analytics — the dashboard reads it directly.  This module
complements that by writing the same numbers as one JSON object per
line to a file so external tooling (Loki, Promtail, Vector, jq,
``tail -f | grep``) can consume them without opening a DB connection.

Default-off: missing ``metrics_file`` config → emit is a no-op.  We
never raise out of ``emit()``: a broken filesystem must not break the
tick loop.

Idempotent shape — every line is one flat dict with:

* ``ts`` — ISO timestamp (caller-supplied or ``time.time()``),
* ``event`` — short string (e.g. ``"tick"``, ``"trade"``, ``"alert"``),
* plus arbitrary top-level fields for the metric itself.

Nested objects are allowed but discouraged — flat payloads aggregate
more easily in log pipelines.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class MetricsWriter:
    """Append-only JSONL metrics emitter.

    The writer is a cheap, stateless handle.  Create once per bot run
    (or per process); call :meth:`emit` from anywhere.  The helper is
    intentionally *not* a singleton — tests want fresh instances and
    the live bot only needs one.

    Parameters
    ----------
    path
        Destination file.  Opened in append mode per call, closed after
        each write, so the file can be rotated out from under the bot
        without breaking writes (the next emit recreates it).  An empty
        string disables the writer entirely.
    clock
        Optional injectable clock returning a float seconds epoch.
        Tests override for determinism.
    """

    def __init__(self, path: str = "", clock=time.time) -> None:
        self.path: Path | None = Path(path) if path else None
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def emit(self, event: str, **fields: Any) -> None:
        """Write one JSON record.  Never raises."""
        if self.path is None:
            return
        record: dict[str, Any] = {"ts": fields.pop("ts", None) or self._clock(),
                                   "event": event}
        record.update(fields)
        try:
            line = json.dumps(record, default=_json_default)
        except (TypeError, ValueError):
            # Fallback: stringify unknowns so we never drop a metric.
            logger.debug("MetricsWriter: non-serialisable fields for %s", event)
            safe = {k: repr(v) for k, v in record.items()}
            line = json.dumps(safe)
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            # Disk full / permissions / rotating symlink races — all
            # recoverable on the next tick; log and move on.
            logger.exception("MetricsWriter: failed to append to %s", self.path)


def _json_default(obj):
    """Graceful fallback for JSON-unfriendly types (Decimal, set, etc.)."""
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    return str(obj)


def build_from_config(cfg) -> MetricsWriter:
    """Construct a :class:`MetricsWriter` from a :class:`Config`-like object.

    Reads ``metrics_file`` — empty / missing disables the writer.
    """
    path = getattr(cfg, "metrics_file", "") or ""
    return MetricsWriter(path)
