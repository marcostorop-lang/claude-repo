# Learning Phase Plan

**Last updated**: 2026-04-08
**Phase**: Paper Trading / Data Collection
**Target duration**: Minimum 4 weeks before any live consideration

---

## 1. Data to Collect During Next Weeks

### Per-Tick Data
- Markets scanned (total count)
- Markets passing filters (count + list)
- Markets rejected and rejection reasons
- Spread at decision time for each evaluated market
- Depth at decision time (if available)
- Strategy signals generated (action, confidence, reason)
- Signals rejected by risk manager (count + reasons)
- Time spent per tick

### Per-Trade Data
- Entry price, spread at entry, midpoint at entry
- Strategy name and parameters at time of trade
- Signal confidence at entry
- Exit price, spread at exit, exit reason (SL/TP/strategy/manual)
- Hold duration
- Simulated slippage (half-spread cost)
- Market metadata (question, category, time-to-resolution)
- PnL (raw and slippage-adjusted)

### Daily Aggregates
- Total markets scanned
- Total signals generated vs trades executed
- Total trades opened / closed
- Realized PnL (raw and adjusted)
- Unrealized PnL
- Maximum drawdown
- Exposure utilization (avg / peak)
- Win rate (rolling 7-day)
- Average hold time

---

## 2. Weekly Review Checklist

Each week, review and document:

- [ ] **Total trades**: Is the bot trading? Too much? Too little?
- [ ] **Win rate**: Is it above 50%? Trending up or down?
- [ ] **PnL**: Positive? After spread adjustment? Trend?
- [ ] **Max drawdown**: Within tolerance? Growing?
- [ ] **Average hold time**: Reasonable for prediction markets?
- [ ] **Market selection**: Are we trading diverse markets or concentrated?
- [ ] **Spread cost**: How much would spread cost erode paper PnL?
- [ ] **Signal quality**: Is confidence correlated with outcome?
- [ ] **Filter effectiveness**: Are rejections preventing bad trades?
- [ ] **Strategy comparison**: Which strategy is performing better? Why?
- [ ] **Anomalies**: Any unexpected behavior in logs?
- [ ] **Data quality**: Any gaps in price history or trade records?

---

## 3. Evidence-Quality Criteria

### Minimum Thresholds for Confidence
- N > 50 closed trades before drawing any conclusions
- N > 100 closed trades before considering parameter changes
- Win rate confidence interval must exclude 50% at 95% level
- PnL must be positive after deducting 2x estimated spread cost
- Edge must persist across multiple market categories
- Performance must be stable (no single lucky trade driving results)

### Red Flags That Invalidate Learning
- Paper PnL driven by 1-2 large trades
- Win rate degrades as sample size grows
- Most PnL comes from markets near resolution (information already priced in)
- Edge disappears when spread cost is applied
- Bot trades almost exclusively one market or category
- Strategy only works in one direction (only BUY or only SELL)

---

## 4. Encouraging Patterns

- Consistent small wins across many markets
- Edge persists after accounting for spread
- Win rate improves with higher confidence signals
- Risk controls are preventing losses (SL triggers before catastrophic loss)
- Both strategies contribute positive PnL
- Markets selected are diverse (different categories, different timeframes)

---

## 5. Invalidating Patterns

- Persistent negative PnL after spread adjustment
- Win rate below 50% at N > 100
- All profits come from unrealized positions
- Maximum drawdown exceeds 20% of starting balance
- Bot trades too infrequently to generate meaningful data
- Signal confidence is uncorrelated with outcomes
- Most trades are in same market/category (concentration, not edge)
