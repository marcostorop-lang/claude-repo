# Project Memory for Claude Code

## Project identity

This repository contains:

* an EXISTING Polymarket trading bot
* an EXISTING dashboard
  Both are already functioning.
  This is not a greenfield project.

## Codebase layout

* `src/` is the **canonical** trading bot.  All production guards
  (paper/live triple gate, daily + drawdown circuit breakers, paper-
  trade friction, reconciliation, alerting, Prometheus metrics) live
  here.  The `Dockerfile` and `run_bot.sh` target `python -m src.main`.
* `bot/` is **legacy / experimental** — kept for the Claude-oracle
  research strategies and the async data feeds; do **not** extend it.
  See `bot/__init__.py` for the deprecation note.  Fixes to risk,
  accounting, or execution must land in `src/`, not here.
* `dashboard/backend/` (FastAPI) + `dashboard/frontend/` (Next.js) is
  the **canonical** dashboard.  `generate_dashboard.js` is a deprecated
  static generator; the FastAPI `/api/*` and `/metrics` endpoints are
  the source of truth for any new visualisation.

## Permanent rules

* Never rebuild from scratch unless explicitly asked.
* Default mode is PAPER ONLY.
* ALLOW_LIVE_TRADING must remain false unless explicitly authorized in a future session.
* No real orders may ever be sent while live trading is disabled.
* Current priority is learning quality, not paper PnL.
* Prioritize observability, simulation realism, accounting correctness, dashboard truthfulness, and risk controls before strategy complexity.
* Treat Polymarket microstructure as core: spread, depth, partial fills, stale data, liquidity, timing, market eligibility, and correlated exposures matter.
* The dashboard must reflect backend truth.
* Any change touching execution, accounting, risk, or portfolio state must include tests or explicit verification.
* Prefer surgical, reversible changes.
* Move dangerous constants to config.
* Document assumptions near relevant code.

## Autonomy policy

* Work autonomously by default.
* Do not ask routine questions that can be answered by inspecting the repo, logs, configs, storage, tests, and dashboard.
* Use conservative assumptions and continue.
* Escalate only for true blockers, missing credentials/access, dangerous irreversible actions, or anything that could affect real-money trading.

## Improvement priorities

1. Prevent unsafe behavior
2. Improve logging and measurement
3. Improve paper-trading realism
4. Improve accounting and exposure correctness
5. Improve dashboard visibility and truthfulness
6. Improve experiment quality and traceability
7. Improve strategy logic only after the above are solid

## Reporting expectations

Report with:

1. Executive summary
2. What was inspected
3. Risks found
4. Changes made
5. Why they matter
6. What should be reviewed next
