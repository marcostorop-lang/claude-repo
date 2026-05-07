"""
Preflight checks — run before flipping the bot to live.

Designed to answer the operator's pre-launch questions in *one* command:

* Are the two-gate live flags coherent, or am I about to silently stay in paper?
* Is the SQLite DB writable (and not on a read-only mount)?
* Is the Polymarket Gamma + CLOB API reachable from this host?
* Are alerts wired (so a daily-loss-breach actually pages me)?
* Is the kill-switch file absent (so the bot can actually start)?
* Are my risk limits internally coherent?
* In live mode: is the wallet funded above the configured exposure cap?

The function below is pure-data: it takes a :class:`Config` and returns
a list of :class:`CheckResult` objects.  The CLI wrapper formats them
into a table and sets the process exit code (``0`` if no FAIL, ``1``
otherwise — WARNs are surfaced but do not block).

This is the thing to run *every time* before changing live flags.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.config import Config

logger = logging.getLogger(__name__)


# ----- result type -------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str       # "OK" | "WARN" | "FAIL"
    detail: str

    def is_blocking(self) -> bool:
        return self.status == "FAIL"


# ----- individual checks (each returns a CheckResult) --------------------


def _check_two_gate_live(cfg: Config) -> CheckResult:
    mode = cfg.trading_mode.lower()
    allow = cfg.allow_live_trading
    phrase_ok = cfg.i_understand_real_money == cfg.LIVE_CONFIRMATION_PHRASE
    if mode != "live":
        return CheckResult(
            "two_gate_live", "OK",
            f"PAPER mode (TRADING_MODE={cfg.trading_mode!r}); no live gates needed.",
        )
    if not allow:
        return CheckResult(
            "two_gate_live", "FAIL",
            "TRADING_MODE=live but ALLOW_LIVE_TRADING is false. "
            "Live orders will NOT be sent.",
        )
    if not phrase_ok:
        return CheckResult(
            "two_gate_live", "FAIL",
            "I_UNDERSTAND_REAL_MONEY does not equal "
            f"'{cfg.LIVE_CONFIRMATION_PHRASE}'. Bot will stay in paper.",
        )
    return CheckResult(
        "two_gate_live", "OK",
        "Both live gates confirmed. Bot WILL send real orders.",
    )


def _check_credentials_present(cfg: Config) -> CheckResult:
    if not cfg.is_live:
        return CheckResult(
            "credentials", "OK",
            "Paper mode — no credentials required.",
        )
    missing = []
    if not cfg.private_key:
        missing.append("PRIVATE_KEY")
    if not cfg.api_key:
        missing.append("POLY_API_KEY")
    if missing:
        return CheckResult(
            "credentials", "FAIL",
            f"Missing required live-mode env: {', '.join(missing)}.",
        )
    return CheckResult(
        "credentials", "OK", "PRIVATE_KEY and POLY_API_KEY set.",
    )


def _check_kill_switch_absent(cfg: Config) -> CheckResult:
    if Path(cfg.kill_switch_file).exists():
        return CheckResult(
            "kill_switch", "FAIL",
            f"Kill-switch file present at {cfg.kill_switch_file!r}. "
            "Bot will refuse to start. Remove it first.",
        )
    return CheckResult(
        "kill_switch", "OK",
        f"No kill-switch file at {cfg.kill_switch_file!r}.",
    )


def _check_db_writable(cfg: Config) -> CheckResult:
    """Verify the SQLite path is openable AND we can write a tx that rolls back."""
    import sqlite3
    db_path = cfg.sqlite_db_path
    parent = Path(db_path).parent or Path(".")
    if not parent.exists():
        return CheckResult(
            "db_writable", "FAIL",
            f"DB parent directory does not exist: {parent}.",
        )
    try:
        # Open + run a probe insert in a transaction we roll back so we
        # never persist test rows.  If the volume is read-only, this throws.
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _preflight_probe (id INTEGER, ts TEXT)"
        )
        conn.execute(
            "INSERT INTO _preflight_probe (id, ts) VALUES (?, ?)",
            (1, "probe"),
        )
        conn.rollback()
        conn.close()
        return CheckResult(
            "db_writable", "OK", f"SQLite at {db_path} is writable.",
        )
    except sqlite3.OperationalError as e:
        return CheckResult(
            "db_writable", "FAIL", f"SQLite at {db_path} is NOT writable: {e}",
        )
    except Exception as e:
        return CheckResult(
            "db_writable", "FAIL", f"SQLite probe raised {type(e).__name__}: {e}",
        )


def _check_risk_limits_coherent(cfg: Config) -> CheckResult:
    """Sanity-check the risk limits don't form a degenerate configuration."""
    issues = []
    if cfg.max_position_size <= 0:
        issues.append("MAX_POSITION_SIZE must be > 0")
    if cfg.max_total_exposure <= 0:
        issues.append("MAX_TOTAL_EXPOSURE must be > 0")
    if cfg.max_position_size > cfg.max_total_exposure:
        issues.append(
            f"MAX_POSITION_SIZE ${cfg.max_position_size:.2f} > "
            f"MAX_TOTAL_EXPOSURE ${cfg.max_total_exposure:.2f} — single "
            "position can blow the global cap."
        )
    if cfg.max_daily_loss <= 0:
        issues.append("MAX_DAILY_LOSS must be > 0")
    if cfg.max_daily_loss >= cfg.max_total_exposure:
        # Not strictly broken but suggests the breaker can never fire
        # before the exposure cap stops new entries — surface it.
        issues.append(
            f"MAX_DAILY_LOSS ${cfg.max_daily_loss:.2f} ≥ MAX_TOTAL_EXPOSURE "
            f"${cfg.max_total_exposure:.2f} — circuit breaker may not fire "
            "before exposure cap is reached.",
        )
    if cfg.min_price >= cfg.max_price:
        issues.append(
            f"MIN_PRICE {cfg.min_price:.2f} ≥ MAX_PRICE {cfg.max_price:.2f} — "
            "no price band is tradable.",
        )
    if not issues:
        return CheckResult(
            "risk_limits", "OK",
            f"max_pos=${cfg.max_position_size:.0f}, "
            f"max_total=${cfg.max_total_exposure:.0f}, "
            f"max_daily_loss=${cfg.max_daily_loss:.0f}, "
            f"price_band=[{cfg.min_price:.2f}, {cfg.max_price:.2f}].",
        )
    severity = "FAIL" if any("must" in i for i in issues) else "WARN"
    return CheckResult("risk_limits", severity, "; ".join(issues))


