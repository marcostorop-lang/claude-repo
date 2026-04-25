"""Integration tests — verify real external APIs respond correctly.

These tests hit live endpoints (no mocking) to ensure the data feeds
actually work in production.  They are marked with @pytest.mark.integration
so they can be skipped in CI with: pytest -m "not integration"

Each test has a generous timeout and treats HTTP errors as skip (not fail)
to avoid false negatives when APIs are temporarily down.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.integration


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# CoinGecko
# ===========================================================================


class TestCoinGeckoIntegration:
    def test_simple_price_returns_data(self):
        import httpx

        async def _fetch():
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": "bitcoin", "vs_currencies": "usd"},
                )
                return resp.status_code, resp.json()

        status, data = _run(_fetch())
        if status == 429:
            pytest.skip("CoinGecko rate-limited")
        assert status == 200
        assert "bitcoin" in data
        assert "usd" in data["bitcoin"]
        assert data["bitcoin"]["usd"] > 0

    def test_trending_returns_coins(self):
        import httpx

        async def _fetch():
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://api.coingecko.com/api/v3/search/trending",
                )
                return resp.status_code, resp.json()

        status, data = _run(_fetch())
        if status == 429:
            pytest.skip("CoinGecko rate-limited")
        assert status == 200
        assert "coins" in data
        assert isinstance(data["coins"], list)


# ===========================================================================
# Fear & Greed Index
# ===========================================================================


class TestFearAndGreedIntegration:
    def test_fng_returns_valid_index(self):
        import httpx

        async def _fetch():
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get("https://api.alternative.me/fng/?limit=1")
                return resp.status_code, resp.json()

        status, data = _run(_fetch())
        if status != 200:
            pytest.skip(f"Fear & Greed API returned {status}")
        assert "data" in data
        assert len(data["data"]) >= 1
        value = int(data["data"][0]["value"])
        assert 0 <= value <= 100


# ===========================================================================
# DefiLlama
# ===========================================================================


class TestDefiLlamaIntegration:
    def test_tvl_returns_data(self):
        import httpx

        async def _fetch():
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get("https://api.llama.fi/v2/historicalChainTvl")
                return resp.status_code, resp.json()

        status, data = _run(_fetch())
        if status != 200:
            pytest.skip(f"DefiLlama returned {status}")
        assert isinstance(data, list)
        assert len(data) > 0
        assert "tvl" in data[-1]
        assert data[-1]["tvl"] > 0


# ===========================================================================
# ESPN
# ===========================================================================


class TestESPNIntegration:
    def test_nba_scoreboard_accessible(self):
        import httpx

        async def _fetch():
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
                )
                return resp.status_code, resp.json()

        status, data = _run(_fetch())
        if status != 200:
            pytest.skip(f"ESPN returned {status}")
        assert "events" in data
        assert isinstance(data["events"], list)

    def test_nfl_scoreboard_accessible(self):
        import httpx

        async def _fetch():
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
                )
                return resp.status_code, resp.json()

        status, data = _run(_fetch())
        if status != 200:
            pytest.skip(f"ESPN returned {status}")
        assert "events" in data


# ===========================================================================
# Polymarket Gamma API
# ===========================================================================


class TestGammaAPIIntegration:
    def test_active_markets_returns_data(self):
        import httpx

        async def _fetch():
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"limit": "3", "active": "true"},
                )
                return resp.status_code, resp.json()

        status, data = _run(_fetch())
        if status != 200:
            pytest.skip(f"Gamma API returned {status}")
        assert isinstance(data, list)
        assert len(data) > 0
        assert "question" in data[0] or "title" in data[0]

    def test_resolved_events_returns_data(self):
        import httpx

        async def _fetch():
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://gamma-api.polymarket.com/events",
                    params={"active": "false", "closed": "true", "limit": "3"},
                )
                return resp.status_code, resp.json()

        status, data = _run(_fetch())
        if status != 200:
            pytest.skip(f"Gamma API returned {status}")
        assert isinstance(data, list)


# ===========================================================================
# Google News RSS
# ===========================================================================


class TestGoogleNewsIntegration:
    def test_rss_search_returns_xml(self):
        import httpx

        async def _fetch():
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://news.google.com/rss/search",
                    params={"q": "bitcoin", "hl": "en", "gl": "US", "ceid": "US:en"},
                )
                return resp.status_code, resp.text

        status, text = _run(_fetch())
        if status != 200:
            pytest.skip(f"Google News returned {status}")
        assert "<rss" in text.lower() or "<xml" in text.lower() or "<item>" in text.lower()


# ===========================================================================
# End-to-end: data feed classes return valid results
# ===========================================================================


class TestDataFeedEndToEnd:
    def test_crypto_feed_returns_data(self):
        from bot.core.data_feeds import CryptoDataFeed

        async def _run_feed():
            feed = CryptoDataFeed()
            return await feed.fetch("Will Bitcoin exceed $100k?")

        try:
            result = _run(_run_feed())
        except Exception:
            pytest.skip("CryptoDataFeed failed (network)")
        if result is None:
            pytest.skip("CryptoDataFeed returned None (rate limited?)")
        assert "CRYPTO DATA" in result.data_text
        assert result.source == "CoinGecko+DefiLlama"

    def test_sports_feed_detects_and_fetches(self):
        from bot.core.data_feeds import SportsDataFeed

        feed = SportsDataFeed()
        sport, league = feed._detect_sport("Will the Lakers win the NBA title?")
        assert sport == "basketball"

        async def _run_feed():
            return await feed.fetch("Will the Lakers win the NBA title?")

        try:
            result = _run(_run_feed())
        except Exception:
            pytest.skip("SportsDataFeed failed (network)")
        # Result may be None during offseason
        if result is not None:
            assert "SPORTS DATA" in result.data_text
