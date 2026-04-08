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
        raw_markets = self.client.get_active_markets_limited(self.cfg.max_markets_fetch)
        logger.info("Fetched %d raw markets from Gamma API (limit=%d).", len(raw_markets), self.cfg.max_markets_fetch)

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
        spread_rejected = 0
        for snap in filtered[: self.cfg.max_markets]:
            snap.price = self.client.get_price(snap.token_id)
            snap.spread = self.client.get_spread(snap.token_id)
            if not snap.is_valid:
                continue
            if not self._passes_spread_filter(snap):
                spread_rejected += 1
                logger.debug("Spread filter rejected %s (spread=%.4f > max=%.4f)", snap.token_id[:12], snap.spread or 0, self.cfg.max_spread)
                continue
            enriched.append(snap)

        if spread_rejected:
            logger.info("Spread filter rejected %d markets (max_spread=%.4f).", spread_rejected, self.cfg.max_spread)
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

    def _passes_spread_filter(self, snap: MarketSnapshot) -> bool:
        """Check spread after enrichment. Returns True if spread is acceptable."""
        if snap.spread is None:
            return True  # No spread data — allow (conservative: logged separately)
        return snap.spread <= self.cfg.max_spread
