"""Simple, opt-in alerting for the bot.

The runbook (``docs/runbook.md``) calls out "push alerting — known gap".
This module fills that gap with the smallest possible footprint:

* :class:`AlertSink` protocol — anything with ``emit(Alert)``,
* :class:`FileAlertSink` — append-only JSONL to a file (always on, free),
* :class:`WebhookAlertSink` — POST to a URL (Slack/Discord/Teams),
* :class:`AlertManager` — fan-out + de-duplication + rate-limiting.

Default-off: if no sink is registered, ``emit()`` is a no-op.  The tick
loop may call ``alerts.notify(...)`` unconditionally without adding
latency when alerting isn't configured.  The manager *never* raises; a
broken webhook is logged and swallowed so it cannot crash the bot.

De-duplication: identical ``(severity, subject)`` pairs within
``dedupe_window_s`` seconds collapse to a single alert.  Rate-limit:
at most ``max_per_minute`` alerts go to external sinks; file sink
never drops anything (disk is cheap and audit trails are precious).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Alert:
    """A single alert payload.

    ``severity`` uses log-style levels (``info`` / ``warning`` /
    ``critical``).  ``subject`` is the short one-liner the operator sees
    first; ``detail`` carries the full structured context.  Keep
    ``detail`` JSON-serialisable.
    """

    severity: str
    subject: str
    detail: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps({
            "severity": self.severity,
            "subject": self.subject,
            "detail": self.detail,
            "timestamp": self.timestamp,
        })


# --------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------

class AlertSink(Protocol):
    def emit(self, alert: Alert) -> None: ...


class FileAlertSink:
    """Append one JSON object per line to ``path``.

    Idempotent: the file is opened in append mode and closed after each
    write, so concurrent bot instances sharing the same path do not
    stomp each other's lines.  (Polymarket's single-bot-per-key model
    makes concurrency unlikely, but it's free insurance.)
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def emit(self, alert: Alert) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(alert.to_json() + "\n")
        except OSError:
            logger.exception("FileAlertSink: failed to write to %s", self.path)


_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def _meets_severity(level: str, threshold: str) -> bool:
    """True iff ``level`` is at least as severe as ``threshold``."""
    lv = _SEVERITY_ORDER.get((level or "").lower(), 0)
    th = _SEVERITY_ORDER.get((threshold or "info").lower(), 0)
    return lv >= th


class WebhookAlertSink:
    """POST ``{"text": subject, "attachments": [...]}`` to a webhook.

    Shape is Slack-compatible and Discord-compatible (Discord accepts
    ``content`` field too; we include both for convenience).  The
    request library is imported lazily so the bot has no hard dep on
    ``requests`` if the webhook isn't used.

    ``min_severity`` gates delivery so an always-on-info-level bot
    doesn't spam Slack with every tick-level notice.  Defaults to
    ``"info"`` (no filtering) for back-compat.
    """

    def __init__(
        self,
        url: str,
        timeout_s: float = 3.0,
        min_severity: str = "info",
    ) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self.min_severity = min_severity

    def emit(self, alert: Alert) -> None:
        if not _meets_severity(alert.severity, self.min_severity):
            return
        try:
            import requests  # lazy — only needed if the sink is active
        except ImportError:
            logger.warning(
                "WebhookAlertSink: `requests` not installed; dropping alert."
            )
            return

        prefix = {"critical": "*CRITICAL*", "warning": "*WARN*",
                  "info": "*INFO*"}.get(alert.severity, alert.severity.upper())
        text = f"{prefix} — {alert.subject}"
        payload = {
            "text": text,
            "content": text,  # Discord-compatible
            "attachments": [{
                "color": {"critical": "#d00000", "warning": "#ffbb33"}.get(
                    alert.severity, "#3080c0"),
                "text": json.dumps(alert.detail, indent=2)[:1800],
                "ts": int(alert.timestamp),
            }],
        }
        try:
            resp = requests.post(self.url, json=payload, timeout=self.timeout_s)
            if resp.status_code >= 400:
                logger.warning(
                    "WebhookAlertSink: %s responded %s",
                    self.url, resp.status_code,
                )
        except Exception:
            # Network/DNS/timeout — never propagate.
            logger.exception("WebhookAlertSink: POST failed — dropping alert.")


