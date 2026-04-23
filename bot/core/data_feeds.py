"""Category-specific data feeds — give Claude specialized data the market hasn't processed.

For crypto markets: real-time prices + TVL from CoinGecko / DefiLlama.
For politics: polling averages from FiveThirtyEight public data.
For sports: basic stats from free APIs.

All APIs are free (no keys required).  Each feed caches aggressively
to stay within rate limits (CoinGecko: ~30 req/min free tier).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

from bot.config import cfg
from bot.core.cache import TTLCache

logger = logging.getLogger(__name__)

# Common coin name → CoinGecko ID mapping
_COIN_MAP: dict[str, str] = {
    "bitcoin": "bitcoin", "btc": "bitcoin",
    "ethereum": "ethereum", "eth": "ethereum",
    "solana": "solana", "sol": "solana",
    "cardano": "cardano", "ada": "cardano",
    "dogecoin": "dogecoin", "doge": "dogecoin",
    "polygon": "matic-network", "matic": "matic-network",
    "avalanche": "avalanche-2", "avax": "avalanche-2",
    "chainlink": "chainlink", "link": "chainlink",
    "polkadot": "polkadot", "dot": "polkadot",
    "xrp": "ripple", "ripple": "ripple",
    "litecoin": "litecoin", "ltc": "litecoin",
}

# Category aliases for routing
_CATEGORY_ALIASES: dict[str, str] = {
    "cryptocurrency": "crypto", "bitcoin": "crypto", "ethereum": "crypto",
    "defi": "crypto", "token": "crypto", "blockchain": "crypto",
    "election": "politics", "president": "politics", "congress": "politics",
    "vote": "politics", "senate": "politics",
    "nba": "sports", "nfl": "sports", "soccer": "sports",
    "football": "sports", "baseball": "sports", "tennis": "sports",
}


@dataclass(frozen=True)
class DataFeedResult:
    source: str
    data_text: str
    raw_data: dict


class CryptoDataFeed:
    """CoinGecko + DefiLlama for crypto markets."""

    def __init__(self) -> None:
        self._cache = TTLCache(default_ttl=60, max_size=100)

    async def fetch(self, question: str, description: str = "") -> DataFeedResult | None:
        coin_ids = self._extract_coin_ids(question + " " + description)
        if not coin_ids:
            return None

        cache_key = "crypto:" + ",".join(sorted(coin_ids))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        parts: list[str] = []
        raw: dict = {}

        # CoinGecko prices
        try:
            prices = await self._coingecko_prices(coin_ids)
            for coin_id, data in prices.items():
                usd = data.get("usd", 0)
                change = data.get("usd_24h_change", 0)
                mcap = data.get("usd_market_cap", 0)
                name = coin_id.upper()[:10]
                parts.append(f"{name}=${usd:,.2f} ({change:+.1f}% 24h, mcap=${mcap/1e9:.1f}B)")
                raw[coin_id] = data
        except Exception:
            logger.debug("CoinGecko fetch failed", exc_info=True)

        # DefiLlama total TVL
        try:
            tvl = await self._defillama_tvl()
            if tvl > 0:
                parts.append(f"Total DeFi TVL=${tvl/1e9:.1f}B")
                raw["total_tvl"] = tvl
        except Exception:
            logger.debug("DefiLlama fetch failed", exc_info=True)

        if not parts:
            return None

        result = DataFeedResult(
            source="CoinGecko+DefiLlama",
            data_text="CRYPTO DATA: " + " | ".join(parts),
            raw_data=raw,
        )
        self._cache.set(cache_key, result)
        return result

    async def _coingecko_prices(self, coin_ids: list[str]) -> dict:
        ids_param = ",".join(coin_ids[:10])
        url = f"{cfg.coingecko_api_url}/simple/price"
        params = {
            "ids": ids_param,
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_market_cap": "true",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    async def _defillama_tvl(self) -> float:
        cached = self._cache.get("defillama_tvl")
        if cached is not None:
            return cached
        url = f"{cfg.defillama_api_url}/v2/historicalChainTvl"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return 0.0
            data = resp.json()
            if isinstance(data, list) and data:
                tvl = float(data[-1].get("tvl", 0))
                self._cache.set("defillama_tvl", tvl, ttl=300)
                return tvl
        return 0.0

    def _extract_coin_ids(self, text: str) -> list[str]:
        text_lower = text.lower()
        words = re.findall(r"[a-z]+", text_lower)
        found: list[str] = []
        seen: set[str] = set()
        for word in words:
            coin_id = _COIN_MAP.get(word)
            if coin_id and coin_id not in seen:
                seen.add(coin_id)
                found.append(coin_id)
        # Also check for $XXk or $XXX,XXX patterns to detect price references
        if not found and any(w in text_lower for w in ("crypto", "bitcoin", "btc")):
            found.append("bitcoin")
        return found[:5]


class PoliticsDataFeed:
    """Polling data from public sources for political markets."""

    def __init__(self) -> None:
        self._cache = TTLCache(default_ttl=3600, max_size=50)

    async def fetch(self, question: str, description: str = "") -> DataFeedResult | None:
        cache_key = f"politics:{question[:60]}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            data = await self._fetch_538_generic()
            if not data:
                return None
            result = DataFeedResult(
                source="FiveThirtyEight",
                data_text=data,
                raw_data={},
            )
            self._cache.set(cache_key, result)
            return result
        except Exception:
            logger.debug("Politics data fetch failed", exc_info=True)
            return None

    async def _fetch_538_generic(self) -> str:
        url = "https://projects.fivethirtyeight.com/polls/president-general/2024/national.json"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return ""
                data = resp.json()
                if not isinstance(data, list) or not data:
                    return ""

            # Extract latest polls
            recent = data[:5]
            parts = []
            for poll in recent:
                pollster = poll.get("pollster", "Unknown")[:20]
                answers = poll.get("answers", [])
                if len(answers) >= 2:
                    a1 = f"{answers[0].get('choice', '?')}={answers[0].get('pct', '?')}%"
                    a2 = f"{answers[1].get('choice', '?')}={answers[1].get('pct', '?')}%"
                    parts.append(f"{pollster}: {a1} vs {a2}")
            if parts:
                return "POLLING DATA: " + " | ".join(parts[:3])
        except Exception:
            pass
        return ""


class SportsDataFeed:
    """Basic sports context from free APIs."""

    def __init__(self) -> None:
        self._cache = TTLCache(default_ttl=600, max_size=50)

    async def fetch(self, question: str, description: str = "") -> DataFeedResult | None:
        # Sports feeds are highly specific; provide general context
        return None


class DataFeedRouter:
    """Route market category to appropriate data feed and return enrichment context."""

    def __init__(self) -> None:
        self._feeds: dict[str, CryptoDataFeed | PoliticsDataFeed | SportsDataFeed] = {
            "crypto": CryptoDataFeed(),
            "politics": PoliticsDataFeed(),
            "sports": SportsDataFeed(),
        }

    async def enrich(self, question: str, description: str = "", category: str = "", tags: list[str] | None = None) -> str:
        """Get category-specific data for a market.  Returns formatted string or ""."""
        if not cfg.data_feeds_enabled:
            return ""

        cat_key = self._resolve_category(category, question, tags or [])
        feed = self._feeds.get(cat_key)
        if feed is None:
            return ""

        try:
            result = await feed.fetch(question, description)
            if result is not None:
                logger.debug("Data feed [%s] returned: %s", cat_key, result.data_text[:80])
                return result.data_text
        except Exception:
            logger.debug("Data feed [%s] failed", cat_key, exc_info=True)
        return ""

    def _resolve_category(self, category: str, question: str, tags: list[str]) -> str:
        # Check direct category
        cat = category.lower()
        if cat in self._feeds:
            return cat
        if cat in _CATEGORY_ALIASES:
            return _CATEGORY_ALIASES[cat]

        # Check tags
        for tag in tags:
            tag_lower = tag.lower()
            if tag_lower in _CATEGORY_ALIASES:
                return _CATEGORY_ALIASES[tag_lower]

        # Check question text
        q = question.lower()
        for keyword, feed_key in _CATEGORY_ALIASES.items():
            if keyword in q:
                return feed_key

        return ""
