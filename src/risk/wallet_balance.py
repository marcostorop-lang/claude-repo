"""
Live wallet balance provider — gates orders by *actual* USDC available.

The bot's exposure caps are derived from operator-configured constants.
Those constants assume the wallet is funded above them.  In live mode,
that assumption can quietly break:

* Operator forgot to top up after a withdrawal.
* USDC was bridged out by a separate process.
* MAX_TOTAL_EXPOSURE was raised mid-flight without funding to match.

Without a wallet check the bot will keep submitting orders that fail
at the SDK layer with cryptic INSUFFICIENT_BALANCE errors, possibly
fragmenting partial fills and leaving the portfolio in a confused
state.  This module fronts the wallet with a small cached balance
provider that the risk manager consults at trade time:

    can_afford = provider.check_can_afford(cost_usd)

Cache: the live balance is refreshed at most once per
``refresh_seconds`` (default 60s) — most ticks reuse the cached value,
so the per-tick cost is one cheap dict lookup.

Fail-safe posture: when the SDK isn't installed, the provider returns
``available=None`` and ``check_can_afford`` allows the trade through
with a debug log.  We never want the wallet probe to be the reason
the bot stops trading — preflight catches the misconfiguration cleanly,
the runtime check is the secondary belt-and-braces line.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class AffordVerdict:
    allowed: bool
    available_usd: float | None
    reason: str


class WalletBalanceProvider:
    """Cached USDC balance with a pluggable fetch function.

    The fetch callable returns ``float`` USDC available, or ``None``
    if the balance can't be read (SDK missing, RPC down, etc.).
    """

    def __init__(
        self,
        fetch_balance: Callable[[], float | None],
        *,
        refresh_seconds: float = 60.0,
        min_buffer_usd: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch_balance
        self._refresh_s = max(1.0, float(refresh_seconds))
        self._buffer = max(0.0, float(min_buffer_usd))
        self._clock = clock
        self._cached: float | None = None
        self._last_fetch_ts: float = -1e9

    # ------------------------------------------------------------------
    # cache management
    # ------------------------------------------------------------------

    def get_balance(self, *, force_refresh: bool = False) -> float | None:
        """Return current USDC balance, refreshing from upstream if stale."""
        now = self._clock()
        if force_refresh or (now - self._last_fetch_ts) >= self._refresh_s:
            try:
                self._cached = self._fetch()
            except Exception as exc:
                logger.debug("Wallet balance fetch raised: %s", exc, exc_info=True)
                # Keep the previous cached value rather than zeroing it,
                # so a transient RPC blip doesn't immediately gate trades.
            self._last_fetch_ts = now
        return self._cached

    def invalidate(self) -> None:
        """Force the next ``get_balance`` to re-fetch."""
        self._last_fetch_ts = -1e9

    # ------------------------------------------------------------------
    # affordability check
    # ------------------------------------------------------------------

    def check_can_afford(self, cost_usd: float) -> AffordVerdict:
        """Decide whether ``cost_usd`` fits in the wallet (minus buffer).

        Cold/unreadable balance → allow (fail-safe; preflight is the
        authoritative gate, this is a runtime sanity check).
        """
        bal = self.get_balance()
        if bal is None:
            return AffordVerdict(
                allowed=True, available_usd=None,
                reason="Wallet balance unreadable — allowing trade (preflight is authoritative).",
            )
        usable = max(0.0, bal - self._buffer)
        if cost_usd > usable:
            return AffordVerdict(
                allowed=False, available_usd=bal,
                reason=(
                    f"Insufficient wallet: need ${cost_usd:.2f}, "
                    f"available ${usable:.2f} "
                    f"(${bal:.2f} balance − ${self._buffer:.2f} buffer)."
                ),
            )
        return AffordVerdict(
            allowed=True, available_usd=bal,
            reason=f"Wallet OK: ${usable:.2f} available, need ${cost_usd:.2f}.",
        )


def build_clob_balance_fetcher(cfg) -> Optional[Callable[[], float | None]]:
    """Construct a fetcher that reads USDC balance via the CLOB SDK.

    Returns ``None`` if the SDK isn't installed or live credentials are
    missing — the caller treats that as "no provider" and skips the
    runtime wallet check.
    """
    if not cfg.is_live:
        return None
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
    except ImportError:
        logger.debug("py-clob-client not importable — no wallet balance fetcher.")
        return None
    if not cfg.private_key:
        return None

    def _fetch() -> float | None:
        try:
            client = ClobClient(
                cfg.clob_url, key=cfg.private_key, chain_id=cfg.chain_id,
            )
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = client.get_balance_allowance(params)
            raw = result.get("balance") if isinstance(result, dict) else None
            if raw is None:
                return None
            return float(raw) / 1_000_000.0
        except Exception:
            logger.exception("CLOB get_balance_allowance failed.")
            return None

    return _fetch
