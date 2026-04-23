"""Real-time news feed — gives Claude information the market hasn't priced in yet.

Aggregates headlines from free sources (Google News RSS, category-specific RSS,
optional NewsAPI.org) and returns a compact context string for the oracle prompt.

All fetches are cached with 5-minute TTL to avoid hammering feeds within a
scan cycle.  Failures are always silent — the oracle works fine without news.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree import ElementTree

import httpx

from bot.config import cfg
from bot.core.cache import TTLCache

logger = logging.getLogger(__name__)

_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "will", "be", "been",
    "by", "in", "on", "at", "to", "for", "of", "with", "and", "or", "not",
    "it", "its", "this", "that", "from", "has", "have", "had", "do", "does",
    "did", "can", "could", "would", "should", "may", "might", "above",
    "below", "before", "after", "between", "about", "into", "over", "under",
    "than", "more", "most", "less", "very", "just", "also", "if", "but",
    "so", "what", "which", "who", "whom", "how", "when", "where", "why",
    "all", "each", "every", "both", "any", "few", "some", "no", "yes",
})

_CATEGORY_FEEDS: dict[str, list[str]] = {
    "crypto": [
        "https://cointelegraph.com/rss",
    ],
    "politics": [
        "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
    ],
    "sports": [
        "https://www.espn.com/espn/rss/news",
    ],
    "general": [
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    ],
}


@dataclass(frozen=True)
class NewsItem:
    headline: str
    source: str
    published_at: str
    relevance: float


class NewsFetcher:
    """Aggregate recent headlines relevant to a market question."""

    def __init__(self) -> None:
        self._cache = TTLCache(default_ttl=300, max_size=200)
        self._newsapi_calls_today = 0
        self._newsapi_date = ""

    async def fetch_relevant_news(
        self,
        question: str,
        category: str = "",
        max_items: int = 5,
    ) -> list[NewsItem]:
        """Fetch headlines relevant to a market question.  Returns sorted by relevance."""
        if not cfg.news_feed_enabled:
            return []

        cache_key = f"news:{question[:80]}:{category}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        keywords = self._extract_keywords(question)
        if not keywords:
            return []

        items: list[NewsItem] = []

        # 1. Google News RSS (always free, no key)
        try:
            query = "+".join(keywords[:5])
            google_items = await self._fetch_google_news(query)
            items.extend(google_items)
        except Exception:
            logger.debug("Google News fetch failed", exc_info=True)

        # 2. Category-specific RSS
        cat_key = self._resolve_category(category, question)
        feeds = _CATEGORY_FEEDS.get(cat_key, _CATEGORY_FEEDS["general"])
        for feed_url in feeds[:2]:
            try:
                rss_items = await self._fetch_rss(feed_url)
                items.extend(rss_items)
            except Exception:
                logger.debug("RSS fetch failed: %s", feed_url[:50], exc_info=True)

        # 3. NewsAPI (if configured, with daily limit)
        if cfg.newsapi_key and self._can_use_newsapi():
            try:
                api_items = await self._fetch_newsapi(" ".join(keywords[:3]))
                items.extend(api_items)
            except Exception:
                logger.debug("NewsAPI fetch failed", exc_info=True)

        # Score and rank
        for i, item in enumerate(items):
            score = self._score_relevance(item.headline, keywords)
            items[i] = NewsItem(
                headline=item.headline,
                source=item.source,
                published_at=item.published_at,
                relevance=score,
            )

        items.sort(key=lambda x: x.relevance, reverse=True)
        result = items[:max_items]
        self._cache.set(cache_key, result)
        return result

    def format_for_oracle(self, items: list[NewsItem]) -> str:
        """Format news items into a compact string for the Claude prompt."""
        if not items:
            return ""
        lines = []
        for i, item in enumerate(items[:5], 1):
            lines.append(f"[{i}] {item.headline} ({item.source}, {item.published_at})")
        return "RECENT NEWS:\n" + "\n".join(lines)

    async def _fetch_google_news(self, query: str) -> list[NewsItem]:
        url = f"https://news.google.com/rss/search?q={query}&hl=en&gl=US&ceid=US:en"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return []
            return self._parse_rss_xml(resp.text, "Google News")

    async def _fetch_rss(self, feed_url: str) -> list[NewsItem]:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(feed_url)
            if resp.status_code != 200:
                return []
            source = feed_url.split("/")[2].replace("www.", "").split(".")[0]
            return self._parse_rss_xml(resp.text, source)

    async def _fetch_newsapi(self, query: str) -> list[NewsItem]:
        self._newsapi_calls_today += 1
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": query,
            "sortBy": "publishedAt",
            "pageSize": "10",
            "apiKey": cfg.newsapi_key,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()

        items = []
        for article in data.get("articles", [])[:10]:
            title = article.get("title", "")
            if not title or title == "[Removed]":
                continue
            pub = article.get("publishedAt", "")[:16].replace("T", " ")
            items.append(NewsItem(
                headline=title[:120],
                source=article.get("source", {}).get("name", "NewsAPI"),
                published_at=pub,
                relevance=0.0,
            ))
        return items

    def _parse_rss_xml(self, xml_text: str, source: str) -> list[NewsItem]:
        items = []
        try:
            root = ElementTree.fromstring(xml_text)
            for item in root.iter("item"):
                title_el = item.find("title")
                pub_el = item.find("pubDate")
                if title_el is None or not title_el.text:
                    continue
                pub = ""
                if pub_el is not None and pub_el.text:
                    pub = pub_el.text[:22]
                items.append(NewsItem(
                    headline=title_el.text.strip()[:120],
                    source=source,
                    published_at=pub,
                    relevance=0.0,
                ))
        except ElementTree.ParseError:
            logger.debug("RSS XML parse error for %s", source)
        return items[:15]

    def _extract_keywords(self, question: str) -> list[str]:
        words = re.findall(r"[A-Za-z0-9$%]+", question.lower())
        keywords = [
            w for w in words
            if w not in _STOP_WORDS and len(w) > 2
        ]
        seen: set[str] = set()
        unique: list[str] = []
        for k in keywords:
            if k not in seen:
                seen.add(k)
                unique.append(k)
        return unique[:8]

    def _score_relevance(self, headline: str, keywords: list[str]) -> float:
        if not keywords:
            return 0.0
        headline_lower = headline.lower()
        matches = sum(1 for k in keywords if k in headline_lower)
        return matches / len(keywords)

    def _resolve_category(self, category: str, question: str) -> str:
        cat = category.lower()
        q = question.lower()
        if any(w in cat or w in q for w in ("crypto", "bitcoin", "btc", "ethereum", "eth", "token", "defi")):
            return "crypto"
        if any(w in cat or w in q for w in ("politi", "election", "president", "vote", "congress", "trump", "biden")):
            return "politics"
        if any(w in cat or w in q for w in ("sport", "nba", "nfl", "soccer", "football", "game", "match", "team")):
            return "sports"
        return "general"

    def _can_use_newsapi(self) -> bool:
        from datetime import date
        today = date.today().isoformat()
        if today != self._newsapi_date:
            self._newsapi_calls_today = 0
            self._newsapi_date = today
        return self._newsapi_calls_today < 90
