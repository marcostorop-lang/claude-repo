"""
First-N live trades autopause gate.

When crossing from paper to live, the highest-value thing an operator
can do is *verify the first few fills by hand*.  Did the order that
the bot logged actually show up on-chain at the right price?  Did the
portfolio state converge to the wallet balance?  Did alerts fire in
Slack / email as expected?  Every one of those answers is cheap to
check on trade #1 and impossible to reconstruct on trade #100 once
hundreds of inflight legs have entangled.

This gate enforces a hand-brake: after ``N`` live BUY fills, BUYs are
refused until the operator drops an ack file into the working
directory.  The ack file is the single atomic "I've checked the first
fills and I'm satisfied" signal — no env var toggles, no SIGHUPs, no
editable config inside a running process.

Paper mode is a no-op.  Threshold ``0`` disables the gate entirely
(back-compat).  SELLs are *never* gated — once a live position is
open the operator must always be able to close it; an autopaused bot
that can't exit is a bot that accumulates risk it can no longer drain.

This module is pure-data: the gate counts live BUY fills and consults
a file path.  The bot wiring records fills (one line in ``main.run``)
and inserts a gate check into ``RiskManager``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class AutopauseVerdict:
    allowed: bool
    reason: str


class LiveTradeAutopauseGate:
    """Block BUYs once ``threshold`` live fills have executed.

    Count is seeded from storage at startup (survives restarts) and
    incremented per live BUY fill thereafter.  Unblocks when the
    operator touches ``ack_file`` — acknowledging the first-N sample
    by hand.
    """

    def __init__(
        self,
        *,
        threshold: int,
        ack_file: str,
        initial_count: int = 0,
    ) -> None:
        self._threshold = max(0, int(threshold))
        self._ack_file = ack_file or ""
        self._count = max(0, int(initial_count))

    @property
    def enabled(self) -> bool:
        return self._threshold > 0

    @property
    def count(self) -> int:
        return self._count

    @property
    def threshold(self) -> int:
        return self._threshold

    def record_live_fill(self, side: str) -> None:
        """Call after each successful live execution.

        SELLs do not count toward the threshold — the point of the
        gate is to verify *entries* by hand before the bot opens more
        of them.  SELLs are exits against already-verified positions.
        """
        if not self.enabled:
            return
        if (side or "").upper() == "BUY":
            self._count += 1

    def check_buy_allowed(self) -> AutopauseVerdict:
        """Decide whether the next live BUY is permitted."""
        if not self.enabled:
            return AutopauseVerdict(True, "autopause disabled")
        if self._count < self._threshold:
            return AutopauseVerdict(
                True,
                f"autopause: {self._count}/{self._threshold} live BUYs executed",
            )
        if self._ack_file and os.path.exists(self._ack_file):
            return AutopauseVerdict(
                True,
                f"autopause: threshold {self._threshold} reached, acknowledged",
            )
        return AutopauseVerdict(
            False,
            (
                f"autopause: {self._count} live BUY fill(s) already executed — "
                f"hand-verify them, then touch {self._ack_file!r} to resume."
            ),
        )
