# Experiment Framework

**Last updated**: 2026-04-08

---

## 1. Baseline Definition

The current baseline is defined as:
- Strategy: `simple_momentum` with MOMENTUM_WINDOW=5, MOMENTUM_THRESHOLD=0.03
- Risk: MAX_POSITION_SIZE=50, MAX_TOTAL_EXPOSURE=200, SL=10%, TP=20%
- Filters: MIN_VOLUME=1000, MIN_LIQUIDITY=500, MAX_MARKETS=20
- Execution: Paper fills at midpoint, no slippage

The baseline metrics (to be measured after N > 50 closed trades):
- Win rate
- Total PnL
- Spread-adjusted PnL
- Max drawdown
- Profit factor
- Average hold time
- Trades per day

---

## 2. Experiment Naming Convention

```
EXP-{YYYYMMDD}-{SEQ}-{SHORT_DESCRIPTION}
```

Examples:
- `EXP-20260408-001-spread-filter-enforcement`
- `EXP-20260415-002-mean-reversion-z-threshold-1.2`
- `EXP-20260422-003-slippage-half-spread`

---

## 3. Config Versioning

Every experiment must:
1. Record the full config snapshot at start time (store in DB or JSON file)
2. Tag trades with the experiment/config version
3. Allow comparison of metrics between config versions

Implementation: Add a `config_version` or `experiment_id` field to the trades table.

---

## 4. Logging Rules

For every experiment:
- Log the hypothesis, parameters, and expected outcome before starting
- Log the start time and config snapshot
- Continue collecting the standard per-trade and per-tick metrics
- Log the end time and summary metrics when concluding

---

## 5. Acceptance Criteria

An experiment is considered successful if ALL of:
- It improves at least one key metric (win rate, PnL, drawdown, realism)
- It does not degrade any safety metric
- It does not reduce observability or traceability
- The improvement is statistically meaningful (not noise)
- The improvement persists when spread cost is accounted for
- The sample size is sufficient (N > 30 trades minimum)

---

## 6. Rejection Criteria

An experiment must be rejected if ANY of:
- It weakens a safety gate or risk control
- It reduces logging or traceability
- It improves paper PnL but wouldn't survive realistic execution
- The improvement is within noise (< 1 standard error)
- It adds complexity without measurable benefit
- It creates a dependency on specific market conditions

---

## 7. Rollback Rules

- Every code change for an experiment must be in a separate commit
- Config changes must be documented in CHANGELOG_AI.md
- If an experiment is rejected, revert the commit
- If an experiment causes unexpected behavior, revert immediately
- Keep the experiment data — even failed experiments generate learning

---

## 8. Experiment Template

```markdown
## EXP-{DATE}-{SEQ}-{NAME}

### Hypothesis
{What do you expect to happen and why?}

### Parameters Changed
{List exact config or code changes}

### Expected Benefit
{Which metric should improve and by how much?}

### Success Metric
{Exact threshold for success}

### Failure Metric
{Exact threshold for failure / rollback}

### Duration
{Minimum time / trades before evaluation}

### Results
{Filled in after experiment concludes}

### Decision
{ACCEPT / REJECT / EXTEND}
```
