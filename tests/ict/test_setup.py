"""Tests for hermes_trading.ict.setup -- spec S:6, S:8, S:9."""
from __future__ import annotations

from hermes_trading.ict.setup import (
    build_setup,
    compute_rr,
    compute_stop,
    compute_target,
    gate_htf_bias,
    grade_from_score,
    in_kill_zone,
    select_entry_zone,
    stage1_gates,
    stage2_score,
)
from hermes_trading.ict.types import (
    Bias,
    BiasDirection,
    BreakKind,
    Breaker,
    DealingRange,
    Direction,
    EntryZoneKind,
    FVG,
    Grade,
    LiquidityKind,
    LiquidityPool,
    LiquiditySource,
    OrderBlock,
    StructureBreak,
    Sweep,
    Swing,
    SwingKind,
    TrendState,
)


def _mss(index=8, direction=Direction.BULLISH):
    return StructureBreak(index=index, kind=BreakKind.MSS, direction=direction,
                           broken_swing=Swing(0, 110, SwingKind.HIGH, 1), close=112)


def _sweep(direction=Direction.BULLISH, price=100.0, penetration=2.0):
    pool = LiquidityPool(price=price, kind=LiquidityKind.SELLSIDE if direction == Direction.BULLISH else LiquidityKind.BUYSIDE,
                          source=LiquiditySource.SWING, index=3)
    return Sweep(index=5, pool=pool, penetration=penetration, direction=direction)


# ── select_entry_zone ──────────────────────────────────────────────────────────


def test_select_entry_zone_prefers_composite_over_lone_zone():
    mss = _mss()
    lone_fvg = FVG(index=9, low=50, high=52, kind=Direction.BULLISH, displacement=True)
    ob = OrderBlock(index=8, low=99, high=101, kind=Direction.BULLISH, break_index=8)
    overlapping_fvg = FVG(index=9, low=98, high=102, kind=Direction.BULLISH, displacement=True)
    result = select_entry_zone(Direction.BULLISH, mss, [lone_fvg, overlapping_fvg], [ob], [])
    zone, kind = result
    assert kind in (EntryZoneKind.FVG, EntryZoneKind.ORDER_BLOCK)
    assert (zone.low, zone.high) in [(98, 102), (99, 101)]  # not the lone, non-overlapping FVG


def test_select_entry_zone_excludes_mitigated_as_of_evaluation_time_and_wrong_direction():
    mss = _mss()  # mss.index == 8
    mitigated_before_mss = FVG(index=6, low=98, high=102, kind=Direction.BULLISH, displacement=True, mitigated_index=7)
    wrong_dir = FVG(index=9, low=98, high=102, kind=Direction.BEARISH, displacement=True)
    assert select_entry_zone(Direction.BULLISH, mss, [mitigated_before_mss, wrong_dir], [], []) is None


def test_select_entry_zone_ignores_mitigation_that_happens_after_the_evaluation_bar():
    """A zone that gets retested LATER (e.g. months later in a long backtest) was still
    valid at MSS time -- mitigation is checked as of mss.index, not "ever mitigated"."""
    mss = _mss()  # mss.index == 8
    mitigated_later = FVG(index=9, low=98, high=102, kind=Direction.BULLISH, displacement=True, mitigated_index=500)
    zone, kind = select_entry_zone(Direction.BULLISH, mss, [mitigated_later], [], [])
    assert zone is mitigated_later


def test_select_entry_zone_excludes_zones_before_mss():
    mss = _mss(index=8)
    stale_fvg = FVG(index=4, low=98, high=102, kind=Direction.BULLISH, displacement=True)
    assert select_entry_zone(Direction.BULLISH, mss, [stale_fvg], [], []) is None


def test_select_entry_zone_breaker_uses_order_block_bounds():
    mss = _mss()
    ob = OrderBlock(index=2, low=95, high=100, kind=Direction.BEARISH, break_index=3)
    breaker = Breaker(order_block=ob, flip_index=8, kind=Direction.BULLISH)
    zone, kind = select_entry_zone(Direction.BULLISH, mss, [], [], [breaker])
    assert kind == EntryZoneKind.BREAKER
    assert zone is breaker


# ── compute_stop / compute_target / compute_rr ─────────────────────────────────


def test_compute_stop_bullish():
    sweep = _sweep(Direction.BULLISH, price=100.0, penetration=5.0)
    # wick extreme = 100 - 5 = 95; buffer = 0.25 * 10 = 2.5 -> stop = 92.5
    assert compute_stop(sweep, atr_value=10.0) == 92.5


