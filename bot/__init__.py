"""Legacy / experimental codebase — NOT the canonical bot.

DEPRECATED — kept for the Claude-oracle prototypes and the async data
feeds; not the production trading path.  The canonical bot lives under
``src/`` and is the only module:

* exposed by ``run_bot.sh`` / ``run_bot.bat`` / ``Dockerfile`` (``python -m src.main``),
* covered by the systemic safety guards (paper/live triple gate, daily-
  and drawdown circuit breakers, paper-friction accounting),
* observed by the FastAPI dashboard in ``dashboard/`` and the Prometheus
  ``/metrics`` endpoint.

Status of this package:

* Strategies under ``bot/strategies/`` (probability-arb, logical-arb,
  market-making) require an ANTHROPIC_API_KEY; useful for research, not
  for the default paper-trading flow.
* Risk manager under ``bot/core/risk_manager.py`` has its own drawdown
  logic that ``src/risk/manager.py`` was brought into parity with — do
  not extend ``bot/`` going forward.

Do **not** add new modules here.  Improvements to risk, accounting,
execution, or strategies must land in ``src/``.

If you're looking at this file as a maintainer:
- audit lives in ``AUDIT_CURRENT_SYSTEM.md`` and ``CHANGELOG_AI.md``,
- the deprecation rationale is in ``CLAUDE.md`` (section "Codebase layout").
"""
