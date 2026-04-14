"""Property-based tests for :mod:`src.portfolio.tracker`.

The tracker is the single point where paper-trading accounting becomes
"truth" for the risk manager and the dashboard.  Accounting bugs here
are invisible in isolated unit tests — they only appear when a long
sequence of realistic fills pile up.  Property-based tests randomly
generate such sequences and assert invariants that must hold no
matter what order things happen in.

Invariants we check:

1. **Size non-negativity.**  No open position ever has ``size < 0``.
2. **Conservation of realised PnL.**  Closing at entry price produces
   zero PnL, regardless of history.
3. **Round-trip P&L sign.**  BUY → SELL at a higher price is always
   profit; BUY → SELL at a lower price is always loss.
4. **Reconstruction idempotence.**  ``reconstruct_from_trades`` twice
   on the same trade list yields identical state.
5. **Exposure non-negativity.**  ``total_exposure`` ≥ 0 always.
6. **Flip correctness.**  A SELL larger than a long leaves a SELL
   position whose size is exactly the residual.

These are light-weight — 50 examples each, cheap to run in CI.
"""

from __future__ import annotations

import math

import pytest

pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.portfolio.tracker import PortfolioTracker, Position


# ---------------------------------------------------------------------------
# Strategies — the basic building blocks of a trade stream
# ---------------------------------------------------------------------------


# Polymarket prices live in (0, 1); 2 dp tracks real market granularity.
prices = st.floats(
    min_value=0.01, max_value=0.99,
    allow_nan=False, allow_infinity=False,
).map(lambda x: round(x, 2))

# Position sizes are dollar notional in paper mode.  Cap at 500 so the
# realised PnL stays numerically well-behaved across hundreds of fills.
sizes = st.floats(
    min_value=0.01, max_value=500.0,
    allow_nan=False, allow_infinity=False,
).map(lambda x: round(x, 4))

sides = st.sampled_from(["BUY", "SELL"])

# A small pool of tokens so sequences routinely revisit the same
# position — that's where merging / partial-close bugs hide.
token_ids = st.sampled_from(["tok_a", "tok_b", "tok_c", "tok_d"])


@st.composite
def trade_dicts(draw) -> dict:
    token = draw(token_ids)
    return {
        "token_id": token,
        # Condition id tracks token so Yes/No legs share a condition.
        "condition_id": f"cond_{token[-1]}",
        "side": draw(sides),
        "size": draw(sizes),
        "price": draw(prices),
        "strategy": "proptest",
        "order_id": draw(st.text(min_size=1, max_size=8)),
        "timestamp": "2026-01-01T00:00:00",
        "category": "",
    }