def _check_alerts_wired(cfg: Config) -> CheckResult:
    """In live mode warn loudly if no alert sinks are configured."""
    try:
        from src.utils.alerts import build_from_config
        alerts = build_from_config(cfg)
    except Exception as e:
        return CheckResult(
            "alerts", "FAIL",
            f"build_from_config raised {type(e).__name__}: {e}",
        )
    n_sinks = len(alerts.sinks) if alerts is not None else 0
    if cfg.is_live and n_sinks == 0:
        return CheckResult(
            "alerts", "WARN",
            "Live mode but no alert sinks configured. "
            "Daily-loss / VaR breaches will be silent. "
            "Set ALERTS_FILE or ALERTS_WEBHOOK_URL.",
        )
    if n_sinks == 0:
        return CheckResult(
            "alerts", "OK",
            "Paper mode without alert sinks (acceptable).",
        )
    return CheckResult(
        "alerts", "OK", f"{n_sinks} alert sink(s) wired.",
    )


def _check_backup_dir(cfg: Config) -> CheckResult:
    path = getattr(cfg, "db_backup_dir", "") or ""
    if not path:
        return CheckResult(
            "backup_dir", "OK",
            "DB_BACKUP_DIR not set (backups disabled — acceptable).",
        )
    p = Path(path)
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return CheckResult(
                "backup_dir", "FAIL",
                f"DB_BACKUP_DIR={path!r} cannot be created: {e}",
            )
    # Probe writability.
    try:
        with tempfile.NamedTemporaryFile(dir=str(p), delete=True):
            pass
    except OSError as e:
        return CheckResult(
            "backup_dir", "FAIL",
            f"DB_BACKUP_DIR={path!r} not writable: {e}",
        )
    return CheckResult(
        "backup_dir", "OK", f"{path} writable; backups every "
        f"{cfg.db_backup_interval_hours}h, keep {cfg.db_backup_keep}.",
    )


def _check_polymarket_reachable(
    cfg: Config,
    *,
    client_factory: Callable | None = None,
) -> CheckResult:
    """Quick smoke test that we can hit Gamma/CLOB.

    Uses the configured client factory by default; tests inject a stub
    so the check never has to make a real HTTP call.
    """
    try:
        if client_factory is None:
            from src.polymarket.client import PolymarketClient
            client = PolymarketClient(cfg)
        else:
            client = client_factory(cfg)
        markets = client.get_active_markets(limit=1)
    except Exception as e:
        return CheckResult(
            "polymarket_api", "FAIL",
            f"Polymarket API unreachable: {type(e).__name__}: {e}",
        )
    if not markets:
        return CheckResult(
            "polymarket_api", "WARN",
            "Polymarket API reachable but returned 0 active markets — "
            "throttled, schema change, or off-hours?",
        )
    return CheckResult(
        "polymarket_api", "OK",
        f"Reachable; sample market: '{markets[0].get('question', '<no question>')[:60]}'.",
    )


