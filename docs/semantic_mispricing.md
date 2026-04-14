# Semantic Mispricing Engine

> **Status:** opt-in observer / strategy. Disabled by default. Paper-only.
> No live orders are emitted while `ALLOW_LIVE_TRADING=false`, which is the
> permanent default per `CLAUDE.md`.

## What it does

The semantic engine detects **cross-market relationships** on Polymarket and
derives a **synthetic fair price** for each target token from its siblings.
When the executable price (best_bid / best_ask) is materially cheaper or
richer than that synthetic fair value — after fees, safety margin and spread
cost — it emits a `SemanticMispricing` with a confidence score.

Two consumers exist:

1. **Observer** (always available when the engine is enabled): `scan_and_record`
   writes each detection into the `semantic_signals` SQLite table — a shadow
   log used to measure signal quality before trusting the strategy. This
   runs as a gated block inside the tick loop and never perturbs the
   existing strategy.
2. **Strategy adapter**: `SemanticMispricingStrategy` subscribes to the
   detections via `set_semantic_context` and returns BUY / SELL `Signal`
   objects that the standard pipeline (risk manager, executor, portfolio
   tracker) consumes unchanged.

## How relations are classified

Source: `src/analysis/semantic_engine/relations.py`

| Kind | Confidence floor | How it fires |
| ---- | ---------------- | ------------ |
| `INVERSE_OUTCOME` (Yes/No structural) | 0.99 | Same `condition_id`, outcomes are `{yes, no}` |
| `NEG_RISK_LINKED` | 0.95 | Same `condition_id`, multi-outcome event |
| `EQUIVALENT` (textual) | 0.90 | `fuzzy ≥ 0.92` **and** `jaccard ≥ 0.60`, same category |
| `NEAR_EQUIVALENT` (primary) | 0.80 | `fuzzy ≥ 0.80`, `jaccard ≥ 0.50`, `entity_overlap ≥ 0.30` |
| `NEAR_EQUIVALENT` (entity-rescue) | 0.70 | `fuzzy ≥ 0.70`, `jaccard ≥ 0.55`, `entity_overlap ≥ 0.90` |
| `INVERSE_OUTCOME` (cross-condition) | 0.70 | `entity_overlap ≥ 0.50`, `jaccard ≥ 0.40`, polarity flip |
| `TEMPORAL_CHECKPOINT` | 0.30–0.75 | `jaccard ≥ 0.60`, different end dates within 1–365 days |

Structural relations are derived from `condition_id` groupings supplied
directly by the exchange — they are essentially free and near-certain.
Textual and temporal relations use stdlib-only heuristics
(`difflib.SequenceMatcher` for fuzzy, Jaccard over normalized tokens,
capitalized-run entity extraction with sentence-leader stripping, ISO date
diffs). **No LLM, no new dependencies.**

Defaults are deliberately strict — when in doubt the classifier returns
`UNKNOWN`. A phantom relation is worse than no relation at all.

## How synthetic fair price is computed

Source: `src/analysis/semantic_engine/synthetic_price.py`

Precedence: `structural_complement` > `weighted_avg_equivalent` > `temporal_range`.

* **Structural complement** (binary Yes/No, neg-risk, mutually exclusive):

  ```
  fair = clamp(1 − Σ sibling_price, 0.0, 1.0)
  ```

  Confidence starts at the minimum sibling relation confidence and gets a
  small bonus for more contributors. Band is narrow (~1-cent half-width).

* **Weighted-average equivalent / near-equivalent**: weighted mean of
  sibling prices with `weight = relation_confidence × log1p(liquidity)`.
  Dispersion across contributors punishes confidence; a single sibling
  penalty applies.

* **Temporal range**: emits `is_range_only=True` with
  `[lower, upper] = [min_sibling, max_sibling]`, confidence capped at 0.60.
  The scorer treats range-only estimates as a ≤0.50 score cap.

## How mispricings become signals

Source: `src/analysis/semantic_engine/scoring.py`

