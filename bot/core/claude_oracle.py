"""Claude Oracle — structured probability estimation via the Anthropic API.

Sends market context to Claude and parses a calibrated probability
estimate with confidence and reasoning.  Uses prompt caching on the
system prompt (saves ~90% tokens on repeated calls within the same
session).

All SDK calls are dispatched to a worker thread via ``asyncio.to_thread``
so the event loop isn't blocked by the sync Anthropic client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

import anthropic

from bot.config import cfg

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt — cached across calls
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a world-class superforecaster and prediction-market analyst.

Your task: estimate the TRUE probability that a given prediction-market question resolves YES.

CALIBRATION RULES — follow these exactly:
1. Base your estimate on publicly available information, base rates, expert consensus, and market structure.
2. Be well-calibrated: when you say 70%, events like that should happen ~70% of the time.
3. Account for known biases: favourite-longshot bias, recency bias, availability bias.
4. Consider time-to-resolution: events further away have more uncertainty → closer to 50%.
5. Consider market liquidity: thin markets are more likely to be mispriced.
6. Never anchor to the current market price — derive your estimate independently first.
7. Express genuine uncertainty through the confidence field (0.0–1.0).

OUTPUT FORMAT — respond with ONLY this JSON, no other text:
{
  "probability": <float 0.01–0.99>,
  "confidence": <float 0.0–1.0>,
  "reasoning": "<2-4 sentences explaining your estimate>",
  "key_factors": ["<factor1>", "<factor2>", "<factor3>"],
  "edge_direction": "<OVER or UNDER or FAIR>"
}

Rules for fields:
- probability: your independent fair-value estimate (before seeing market price).
- confidence: how confident you are in your own estimate (NOT in the outcome).
  0.3 = very uncertain, 0.6 = moderate confidence, 0.9 = very high confidence.
- edge_direction: OVER if you think the market overprices YES, UNDER if underprices, FAIR if within noise.

Be concise.  No hedging language.  Give a single point estimate."""


# ---------------------------------------------------------------------------
# Oracle data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OracleEstimate:
    """Parsed response from the Claude oracle."""

    probability: float
    confidence: float
    reasoning: str
    key_factors: list[str]
    edge_direction: str  # OVER | UNDER | FAIR

    @property
    def edge_vs(self) -> float:
        """Signed edge: positive means our estimate > 0.5 direction."""
        return 0.0  # caller computes edge vs market price


