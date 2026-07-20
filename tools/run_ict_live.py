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

from hermes_trading.brokers.bybit import BybitBroker
from hermes_trading.ict.live import run_full_cycle
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


def _write_heartbeat(cycle_results) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": time.time(),
        "results": [
            {"asset": r.asset, "status": r.status, "action": r.action, "mutated": r.mutated}
            for r in cycle_results
        ],
    }
    (STATE_ROOT / "heartbeat.json").write_text(json.dumps(payload, indent=2))


def run_once(broker: BybitBroker, public_exchange: ccxt.Exchange, *, dry_run: bool) -> None:
    candles_by_asset = {}
    for asset in ASSETS:
        try:
            candles_by_asset[asset] = fetch_or_update_cache(public_exchange, asset)
        except Exception:
            print(f"[ERROR] cache update failed for {asset}:", flush=True)
            traceback.print_exc(file=sys.stdout)
            candles_by_asset[asset] = []

    results = run_full_cycle(ASSETS, broker, STATE_ROOT, candles_by_asset, dry_run=dry_run, **SCAN_PARAMS)
    for r in results:
        print(f"[{r.asset}] status={r.status} mutated={r.mutated} -- {r.action}", flush=True)
    _write_heartbeat(results)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="Log intended actions without placing real orders or persisting state")
    args = parser.parse_args()

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
