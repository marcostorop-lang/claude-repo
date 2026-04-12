"""Tests for the market fundamentals analysis module."""

from src.analysis.fundamentals import MarketFundamentals, compute_fundamentals, fundamentals_score
from src.storage.sqlite_store import SQLiteStore


def _store_with_prices(token: str, prices: list[float], spreads: list[float] | None = None):
    """Create an in-memory store seeded with price history."""
    store = SQLiteStore(":memory:")
    for i, p in enumerate(prices):
        spread = spreads[i] if spreads else 0.0
        store.insert_price(token, p, f"2026-01-01T00:{i:02d}:00", spread=spread)
    return store


class TestFundamentals:
    def test_no_history_returns_zero_scores(self):
        store = SQLiteStore(":memory:")
        f = compute_fundamentals("tok_unknown", store)
        assert f.maturity == 0.0
        assert f.spread_trend == 0.0

    def test_few_points_returns_low_maturity(self):
        store = _store_with_prices("tok", [0.50, 0.51])
        f = compute_fundamentals("tok", store)
        assert f.maturity < 0.5

    def test_many_points_returns_high_maturity(self):
        store = _store_with_prices("tok", [0.50 + i * 0.001 for i in range(25)])
        f = compute_fundamentals("tok", store)
        assert f.maturity >= 0.9

    def test_narrowing_spread_is_positive(self):
        # Older prices had wide spread, recent ones narrow
        spreads = [0.10, 0.10, 0.09, 0.08, 0.07, 0.05, 0.04, 0.03, 0.02, 0.01]
        prices = [0.50] * 10
        store = _store_with_prices("tok", prices, spreads)
        f = compute_fundamentals("tok", store)
        assert f.spread_trend > 0  # narrowing = positive

    def test_widening_spread_is_negative(self):
        spreads = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10]
        prices = [0.50] * 10
        store = _store_with_prices("tok", prices, spreads)
        f = compute_fundamentals("tok", store)
        assert f.spread_trend < 0  # widening = negative

    def test_high_liquidity_is_positive(self):
        store = _store_with_prices("tok", [0.50] * 5)
        f = compute_fundamentals("tok", store, current_liquidity=100_000)
        assert f.liquidity_trend > 0

    def test_low_liquidity_is_negative(self):
        store = _store_with_prices("tok", [0.50] * 5)
        f = compute_fundamentals("tok", store, current_liquidity=10)
        assert f.liquidity_trend < 0

    def test_fundamentals_score_bounded(self):
        f = MarketFundamentals(
            volume_trend=1.0, liquidity_trend=1.0,
            price_volume_agreement=1.0, spread_trend=1.0, maturity=1.0,
        )
        assert -1.0 <= fundamentals_score(f) <= 1.0

    def test_raw_dict_populated(self):
        store = _store_with_prices("tok", [0.50, 0.51, 0.52, 0.53, 0.54])
        f = compute_fundamentals("tok", store)
        assert "n_ticks" in f.raw
        assert "maturity" in f.raw
