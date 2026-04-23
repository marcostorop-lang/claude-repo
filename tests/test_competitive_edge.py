"""Tests for competitive edge features: news feed, data feeds, cache, speed."""

from __future__ import annotations

import asyncio
import time

import pytest


# ===========================================================================
# TTLCache
# ===========================================================================


class TestTTLCache:
    def test_set_and_get(self):
        from bot.core.cache import TTLCache
        c = TTLCache(default_ttl=10)
        c.set("k1", "v1")
        assert c.get("k1") == "v1"

    def test_expired_key_returns_none(self):
        from bot.core.cache import TTLCache
        c = TTLCache(default_ttl=0.01)
        c.set("k1", "v1")
        time.sleep(0.02)
        assert c.get("k1") is None

    def test_custom_ttl(self):
        from bot.core.cache import TTLCache
        c = TTLCache(default_ttl=0.01)
        c.set("k1", "v1", ttl=10)
        time.sleep(0.02)
        assert c.get("k1") == "v1"  # custom TTL overrides default

    def test_invalidate(self):
        from bot.core.cache import TTLCache
        c = TTLCache()
        c.set("k1", "v1")
        c.invalidate("k1")
        assert c.get("k1") is None

    def test_max_size_eviction(self):
        from bot.core.cache import TTLCache
        c = TTLCache(default_ttl=60, max_size=3)
        c.set("a", 1)
        c.set("b", 2)
        c.set("c", 3)
        c.set("d", 4)  # should evict oldest
        assert c.get("d") == 4
        s = c.stats()
        assert s["size"] <= 3

    def test_stats(self):
        from bot.core.cache import TTLCache
        c = TTLCache()
        c.set("k1", "v1")
        c.get("k1")  # hit
        c.get("k2")  # miss
        s = c.stats()
        assert s["hits"] == 1
        assert s["misses"] == 1
        assert s["hit_rate"] == 0.5

    def test_clear(self):
        from bot.core.cache import TTLCache
        c = TTLCache()
        c.set("k1", "v1")
        c.clear()
        assert c.get("k1") is None


# ===========================================================================
# NewsFetcher
# ===========================================================================


class TestNewsFetcher:
    def test_extract_keywords(self):
        from bot.core.news_feed import NewsFetcher
        nf = NewsFetcher()
        kws = nf._extract_keywords("Will Bitcoin be above $100k by year end?")
        assert "bitcoin" in kws
        assert "$100k" in kws
        assert "will" not in kws
        assert "the" not in kws

    def test_extract_keywords_dedup(self):
        from bot.core.news_feed import NewsFetcher
        nf = NewsFetcher()
        kws = nf._extract_keywords("Bitcoin Bitcoin Bitcoin price price")
        assert kws.count("bitcoin") == 1

    def test_score_relevance(self):
        from bot.core.news_feed import NewsFetcher
        nf = NewsFetcher()
        score = nf._score_relevance("Bitcoin hits new all-time high", ["bitcoin", "price", "high"])
        assert score > 0
        score_zero = nf._score_relevance("Weather forecast for tomorrow", ["bitcoin", "price"])
        assert score > score_zero

    def test_resolve_category(self):
        from bot.core.news_feed import NewsFetcher
        nf = NewsFetcher()
        assert nf._resolve_category("Crypto", "Will Bitcoin reach $100k?") == "crypto"
        assert nf._resolve_category("", "Will Trump win the election?") == "politics"
        assert nf._resolve_category("Sports", "Who wins the NBA finals?") == "sports"
        assert nf._resolve_category("", "Will it rain tomorrow?") == "general"

    def test_format_for_oracle_empty(self):
        from bot.core.news_feed import NewsFetcher
        nf = NewsFetcher()
        assert nf.format_for_oracle([]) == ""

    def test_format_for_oracle_with_items(self):
        from bot.core.news_feed import NewsFetcher, NewsItem
        nf = NewsFetcher()
        items = [
            NewsItem(headline="BTC hits $100k", source="Reuters", published_at="2h ago", relevance=0.9),
            NewsItem(headline="ETH upgrade live", source="CoinDesk", published_at="1h ago", relevance=0.5),
        ]
        result = nf.format_for_oracle(items)
        assert "RECENT NEWS" in result
        assert "BTC hits $100k" in result
        assert "[1]" in result
        assert "[2]" in result

    def test_parse_rss_xml(self):
        from bot.core.news_feed import NewsFetcher
        nf = NewsFetcher()
        xml = """<?xml version="1.0"?>
        <rss><channel>
            <item><title>Headline 1</title><pubDate>Mon, 01 Jan 2024</pubDate></item>
            <item><title>Headline 2</title><pubDate>Tue, 02 Jan 2024</pubDate></item>
        </channel></rss>"""
        items = nf._parse_rss_xml(xml, "test")
        assert len(items) == 2
        assert items[0].headline == "Headline 1"
        assert items[0].source == "test"

    def test_parse_rss_xml_malformed(self):
        from bot.core.news_feed import NewsFetcher
        nf = NewsFetcher()
        items = nf._parse_rss_xml("not xml at all", "test")
        assert items == []

    def test_newsapi_daily_limit(self):
        from bot.core.news_feed import NewsFetcher
        nf = NewsFetcher()
        nf._newsapi_calls_today = 100
        from datetime import date
        nf._newsapi_date = date.today().isoformat()
        assert nf._can_use_newsapi() is False