def _check_wallet_balance(
    cfg: Config,
    *,
    balance_fetcher: Callable[[Config], float | None] | None = None,
) -> CheckResult:
    """In live mode warn if wallet balance < MAX_TOTAL_EXPOSURE.

    Skipped (OK) in paper mode.  ``balance_fetcher`` is injectable so
    tests don't need a wallet — when None we attempt the CLOB SDK call
    and treat any failure as a WARN (we don't want preflight to FAIL
    just because the SDK API surface changed).
    """
    if not cfg.is_live:
        return CheckResult(
            "wallet_balance", "OK",
            "Paper mode — wallet balance check skipped.",
        )
    try:
        if balance_fetcher is not None:
            usdc = balance_fetcher(cfg)
        else:
            usdc = _try_fetch_usdc_balance(cfg)
    except Exception as e:
        return CheckResult(
            "wallet_balance", "WARN",
            f"Wallet balance probe raised {type(e).__name__}: {e}. "
            "Verify funding manually before flipping live.",
        )
    if usdc is None:
        return CheckResult(
            "wallet_balance", "WARN",
            "Could not read wallet balance via CLOB SDK. "
            "Verify funding manually.",
        )
    if usdc < cfg.max_total_exposure:
        return CheckResult(
            "wallet_balance", "FAIL",
            f"Wallet balance ${usdc:.2f} < MAX_TOTAL_EXPOSURE "
            f"${cfg.max_total_exposure:.2f}. Fund wallet or lower the cap.",
        )
    return CheckResult(
        "wallet_balance", "OK",
        f"Wallet ${usdc:.2f} ≥ MAX_TOTAL_EXPOSURE ${cfg.max_total_exposure:.2f}.",
    )


def _try_fetch_usdc_balance(cfg: Config) -> float | None:
    """Best-effort USDC balance via py-clob-client.

    Returns None if the SDK isn't installed or the call surface differs
    — the caller treats that as a WARN, not a FAIL.
    """
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    except ImportError:
        return None
    try:
        client = ClobClient(
            cfg.clob_url, key=cfg.private_key, chain_id=cfg.chain_id,
        )
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        result = client.get_balance_allowance(params)
        # SDK returns the value in 1e6 USDC base units.
        raw = result.get("balance") if isinstance(result, dict) else None
        if raw is None:
            return None
        return float(raw) / 1_000_000.0
    except (AttributeError, TypeError, ValueError, OSError):
        # SDK call surface mismatch / network issue / unparseable balance.
        # Caller treats None as "skip the wallet check, WARN not FAIL."
        return None


# ----- orchestrator ------------------------------------------------------


def run_preflight(
    cfg: Config,
    *,
    skip_network: bool = False,
    client_factory: Callable | None = None,
    balance_fetcher: Callable[[Config], float | None] | None = None,
) -> list[CheckResult]:
    """Run every preflight check and return the results in display order.

    ``skip_network=True`` skips the Polymarket reachability + wallet
    balance probes — useful in CI where outbound HTTPS is blocked.
    """
    checks: list[CheckResult] = [
        _check_two_gate_live(cfg),
        _check_credentials_present(cfg),
        _check_kill_switch_absent(cfg),
        _check_db_writable(cfg),
        _check_risk_limits_coherent(cfg),
        _check_alerts_wired(cfg),
        _check_backup_dir(cfg),
    ]
    if not skip_network:
        checks.append(_check_polymarket_reachable(
            cfg, client_factory=client_factory,
        ))
        checks.append(_check_wallet_balance(
            cfg, balance_fetcher=balance_fetcher,
        ))
    return checks


def format_results(results: list[CheckResult]) -> str:
    """Render results as a fixed-width plaintext table for the CLI."""
    if not results:
        return "(no checks ran)"
    name_w = max(len(r.name) for r in results)
    status_w = 4
    lines = []
    sep = "─" * (name_w + status_w + 80)
    lines.append(sep)
    lines.append(
        f"{'CHECK':<{name_w}}  {'STATUS':<{status_w}}  DETAIL"
    )
    lines.append(sep)
    for r in results:
        lines.append(f"{r.name:<{name_w}}  {r.status:<{status_w}}  {r.detail}")
    lines.append(sep)
    fail = sum(1 for r in results if r.status == "FAIL")
    warn = sum(1 for r in results if r.status == "WARN")
    ok = sum(1 for r in results if r.status == "OK")
    lines.append(
        f"Summary: {ok} OK, {warn} WARN, {fail} FAIL "
        f"(of {len(results)} checks)"
    )
    if fail:
        lines.append("VERDICT: BLOCKED — fix FAIL items before going live.")
    elif warn:
        lines.append("VERDICT: PROCEED WITH CAUTION — review WARN items.")
    else:
        lines.append("VERDICT: GREEN — all checks passed.")
    return "\n".join(lines)
