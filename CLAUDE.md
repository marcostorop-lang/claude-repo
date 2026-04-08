# Project Memory for Claude Code

## Project identity
This repository contains:
- an EXISTING Polymarket trading bot
- an EXISTING dashboard
Both are already functioning.
This is not a greenfield project.

## Permanent project rules
- Never rebuild the project from scratch unless explicitly asked.
- Default mode is PAPER ONLY.
- ALLOW_LIVE_TRADING must remain false unless explicitly authorized in a future session.
- No real orders may ever be sent while live trading is disabled.
- In the current phase, improving learning quality is more important than improving paper PnL.
- Prioritize observability, auditability, simulation realism, accounting correctness, and risk controls before strategy complexity.
- Treat Polymarket market microstructure as a first-class concern: spread, depth, partial fills, stale data, liquidity, market eligibility, and correlated exposures matter.
- The dashboard must reflect backend truth, not assumptions.
- Any change touching execution, accounting, portfolio state, or risk rules must include tests or explicit verification steps.
- Prefer surgical, reversible changes over broad rewrites.
- Move dangerous constants and thresholds to config.
- Document important assumptions near the code that depends on them.

## Improvement priorities
1. Prevent unsafe behavior
2. Improve logging and measurement
3. Improve paper-trading realism
4. Improve accounting and exposure correctness
5. Improve dashboard visibility
6. Improve experiment quality and traceability
7. Improve strategy logic only after the above are solid

## Required engineering behavior
- Inspect relevant files before editing
- Explain why each important change is made
- Keep a clear audit trail of changes
- Do not hide uncertainty
- Do not treat paper performance as proof of real edge
- Reject complexity that does not improve robustness or evidence quality

## Reporting expectations
When working on this repo, report with:
1. Executive summary
2. What was inspected
3. Risks found
4. Changes made
5. Why they matter
6. What should be reviewed next