# ===========================================================================
# DataFeedRouter
# ===========================================================================


class TestDataFeedRouter:
    def test_resolve_category_crypto(self):
        from bot.core.data_feeds import DataFeedRouter
        router = DataFeedRouter()
        assert router._resolve_category("Crypto", "Will BTC hit $100k?", []) == "crypto"
        assert router._resolve_category("", "Will Bitcoin exceed $100k?", []) == "crypto"
        assert router._resolve_category("", "question", ["cryptocurrency"]) == "crypto"

    def test_resolve_category_politics(self):
        from bot.core.data_feeds import DataFeedRouter
        router = DataFeedRouter()
        assert router._resolve_category("", "Will Trump win the election?", []) == "politics"

    def test_resolve_category_unknown(self):
        from bot.core.data_feeds import DataFeedRouter
        router = DataFeedRouter()
        assert router._resolve_category("", "Will it rain?", []) == ""


class TestCryptoDataFeed:
    def test_extract_coin_ids(self):
        from bot.core.data_feeds import CryptoDataFeed
        feed = CryptoDataFeed()
        ids = feed._extract_coin_ids("Will Bitcoin be above $100k?")
        assert "bitcoin" in ids

    def test_extract_multiple_coins(self):
        from bot.core.data_feeds import CryptoDataFeed
        feed = CryptoDataFeed()
        ids = feed._extract_coin_ids("Will ETH flip BTC in market cap?")
        assert "ethereum" in ids
        assert "bitcoin" in ids

    def test_extract_no_coins(self):
        from bot.core.data_feeds import CryptoDataFeed
        feed = CryptoDataFeed()
        ids = feed._extract_coin_ids("Will it rain tomorrow in Paris?")
        assert ids == []


# ===========================================================================
# Oracle integration (news_context + data_context params)
# ===========================================================================


class TestOracleContextParams:
    def test_estimate_probability_accepts_new_params(self):
        import inspect
        from bot.core.claude_oracle import ClaudeOracle
        sig = inspect.signature(ClaudeOracle.estimate_probability)
        assert "news_context" in sig.parameters
        assert "data_context" in sig.parameters
        assert sig.parameters["news_context"].default == ""
        assert sig.parameters["data_context"].default == ""


# ===========================================================================
# ProbabilityArbitrage accepts news_fetcher + data_router
# ===========================================================================


class TestStrategyIntegration:
    def test_prob_arb_accepts_feeds(self):
        from unittest.mock import MagicMock
        from bot.strategies.probability_arbitrage import ProbabilityArbitrage
        pa = ProbabilityArbitrage(
            oracle=MagicMock(),
            risk=MagicMock(),
            news_fetcher=MagicMock(),
            data_router=MagicMock(),
        )
        assert pa._news_fetcher is not None
        assert pa._data_router is not None

    def test_prob_arb_works_without_feeds(self):
        from unittest.mock import MagicMock
        from bot.strategies.probability_arbitrage import ProbabilityArbitrage
        pa = ProbabilityArbitrage(oracle=MagicMock(), risk=MagicMock())
        assert pa._news_fetcher is None
        assert pa._data_router is None


# ===========================================================================
# Config fields
# ===========================================================================


class TestConfigFields:
    def test_news_feed_config_exists(self):
        from bot.config import cfg
        assert hasattr(cfg, "news_feed_enabled")
        assert hasattr(cfg, "newsapi_key")
        assert hasattr(cfg, "news_max_items")

    def test_data_feeds_config_exists(self):
        from bot.config import cfg
        assert hasattr(cfg, "data_feeds_enabled")
        assert hasattr(cfg, "coingecko_api_url")
        assert hasattr(cfg, "defillama_api_url")

    def test_speed_config_exists(self):
        from bot.config import cfg
        assert hasattr(cfg, "speed_parallel_evaluations")
        assert hasattr(cfg, "speed_book_cache_ttl_s")
        assert cfg.speed_parallel_evaluations >= 1
