"""Lightweight error monitoring — track, aggregate, and alert on errors.

No external dependencies (no Sentry). Errors are counted per category,
persisted to a JSONL file, and surfaced via the healthcheck endpoint.
Supports configurable alert thresholds to trigger notifications.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_ERROR_LOG = Path(__file__).resolve().parent.parent / "logs" / "errors.jsonl"
_LOCK = threading.Lock()


@dataclass
class ErrorRecord:
    timestamp: float
    category: str
    message: str
    traceback_str: str = ""
    context: dict = field(default_factory=dict)


class ErrorMonitor:
    """Track and aggregate errors for production monitoring."""

    def __init__(
        self,
        alert_threshold: int = 10,
        alert_window_s: float = 300.0,
    ) -> None:
        self._alert_threshold = alert_threshold
        self._alert_window_s = alert_window_s
        self._counts: dict[str, int] = defaultdict(int)
        self._recent: list[ErrorRecord] = []
        self._max_recent = 100
        self._total_errors = 0
        self._alert_callbacks: list = []
        self._start_time = time.time()

    def record(
        self,
        category: str,
        message: str,
        exc: BaseException | None = None,
        context: dict | None = None,
    ) -> None:
        """Record an error occurrence."""
        tb_str = ""
        if exc is not None:
            tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

        record = ErrorRecord(
            timestamp=time.time(),
            category=category,
            message=message[:500],
            traceback_str=tb_str[:2000],
            context=context or {},
        )

        with _LOCK:
            self._counts[category] += 1
            self._total_errors += 1
            self._recent.append(record)
            if len(self._recent) > self._max_recent:
                self._recent = self._recent[-self._max_recent:]

        self._persist(record)
        self._check_alert(category)

    def _persist(self, record: ErrorRecord) -> None:
        try:
            _ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(_ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": record.timestamp,
                    "category": record.category,
                    "message": record.message,
                    "context": record.context,
                }, default=str) + "\n")
        except Exception:
            pass

    def _check_alert(self, category: str) -> None:
        now = time.time()
        cutoff = now - self._alert_window_s
        with _LOCK:
            recent_count = sum(
                1 for r in self._recent
                if r.category == category and r.timestamp >= cutoff
            )
        if recent_count >= self._alert_threshold:
            msg = (
                f"ERROR ALERT: {category} had {recent_count} errors "
                f"in the last {self._alert_window_s:.0f}s"
            )
            logger.critical(msg)
            for cb in self._alert_callbacks:
                try:
                    cb(category, recent_count, msg)
                except Exception:
                    pass

    def on_alert(self, callback) -> None:
        """Register a callback for error alerts: fn(category, count, message)."""
        self._alert_callbacks.append(callback)

    def summary(self) -> dict:
        """Return error stats for healthcheck/dashboard."""
        with _LOCK:
            now = time.time()
            recent_5m = sum(
                1 for r in self._recent
                if r.timestamp >= now - 300
            )
            categories = dict(self._counts)
            last_errors = [
                {
                    "ts": r.timestamp,
                    "category": r.category,
                    "message": r.message[:100],
                }
                for r in self._recent[-5:]
            ]
        uptime_s = now - self._start_time
        return {
            "total_errors": self._total_errors,
            "errors_last_5m": recent_5m,
            "error_rate_per_hour": round(self._total_errors / (uptime_s / 3600), 2) if uptime_s > 0 else 0,
            "categories": categories,
            "last_errors": last_errors,
            "uptime_s": round(uptime_s, 0),
        }

    def clear(self) -> None:
        with _LOCK:
            self._counts.clear()
            self._recent.clear()
            self._total_errors = 0


# Global singleton
monitor = ErrorMonitor()
