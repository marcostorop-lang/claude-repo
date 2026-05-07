"""Systemic-failure monitor that fans out into AlertManager.

The existing alerts module covers *trading* events (daily-loss breach,
reconciliation drift, latency).  This complements it with *system*
events that tend to silently degrade a long-running bot:

* API error-rate spike (rolling window),
* SQLite write failure / integrity breach,
* Heartbeat watchdog (no tick within N seconds),
* Auth-refresh failure (CLOB / wallet).

All checks are idempotent and side-effect-free except for invoking
``AlertManager.notify``.  Designed so the bot loop can call them
unconditionally; when no manager / no sinks are configured the calls
are O(1) no-ops.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from src.utils.alerts import AlertManager


@dataclass
class SystemMonitor:
    """Aggregate systemic-health signals and forward to AlertManager."""

    manager: AlertManager
    # API error tracking: rolling deque of (ts, was_error) pairs.
    api_window_seconds: float = 300.0
    api_min_samples: int = 20
    api_error_rate_threshold: float = 0.20  # 20% of recent calls failed → alert
    # Heartbeat: maximum allowable seconds between successive ticks.
    heartbeat_max_silence_s: float = 600.0  # 10 minutes
    _api_events: deque = field(default_factory=deque)
    _last_heartbeat: float = 0.0

    # ---- API health ------------------------------------------------------

    def record_api_call(self, was_error: bool) -> None:
        now = time.time()
        self._api_events.append((now, was_error))
        cutoff = now - self.api_window_seconds
        while self._api_events and self._api_events[0][0] < cutoff:
            self._api_events.popleft()
        n = len(self._api_events)
        if n < self.api_min_samples:
            return
        errors = sum(1 for _, e in self._api_events if e)
        rate = errors / n
        if rate >= self.api_error_rate_threshold:
            self.manager.warn(
                "API error-rate elevated",
                rate=round(rate, 3),
                window_seconds=self.api_window_seconds,
                samples=n,
                errors=errors,
            )

    # ---- DB integrity ----------------------------------------------------

    def record_db_error(self, op: str, error: str) -> None:
        """Critical: DB writes must not silently fail.

        We escalate to ``critical`` because every write loss corrupts
        accounting (positions, fees, decision_log).  Operators should
        see this without delay.
        """
        self.manager.critical(
            "SQLite write failure",
            op=op,
            error=error[:300],  # truncate long tracebacks
        )

    # ---- Auth ------------------------------------------------------------

    def record_auth_failure(self, kind: str, error: str) -> None:
        """CLOB or wallet auth failed.

        For paper mode this is mostly informational; for live it blocks
        order submission entirely so we treat it as critical.
        """
        self.manager.critical(
            f"Auth failure ({kind})",
            kind=kind,
            error=error[:300],
        )

    # ---- Heartbeat -------------------------------------------------------

    def heartbeat(self) -> None:
        """Mark a successful tick boundary."""
        self._last_heartbeat = time.time()

    def check_heartbeat(self) -> None:
        """Fire an alert if no tick has been seen recently.

        Called by an external watchdog (e.g. the daily-summary job, a
        cron wrapper, or a pure-Python ``Timer``).  We don't run our
        own thread because the bot's threading model is single-tick.
        """
        if self._last_heartbeat == 0.0:
            return
        silence = time.time() - self._last_heartbeat
        if silence >= self.heartbeat_max_silence_s:
            self.manager.critical(
                "Bot heartbeat missing",
                silence_seconds=int(silence),
                max_silence_seconds=int(self.heartbeat_max_silence_s),
            )