trade_streams = st.lists(trade_dicts(), min_size=0, max_size=40)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _apply(pt: PortfolioTracker, trades: list[dict]) -> None:
    """Feed a trade sequence through the open-position path.

    Using the same entry point the live bot uses (rather than calling
    ``reconstruct_from_trades``) exercises the merging + flipping
    logic on every fill.
    """
    for t in trades:
        pt.open_position(Position(
            token_id=t["token_id"], condition_id=t["condition_id"],
            side=t["side"], size=t["size"], entry_price=t["price"],
            strategy=t["strategy"], order_id=t["order_id"],
            entry_timestamp=t["timestamp"], category=t["category"],
        ))


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestPortfolioInvariants:

    @given(trades=trade_streams)
    @settings(max_examples=50, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_sizes_never_negative(self, trades):
        pt = PortfolioTracker()
        _apply(pt, trades)
        for pos in pt.positions.values():
            assert pos.size > 0, (
                f"Non-positive size for {pos.token_id}: {pos.size}"
            )

    @given(trades=trade_streams)
    @settings(max_examples=50, deadline=None)
    def test_exposure_non_negative(self, trades):
        pt = PortfolioTracker()
        _apply(pt, trades)
        assert pt.total_exposure() >= 0.0

    @given(trades=trade_streams)
    @settings(max_examples=50, deadline=None)
    def test_reconstruct_is_idempotent(self, trades):
        """Running reconstruct_from_trades twice yields the same state.

        Uses only BUY/SELL with realistic token repetition so both
        code paths (merge + close) get exercised.  Any drift between
        runs signals a non-deterministic mutation in the tracker.
        """
        pt1 = PortfolioTracker()
        pt1.reconstruct_from_trades(trades)
        pt2 = PortfolioTracker()
        pt2.reconstruct_from_trades(trades)
        pt2.reconstruct_from_trades(trades)  # second time — should reset first

        # Positions: same tokens, same sizes, same entries, same sides.
        assert set(pt1.positions.keys()) == set(pt2.positions.keys())
        for tok, p1 in pt1.positions.items():
            p2 = pt2.positions[tok]
            assert p1.side == p2.side
            assert math.isclose(p1.size, p2.size, rel_tol=1e-9, abs_tol=1e-9)
            assert math.isclose(p1.entry_price, p2.entry_price,
                                rel_tol=1e-9, abs_tol=1e-9)

        # Realised PnL: identical.
        assert math.isclose(pt1.realised_pnl, pt2.realised_pnl,
                            rel_tol=1e-9, abs_tol=1e-6)


class TestClosingPnl:

    @given(entry=prices, size=sizes)
    @settings(max_examples=50, deadline=None)
    def test_close_at_entry_is_zero_pnl(self, entry, size):
        """Closing at the entry price books exactly zero PnL."""
        pt = PortfolioTracker()
        pt.open_position(Position("t", "c", "BUY", size, entry,
                                  "s", "o"))
        pnl = pt.close_position("t", entry)
        assert abs(pnl) < 1e-9
        assert pt.realised_pnl == pytest.approx(0.0, abs=1e-9)

    @given(
        entry=prices, exit_=prices, size=sizes,
    )
    @settings(max_examples=75, deadline=None)
    def test_buy_roundtrip_sign(self, entry, exit_, size):
        """BUY + SELL round-trip: PnL sign matches price delta."""
        pt = PortfolioTracker()
        pt.open_position(Position("t", "c", "BUY", size, entry,
                                  "s", "o"))
        pnl = pt.close_position("t", exit_)
        if exit_ > entry:
            assert pnl > 0
        elif exit_ < entry:
            assert pnl < 0
        else:
            assert pnl == pytest.approx(0.0, abs=1e-9)

    @given(
        entry=prices, exit_=prices, size=sizes,
    )
    @settings(max_examples=75, deadline=None)
    def test_sell_roundtrip_sign_inverted(self, entry, exit_, size):
        """SELL + BUY-to-close: PnL sign matches *inverse* price delta."""
        pt = PortfolioTracker()
        pt.open_position(Position("t", "c", "SELL", size, entry,
                                  "s", "o"))
        pnl = pt.close_position("t", exit_)
        if exit_ < entry:
            assert pnl > 0
        elif exit_ > entry:
            assert pnl < 0
        else:
            assert pnl == pytest.approx(0.0, abs=1e-9)


class TestFlip:

    @given(
        long_size=st.floats(min_value=1.0, max_value=100.0),
        short_size=st.floats(min_value=1.0, max_value=100.0),
        entry=prices, exit_=prices,
    )
    @settings(max_examples=50, deadline=None)
    def test_oversell_flips_to_short(self, long_size, short_size, entry, exit_):
        """A SELL bigger than the BUY leaves a SELL of the residual size."""
        # Skip degenerate cases — the tracker treats sub-nano residuals
        # as "fully closed".  We're testing the flip, not the rounding.
        if short_size - long_size <= 1e-6:
            return

        pt = PortfolioTracker()
        pt.open_position(Position("t", "c", "BUY", long_size, entry,
                                  "s", "o"))
        pt.open_position(Position("t", "c", "SELL", short_size, exit_,
                                  "s", "o2"))

        # Residual: SELL for (short - long), at the SELL fill's price.
        assert "t" in pt.positions
        residual = pt.positions["t"]
        assert residual.side == "SELL"
        assert residual.size == pytest.approx(short_size - long_size,
                                              rel=1e-9, abs=1e-9)
        assert residual.entry_price == pytest.approx(exit_, rel=1e-9, abs=1e-9)
