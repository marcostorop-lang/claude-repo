"""Regression tests for zombie-tracking persistence across restarts.

Without these, ``last_known_price`` and ``consecutive_missing_price_ticks``
silently reset to zero on every restart and a token already 4 ticks
dark would need ``ZOMBIE_POSITION_MAX_MISSING_TICKS`` more before the
breaker fires — exactly the wrong direction for a safety gate.
"""

from __future__ import annotations

from src.config import Config
from src.portfolio.tracker import PortfolioTracker, Position
from src.storage.sqlite_store import SQLiteStore
from src.main import _observe_position_price


def _pos(token_id: str = "tokA", side: str = "BUY", entry: float = 0.50) -> Position:
    return Position(
        token_id=token_id, condition_id="cidA",
        side=side, size=10.0, entry_price=entry,
        strategy="momentum", order_id="o1",
    )


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------


class TestPositionPriceStateStore:
    def test_upsert_and_get(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            store.upsert_position_price_state(
                "t1",
                last_known_price=0.55,
                last_price_ts="2026-01-01T00:00:00Z",
                consecutive_missing_price_ticks=0,
            )
            row = store.get_position_price_state("t1")
            assert row is not None
            assert row["last_known_price"] == 0.55
            assert row["last_price_ts"] == "2026-01-01T00:00:00Z"
            assert row["consecutive_missing_price_ticks"] == 0
        finally:
            store.close()

    def test_upsert_overwrites_same_token(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            for misses in (0, 1, 2, 3):
                store.upsert_position_price_state(
                    "t1", last_known_price=0.55,
                    last_price_ts="ts",
                    consecutive_missing_price_ticks=misses,
                )
            row = store.get_position_price_state("t1")
            assert row["consecutive_missing_price_ticks"] == 3
            cur = store._conn.execute(
                "SELECT COUNT(*) FROM position_price_state WHERE token_id='t1'",
            )
            # PK enforces single row even with repeated upserts.
            assert cur.fetchone()[0] == 1
        finally:
            store.close()

    def test_get_all_returns_dict_keyed_by_token(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            for tok in ("a", "b", "c"):
                store.upsert_position_price_state(
                    tok, last_known_price=0.5, last_price_ts="ts",
                    consecutive_missing_price_ticks=0,
                )
            states = store.get_all_position_price_states()
            assert set(states.keys()) == {"a", "b", "c"}
        finally:
            store.close()

    def test_delete_removes_row(self, tmp_path):
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            store.upsert_position_price_state(
                "t1", last_known_price=0.5, last_price_ts="ts",
                consecutive_missing_price_ticks=2,
            )
            assert store.get_position_price_state("t1") is not None
            store.delete_position_price_state("t1")
            assert store.get_position_price_state("t1") is None
        finally:
            store.close()


# ---------------------------------------------------------------------------
# _observe_position_price wiring
# ---------------------------------------------------------------------------


class TestObservePersists:
    def test_observation_writes_state_to_db(self, tmp_path):
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            pt = PortfolioTracker()
            pt.open_position(_pos())
            _observe_position_price(pt, cfg, "tokA", 0.55, store=store)
            row = store.get_position_price_state("tokA")
            assert row is not None
            assert row["last_known_price"] == 0.55
            assert row["consecutive_missing_price_ticks"] == 0
        finally:
            store.close()

    def test_missing_writes_incremented_counter(self, tmp_path):
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            pt = PortfolioTracker()
            pt.open_position(_pos())
            for _ in range(3):
                _observe_position_price(pt, cfg, "tokA", None, store=store)
            row = store.get_position_price_state("tokA")
            assert row["consecutive_missing_price_ticks"] == 3
        finally:
            store.close()

    def test_no_store_is_no_op_safe(self, tmp_path):
        cfg = Config()
        pt = PortfolioTracker()
        pt.open_position(_pos())
        # No exception when store is omitted.
        _observe_position_price(pt, cfg, "tokA", 0.55)
        _observe_position_price(pt, cfg, "tokA", None)


# ---------------------------------------------------------------------------
# Restart cycle
# ---------------------------------------------------------------------------


class TestRestartHydration:
    def test_zombie_counter_survives_restart(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ZOMBIE_POSITION_MAX_MISSING_TICKS", "5")
        cfg = Config()
        db_path = str(tmp_path / "t.db")

        # Phase 1: bot session 1 — 4 missed ticks.
        s1 = SQLiteStore(db_path)
        try:
            pt1 = PortfolioTracker()
            pt1.open_position(_pos())
            for _ in range(4):
                _observe_position_price(pt1, cfg, "tokA", None, store=s1)
            assert pt1.positions["tokA"].consecutive_missing_price_ticks == 4
        finally:
            s1.close()

        # Phase 2: bot restarts — same DB, fresh tracker.
        s2 = SQLiteStore(db_path)
        try:
            pt2 = PortfolioTracker()
            pt2.open_position(_pos())
            states = s2.get_all_position_price_states()
            assert "tokA" in states
            # Hydrate as run_loop would.
            pos = pt2.positions["tokA"]
            pos.consecutive_missing_price_ticks = states["tokA"][
                "consecutive_missing_price_ticks"
            ]
            pos.last_known_price = states["tokA"]["last_known_price"]
            pos.last_price_ts = states["tokA"]["last_price_ts"]
            assert pos.consecutive_missing_price_ticks == 4
            # Next missed tick crosses the threshold (5).  Without
            # persistence this would have happened only after 5 more
            # missed ticks post-restart.
            is_zombie, _ = _observe_position_price(
                pt2, cfg, "tokA", None, store=s2,
            )
            assert is_zombie is True
        finally:
            s2.close()

    def test_close_cleans_up_state_row(self, tmp_path):
        cfg = Config()
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            pt = PortfolioTracker()
            pt.open_position(_pos())
            _observe_position_price(pt, cfg, "tokA", 0.55, store=store)
            assert store.get_position_price_state("tokA") is not None
            # Simulate close: ``_close_position_and_record`` calls
            # delete_position_price_state.
            store.delete_position_price_state("tokA")
            assert store.get_position_price_state("tokA") is None
        finally:
            store.close()

    def test_orphan_state_pruned_on_restart(self, tmp_path):
        # State row exists for a token the trade-replay didn't reopen
        # (e.g. the position was closed in a previous session and we
        # forgot to delete the row).  Hydration must drop it instead
        # of letting the table drift forever.
        store = SQLiteStore(str(tmp_path / "t.db"))
        try:
            store.upsert_position_price_state(
                "ghost", last_known_price=0.5, last_price_ts="ts",
                consecutive_missing_price_ticks=10,
            )
            # Simulate the hydration path from run_loop with no held
            # positions.
            held: set[str] = set()
            states = store.get_all_position_price_states()
            for tok in states:
                if tok not in held:
                    store.delete_position_price_state(tok)
            assert store.get_all_position_price_states() == {}
        finally:
            store.close()
