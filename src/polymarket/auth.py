"""
Authentication helpers for Polymarket CLOB API.

The official ``py-clob-client`` handles signing internally when given a
private key.  This module centralises the creation of authenticated and
unauthenticated client instances.

For **live trading** the following env vars must be set:
    PRIVATE_KEY, POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE

For **paper trading** no credentials are needed — only public endpoints
are used.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import Config

logger = logging.getLogger(__name__)


def build_clob_client(cfg: "Config"):
    """
    Return a ``ClobClient`` instance.

    * Paper mode  -> unauthenticated (read-only)
    * Live mode   -> authenticated (can place orders)

    Returns None if the SDK is not installed or credentials are missing
    for live mode.
    """
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        logger.warning(
            "py-clob-client is not installed. "
            "Install it with: pip install py-clob-client"
        )
        return None

    if cfg.is_live:
        if not cfg.private_key:
            raise ValueError("PRIVATE_KEY is required for live trading.")
        logger.info("Creating authenticated CLOB client (LIVE mode).")
        client = ClobClient(
            cfg.clob_url,
            key=cfg.private_key,
            chain_id=cfg.chain_id,
            creds={
                "apiKey": cfg.api_key,
                "secret": cfg.api_secret,
                "passphrase": cfg.passphrase,
            } if cfg.api_key else None,
        )
        # Derive API creds if none were provided
        if not cfg.api_key:
            logger.info("No API key provided — deriving API credentials…")
            try:
                client.set_api_creds(client.create_or_derive_api_creds())
            except Exception:
                logger.exception("Failed to derive API credentials.")
                raise
        return client

    # Paper / read-only mode — no key needed
    logger.info("Creating unauthenticated CLOB client (PAPER mode).")
    return ClobClient(cfg.clob_url, chain_id=cfg.chain_id)
