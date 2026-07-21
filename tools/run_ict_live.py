"""
tools/run_ict_live.py -- ICT live (real-money) trading daemon. Places and
manages real orders on Bybit. Spec S:5/S:6/S:7, extended into live
execution per the session-19/20 "skip paper, go live" decision -- see
memory.md and the approved live-worker deployment plan for the full
context and the safety/correctness gaps flagged before this was built.

Every SCAN_INTERVAL_SECS: updates each asset's Bybit-sourced rolling 15m
cache (tools/fetch_ict_live_data_bybit.py), then runs one full account
cycle (hermes_trading.ict.live.run_full_cycle) -- reconcile against the
broker's actual position/order state, manage any open position (2R
partial + breakeven + trailing stop), resolve any resting order
(pending/invalidated/expired), and look for new qualified setups on
assets that are flat.

Hard-fails at startup (loud, not a silent idle) unless
HERMES_TRADING_MODE=live and HERMES_TRADING_I_ACCEPT_RISK=true are both
set -- the old system's explicit safety-acknowledgment gate, preserved
here. --dry-run logs every intended action without ever calling a
broker-mutating method or persisting state that would affect a later
real run.

Run: python tools/run_ict_live.py [--dry-run]
Stop: Ctrl-C (or kill the process; state is persisted after every
mutating action, not just at cycle end -- see hermes_trading.ict.live)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

import ccxt
from dotenv import load_dotenv

from hermes_trading.brokers.bybit import BybitBroker
from hermes_trading.ict.context import build_market_context, context_kwargs
from hermes_trading.ict.live import AssetStateStore, _detection_kwargs, run_full_cycle
from hermes_trading.ict.scanner import build_detection_context
from hermes_trading.ict.risk import (
    DEFAULT_DAILY_LOSS_LIMIT_PCT,
    DEFAULT_MAX_CONCURRENT_TRADES,
    DEFAULT_WEEKLY_LOSS_LIMIT_PCT,
    circuit_breaker_status,
)
from tools.fetch_ict_live_data_bybit import ASSETS, fetch_or_update_cache

SCAN_INTERVAL_SECS = 15 * 60
STATE_ROOT = Path(__file__).resolve().parent.parent / "state-ict-live"

# Calibrated parameters -- kept in sync with tools/run_ict_backtest.py's
# CALIBRATED_PARAMS and tools/run_ict_scanner.py's SCAN_PARAMS manually;
# if that changes, update here too.
SCAN_PARAMS = dict(
    kill_zones=((0, 24),),
    swing_n_weekly=2,
    disp_atr_mult=0.75,
    min_rr=0.8,
    min_target_atr_mult=1.5,
    b_threshold=9,
    max_bars_after_mss=20,
    state_ttl_bars=40,
)


def _check_safety_gate() -> None:
    mode = os.getenv("HERMES_TRADING_MODE", "")
    accepted = os.getenv("HERMES_TRADING_I_ACCEPT_RISK", "").lower() == "true"
    if mode != "live" or not accepted:
        print(
            "FATAL: refusing to start -- HERMES_TRADING_MODE must be 'live' and "
            "HERMES_TRADING_I_ACCEPT_RISK must be 'true'. "
            f"Currently: HERMES_TRADING_MODE={mode!r}, "
            f"HERMES_TRADING_I_ACCEPT_RISK={os.getenv('HERMES_TRADING_I_ACCEPT_RISK', '')!r}",
            file=sys.stderr, flush=True,
        )
        sys.exit(1)


def _atomic_write_json(path: Path, payload: dict) -> None:
    """
    Write via a temp file + os.replace so a dashboard polling this path can
    never read a half-written file. os.replace is atomic on the same
    filesystem, which .tmp-alongside-the-target guarantees.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    os.replace(tmp, path)


def _circuit_breaker_snapshot(equity: float) -> dict:
    """
    Read (never write) the per-asset circuit-breaker buckets the trading
    path maintains, and report the account-level worst case. Read-only by
    design: the authoritative bucket update belongs to _look_for_new_setup,
    and duplicating it here could desync the two.
    """
    daily, weekly = 0.0, 0.0
    for asset in ASSETS:
        try:
            cb = AssetStateStore(STATE_ROOT / asset.replace("/", "_")).load_circuit_breaker()
        except Exception:
            continue
        if cb.get("equity_at_day_start"):
            daily = min(daily, (equity - cb["equity_at_day_start"]) / cb["equity_at_day_start"])
        if cb.get("equity_at_week_start"):
            weekly = min(weekly, (equity - cb["equity_at_week_start"]) / cb["equity_at_week_start"])
    return {
        "daily_pnl_pct": daily,
        "weekly_pnl_pct": weekly,
        "daily_limit_pct": DEFAULT_DAILY_LOSS_LIMIT_PCT,
        "weekly_limit_pct": DEFAULT_WEEKLY_LOSS_LIMIT_PCT,
        "active": circuit_breaker_status(daily, weekly),
    }


def _write_heartbeat(cycle_results, *, equity, busy_count, cycle_seconds, dry_run) -> None:
    """
    Account-level liveness + the facts the dashboard can't derive on its
    own. `scan_params` is emitted from the RUNNING process deliberately:
    the same values are duplicated by hand in run_ict_backtest.py and
    run_ict_scanner.py, so a dashboard that reads them from here can never
    show a stale copy.
    """
    payload = {
        "ts": time.time(),
        "equity_usd": equity,
        "busy_count": busy_count,
        "max_concurrent": DEFAULT_MAX_CONCURRENT_TRADES,
        "circuit_breaker": _circuit_breaker_snapshot(equity) if equity else None,
        "scan_params": {k: (list(v) if isinstance(v, tuple) else v) for k, v in SCAN_PARAMS.items()},
        "cycle_seconds": round(cycle_seconds, 1),
        "dry_run": dry_run,
        "assets": ASSETS,
        "results": [
            {"asset": r.asset, "status": r.status, "action": r.action, "mutated": r.mutated}
            for r in cycle_results
        ],
    }
    _atomic_write_json(STATE_ROOT / "heartbeat.json", payload)