def test_compute_stop_bearish():
    sweep = _sweep(Direction.BEARISH, price=100.0, penetration=5.0)
    # wick extreme = 100 + 5 = 105; buffer = 2.5 -> stop = 107.5
    assert compute_stop(sweep, atr_value=10.0) == 107.5


def test_compute_target_bullish_nearest_buyside_above_entry():
    pools = [
        LiquidityPool(price=120, kind=LiquidityKind.BUYSIDE, source=LiquiditySource.SWING, index=1),
        LiquidityPool(price=110, kind=LiquidityKind.BUYSIDE, source=LiquiditySource.SWING, index=2),  # nearest
        LiquidityPool(price=90, kind=LiquidityKind.SELLSIDE, source=LiquiditySource.SWING, index=3),  # wrong side
    ]
    assert compute_target(Direction.BULLISH, entry=100.0, pools=pools) == 110


def test_compute_target_none_when_no_qualifying_pool():
    pools = [LiquidityPool(price=90, kind=LiquidityKind.SELLSIDE, source=LiquiditySource.SWING, index=1)]
    assert compute_target(Direction.BULLISH, entry=100.0, pools=pools) is None


def test_compute_target_min_distance_excludes_trivially_close_pools():
    # liquidity_pools() treats every confirmed swing as a pool, so without a floor
    # "nearest" is almost always some pool a few ticks away -- not a meaningful target.
    pools = [
        LiquidityPool(price=100.5, kind=LiquidityKind.BUYSIDE, source=LiquiditySource.SWING, index=1),  # too close
        LiquidityPool(price=120.0, kind=LiquidityKind.BUYSIDE, source=LiquiditySource.SWING, index=2),  # far enough
    ]
    assert compute_target(Direction.BULLISH, entry=100.0, pools=pools, min_distance=10.0) == 120.0
    assert compute_target(Direction.BULLISH, entry=100.0, pools=pools, min_distance=0.0) == 100.5


def test_compute_rr():
    assert compute_rr(entry=100, stop=96, target=112) == 3.0


# ── in_kill_zone ────────────────────────────────────────────────────────────────


def test_in_kill_zone_london():
    assert in_kill_zone(8 * 3_600_000) is True   # 08:00 UTC
    assert in_kill_zone(6 * 3_600_000) is False  # 06:00 UTC


def test_in_kill_zone_new_york():
    assert in_kill_zone(13 * 3_600_000) is True   # 13:00 UTC
    assert in_kill_zone(16 * 3_600_000) is False  # 16:00 UTC


# ── gate_htf_bias ─────────────────────────────────────────────────────────────


def test_gate_htf_bias_passes_when_direction_matches_computed_bias():
    long_bias = Bias(BiasDirection.LONG, TrendState.UPTREND, TrendState.RANGE, "weekly uptrend, daily range, discount")
    assert gate_htf_bias(Direction.BULLISH, long_bias) is True
    short_bias = Bias(BiasDirection.SHORT, TrendState.DOWNTREND, TrendState.DOWNTREND, "weekly and daily both downtrend")
    assert gate_htf_bias(Direction.BEARISH, short_bias) is True


def test_gate_htf_bias_fails_when_direction_does_not_match_bias():
    long_bias = Bias(BiasDirection.LONG, TrendState.UPTREND, TrendState.UPTREND, "weekly and daily both uptrend")
    assert gate_htf_bias(Direction.BEARISH, long_bias) is False


def test_gate_htf_bias_fails_on_no_trade_bias():
    no_trade = Bias(BiasDirection.NO_TRADE, TrendState.RANGE, TrendState.RANGE, "conflict")
    assert gate_htf_bias(Direction.BULLISH, no_trade) is False
    assert gate_htf_bias(Direction.BEARISH, no_trade) is False


# ── stage2_score / grade_from_score ─────────────────────────────────────────────


def test_stage2_score_max_is_20():
    dr = DealingRange(low=90, high=130, low_index=0, high_index=2)
    fvg = FVG(index=9, low=98, high=102, kind=Direction.BULLISH, displacement=True)
    score = stage2_score(
        TrendState.UPTREND, TrendState.UPTREND, Direction.BULLISH,
        _sweep(), _mss(), True, True, True,
        fvg, EntryZoneKind.FVG, dr, rr=3.0,
    )
    assert score == 20


def test_stage2_score_zero_when_nothing_present():
    score = stage2_score(
        TrendState.RANGE, TrendState.RANGE, Direction.BULLISH,
        None, None, False, False, False,
        None, None, None, rr=None,
    )
    assert score == 0