1. **Direction**: `BUY` when `fair > best_ask`, `SELL` when `fair < best_bid`,
   otherwise `NONE`.
2. **Gross edge**: `|fair − exec_price|`, using `best_ask` for BUY and
   `best_bid` for SELL (executable price, not midpoint).
3. **Costs**: `taker_fee_bps + safety_margin_bps + 0.25 × spread` (bps).
4. **Net edge**: `gross_edge − total_cost`. Signals fire only when
   `net_edge ≥ min_net_edge`.
5. **Score**: weighted blend of magnitude (30%), confidence (30%),
   contributor count (15%), spread penalty (15%), liquidity (10%),
   with hard floors: wide spread capped at 0.40, thin book capped at 0.40,
   range-only capped at 0.50. Signals fire only when
   `score ≥ min_signal_score`.
6. **Maker hint**: when `prefer_maker=True`, the scorer assumes the passive
   side (best_bid for BUY, best_ask for SELL) and skips the taker fee.

The `SemanticMispricingStrategy` adapter takes the per-token detection
published via `set_semantic_context`, re-checks the score / net-edge gates
against current `Config`, and emits a `Signal` whose
`features["edge"]` is signed by direction so the risk manager's existing
Kelly sizing and `min_edge_for_trade` gate keep working without changes.

## Activation

All flags are **off by default**. Add any of the following to your `.env`:

| Flag | Default | Purpose |
| ---- | ------- | ------- |
| `SEMANTIC_ENGINE_ENABLED` | `false` | Master switch for the observer and strategy |
| `SEMANTIC_ENGINE_MODE` | `shadow` | `disabled` \| `shadow` \| `live` |
| `STRATEGY` | `simple_momentum` | Set to `semantic_mispricing` to use the adapter as the active strategy |
| `SEMANTIC_MIN_RELATION_CONFIDENCE` | `0.65` | Drop relations below this floor |
| `SEMANTIC_MIN_NET_EDGE` | `0.02` | Minimum edge after costs (2 cents) |
| `SEMANTIC_MIN_SIGNAL_SCORE` | `0.60` | Minimum composite score |
| `SEMANTIC_MAX_SPREAD` | `0.05` | Reject wide books |
| `SEMANTIC_MIN_LIQUIDITY` | `500.0` | Soft liquidity floor for scoring |
| `SEMANTIC_MIN_SIBLING_LIQUIDITY` | `100.0` | Siblings below this are ignored during synth aggregation |
| `SEMANTIC_SAFETY_MARGIN_BPS` | `50.0` | Extra buffer baked into net edge |
| `SEMANTIC_MAX_RELATED_MARKETS` | `8` | Cap O(N²) text matching |
| `SEMANTIC_USE_NEG_RISK_LINKS` | `true` | Use multi-outcome siblings |
| `SEMANTIC_USE_TEMPORAL_LINKS` | `true` | Allow temporal range estimates |
| `SEMANTIC_USE_INVERSE_LINKS` | `true` | Allow cross-condition inverse detection |
| `SEMANTIC_USE_TEXTUAL_LINKS` | `true` | Allow textual equivalent / near-equivalent |
| `SEMANTIC_CALIBRATION_OVERLAY_ENABLED` | `false` | Reserved for future calibration step |

### Recommended ramp

1. Run with `SEMANTIC_ENGINE_ENABLED=true`, `SEMANTIC_ENGINE_MODE=shadow`,
   and the default momentum strategy. Inspect the `semantic_signals` table
   for a few days.
2. Once the detections look sane, switch `STRATEGY=semantic_mispricing`
   while `ALLOW_LIVE_TRADING=false`. Paper-trade for at least one cycle and
   compare realised edge vs. `net_edge` stored at detection time.
3. Only then would live activation be considered — and that still requires
   a separate explicit authorization per `CLAUDE.md`.

## Metrics to review before live activation

The `semantic_signals` table captures (per detection):

* `timestamp`, `token_id`, `condition_id`, `question`, `category`, `side`
* book at detection: `best_bid`, `best_ask`, `midpoint`, `spread`, `liquidity`
* synthetic: `synthetic_fair`, `synthetic_lower`, `synthetic_upper`,
  `synthetic_method`, `synthetic_confidence`, `contributors_n`