def _build_detection_contexts(candles_by_asset: dict) -> dict:
    """
    Build each asset's DetectionContext ONCE per cycle, to be shared by the
    trading scan and the display snapshot.

    This is the expensive step in the whole cycle -- resample + swings +
    sweeps + BOS/MSS + FVG/OB/breaker over the full (ever-growing, 70k+ bar)
    15m cache. It used to run twice per asset: once inside scan_asset and
    again inside build_market_context, which measured at ~90-100s of pure
    duplication per cycle on 2026-07-21.

    Failure is isolated per asset and non-fatal: a None context just means
    that asset's cycle builds its own, exactly as before this optimisation.
    """
    contexts = {}
    for asset in ASSETS:
        candles = candles_by_asset.get(asset) or []
        if not candles:
            continue
        try:
            contexts[asset] = build_detection_context(candles, **_detection_kwargs(SCAN_PARAMS))
        except Exception:
            print(f"[WARN] detection-context build failed for {asset}; "
                  f"its cycle will build its own:", flush=True)
            traceback.print_exc(file=sys.stdout)
    return contexts


def _dump_market_context(candles_by_asset: dict, equity: float, detection_contexts: dict) -> None:
    """
    Persist the per-asset market snapshot the dashboard renders.

    Deliberately runs AFTER run_full_cycle has completed, reads only
    already-fetched candles, and isolates every asset in its own
    try/except: this is an observability feature and it must never be able
    to interfere with trading. A failure here logs and moves on.
    """
    for asset in ASSETS:
        try:
            candles = candles_by_asset.get(asset) or []
            if not candles:
                continue
            ctx = build_market_context(candles, asset, equity,
                                        detection_context=detection_contexts.get(asset),
                                        **context_kwargs(SCAN_PARAMS))
            _atomic_write_json(STATE_ROOT / asset.replace("/", "_") / "context.json", ctx)
        except Exception:
            print(f"[WARN] market-context dump failed for {asset} (trading unaffected):", flush=True)
            traceback.print_exc(file=sys.stdout)


def run_once(broker: BybitBroker, public_exchange: ccxt.Exchange, *, dry_run: bool) -> None:
    cycle_start = time.time()
    candles_by_asset = {}
    for asset in ASSETS:
        try:
            candles_by_asset[asset] = fetch_or_update_cache(public_exchange, asset)
        except Exception:
            print(f"[ERROR] cache update failed for {asset}:", flush=True)
            traceback.print_exc(file=sys.stdout)
            candles_by_asset[asset] = []

    # One detection context per asset, shared by the trading cycle below and
    # the display snapshot further down -- see _build_detection_contexts.
    detection_contexts = _build_detection_contexts(candles_by_asset)

    results = run_full_cycle(ASSETS, broker, STATE_ROOT, candles_by_asset, dry_run=dry_run,
                             detection_contexts=detection_contexts, **SCAN_PARAMS)
    for r in results:
        print(f"[{r.asset}] status={r.status} mutated={r.mutated} -- {r.action}", flush=True)

    # Account-level facts for the dashboard. Both calls are wrapped: a
    # broker hiccup fetching balance for a *display* field must not surface
    # as a cycle error, since the trading decisions are already made.
    equity, busy_count = 0.0, 0
    try:
        equity = broker.get_balance()
        busy_count = len(broker.get_positions()) + len(broker.get_open_orders())
    except Exception:
        print("[WARN] could not fetch account summary for heartbeat (trading unaffected):", flush=True)
        traceback.print_exc(file=sys.stdout)

    _dump_market_context(candles_by_asset, equity, detection_contexts)
    _write_heartbeat(results, equity=equity, busy_count=busy_count,
                     cycle_seconds=time.time() - cycle_start, dry_run=dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="Log intended actions without placing real orders or persisting state")
    args = parser.parse_args()

    load_dotenv()  # populates BYBIT_API_KEY/BYBIT_API_SECRET/HERMES_TRADING_* from .env, matching hermes_trading.run's pattern
    _check_safety_gate()

    broker = BybitBroker()
    # Separate public (unauthenticated) client for candle fetching --
    # matches fetch_ict_live_data_bybit.py's own standalone usage and
    # keeps the public-data/authenticated-account boundary clean rather
    # than reaching into BybitBroker's internals.
    public_exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "linear"}})
    public_exchange.load_markets()

    print(
        f"ICT live worker starting -- mode=live dry_run={args.dry_run} assets={ASSETS} "
        f"interval={SCAN_INTERVAL_SECS}s params={SCAN_PARAMS}",
        flush=True,
    )
    while True:
        cycle_start = time.time()
        try:
            run_once(broker, public_exchange, dry_run=args.dry_run)
        except Exception:
            print("[ERROR] cycle failed:", flush=True)
            traceback.print_exc(file=sys.stdout)
        elapsed = time.time() - cycle_start
        sleep_for = max(0.0, SCAN_INTERVAL_SECS - elapsed)
        print(f"[cycle] done in {elapsed:.1f}s, sleeping {sleep_for:.0f}s", flush=True)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