def test_grade_from_score_thresholds():
    assert grade_from_score(14) == Grade.A_PLUS
    assert grade_from_score(20) == Grade.A_PLUS
    assert grade_from_score(13) == Grade.B
    assert grade_from_score(11) == Grade.B
    assert grade_from_score(10) == Grade.NONE
    assert grade_from_score(0) == Grade.NONE


# ── stage1_gates ─────────────────────────────────────────────────────────────


def test_stage1_gates_all_pass():
    bias = Bias(BiasDirection.LONG, TrendState.UPTREND, TrendState.UPTREND, "weekly and daily both uptrend")
    passed, failures = stage1_gates(
        Direction.BULLISH, bias,
        _sweep(), _mss(), object(), rr=3.0, timestamp_ms=8 * 3_600_000,
        size=object(),
    )
    assert passed is True
    assert failures == []


def test_stage1_gates_flags_each_failure_independently():
    bias = Bias(BiasDirection.LONG, TrendState.UPTREND, TrendState.UPTREND, "weekly and daily both uptrend")
    base_kwargs = dict(
        direction=Direction.BULLISH, bias=bias,
        sweep=_sweep(), mss=_mss(), entry_zone=object(), rr=3.0, timestamp_ms=8 * 3_600_000, size=object(),
    )
    passed, failures = stage1_gates(**{**base_kwargs, "rr": 1.0})
    assert passed is False and failures == ["rr"]

    passed, failures = stage1_gates(**{**base_kwargs, "timestamp_ms": 16 * 3_600_000})
    assert passed is False and failures == ["session"]

    passed, failures = stage1_gates(**{**base_kwargs, "size": None})
    assert passed is False and failures == ["risk_filter"]

    passed, failures = stage1_gates(**{**base_kwargs, "sweep": None})
    assert passed is False and failures == ["liquidity_event"]

    no_trade_bias = Bias(BiasDirection.NO_TRADE, TrendState.RANGE, TrendState.RANGE, "conflict")
    passed, failures = stage1_gates(**{**base_kwargs, "bias": no_trade_bias})
    assert passed is False and failures == ["htf_bias"]


# ── build_setup (integration) ────────────────────────────────────────────────


def _full_a_plus_scenario():
    bias = Bias(BiasDirection.LONG, TrendState.UPTREND, TrendState.UPTREND, "weekly and daily both uptrend")
    sweep = _sweep(Direction.BULLISH, price=100.0, penetration=2.0)
    mss = _mss(index=8)
    ob = OrderBlock(index=8, low=99, high=101, kind=Direction.BULLISH, break_index=8)
    fvg = FVG(index=9, low=98, high=102, kind=Direction.BULLISH, displacement=True)
    # min_distance = min_target_atr_mult(2.0) * atr_value(8.0) = 16 -- pool must clear entry(100) by >= 16.
    pools = [LiquidityPool(price=124, kind=LiquidityKind.BUYSIDE, source=LiquiditySource.SWING, index=1)]
    dr = DealingRange(low=90, high=130, low_index=0, high_index=2)
    return dict(
        bias=bias, sweep=sweep, mss=mss, fvgs=[fvg], order_blocks=[ob], breakers=[],
        pools=pools, dealing_range=dr, atr_value=8.0, displacement=True,
        timestamp_ms=8 * 3_600_000, equity=1000.0,
    )


def test_build_setup_full_a_plus_qualifies():
    setup = build_setup(**_full_a_plus_scenario())
    assert setup is not None
    assert setup.qualified is True
    assert setup.grade == Grade.A_PLUS
    assert setup.score == 20
    assert setup.entry_price == 100.0
    assert setup.stop_price == 96.0  # wick 98 - buffer(0.25*8=2)
    assert setup.target_price == 124
    assert setup.rr == 6.0


def test_build_setup_no_trade_bias_returns_none():
    scenario = _full_a_plus_scenario()
    scenario["bias"] = Bias(BiasDirection.NO_TRADE, TrendState.RANGE, TrendState.RANGE, "conflict")
    assert build_setup(**scenario) is None


def test_build_setup_disqualified_when_rr_too_low():
    scenario = _full_a_plus_scenario()
    # Move the only target pool very close to entry so RR < 2.
    scenario["pools"] = [LiquidityPool(price=101.0, kind=LiquidityKind.BUYSIDE, source=LiquiditySource.SWING, index=1)]
    setup = build_setup(**scenario)
    assert setup.qualified is False
    assert "rr" in setup.gate_failures


def test_build_setup_disqualified_outside_session():
    scenario = _full_a_plus_scenario()
    scenario["timestamp_ms"] = 16 * 3_600_000  # outside both kill zones
    setup = build_setup(**scenario)
    assert setup.qualified is False
    assert "session" in setup.gate_failures
