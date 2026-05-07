"""
Logging configuration.

Call ``setup_logging()`` once at startup. All modules should use:

    import logging
    logger = logging.getLogger(__name__)

The file handler rotates by size so a long-running bot never fills the
disk silently — the runbook lists "rotate bot.log" as a known gap, and
unbounded log growth has masked real crashes in the past.

A redaction filter is attached to every handler we manage so accidental
``logger.debug("Config: %s", cfg)`` calls cannot exfiltrate secrets to
disk or to stdout.  The filter is idempotent and runs in ``log.filter``
(before formatting), which means it sees the message *and* every arg.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys


# Names that, when found inside a logged value, should be masked.
# Matched case-insensitively against keys in mappings AND against
# ``key=value`` / ``key: value`` patterns in formatted strings.
_SECRET_KEYS = (
    "private_key",
    "api_secret",
    "passphrase",
    "secret_key",
    "auth_token",
    "bearer",
)
_SECRET_KEY_RE = re.compile(
    r"(?i)\b(" + "|".join(_SECRET_KEYS) + r")\b\s*[=:]\s*['\"]?([^\s'\",}\)]+)['\"]?"
)
# Bare 0x-hex keys (private keys) — 64 hex chars after 0x is the
# canonical 32-byte signing key length.  Match a few sensible lengths
# to be robust to truncation in logs.
_HEX_KEY_RE = re.compile(r"0x[0-9a-fA-F]{40,}")
_REDACTED = "***REDACTED***"


def _redact_str(s: str) -> str:
    if not s:
        return s
    s = _SECRET_KEY_RE.sub(lambda m: f"{m.group(1)}={_REDACTED}", s)
    s = _HEX_KEY_RE.sub(_REDACTED, s)
    return s


def _redact_value(v):
    """Recursively redact secrets inside common container types.

    We avoid touching unfamiliar objects (e.g. SDK clients) to stay
    cheap and side-effect-free.  The string-level regex is the fallback
    that catches anything formatted by ``__repr__`` that happens to
    expose ``private_key=...``.
    """
    if isinstance(v, str):
        return _redact_str(v)
    if isinstance(v, dict):
        return {
            k: (_REDACTED if str(k).lower() in _SECRET_KEYS else _redact_value(val))
            for k, val in v.items()
        }
    if isinstance(v, (list, tuple)):
        red = [_redact_value(x) for x in v]
        return type(v)(red) if isinstance(v, tuple) else red
    return v


class _RedactionFilter(logging.Filter):
    """Strip credentials from log records before formatting.

    Operates on both ``record.msg`` and ``record.args`` so structured
    ``logger.info("key=%s", secret)`` calls get caught alongside
    pre-formatted ``logger.info("key=%s" % secret)`` ones.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = _redact_str(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = _redact_value(record.args)
                elif isinstance(record.args, tuple):
                    record.args = tuple(_redact_value(a) for a in record.args)
        except Exception:  # never let the filter itself break logging
            pass
        return True


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Configure root logger with console and optional *rotating* file handler.

    Parameters
    ----------
    level
        Log level name (``"INFO"``, ``"DEBUG"``, ...).
    log_file
        Path to the log file.  ``None`` or empty disables the file handler.
    max_bytes
        Rotate once the active file grows past this.  Default 10 MB.
        Set to 0 to disable rotation (old behaviour — not recommended).
    backup_count
        How many rotated files to keep alongside the active one.
        Default 5 → at most ``(backup_count + 1) * max_bytes`` of disk
        used by logs.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Idempotency: repeated calls (e.g. in tests) must not stack handlers.
    # We replace only the handlers this function owns — leave others alone.
    for h in list(root.handlers):
        if getattr(h, "_claude_managed", False):
            root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    redaction = _RedactionFilter()
    # Trace IDs are attached to every record so a downstream formatter
    # can include them; the default formatter does not (keeps lines
    # short for the common case).  See ``src/utils/trace.py``.
    from src.utils.trace import TraceContextFilter
    trace = TraceContextFilter()

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(redaction)
    console.addFilter(trace)
    console._claude_managed = True  # type: ignore[attr-defined]
    root.addHandler(console)

    # File handler — rotating unless ``max_bytes == 0`` (legacy mode).
    if log_file:
        if max_bytes > 0:
            fh: logging.Handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=int(max_bytes),
                backupCount=int(backup_count),
                encoding="utf-8",
            )
        else:
            fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(redaction)
        fh.addFilter(trace)
        fh._claude_managed = True  # type: ignore[attr-defined]
        root.addHandler(fh)

