"""
Promote-to-live validation harness.

Combines the walk-forward report (``src/backtest/walk_forward.py``)
with risk metrics (``src/analysis/risk_metrics.py``) into a single
binary verdict — **PROMOTE** if every threshold passes, **REJECT**
otherwise — together with a checklist of which conditions failed
and by how much.

Design choice: the verdict is a *conjunction* (AND) of independent
gates.  Any single failing gate flips the result to REJECT.  This is
intentional — passing 4 of 5 gates by luck is not a defensible
reason to bet real money.

This module is invoked from the CLI ``validate-strategy`` command.
It never runs from the trading loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from src.analysis.risk_metrics import (
    block_bootstrap_sharpe,
    deflated_sharpe_ratio,
    max_drawdown,
    probabilistic_sharpe_ratio,
    returns_summary,
)
from src.backtest.walk_forward import WalkForwardReport


@dataclass
class GateResult:
    name: str
    passed: bool
    observed: float
    threshold: float
    detail: str = ""


@dataclass
class ValidationReport:
    strategy: str
    promote: bool
    gates: list[GateResult] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def failing_gates(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed]

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "promote": self.promote,
            "gates": [g.__dict__ for g in self.gates],
            "summary": self.summary,
        }

    def pretty(self) -> str:
        lines = [
            f"Strategy:   {self.strategy}",
            f"Verdict:    {'PROMOTE' if self.promote else 'REJECT'}",
            "",
            "Gates:",
        ]
        for g in self.gates:
            mark = "OK " if g.passed else "FAIL"
            lines.append(
                f"  [{mark}] {g.name:<30} observed={g.observed:>10.4f}  "
                f"threshold={g.threshold:>10.4f}  {g.detail}"
            )
        if self.summary:
            lines.append("")
            lines.append("Summary:")
            for k, v in self.summary.items():
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)


def evaluate_validation(
    *,
    strategy_name: str,
    walk_forward: WalkForwardReport,
    cfg,
) -> ValidationReport:
    """Score a walk-forward run against the configured promote-to-live gates.

    Parameters
    ----------
    walk_forward :
        The OOS report produced by ``run_walk_forward``.
    cfg :
        The :class:`Config` whose ``validation_*`` fields define the
        thresholds.  All fields are read defensively via ``getattr``
        so an older Config still loads.
    """
    gates: list[GateResult] = []

    n_windows = walk_forward.n_windows
    total_trades = walk_forward.total_trades()
    pooled = walk_forward.pooled_returns

    # ----- Gate 1: enough closed trades to draw conclusions -----
    min_trades = int(getattr(cfg, "validation_min_trades", 200))
    gates.append(GateResult(
        name="trades >= min_trades",
        passed=total_trades >= min_trades,
        observed=float(total_trades),
        threshold=float(min_trades),
        detail="t-stats and bootstrap CIs are unstable below the floor",
    ))

    # ----- Gate 2: at least X fraction of OOS windows are profitable -----
    min_winning_frac = float(getattr(cfg, "validation_min_oos_winning_windows_frac", 0.6))
    winning = walk_forward.winning_windows()
    winning_frac = (winning / n_windows) if n_windows > 0 else 0.0
    gates.append(GateResult(
        name="OOS winning-window fraction",
        passed=winning_frac >= min_winning_frac and n_windows > 0,
        observed=winning_frac,
        threshold=min_winning_frac,
        detail=f"{winning}/{n_windows} windows with PnL>0",
    ))

    # ----- Gate 3: median OOS Sharpe -----
    min_med_sr = float(getattr(cfg, "validation_min_median_oos_sharpe", 0.5))
    median_sr = walk_forward.median_oos_sharpe()
    gates.append(GateResult(
        name="median OOS Sharpe",
        passed=median_sr >= min_med_sr,
        observed=median_sr,
        threshold=min_med_sr,
        detail="annualized; resilience across regimes",
    ))

    # ----- Gate 4: Probabilistic Sharpe Ratio -----
    min_psr = float(getattr(cfg, "validation_min_psr", 0.95))
    periods_per_year = int(getattr(cfg, "validation_periods_per_year", 252))
    psr = probabilistic_sharpe_ratio(
        pooled, benchmark_sharpe=0.0, periods_per_year=periods_per_year,
    )
    gates.append(GateResult(
        name="Probabilistic Sharpe Ratio",
        passed=psr >= min_psr,
        observed=psr,
        threshold=min_psr,
        detail="P(true SR > 0) corrected for skew/kurtosis",
    ))

    # ----- Gate 5: Deflated Sharpe Ratio -----
    n_trials = int(getattr(cfg, "validation_n_trials", 1))
    min_dsr = float(getattr(cfg, "validation_min_dsr", 0.95))
    # When n_trials = 1 the deflation is trivial and DSR == PSR vs. 0.
    # When >1 we use the cross-sectional Sharpe of the OOS windows as
    # an estimate of V[SR] under the null.
    sharpe_estimates = [w.sharpe_annualized for w in walk_forward.windows] or None
    dsr = deflated_sharpe_ratio(
        pooled,
        n_trials=n_trials,
        sharpe_estimates=sharpe_estimates,
        periods_per_year=periods_per_year,
    )
    gates.append(GateResult(
        name="Deflated Sharpe Ratio",
        passed=dsr >= min_dsr,
        observed=dsr,
        threshold=min_dsr,
        detail=f"corrected for n_trials={n_trials} multiple-testing",
    ))

    # ----- Gate 6: max drawdown of pooled OOS curve under threshold -----
    max_dd_thr = float(getattr(cfg, "validation_max_drawdown_pct", 0.15))
    pooled_max_dd = max_drawdown(pooled)
    gates.append(GateResult(
        name="pooled OOS max drawdown",
        passed=pooled_max_dd <= max_dd_thr,
        observed=pooled_max_dd,
        threshold=max_dd_thr,
        detail="cumulative-return DD on the concatenated OOS series",
    ))

    # ----- Gate 7: bootstrap CI lower bound for Sharpe > 0 -----
    boot = block_bootstrap_sharpe(
        pooled,
        n_resamples=500,
        seed=int(getattr(cfg, "validation_seed", 0)),
        confidence=0.95,
        periods_per_year=periods_per_year,
    )
    gates.append(GateResult(
        name="bootstrap 95% lower bound for Sharpe > 0",
        passed=boot.lower > 0.0,
        observed=boot.lower,
        threshold=0.0,
        detail=f"point={boot.point:.3f} block_size={boot.block_size}",
    ))

    promote = all(g.passed for g in gates)

    summary = {
        "n_windows": n_windows,
        "n_tokens": walk_forward.n_tokens,
        "total_trades": total_trades,
        "pooled_returns": len(pooled),
        "median_oos_sharpe": median_sr,
        "psr": psr,
        "dsr": dsr,
        "pooled_max_dd": pooled_max_dd,
        "bootstrap_ci_lower": boot.lower,
        "bootstrap_ci_upper": boot.upper,
    }
    return ValidationReport(
        strategy=strategy_name, promote=promote, gates=gates, summary=summary,
    )


def report_to_json(report: ValidationReport) -> str:
    return json.dumps(report.to_dict(), indent=2)
