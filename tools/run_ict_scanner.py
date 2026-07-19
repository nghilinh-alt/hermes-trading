"""
tools/run_ict_scanner.py -- ICT Phase 3 scanner daemon. Spec S:12.

Every SCAN_INTERVAL_SECS: updates each asset's rolling 15m cache
(tools/fetch_ict_live_data.py), runs scan_asset() (hermes_trading.ict.scanner),
and appends any NEW (not already-alerted) qualified-and-pending setup to
that asset's alerts.jsonl. Alert-only: no order placement, no position
tracking, no API keys used (public market data only).

Alert dedup persists across restarts via state-ict/<asset>/alerted.json
(a JSON list of alert timestamps already emitted). The live cache
(data/ict-live/) is append-only/ever-growing (see fetch_ict_live_data.py's
docstring) specifically so bar indices -- and therefore alert dedup, which
scan_asset keys by bar timestamp, itself stable regardless of indexing --
stay correct across the life of the cache.

Run: python tools/run_ict_scanner.py
Stop: Ctrl-C (or kill the process; state is persisted every cycle)
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path

import ccxt

from hermes_trading.ict.scanner import scan_asset
from tools.fetch_ict_live_data import ASSETS, fetch_or_update_cache

SCAN_INTERVAL_SECS = 15 * 60
EQUITY_FOR_SIZING = 1000.0  # illustrative only -- no real balance/API-key access this phase
STATE_DIR = Path(__file__).resolve().parent.parent / "state-ict"

# Calibrated parameters from the session-18/19 calibration pass -- see
# tools/run_ict_backtest.py's CALIBRATED_PARAMS docstring for the full
# reasoning behind each value. Kept in sync manually; if that changes,
# update here too.
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


def _alerted_path(asset: str) -> Path:
    return STATE_DIR / asset.replace("/", "_") / "alerted.json"


def _alerts_log_path(asset: str) -> Path:
    return STATE_DIR / asset.replace("/", "_") / "alerts.jsonl"


def _load_alerted(asset: str) -> set[int]:
    path = _alerted_path(asset)
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()))


def _save_alerted(asset: str, alerted: set[int]) -> None:
    path = _alerted_path(asset)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(alerted)))


def _append_alert_log(asset: str, alert) -> None:
    path = _alerts_log_path(asset)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = asdict(alert)
    record["direction"] = alert.direction.value
    record["grade"] = alert.grade.value
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def scan_once(exchange: ccxt.Exchange) -> None:
    for asset in ASSETS:
        try:
            candles = fetch_or_update_cache(exchange, asset)
            alerted = _load_alerted(asset)
            alerts = scan_asset(candles, asset, EQUITY_FOR_SIZING, already_alerted=alerted, **SCAN_PARAMS)
            for alert in alerts:
                print(f"[ALERT] {asset} {alert.direction.value} grade={alert.grade.value} score={alert.score} "
                      f"entry_zone={alert.entry_zone} stop={alert.stop:.4f} target={alert.target:.4f} rr={alert.rr:.2f}",
                      flush=True)
                _append_alert_log(asset, alert)
                alerted.add(alert.timestamp)
            if alerts:
                _save_alerted(asset, alerted)
        except Exception:
            print(f"[ERROR] scan cycle failed for {asset}:", flush=True)
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()


def main() -> None:
    exchange = ccxt.binance({"enableRateLimit": True})
    exchange.load_markets()
    print(f"ICT scanner starting -- assets={ASSETS} interval={SCAN_INTERVAL_SECS}s params={SCAN_PARAMS}", flush=True)
    while True:
        cycle_start = time.time()
        scan_once(exchange)
        elapsed = time.time() - cycle_start
        sleep_for = max(0.0, SCAN_INTERVAL_SECS - elapsed)
        print(f"[cycle] done in {elapsed:.1f}s, sleeping {sleep_for:.0f}s", flush=True)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
