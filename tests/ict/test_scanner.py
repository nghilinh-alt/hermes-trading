"""
Tests for hermes_trading.ict.scanner -- spec S:12.

The core parity test uses real *fetched* BTC history (data/ict-backtest/BTC_USDT.csv,
gitignored -- see tests/ict/fixtures/README.md for why real data isn't
committed in this environment) because Phase 1/2 already demonstrated how
hard it is to hand-engineer a synthetic fixture that survives the full
bias+sweep+MSS+zone+RR gate stack end to end; real market data reliably
contains qualifying setups, synthetic data usually doesn't without a lot
of fragile tuning. Skips gracefully if the CSV isn't present (e.g. a fresh
clone that hasn't run tools/fetch_ict_backtest_data.py).

Uses only the first ~2 years of the (now up to 5-year) cached CSV and a
module-scoped fixture to find the anchor setup once -- the full 5-year
series makes the detection pipeline (particularly detect_sweep) slow
enough that re-running it per-test made this file take 12 minutes; 2
years is enough to reliably contain a qualifying setup and keeps this at
a few seconds.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from hermes_trading.ict.backtest import (
    DEFAULT_SWING_N,
    _htf_series_asof,
    _matching_sweep,
    resample,
    resolve_setup_status,
)
from hermes_trading.ict.bias import compute_bias, dealing_range
from hermes_trading.ict.imbalance import find_breakers, find_fvg, find_order_blocks, is_displacement
from hermes_trading.ict.liquidity import detect_sweep, liquidity_pools
from hermes_trading.ict.scanner import build_detection_context, locate_pending_setup, scan_asset
from hermes_trading.ict.setup import build_setup
from hermes_trading.ict.structure import detect_bos, detect_mss, find_swings
from hermes_trading.ict.types import BiasDirection, Direction
from hermes_trading.ict.util import Candle, atr as atr_fn

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "ict-backtest"
BTC_CSV = DATA_DIR / "BTC_USDT.csv"
TWO_YEARS_OF_15M_BARS = 2 * 365 * 96

pytestmark = pytest.mark.skipif(not BTC_CSV.exists(), reason="real historical BTC data not fetched in this environment")

CALIBRATED = dict(
    kill_zones=((0, 24),), swing_n_weekly=2, disp_atr_mult=0.75, min_rr=0.8,
    min_target_atr_mult=1.5, b_threshold=9, max_bars_after_mss=20, state_ttl_bars=40,
)


def _load_csv(path: Path, limit: int | None = None) -> list[Candle]:
    candles = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if limit is not None and i >= limit:
                break
            candles.append(Candle(
                timestamp=int(float(row["timestamp"])), open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]), volume=float(row["volume"]),
            ))
    return candles


def _find_a_real_qualified_and_filled_mss(candles):
    """Scan real BTC history for one MSS event build_setup calls qualified and that later fills -- for parity testing."""
    exec_full = resample(candles, "1h")
    daily_full = resample(candles, "1d")
    weekly_full = resample(candles, "1w")
    swings = find_swings(exec_full, DEFAULT_SWING_N["1h"])
    pools = liquidity_pools(exec_full, swings, atr_period=14)
    sweeps = detect_sweep(exec_full, pools, atr_period=14)
    bos = detect_bos(exec_full, swings)
    mss_events = detect_mss(exec_full, swings, sweeps)
    fvgs = find_fvg(exec_full, atr_period=14, disp_atr_mult=CALIBRATED["disp_atr_mult"])
    obs = find_order_blocks(exec_full, bos + mss_events, atr_period=14, disp_atr_mult=CALIBRATED["disp_atr_mult"])
    brks = find_breakers(obs, bos + mss_events, exec_full, atr_period=14, disp_atr_mult=CALIBRATED["disp_atr_mult"])
    atr_series = atr_fn(exec_full, 14)

    for mss in sorted(mss_events, key=lambda b: b.index):
        ref_atr = atr_series[mss.index]
        if ref_atr is None:
            continue
        sweep = _matching_sweep(sweeps, mss)
        if sweep is None or sweep.direction != mss.direction:
            continue
        ts = exec_full[mss.index].timestamp
        daily_trunc = _htf_series_asof(daily_full, "1d", ts)
        weekly_trunc = _htf_series_asof(weekly_full, "1w", ts)
        if len(daily_trunc) < 2 or len(weekly_trunc) < 2:
            continue
        daily_swings = find_swings(daily_trunc, 3)
        weekly_swings = find_swings(weekly_trunc, CALIBRATED["swing_n_weekly"])
        bias = compute_bias(weekly_trunc, weekly_swings, daily_trunc, daily_swings, price=exec_full[mss.index].close)
        if bias.direction == BiasDirection.NO_TRADE:
            continue
        if (mss.direction == Direction.BULLISH) != (bias.direction == BiasDirection.LONG):
            continue
        dr = dealing_range(daily_swings, as_of_index=len(daily_trunc) - 1)
        pools_asof = liquidity_pools(exec_full, swings, as_of_index=mss.index, atr_period=14,
                                      daily_candles=daily_trunc, weekly_candles=weekly_trunc)
        displacement = is_displacement(exec_full, mss.index, atr_period=14,
                                        disp_atr_mult=CALIBRATED["disp_atr_mult"], atr_series=atr_series)
        setup = build_setup(bias, sweep, mss, fvgs, obs, brks, pools_asof, dr, ref_atr, displacement, ts, 1000.0,
                             kill_zones=CALIBRATED["kill_zones"], min_rr=CALIBRATED["min_rr"],
                             max_bars_after_mss=CALIBRATED["max_bars_after_mss"],
                             min_target_atr_mult=CALIBRATED["min_target_atr_mult"], b_threshold=CALIBRATED["b_threshold"])
        if setup is None or not setup.qualified:
            continue
        status, fill_index = resolve_setup_status(
            exec_full, mss.index + 1, setup.entry_price, setup.direction, sweep, mss,
            CALIBRATED["state_ttl_bars"], len(exec_full) - 1, atr_series=atr_series,
        )
        if status == "filled":
            return mss, setup, fill_index
    return None


@pytest.fixture(scope="module")
def anchor():
    """Loaded + searched once per test session, shared by every test that needs it."""
    candles = _load_csv(BTC_CSV, limit=TWO_YEARS_OF_15M_BARS)
    found = _find_a_real_qualified_and_filled_mss(candles)
    assert found is not None, "expected at least one real qualified-then-filled BTC setup in the first 2 years to anchor these tests on"
    mss, setup, fill_index = found
    return candles, mss, setup, fill_index


def test_scanner_alerts_while_pending_and_stops_once_resolved(anchor):
    """
    Core parity property: scan_asset must alert on a real qualified setup
    while it's still pending (before its known fill bar), and must NOT
    alert on it once it's resolved (at/after the fill bar) -- proving the
    scanner's "as of now" logic agrees with the backtest's "as of the full
    history" logic on the exact same real market data.
    """
    candles, mss, setup, fill_index = anchor

    before = scan_asset(candles, "BTC/USDT", 1000.0, as_of_index=fill_index - 1, **CALIBRATED)
    matching = [a for a in before if abs((a.entry_zone[0] + a.entry_zone[1]) / 2 - setup.entry_price) < 1e-6]
    assert matching, "expected an alert for the still-pending setup before it filled"
    alert = matching[0]
    assert alert.direction == setup.direction
    assert alert.state == "ENTRY_ARMED"
    assert alert.stop == setup.stop_price
    assert alert.target == setup.target_price
    assert alert.rr == setup.rr
    assert alert.grade == setup.grade
    assert alert.mss_level == mss.broken_swing.price

    after = scan_asset(candles, "BTC/USDT", 1000.0, as_of_index=fill_index, **CALIBRATED)
    matching_after = [a for a in after if abs((a.entry_zone[0] + a.entry_zone[1]) / 2 - setup.entry_price) < 1e-6]
    assert matching_after == [], "a resolved (filled) setup must not still be alerted"


def test_scanner_dedup_via_already_alerted(anchor):
    candles, mss, setup, fill_index = anchor

    first = scan_asset(candles, "BTC/USDT", 1000.0, as_of_index=fill_index - 1, **CALIBRATED)
    ts = next(a.timestamp for a in first if abs((a.entry_zone[0] + a.entry_zone[1]) / 2 - setup.entry_price) < 1e-6)

    second = scan_asset(candles, "BTC/USDT", 1000.0, as_of_index=fill_index - 1, already_alerted={ts}, **CALIBRATED)
    assert all(a.timestamp != ts for a in second), "already-alerted MSS event must be suppressed"


def test_scan_asset_empty_series_returns_no_alerts():
    assert scan_asset([], "BTC/USDT", 1000.0) == []


def test_locate_pending_setup_finds_same_sweep_and_mss_scan_asset_found(anchor):
    """locate_pending_setup must recover the identical (Sweep, StructureBreak) pair
    scan_asset's own internal loop found for a known-pending setup's MSS timestamp."""
    candles, mss, setup, fill_index = anchor

    ctx = build_detection_context(candles, disp_atr_mult=CALIBRATED["disp_atr_mult"])
    mss_timestamp = ctx.exec_full[mss.index].timestamp

    found = locate_pending_setup(ctx, mss_timestamp)
    assert found is not None
    sweep, located_mss = found
    assert located_mss.index == mss.index
    assert located_mss.direction == mss.direction
    assert sweep.direction == setup.direction
