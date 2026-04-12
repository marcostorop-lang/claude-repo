"""
Experiment runner.

Consumes a JSON manifest of experiments, runs each through the backtester
against the same price history, and writes a comparison report.

Manifest schema (minimal):

    {
      "name": "momentum_threshold_sweep",
      "experiments": [
        {
          "id": "mom_w3_t02",
          "strategy": "simple_momentum",
          "params": {
            "MOMENTUM_WINDOW": "3",
            "MOMENTUM_THRESHOLD": "0.02",
            "STOP_LOSS_PCT": "0.10",
            "TAKE_PROFIT_PCT": "0.15"
          }
        },
        ...
      ]
    }

Per experiment we:
- Override env vars to build a new Config
- Build the strategy
- Run the Backtester on the price histories loaded from SQLite
- Collect the summary stats

At the end we emit a markdown table + a JSON file with full details.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from src.backtest.engine import Backtester, load_price_histories_from_store
from src.config import Config
from src.storage.sqlite_store import SQLiteStore
from src.strategy.base import BaseStrategy
from src.strategy.composite import CompositeStrategy
from src.strategy.edge_based import EdgeBasedStrategy
from src.strategy.mean_reversion import MeanReversion
from src.strategy.simple_momentum import SimpleMomentum

logger = logging.getLogger(__name__)


def _build_strategy(cfg: Config, name: str) -> BaseStrategy:
    if name == "mean_reversion":
        return MeanReversion(cfg)
    if name == "composite":
        return CompositeStrategy(cfg)
    if name == "edge_based":
        # Backtests run without a live store; strategy degrades to pure edge.
        return EdgeBasedStrategy(cfg, store=None)
    return SimpleMomentum(cfg)


def run_manifest(manifest_path: str, _unused_cfg: Config, store: SQLiteStore) -> str:
    """Run every experiment in the manifest and return a markdown summary."""
    path = Path(manifest_path)
    if not path.exists():
        return f"Manifest not found: {manifest_path}"

    manifest = json.loads(path.read_text())
    experiments = manifest.get("experiments", [])
    if not experiments:
        return "Manifest has no experiments."

    # Load price histories once — every experiment replays the same data.
    histories = load_price_histories_from_store(store, min_points=3)
    if not histories:
        return (
            "No price history available for backtesting. "
            "Run the live bot for a while first so prices accumulate, then re-run."
        )

    results: list[dict] = []
    original_env = dict(os.environ)
    try:
        for exp in experiments:
            params = exp.get("params", {})
            # Apply overrides to env, rebuild Config, run backtest
            for k, v in params.items():
                os.environ[k] = str(v)
            cfg = Config()
            strategy_name = exp.get("strategy") or cfg.strategy
            strat = _build_strategy(cfg, strategy_name)
            bt = Backtester(cfg, strat, assumed_spread=float(exp.get("assumed_spread", 0.02)))
            report = bt.run(histories)
            results.append({
                "id": exp.get("id", strategy_name),
                "strategy": strategy_name,
                "params": params,
                "signals": report.signals_generated,
                "trades": report.trades_closed,
                "win_rate": report.win_rate,
                "avg_return_pct": report.avg_return_pct,
                "total_return_pct": report.total_return_pct,
                "max_drawdown_pct": report.max_drawdown_pct,
                "total_pnl": report.total_pnl,
            })
            # Reset env before the next experiment
            for k in list(params.keys()):
                if k in original_env:
                    os.environ[k] = original_env[k]
                else:
                    os.environ.pop(k, None)
    finally:
        # Hard restore
        os.environ.clear()
        os.environ.update(original_env)

    # Persist full results
    out_dir = Path("experiments/results")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / f"{manifest.get('name', 'run')}.json"
    out_json.write_text(json.dumps(results, indent=2))

    # Build a markdown comparison table
    lines = [
        f"# Experiment: {manifest.get('name', 'unnamed')}",
        "",
        f"Tokens replayed: {len(histories)}",
        "",
        "| id | strategy | signals | trades | win_rate | avg_ret | total_ret | max_dd | pnl |",
        "|----|----------|--------:|-------:|---------:|--------:|----------:|-------:|----:|",
    ]
    for r in results:
        lines.append(
            f"| {r['id']} | {r['strategy']} | {r['signals']} | {r['trades']} | "
            f"{r['win_rate']:.1%} | {r['avg_return_pct']:+.2%} | {r['total_return_pct']:+.2%} | "
            f"-{r['max_drawdown_pct']:.2%} | {r['total_pnl']:+.2f} |"
        )
    lines.append("")
    lines.append(f"Full results: `{out_json}`")
    return "\n".join(lines)