# --------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------

class AlertManager:
    """Fan-out dispatcher with de-dupe + rate-limit.

    The manager is cheap to construct empty; default bot wiring should
    always instantiate one so tick code can call ``notify`` blindly.
    """

    def __init__(
        self,
        sinks: Iterable[AlertSink] | None = None,
        dedupe_window_s: float = 300.0,
        max_per_minute: int = 20,
    ) -> None:
        self.sinks: list[AlertSink] = list(sinks or [])
        self.dedupe_window_s = dedupe_window_s
        self.max_per_minute = max_per_minute
        # (severity, subject) -> last-emit monotonic timestamp
        self._recent: dict[tuple[str, str], float] = {}
        # rolling second-granularity bucket list for rate limiting
        self._minute_bucket: list[float] = []

    # -------- registration -------------------------------------------------

    def add(self, sink: AlertSink) -> None:
        self.sinks.append(sink)

    def reset(self) -> None:
        self._recent.clear()
        self._minute_bucket.clear()

    # -------- dispatch -----------------------------------------------------

    def notify(
        self, severity: str, subject: str, detail: dict | None = None,
    ) -> bool:
        """Send an alert.  Returns True if it reached at least one sink."""
        if not self.sinks:
            return False
        now = time.time()
        key = (severity, subject)
        # De-dupe
        last = self._recent.get(key)
        if last is not None and (now - last) < self.dedupe_window_s:
            return False
        # Rate limit (sliding 60-second window)
        self._minute_bucket = [t for t in self._minute_bucket if now - t < 60.0]
        if len(self._minute_bucket) >= self.max_per_minute:
            logger.warning("AlertManager: rate limit hit — dropping %s", subject)
            return False

        alert = Alert(
            severity=severity, subject=subject,
            detail=dict(detail or {}), timestamp=now,
        )
        any_emitted = False
        for sink in self.sinks:
            try:
                sink.emit(alert)
                any_emitted = True
            except Exception:
                logger.exception("AlertManager: sink %s raised", type(sink).__name__)
        if any_emitted:
            self._recent[key] = now
            self._minute_bucket.append(now)
        return any_emitted

    # Convenience shortcuts ---------------------------------------------------

    def info(self, subject: str, **detail) -> bool:
        return self.notify("info", subject, detail)

    def warn(self, subject: str, **detail) -> bool:
        return self.notify("warning", subject, detail)

    def critical(self, subject: str, **detail) -> bool:
        return self.notify("critical", subject, detail)


# --------------------------------------------------------------------------
# Factory for the bot
# --------------------------------------------------------------------------

def build_from_config(cfg) -> AlertManager:
    """Construct an :class:`AlertManager` from a :class:`Config`-like object.

    Reads:

    * ``alert_log_file`` (str, "" disables) — JSONL file sink,
    * ``alert_webhook_url`` (str, "" disables) — Slack/Discord webhook,
    * ``alert_dedupe_seconds`` (float, default 300.0),
    * ``alert_max_per_minute`` (int, default 20).

    Missing attributes are tolerated so this helper never breaks older
    Config objects.
    """
    dedupe = float(getattr(cfg, "alert_dedupe_seconds", 300.0))
    cap = int(getattr(cfg, "alert_max_per_minute", 20))
    mgr = AlertManager(dedupe_window_s=dedupe, max_per_minute=cap)

    log_path = getattr(cfg, "alert_log_file", "")
    if log_path:
        mgr.add(FileAlertSink(log_path))

    url = getattr(cfg, "alert_webhook_url", "")
    if url:
        min_sev = getattr(cfg, "alert_webhook_min_severity", "info")
        mgr.add(WebhookAlertSink(url, min_severity=min_sev))

    return mgr
