"""
Tests for hermes_trading.ict.context -- the dashboard's market snapshot.

The contract that matters: the snapshot must agree with what the TRADING
path saw. `test_qualified_candidates_match_scan_asset` is the load-bearing
test here -- if the context builder ever drifts from `scan_asset`, the
dashboard starts showing Linh setups the worker never acted on (or hiding
ones it did), which is worse than showing nothing.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from hermes_trading.ict.context import (
    MAX_EVENTS_PER_KIND,
    build_market_context,
    context_kwargs,
)
from hermes_trading.ict.scanner import build_detection_context, scan_asset
from hermes_trading.ict.util import Candle

DATA = Path(__file__).resolve().parent.parent.parent / "data" / "ict-backtest" / "BTC_USDT.csv"

# Same calibrated set the live worker runs (tools/run_ict_live.SCAN_PARAMS).
SCAN_PARAMS = dict(
    kill_zones=((0, 24),), swing_n_weekly=2, disp_atr_mult=0.75, min_rr=0.8,
    min_target_atr_mult=1.5, b_threshold=9, max_bars_after_mss=20, state_ttl_bars=40,
)


def _load(limit: int | None = None) -> list[Candle]:
    rows = []
    with DATA.open() as f:
        for r in csv.DictReader(f):
            rows.append(Candle(int(float(r["timestamp"])), float(r["open"]), float(r["high"]),
                               float(r["low"]), float(r["close"]), float(r["volume"])))
    return rows[-limit:] if limit else rows


@pytest.fixture(scope="module")
def candles():
    if not DATA.exists():
        pytest.skip("real BTC CSV not present (gitignored, regenerate with tools/fetch_ict_backtest_data.py)")
    return _load(12000)


@pytest.fixture(scope="module")
def candles_with_candidates():
    """
    A truncated real-data slice that actually contains candidate setups.

    Truncating the series is equivalent to passing `as_of_index` (detection
    is deterministic and no-lookahead, so hiding the future can't change the
    past) but costs a fraction of the time, since the whole pipeline then
    runs over a shorter series -- and it mirrors what the live worker really
    sees. Most windows produce NO candidates at all (compute_bias returns
    NO_TRADE, so build_setup is never reached), which is a genuine property
    of this strategy, not a fixture problem -- hence the search.
    """
    if not DATA.exists():
        pytest.skip("real BTC CSV not present (gitignored, regenerate with tools/fetch_ict_backtest_data.py)")
    base = _load(30000)
    for cut in range(8000, 13000, 400):
        sliced = base[:cut]
        ctx = build_market_context(sliced, "BTC/USDT", 808.03, **context_kwargs(SCAN_PARAMS))
        if ctx["candidates"]:
            return sliced, ctx
    pytest.skip("no window with candidate setups found in this data slice")


def test_empty_history_returns_error_not_exception():
    out = build_market_context([], "BTC/USDT", 1000.0)
    assert out["error"] == "no candle history"
    assert out["asset"] == "BTC/USDT"


def test_snapshot_is_json_serialisable_and_bounded(candles):
    ctx = build_market_context(candles, "BTC/USDT", 808.03, **context_kwargs(SCAN_PARAMS))
    blob = json.dumps(ctx)  # must not raise -- enums/dataclasses all coerced
    # The whole point of the display bounds: this file is fetched over SSH
    # every poll, so an unbounded zone list would be a real problem.
    assert len(blob) < 60_000
    assert len(ctx["recent_events"]["sweeps"]) <= MAX_EVENTS_PER_KIND
    assert len(ctx["recent_events"]["mss"]) <= MAX_EVENTS_PER_KIND
    for kind, zones in ctx["zones"].items():
        assert len(zones) <= 8, kind


def test_zones_are_near_current_price(candles):
    """Bounded zones must actually bracket the market, not sit lightyears away."""
    ctx = build_market_context(candles, "BTC/USDT", 808.03, **context_kwargs(SCAN_PARAMS))
    price, atr = ctx["price"], ctx["atr"]
    assert price > 0 and atr > 0
    for z in ctx["zones"]["fvg"] + ctx["zones"]["order_blocks"] + ctx["zones"]["breakers"]:
        assert min(abs(price - z["low"]), abs(price - z["high"])) <= 6 * atr


def test_no_lookahead_at_as_of_index(candles):
    """
    Nothing in the snapshot may reference a bar after as_of_index -- the
    same no-lookahead guarantee the detection primitives carry.
    """
    ctx_det = build_detection_context(candles, disp_atr_mult=0.75)
    aoi = len(ctx_det.exec_full) - 200
    cutoff_ts = ctx_det.exec_full[aoi].timestamp

    out = build_market_context(candles, "BTC/USDT", 808.03, as_of_index=aoi, **context_kwargs(SCAN_PARAMS))
    assert out["last_bar_ts"] == cutoff_ts
    for ev in out["recent_events"]["sweeps"] + out["recent_events"]["mss"]:
        assert ev["bar_ts"] <= cutoff_ts
    for c in out["candidates"]:
        assert c["mss_ts"] <= cutoff_ts
    for kind in ("fvg", "order_blocks", "breakers"):
        for z in out["zones"][kind]:
            if z.get("bar_ts"):
                assert z["bar_ts"] <= cutoff_ts


def test_qualified_candidates_match_scan_asset(candles_with_candidates):
    """
    THE contract: every candidate the context builder marks qualified-and-
    pending must be exactly what scan_asset would alert on at the same bar,
    with identical entry/stop/target/rr/grade/score. The dashboard must
    never disagree with the trading path about what a live setup is.

    Note this also passes vacuously-but-meaningfully when both sides are
    empty: the assertion is set equality, so a context builder that
    invented an armed setup scan_asset didn't return would fail here.
    """
    sliced, ctx = candles_with_candidates
    alerts = scan_asset(sliced, "BTC/USDT", 808.03, **SCAN_PARAMS)

    armed = {c["mss_ts"]: c for c in ctx["candidates"]
             if c["qualified"] and c["status"] == "pending"}
    assert {a.timestamp for a in alerts} == set(armed)

    for a in alerts:
        c = armed[a.timestamp]
        assert c["direction"] == a.direction.value
        assert c["grade"] == a.grade.value
        assert c["score"] == a.score
        assert c["entry_price"] == pytest.approx((a.entry_zone[0] + a.entry_zone[1]) / 2)
        assert c["stop_price"] == pytest.approx(a.stop)
        assert c["target_price"] == pytest.approx(a.target)
        assert c["rr"] == pytest.approx(a.rr)
        assert c["swept_level"] == pytest.approx(a.swept_level)
        assert c["mss_level"] == pytest.approx(a.mss_level)


def test_near_misses_are_surfaced_with_reasons(candles_with_candidates):
    """
    A rejected candidate must still be reported, with its score and the
    gates it failed -- that's the entire reason this module exists rather
    than the dashboard just calling scan_asset, which discards them.
    """
    _, ctx = candles_with_candidates
    valid_gates = {"htf_bias", "liquidity_event", "mss", "entry_zone",
                   "rr", "session", "risk_filter", "score_below_b"}

    near_misses = [c for c in ctx["candidates"] if not c["qualified"]]
    assert near_misses, "expected at least one near-miss in a window chosen for having candidates"
    for c in near_misses:
        assert c["gate_failures"], "an unqualified candidate must say why"
        assert set(c["gate_failures"]) <= valid_gates
        assert 0 <= c["score"] <= 20

    # gate_summary must account for the rejections, so the dashboard funnel
    # can't silently under-report.
    assert ctx["gate_summary"]
    assert set(ctx["gate_summary"]) <= valid_gates


def test_candidates_sorted_qualified_first(candles_with_candidates):
    _, ctx = candles_with_candidates
    quals = [c["qualified"] for c in ctx["candidates"]]
    assert quals == sorted(quals, reverse=True)


def test_context_kwargs_filters_unknown_params():
    """Forwarding a worker's SCAN_PARAMS verbatim must never raise a TypeError."""
    out = context_kwargs({"min_rr": 0.8, "totally_unknown": 1, "kill_zones": ((0, 24),)})
    assert out == {"min_rr": 0.8, "kill_zones": ((0, 24),)}
    build_market_context([], "BTC/USDT", 1000.0, **out)  # must not raise