@dataclass(frozen=True)
class LogicalRelation:
    """A detected logical constraint between markets."""

    market_a_id: str
    market_b_id: str
    relation: str  # "implies", "excludes", "complements", "sums_to_one"
    explanation: str
    constraint_violation: float  # how much the prices violate the constraint
    confidence: float


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ClaudeOracle:
    """Async wrapper around the Anthropic SDK for probability estimation."""

    def __init__(self) -> None:
        if not cfg.anthropic_api_key:
            logger.warning("ANTHROPIC_API_KEY not set — oracle calls will fail.")
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    async def estimate_probability(
        self,
        question: str,
        description: str = "",
        current_price: float | None = None,
        volume: float = 0.0,
        liquidity: float = 0.0,
        category: str = "",
        end_date: str = "",
        book_summary: str = "",
    ) -> OracleEstimate | None:
        """Ask Claude for a calibrated probability estimate.

        Returns None on parse failure or API error.
        """
        context_parts = [f"QUESTION: {question}"]
        if description:
            context_parts.append(f"DESCRIPTION: {description[:400]}")
        if category:
            context_parts.append(f"CATEGORY: {category}")
        if end_date:
            context_parts.append(f"RESOLUTION DATE: {end_date}")
        if volume > 0:
            context_parts.append(f"VOLUME: ${volume:,.0f}")
        if liquidity > 0:
            context_parts.append(f"LIQUIDITY: ${liquidity:,.0f}")
        if book_summary:
            context_parts.append(f"ORDERBOOK: {book_summary}")
        if current_price is not None:
            context_parts.append(
                f"CURRENT MARKET PRICE (YES): {current_price:.4f} "
                f"(show this AFTER forming your independent estimate)"
            )

        user_msg = "\n".join(context_parts)

        try:
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=cfg.claude_model,
                max_tokens=cfg.claude_max_tokens,
                temperature=cfg.claude_temperature,
                system=[{
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_msg}],
            )

            raw = resp.content[0].text.strip()
            return _parse_estimate(raw)

        except anthropic.RateLimitError:
            logger.warning("Claude rate-limited — backing off.")
        except anthropic.APIError as exc:
            logger.error("Claude API error: %s", exc)
        except Exception:
            logger.exception("Oracle estimate failed.")
        return None

    async def detect_logical_relations(
        self,
        markets: list[dict],
    ) -> list[LogicalRelation]:
        """Ask Claude to find logical constraints between a set of markets.

        ``markets`` is a list of dicts with keys:
        condition_id, question, outcomes, outcome_prices.
        """
        if len(markets) < 2:
            return []

        market_text = "\n".join(
            f"[{i+1}] {m['question']} | YES={m['outcome_prices'][0]:.2f} | id={m['condition_id'][:16]}"
            for i, m in enumerate(markets)
            if m.get("outcome_prices")
        )

        user_msg = f"""Analyze these prediction markets for LOGICAL relationships:

{market_text}

Find ANY of these relationship types:
1. IMPLIES: If A is true then B must be true (A → B)
2. EXCLUDES: A and B cannot both be true (mutually exclusive)
3. COMPLEMENTS: A and B are complementary outcomes of the same event
4. SUMS_TO_ONE: A group of outcomes that must sum to 100%

For each relationship found, check if current prices VIOLATE the constraint.

Respond with ONLY this JSON array (empty array if no relationships found):
[
  {{
    "market_a_index": <1-based index>,
    "market_b_index": <1-based index>,
    "relation": "<implies|excludes|complements|sums_to_one>",
    "explanation": "<1 sentence>",
    "constraint_violation": <float: how much prices violate in absolute terms, 0 if consistent>,
    "confidence": <float 0-1: how confident you are in this relationship>
  }}
]"""

        try:
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=cfg.claude_model,
                max_tokens=cfg.claude_max_tokens,
                temperature=0.1,
                system=[{
                    "type": "text",
                    "text": (
                        "You are a logic and probability expert. "
                        "Identify logical constraints between prediction markets. "
                        "Only report relationships you are confident about. "
                        "Respond with ONLY valid JSON."
                    ),
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_msg}],
            )

            raw = resp.content[0].text.strip()
            return _parse_relations(raw, markets)

        except Exception:
            logger.exception("Logical relation detection failed.")
        return []

    async def assess_volatility(
        self,
        question: str,
        description: str = "",
    ) -> float:
        """Return a volatility multiplier (0.5–3.0) for market-making spread.

        Higher = wider spread needed.
        """
        try:
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=cfg.claude_model,
                max_tokens=256,
                temperature=0.1,
                system=[{
                    "type": "text",
                    "text": (
                        "You assess prediction market volatility. "
                        "Return ONLY a JSON object: "
                        '{\"volatility_multiplier\": <float 0.5-3.0>, '
                        '\"reason\": \"<10 words>\"}'
                    ),
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": f"Market: {question}\n{description[:200]}"}],
            )
            raw = resp.content[0].text.strip()
            parsed = _extract_json(raw)
            if parsed and "volatility_multiplier" in parsed:
                return max(0.5, min(3.0, float(parsed["volatility_multiplier"])))
        except Exception:
            logger.debug("Volatility assessment failed.", exc_info=True)
        return 1.0


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict | list | None:
    """Extract the first JSON object or array from text."""
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try to find JSON in the text
    for pattern in [r"\{[^{}]*\}", r"\[[\s\S]*\]"]:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                continue
    # Last resort: find nested JSON
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    break
    return None


def _parse_estimate(raw: str) -> OracleEstimate | None:
    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        logger.warning("Oracle returned non-dict: %s", raw[:200])
        return None

    try:
        prob = float(parsed["probability"])
        conf = float(parsed.get("confidence", 0.5))
        prob = max(0.01, min(0.99, prob))
        conf = max(0.0, min(1.0, conf))
        return OracleEstimate(
            probability=prob,
            confidence=conf,
            reasoning=str(parsed.get("reasoning", "")),
            key_factors=parsed.get("key_factors", []),
            edge_direction=str(parsed.get("edge_direction", "FAIR")).upper(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Failed to parse oracle estimate: %s — %s", exc, raw[:200])
        return None


def _parse_relations(raw: str, markets: list[dict]) -> list[LogicalRelation]:
    parsed = _extract_json(raw)
    if not isinstance(parsed, list):
        return []

    results: list[LogicalRelation] = []
    for item in parsed:
        try:
            idx_a = int(item["market_a_index"]) - 1
            idx_b = int(item["market_b_index"]) - 1
            if idx_a < 0 or idx_a >= len(markets) or idx_b < 0 or idx_b >= len(markets):
                continue
            results.append(LogicalRelation(
                market_a_id=markets[idx_a]["condition_id"],
                market_b_id=markets[idx_b]["condition_id"],
                relation=str(item.get("relation", "")).lower(),
                explanation=str(item.get("explanation", "")),
                constraint_violation=float(item.get("constraint_violation", 0)),
                confidence=float(item.get("confidence", 0)),
            ))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return results
