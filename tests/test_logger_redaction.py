"""Credential-redaction tests for the root logger.

A leak here would write a wallet private key to ``bot.log`` (read by
the dashboard, copied by ops, sometimes shipped to a log aggregator).
The filter is the last line of defence — these tests pin its
behaviour against the most plausible accidents:

* Operator types ``logger.debug("Config: %s", cfg)`` — every secret
  field of the dataclass would otherwise surface via ``repr``.
* Operator interpolates a private key into a message string.
* Operator logs a dict that *contains* a secret (e.g. order_args).
"""

from __future__ import annotations

import io
import logging
import os
from unittest import mock

from src.logger import _redact_str, _redact_value, setup_logging


def test_redact_str_masks_named_keys():
    s = "private_key=0xabcd1234567890, ok=yes, api_secret='topsecret'"
    out = _redact_str(s)
    assert "topsecret" not in out
    assert "0xabcd1234567890" not in out
    assert "REDACTED" in out


def test_redact_str_masks_bare_hex_key():
    hex_key = "0x" + "a" * 64
    out = _redact_str(f"signing with {hex_key} now")
    assert hex_key not in out
    assert "REDACTED" in out


def test_redact_str_keeps_short_hex_addresses():
    """40-char hex (Ethereum address) IS still masked — that's a feature.

    Both wallet addresses and private keys are sensitive in logs.  We err
    on the side of masking; the test pins the policy.
    """
    addr = "0x" + "f" * 40
    out = _redact_str(f"to {addr} sent 1 USDC")
    assert addr not in out


def test_redact_value_dict_with_secret_key():
    payload = {"private_key": "0xdeadbeef", "amount": 10}
    out = _redact_value(payload)
    assert out["private_key"] == "***REDACTED***"
    assert out["amount"] == 10


def test_redact_value_nested():
    payload = {"creds": {"api_secret": "abc"}, "ok": True}
    out = _redact_value(payload)
    # nested dict's KEY 'api_secret' is masked because we recurse;
    # the top-level key 'creds' is not in the secret list.
    assert out["creds"]["api_secret"] == "***REDACTED***"
    assert out["ok"] is True


def test_filter_redacts_logger_call(tmp_path):
    log_path = tmp_path / "bot.log"
    setup_logging(level="DEBUG", log_file=str(log_path), max_bytes=0)
    log = logging.getLogger("test_redaction")
    pk = "0x" + "9" * 64
    log.info("Config: private_key=%s", pk)
    log.info("Loaded credentials %s", {"api_secret": "topsecret"})
    # Flush
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_path.read_text(encoding="utf-8")
    assert pk not in text
    assert "topsecret" not in text
    # Tear down handlers so other tests aren't affected.
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_claude_managed", False):
            root.removeHandler(h)


def test_filter_idempotent_repeat_setup(tmp_path):
    """Calling setup_logging twice must not stack handlers (or filters)."""
    log_path = tmp_path / "bot.log"
    setup_logging(level="INFO", log_file=str(log_path), max_bytes=0)
    setup_logging(level="INFO", log_file=str(log_path), max_bytes=0)
    managed = [
        h for h in logging.getLogger().handlers
        if getattr(h, "_claude_managed", False)
    ]
    # Exactly one console + one file handler after two calls.
    assert len(managed) == 2
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_claude_managed", False):
            root.removeHandler(h)


def test_filter_does_not_break_normal_messages(tmp_path):
    log_path = tmp_path / "bot.log"
    setup_logging(level="DEBUG", log_file=str(log_path), max_bytes=0)
    log = logging.getLogger("test_redaction_innocent")
    log.info("Tick complete: markets=%d, signals=%d", 12, 3)
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_path.read_text(encoding="utf-8")
    assert "markets=12" in text
    assert "signals=3" in text
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_claude_managed", False):
            root.removeHandler(h)
