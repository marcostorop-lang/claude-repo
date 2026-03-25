"""
Market data retrieval and filtering.

This module is responsible for downloading markets from the Gamma API,
extracting relevant tokens, and applying configurable filters (volume,
liquidity, spread, etc.).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.config import Config
from src.polymarket.client import PolymarketClient

logger = logging.getLogger(__name__)


@dataclass
class MarketSnapshot:
    """Lightweight view of a single market / token suitable for strategies."""

    condition_id: str
    question: str
    token_id: str
    outcome: str
    price: float | None
    spread: float | None
    volume: float
    liquidity: float
    active: bool

    @property
    def is_valid(self) -> bool:
        return self.price is not None and self.active


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class MarketDataService:
    """Fetches, filters and enriches market data."""

    def __init__(self, client: PolymarketClient, cfg: Config) -> None:
        self.client = client
        self.cfg = cfg

    def fetch_and_filter(self) -> list[MarketSnapshot]:
        """Return a filtered list of active market snapshots."""
        raw_markets = self.client.get_all_active_markets()
        logger.info("Fetched %d raw markets from Gamma API.", len(raw_markets))

        snapshots: list[MarketSnapshot] = []
        for mkt in raw_markets:
            # Each market can have multiple tokens (outcomes)
            tokens = mkt.get("clobTokenIds") or mkt.get("clob_token_ids") or []
            outcomes = mkt.get("outcomes") or []
            if isinstance(outcomes, str):
                # Gamma sometimes returns a JSON string
                import json
                try:
                    outcomes = json.loads(outcomes)
                except Exception:
                    outcomes = []
            if isinstance(tokens, str):
                import json
                try:
                    tokens = json.loads(tokens)
                except Exception:
                    tokens = []

            volume = _safe_float(mkt.get("volume") or mkt.get("volumeNum"))
            liquidity = _safe_float(mkt.get("liquidity") or mkt.get("liquidityNum"))

            for i, token_id in enumerate(tokens):
                outcome_label = outcomes[i] if i < len(outcomes) else f"outcome_{i}"
                snap = MarketSnapshot(
                    condition_id=mkt.get("conditionId") or mkt.get("condition_id", ""),
                    question=mkt.get("question", ""),
                    token_id=token_id,
                    outcome=outcome_label,
                    price=None,
                    spread=None,
                    volume=volume,
                    liquidity=liquidity,
                    active=bool(mkt.get("active", True)),
                )
                snapshots.append(snap)

        # Apply filters
        filtered = [s for s in snapshots if self._passes_filters(s)]
        logger.info("After filtering: %d snapshots from %d candidates.", len(filtered), len(snapshots))

        # Enrich with price/spread (limited to max_markets)
        enriched: list[MarketSnapshot] = []
        for snap in filtered[: self.cfg.max_markets]:
            snap.price = self.client.get_price(snap.token_id)
            snap.spread = self.client.get_spread(snap.token_id)
            if snap.is_valid:
                enriched.append(snap)

        logger.info("Enriched %d valid market snapshots.", len(enriched))
        return enriched

    def _passes_filters(self, snap: MarketSnapshot) -> bool:
        if not snap.active:
            return False
        if snap.volume < self.cfg.min_volume:
            return False
        if snap.liquidity < self.cfg.min_liquidity:
            return False
        return True
