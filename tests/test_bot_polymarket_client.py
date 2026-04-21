"""Integration tests for the Polymarket client (mocked HTTP + SDK).

Uses asyncio.run() directly rather than pytest-asyncio to match the
existing test conventions in this repo.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.core import polymarket_client as pc
from bot.core.utils import Side


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if asyncio.get_event_loop().is_running() else asyncio.run(coro)


# ---------------------------------------------------------------------------
# fetch_active_markets
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = json.dumps(data) if not isinstance(data, str) else data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "fail",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(self.status_code, text=self.text),
            )


def _mock_httpx_client(resp):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    client.post = AsyncMock(return_value=resp)
    return client


def test_fetch_active_markets_parses_correctly():
    sample = [{
        "conditionId": "0xcond1",
        "question": "Will Bitcoin be above $100k by year end?",
        "description": "BTC-USD spot price on Dec 31",
        "category": "Crypto",
        "endDate": "2026-12-31",
        "volume": "15000",
        "liquidity": "5000",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.45", "0.55"]',
        "clobTokenIds": '["tok_yes", "tok_no"]',
        "tags": '["crypto", "bitcoin"]',
        "negRisk": False,
    }]
    resp = _FakeResponse(sample)

    with patch.object(pc.httpx, "AsyncClient", return_value=_mock_httpx_client(resp)):
        markets = asyncio.run(pc.fetch_active_markets(min_volume=0, limit=10))

    assert len(markets) == 1
    m = markets[0]
    assert m.condition_id == "0xcond1"
    assert m.question.startswith("Will Bitcoin")
    assert m.volume == 15000.0
    assert m.liquidity == 5000.0
    assert m.outcomes == ["Yes", "No"]
    assert m.outcome_prices == [0.45, 0.55]
    assert m.token_ids == ["tok_yes", "tok_no"]
    assert m.tags == ["crypto", "bitcoin"]


def test_fetch_active_markets_filters_by_min_volume():
    sample = [
        {"conditionId": "0xA", "question": "Q A", "volume": "1000",
         "outcomes": "[]", "outcomePrices": "[]", "clobTokenIds": "[]"},
        {"conditionId": "0xB", "question": "Q B", "volume": "10000",
         "outcomes": "[]", "outcomePrices": "[]", "clobTokenIds": "[]"},
    ]
    resp = _FakeResponse(sample)

    with patch.object(pc.httpx, "AsyncClient", return_value=_mock_httpx_client(resp)):
        markets = asyncio.run(pc.fetch_active_markets(min_volume=5000, limit=10))

    assert len(markets) == 1
    assert markets[0].condition_id == "0xB"


def test_fetch_active_markets_handles_http_error():
    resp = _FakeResponse({"error": "rate limited"}, status_code=429)
    with patch.object(pc.httpx, "AsyncClient", return_value=_mock_httpx_client(resp)):
        markets = asyncio.run(pc.fetch_active_markets(limit=10))
    assert markets == []


def test_fetch_active_markets_handles_malformed_entries():
    sample = [
        {"conditionId": "0xgood", "question": "OK", "volume": "10000",
         "outcomes": '["Yes", "No"]', "outcomePrices": '["0.4","0.6"]',
         "clobTokenIds": '["tok1","tok2"]'},
        {"conditionId": "0xbad", "outcomePrices": "not-valid-json", "volume": "10000"},
    ]
    resp = _FakeResponse(sample)
    with patch.object(pc.httpx, "AsyncClient", return_value=_mock_httpx_client(resp)):
        markets = asyncio.run(pc.fetch_active_markets(limit=10))
    # Bad entry dropped, good one kept
    assert len(markets) == 1
    assert markets[0].condition_id == "0xgood"


def test_fetch_active_markets_empty_response():
    resp = _FakeResponse([])
    with patch.object(pc.httpx, "AsyncClient", return_value=_mock_httpx_client(resp)):
        markets = asyncio.run(pc.fetch_active_markets(limit=10))
    assert markets == []


# ---------------------------------------------------------------------------
# get_book
# ---------------------------------------------------------------------------


def test_get_book_parses_bids_asks():
    book_data = {
        "bids": [{"price": "0.48", "size": "100"}, {"price": "0.47", "size": "200"}],
        "asks": [{"price": "0.52", "size": "150"}, {"price": "0.53", "size": "250"}],
    }
    resp = _FakeResponse(book_data)
    with patch.object(pc.httpx, "AsyncClient", return_value=_mock_httpx_client(resp)):
        snap = asyncio.run(pc.get_book("tok1"))

    assert snap.best_bid == 0.48
    assert snap.best_ask == 0.52
    assert snap.bid_depth_usd == pytest.approx(0.48 * 100 + 0.47 * 200)
    assert snap.ask_depth_usd == pytest.approx(0.52 * 150 + 0.53 * 250)
    assert snap.midpoint == pytest.approx(0.5)


def test_get_book_handles_empty_book():
    resp = _FakeResponse({"bids": [], "asks": []})
    with patch.object(pc.httpx, "AsyncClient", return_value=_mock_httpx_client(resp)):
        snap = asyncio.run(pc.get_book("tok1"))
    assert snap.best_bid == 0.0
    assert snap.best_ask == 0.0
    assert snap.midpoint == 0.0


def test_get_book_handles_non_200():
    resp = _FakeResponse({"error": "nope"}, status_code=500)
    with patch.object(pc.httpx, "AsyncClient", return_value=_mock_httpx_client(resp)):
        snap = asyncio.run(pc.get_book("tok1"))
    assert snap.best_bid == 0.0
    assert snap.best_ask == 0.0


# ---------------------------------------------------------------------------
# place_order (paper)
# ---------------------------------------------------------------------------


def test_place_order_paper_mode_simulates_fill():
    """In paper mode, orders always return success with instant fill."""
    result = asyncio.run(pc.place_order("tok1", Side.BUY, 0.5, 10))
    assert result.success is True
    assert result.mode == "paper"
    assert result.filled_size == 10
    assert result.fill_price == 0.5
    assert result.order_id.startswith("paper-")


def test_cancel_order_paper_is_noop():
    assert asyncio.run(pc.cancel_order("some-id")) is True


def test_cancel_all_paper_returns_zero():
    assert asyncio.run(pc.cancel_all()) == 0