* edges: `gross_edge`, `net_edge`, `score`
* `relations_json`: which siblings were used and at what confidence
* `features_json`: scorer components for forensics
* `mode`: `shadow` or `live`

Before switching `SEMANTIC_ENGINE_MODE=live`, inspect:

1. Hit rate by `synthetic_method` (`structural_complement` is expected to
   dominate; temporal/textual should be sparse).
2. Distribution of `net_edge` at fill vs. at detection — check slippage.
3. Relation-kind mix — too many textual NEAR_EQUIVALENT hits is a red flag.
4. Correlation of score with realised PnL in paper runs.

### Dashboard surface

The static dashboard (`generate_dashboard.js` → `docs/dashboard.html`) renders
a `Semantic Mispricing Engine` section whenever rows exist in
`semantic_signals` *or* the live bot exports a `semantic` block in
`bot_state.json` (engine currently enabled).  The section shows:

* engine state from config (enabled, mode, thresholds),
* aggregate counts (total detections, BUY/SELL split),
* averages (score, net edge, synthetic confidence),
* synthetic-method mix,
* the last 20 detections with market, method, bid/ask, fair, net edge,
  score, confidence and mode.

The same aggregate is produced by
`SQLiteStore.semantic_signals_summary()` — reuse that helper for any
CLI / alerting glue instead of re-querying the table.

## Limitations

* **No LLM**. Textual similarity is stdlib-only; genuinely paraphrased
  questions below the rescue thresholds will be missed.
* **Static categories**. Cross-category text matches are suppressed by
  default (`same_category_required=True`). A mis-tagged market can hide a
  real equivalence.
* **Snapshot-only**. Synthetic prices use current tick snapshots; there is
  no cross-tick smoothing. Stale siblings contaminate the fair value.
* **Liquidity floors are crude**. Depth-aware sizing is delegated to the
  existing risk manager; the scorer only applies hard floors.
* **Binary Yes/No overfits** the existing venue convention. If Polymarket
  ever changes outcome labelling, `classify_structural_pair` will
  degrade to `NEG_RISK_LINKED` (still correct, slightly less confident).

## File map

| Module | Purpose |
| ------ | ------- |
| `src/analysis/semantic_engine/types.py` | Dataclasses: `MarketRelation`, `SyntheticPrice`, `SemanticMispricing`, `RelationKind` |
| `src/analysis/semantic_engine/text_utils.py` | Stdlib text primitives (normalize / fuzzy / jaccard / entities / dates) |
| `src/analysis/semantic_engine/relations.py` | `RelationClassifierConfig`, structural + textual classifiers, `discover_relations`, `summarise_relations` |
| `src/analysis/semantic_engine/synthetic_price.py` | `estimate_fair_price` with precedence routing |
| `src/analysis/semantic_engine/scoring.py` | `ScoringConfig`, `score_mispricing` |
| `src/analysis/semantic_engine/engine.py` | `find_semantic_mispricings`, `scan_and_record`, `summarise` |
| `src/strategy/semantic_mispricing.py` | `SemanticMispricingStrategy` adapter |
| `src/storage/sqlite_store.py` | `semantic_signals` table + `insert_semantic_signals` / `get_recent_semantic_signals` |
| `src/main.py` | Gated observer in tick loop + factory branch for the strategy |
| `src/config.py` | All `SEMANTIC_*` env flags |
| `tests/test_semantic_*.py` | 90 tests covering text utils, relations, synthetic pricing, scoring, orchestration, strategy wiring |

## Regression guarantee

`tests/test_semantic_mispricing_strategy.py::TestNoRegressionWhenDisabled`
executes a full tick with `SEMANTIC_ENGINE_ENABLED=false` and the default
momentum strategy, asserting the legacy behaviour is byte-for-byte
unchanged. The observer is the *only* new work inside `_tick`, and it is
skipped entirely when the engine is disabled.
