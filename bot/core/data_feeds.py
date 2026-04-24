"""Category-specific data feeds — give Claude specialized data the market hasn't processed.

For crypto markets: real-time prices + TVL + Fear & Greed + trending from CoinGecko / DefiLlama.
For politics: polling averages + base rates for common political events.
For sports: live scores + standings from ESPN.
For finance: stock prices + market indices.

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
    "pepe": "pepe",
    "shib": "shiba-inu", "shiba": "shiba-inu",
    "toncoin": "the-open-network", "ton": "the-open-network",
    "sui": "sui",
    "near": "near",
    "arbitrum": "arbitrum", "arb": "arbitrum",
    "optimism": "optimism", "op": "optimism",
}

_CATEGORY_ALIASES: dict[str, str] = {
    "cryptocurrency": "crypto", "bitcoin": "crypto", "ethereum": "crypto",
    "defi": "crypto", "token": "crypto", "blockchain": "crypto",
    "election": "politics", "president": "politics", "congress": "politics",
    "vote": "politics", "senate": "politics",
    "nba": "sports", "nfl": "sports", "soccer": "sports",
    "football": "sports", "baseball": "sports", "tennis": "sports",
    "mlb": "sports", "nhl": "sports", "premier": "sports",
    "stock": "finance", "shares": "finance", "earnings": "finance",
    "gdp": "finance", "interest": "finance", "inflation": "finance",
    "fed": "finance", "nasdaq": "finance", "s&p": "finance",
    "dow": "finance", "treasury": "finance", "ipo": "finance",
}

_POLITICAL_BASE_RATES: dict[str, tuple[float, str]] = {
    "incumbent_reelection": (0.67, "Incumbents win reelection ~67% of the time in US presidential races"),
    "senate_confirm": (0.85, "~85% of Cabinet nominees are confirmed by the Senate"),
    "scotus_confirm": (0.80, "Supreme Court nominees confirmed ~80% historically"),
    "midterm_loss": (0.89, "President's party loses House seats in midterms ~89% of the time"),
    "impeach_convict": (0.0, "No US president has been convicted after impeachment (0/3)"),
    "govt_shutdown": (0.30, "Government shutdowns occur in ~30% of budget cycles"),
    "veto_override": (0.07, "Congress overrides presidential vetoes only ~7% of the time"),
}

_SPORT_PATTERNS: dict[str, tuple[str, str]] = {
    "nba": ("basketball", "nba"),
    "nfl": ("football", "nfl"),
    "mlb": ("baseball", "mlb"),
    "nhl": ("hockey", "nhl"),
    "soccer": ("soccer", "eng.1"),
}


@dataclass(frozen=True)
class DataFeedResult:
    source: str
    data_text: str
    raw_data: dict


class CryptoDataFeed:
    """CoinGecko + DefiLlama + Fear & Greed + trending for crypto markets."""

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

        try:
            tvl = await self._defillama_tvl()
            if tvl > 0:
                parts.append(f"Total DeFi TVL=${tvl/1e9:.1f}B")
                raw["total_tvl"] = tvl
        except Exception:
            logger.debug("DefiLlama fetch failed", exc_info=True)

        try:
            fng = await self._fear_and_greed()
            if fng:
                parts.append(f"Fear&Greed={fng['value']}/100 ({fng['label']})")
                raw["fear_greed"] = fng
        except Exception:
            logger.debug("Fear & Greed fetch failed", exc_info=True)

        try:
            trending = await self._trending_coins()
            if trending:
                parts.append(f"Trending: {', '.join(trending[:5])}")
                raw["trending"] = trending
        except Exception:
            logger.debug("Trending coins fetch failed", exc_info=True)

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

    async def _fear_and_greed(self) -> dict | None:
        cached = self._cache.get("fng")
        if cached is not None:
            return cached
        url = "https://api.alternative.me/fng/?limit=1"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            data = resp.json()
            items = data.get("data", [])
            if not items:
                return None
            value = int(items[0].get("value", 50))
            label = items[0].get("value_classification", "Neutral")
            result = {"value": value, "label": label}
            self._cache.set("fng", result, ttl=3600)
            return result

    async def _trending_coins(self) -> list[str]:
        cached = self._cache.get("trending")
        if cached is not None:
            return cached
        url = f"{cfg.coingecko_api_url}/search/trending"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return []
            data = resp.json()
            coins = data.get("coins", [])
            names = [c.get("item", {}).get("name", "") for c in coins if c.get("item")]
            names = [n for n in names if n]
            self._cache.set("trending", names, ttl=600)
            return names

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
        if not found and any(w in text_lower for w in ("crypto", "bitcoin", "btc")):
            found.append("bitcoin")
        return found[:5]


class PoliticsDataFeed:
    """Polling data + base rates for political markets."""

    def __init__(self) -> None:
        self._cache = TTLCache(default_ttl=3600, max_size=50)

    async def fetch(self, question: str, description: str = "") -> DataFeedResult | None:
        cache_key = f"politics:{question[:60]}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        parts: list[str] = []
        raw: dict = {}

        base_rate = self._estimate_base_rate(question)
        if base_rate:
            parts.append(base_rate)
            raw["base_rate"] = base_rate

        try:
            poll_text = await self._fetch_538_generic()
            if poll_text:
                parts.append(poll_text)
                raw["polls"] = poll_text
        except Exception:
            logger.debug("Politics data fetch failed", exc_info=True)

        try:
            related = await self._fetch_related_markets(question)
            if related:
                parts.append(related)
                raw["related_markets"] = related
        except Exception:
            logger.debug("Related markets fetch failed", exc_info=True)

        if not parts:
            return None

        result = DataFeedResult(
            source="Politics+Polls",
            data_text="POLITICAL DATA: " + " | ".join(parts),
            raw_data=raw,
        )
        self._cache.set(cache_key, result)
        return result

    def _estimate_base_rate(self, question: str) -> str:
        q = question.lower()
        for key, (rate, explanation) in _POLITICAL_BASE_RATES.items():
            triggers = key.split("_")
            if all(t in q for t in triggers):
                return f"BASE RATE: {rate:.0%} — {explanation}"
        if any(w in q for w in ("confirm", "confirmation", "nominee")):
            return "BASE RATE: ~85% — Most presidential nominees are confirmed"
        if any(w in q for w in ("reelect", "re-elect", "reelection")):
            return "BASE RATE: ~67% — Incumbents win reelection ~67% of the time"
        return ""

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
                return "POLLS: " + " | ".join(parts[:3])
        except Exception:
            pass
        return ""

    async def _fetch_related_markets(self, question: str) -> str:
        keywords = re.findall(r"[A-Za-z]+", question.lower())
        keywords = [w for w in keywords if len(w) > 3 and w not in (
            "will", "the", "that", "this", "what", "when", "before", "after",
        )]
        if not keywords:
            return ""
        search_term = keywords[0]
        cached = self._cache.get(f"related:{search_term}")
        if cached is not None:
            return cached
        url = f"{cfg.gamma_url}/events"
        params = {"active": "true", "closed": "false", "limit": "5", "slug_contains": search_term}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    return ""
                data = resp.json()
                if not isinstance(data, list) or not data:
                    return ""
            parts = []
            for event in data[:3]:
                title = event.get("title", "")[:60]
                markets = event.get("markets", [])
                if markets:
                    price = markets[0].get("outcomePrices", [0.5])[0]
                    parts.append(f"{title} (YES={float(price):.0%})")
            if parts:
                result = "RELATED MARKETS: " + " | ".join(parts)
                self._cache.set(f"related:{search_term}", result, ttl=300)
                return result
        except Exception:
            pass
        return ""


class SportsDataFeed:
    """Live scores and standings from ESPN for sports markets."""

    def __init__(self) -> None:
        self._cache = TTLCache(default_ttl=300, max_size=50)

    async def fetch(self, question: str, description: str = "") -> DataFeedResult | None:
        cache_key = f"sports:{question[:60]}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        sport, league = self._detect_sport(question)
        if not sport:
            return None

        parts: list[str] = []
        raw: dict = {}

        try:
            scores = await self._fetch_scoreboard(sport, league)
            if scores:
                parts.append(scores)
                raw["scores"] = scores
        except Exception:
            logger.debug("ESPN scoreboard fetch failed", exc_info=True)

        try:
            standings = await self._fetch_standings(sport, league)
            if standings:
                parts.append(standings)
                raw["standings"] = standings
        except Exception:
            logger.debug("ESPN standings fetch failed", exc_info=True)

        if not parts:
            return None

        result = DataFeedResult(
            source="ESPN",
            data_text="SPORTS DATA: " + " | ".join(parts),
            raw_data=raw,
        )
        self._cache.set(cache_key, result)
        return result

    def _detect_sport(self, question: str) -> tuple[str, str]:
        q = question.lower()
        for key, (sport, league) in _SPORT_PATTERNS.items():
            if key in q or sport in q:
                return sport, league
        if any(w in q for w in ("lakers", "celtics", "warriors", "bucks", "heat")):
            return "basketball", "nba"
        if any(w in q for w in ("chiefs", "eagles", "cowboys", "49ers", "ravens")):
            return "football", "nfl"
        return "", ""

    async def _fetch_scoreboard(self, sport: str, league: str) -> str:
        cached = self._cache.get(f"scoreboard:{sport}:{league}")
        if cached is not None:
            return cached
        url = f"{cfg.espn_api_url}/{sport}/{league}/scoreboard"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return ""
            data = resp.json()
        events = data.get("events", [])
        if not events:
            return ""
        parts = []
        for event in events[:5]:
            status = event.get("status", {}).get("type", {}).get("shortDetail", "")
            competitions = event.get("competitions", [{}])
            if competitions:
                competitors = competitions[0].get("competitors", [])
                if len(competitors) >= 2:
                    home = competitors[0]
                    away = competitors[1]
                    h_name = home.get("team", {}).get("abbreviation", "?")
                    a_name = away.get("team", {}).get("abbreviation", "?")
                    h_score = home.get("score", "?")
                    a_score = away.get("score", "?")
                    parts.append(f"{a_name} {a_score}-{h_score} {h_name} ({status})")
        if parts:
            result = "SCORES: " + " | ".join(parts[:4])
            self._cache.set(f"scoreboard:{sport}:{league}", result, ttl=120)
            return result
        return ""

    async def _fetch_standings(self, sport: str, league: str) -> str:
        cached = self._cache.get(f"standings:{sport}:{league}")
        if cached is not None:
            return cached
        url = f"{cfg.espn_api_url}/{sport}/{league}/standings"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return ""
                data = resp.json()
            children = data.get("children", [])
            if not children:
                return ""
            top_teams = []
            for division in children[:2]:
                entries = division.get("standings", {}).get("entries", [])
                for entry in entries[:3]:
                    team = entry.get("team", {}).get("abbreviation", "?")
                    stats = entry.get("stats", [])
                    record = ""
                    for s in stats:
                        if s.get("name") == "overall":
                            record = s.get("displayValue", "")
                            break
                    top_teams.append(f"{team}({record})" if record else team)
            if top_teams:
                result = f"TOP TEAMS: {', '.join(top_teams[:8])}"
                self._cache.set(f"standings:{sport}:{league}", result, ttl=3600)
                return result
        except Exception:
            pass
        return ""


class FinancialDataFeed:
    """Stock prices and market indices from free public endpoints."""

    def __init__(self) -> None:
        self._cache = TTLCache(default_ttl=300, max_size=100)

    _TICKER_MAP: dict[str, str] = {
        "apple": "AAPL", "aapl": "AAPL",
        "google": "GOOGL", "googl": "GOOGL", "alphabet": "GOOGL",
        "microsoft": "MSFT", "msft": "MSFT",
        "amazon": "AMZN", "amzn": "AMZN",
        "tesla": "TSLA", "tsla": "TSLA",
        "nvidia": "NVDA", "nvda": "NVDA",
        "meta": "META",
        "netflix": "NFLX", "nflx": "NFLX",
        "sp500": "^GSPC", "s&p": "^GSPC",
        "nasdaq": "^IXIC",
        "dow": "^DJI", "djia": "^DJI",
    }

    async def fetch(self, question: str, description: str = "") -> DataFeedResult | None:
        tickers = self._extract_tickers(question + " " + description)
        if not tickers:
            return None

        cache_key = "finance:" + ",".join(sorted(tickers))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        parts: list[str] = []
        raw: dict = {}

        for ticker in tickers[:5]:
            try:
                quote = await self._fetch_quote(ticker)
                if quote:
                    parts.append(quote["text"])
                    raw[ticker] = quote
            except Exception:
                logger.debug("Finance fetch failed for %s", ticker, exc_info=True)

        if not parts:
            return None

        result = DataFeedResult(
            source="Finance",
            data_text="FINANCIAL DATA: " + " | ".join(parts),
            raw_data=raw,
        )
        self._cache.set(cache_key, result)
        return result

    async def _fetch_quote(self, ticker: str) -> dict | None:
        cached = self._cache.get(f"quote:{ticker}")
        if cached is not None:
            return cached
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"range": "5d", "interval": "1d"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    return None
                data = resp.json()
            chart = data.get("chart", {}).get("result", [])
            if not chart:
                return None
            meta = chart[0].get("meta", {})
            price = meta.get("regularMarketPrice", 0)
            prev_close = meta.get("chartPreviousClose", 0)
            change_pct = (price - prev_close) / prev_close * 100 if prev_close > 0 else 0.0
            symbol = meta.get("symbol", ticker)
            result = {
                "text": f"{symbol}=${price:,.2f} ({change_pct:+.1f}%)",
                "price": price,
                "change_pct": change_pct,
            }
            self._cache.set(f"quote:{ticker}", result, ttl=300)
            return result
        except Exception:
            return None

    def _extract_tickers(self, text: str) -> list[str]:
        text_lower = text.lower()
        found: list[str] = []
        seen: set[str] = set()
        words = re.findall(r"[a-z&]+", text_lower)
        for word in words:
            ticker = self._TICKER_MAP.get(word)
            if ticker and ticker not in seen:
                seen.add(ticker)
                found.append(ticker)
        ticker_refs = re.findall(r"\$([A-Z]{2,5})", text)
        for t in ticker_refs:
            if t not in seen:
                seen.add(t)
                found.append(t)
        return found[:5]


class DataFeedRouter:
    """Route market category to appropriate data feed and return enrichment context."""

    def __init__(self) -> None:
        self._feeds: dict[str, CryptoDataFeed | PoliticsDataFeed | SportsDataFeed | FinancialDataFeed] = {
            "crypto": CryptoDataFeed(),
            "politics": PoliticsDataFeed(),
            "sports": SportsDataFeed(),
            "finance": FinancialDataFeed(),
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
        cat = category.lower()
        if cat in self._feeds:
            return cat
        if cat in _CATEGORY_ALIASES:
            return _CATEGORY_ALIASES[cat]

        for tag in tags:
            tag_lower = tag.lower()
            if tag_lower in _CATEGORY_ALIASES:
                return _CATEGORY_ALIASES[tag_lower]

        q = question.lower()
        for keyword, feed_key in _CATEGORY_ALIASES.items():
            if keyword in q:
                return feed_key

        return ""
