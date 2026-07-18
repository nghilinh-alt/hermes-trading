"""Tests for hermes_trading.ict.risk -- spec S:7, including the spec's own worked-example table."""
from __future__ import annotations

import pytest

from hermes_trading.ict.risk import circuit_breaker_status, max_concurrent_ok, position_size
from hermes_trading.ict.types import Grade

EQUITY = 1000.0
ENTRY = 100.0


def _stop_for_pct(pct: float) -> float:
    return ENTRY * (1 - pct)


# ── position_size: spec S:7 worked-example table (A+, $1000 equity, $200 target risk) ──


@pytest.mark.parametrize(
    "stop_pct,expected_notional,expected_leverage,expected_risk",
    [
        (0.02, 10000.0, 10, 200.0),
        (0.03, 6666.6667, 7, 200.0),
        (0.04, 5000.0, 5, 200.0),
        (0.06, 3333.3333, 4, 200.0),
        (0.015, 10000.0, 10, 150.0),  # too tight -> capped, realized risk falls below target
    ],
)
def test_position_size_matches_spec_worked_example_table(stop_pct, expected_notional, expected_leverage, expected_risk):
    ps = position_size(EQUITY, ENTRY, _stop_for_pct(stop_pct), Grade.A_PLUS)
    assert ps.notional == pytest.approx(expected_notional, rel=1e-3)
    assert ps.leverage == expected_leverage
    assert ps.risk_usd == pytest.approx(expected_risk, rel=1e-3)
    assert ps.qty == pytest.approx(ps.notional / ENTRY)
    assert ps.stop_pct == pytest.approx(stop_pct)


def test_position_size_grade_b_is_half_risk():
    ps_a = position_size(EQUITY, ENTRY, _stop_for_pct(0.02), Grade.A_PLUS)
    ps_b = position_size(EQUITY, ENTRY, _stop_for_pct(0.02), Grade.B)
    assert ps_b.risk_usd == pytest.approx(ps_a.risk_usd / 2)


def test_position_size_none_for_no_grade():
    assert position_size(EQUITY, ENTRY, _stop_for_pct(0.02), Grade.NONE) is None


def test_position_size_rejects_zero_distance_stop():
    with pytest.raises(ValueError):
        position_size(EQUITY, ENTRY, ENTRY, Grade.A_PLUS)


def test_position_size_never_exceeds_lev_max_notional():
    # Even an absurdly tight stop can't push notional past equity * lev_max.
    ps = position_size(EQUITY, ENTRY, _stop_for_pct(0.001), Grade.A_PLUS, lev_max=10)
    assert ps.leverage == 10
    assert ps.notional == pytest.approx(EQUITY * 10)


# ── circuit_breaker_status ────────────────────────────────────────────────────


def test_circuit_breaker_trips_on_daily_loss():
    assert circuit_breaker_status(-0.20, 0.0) is True
    assert circuit_breaker_status(-0.25, 0.0) is True
    assert circuit_breaker_status(-0.19, 0.0) is False


def test_circuit_breaker_trips_on_weekly_loss():
    assert circuit_breaker_status(0.0, -0.40) is True
    assert circuit_breaker_status(0.0, -0.39) is False


def test_circuit_breaker_clear_when_within_limits():
    assert circuit_breaker_status(-0.05, -0.10) is False


# ── max_concurrent_ok ──────────────────────────────────────────────────────────


def test_max_concurrent_ok_default_one():
    assert max_concurrent_ok(0) is True
    assert max_concurrent_ok(1) is False


def test_max_concurrent_ok_custom_limit():
    assert max_concurrent_ok(2, max_concurrent=3) is True
    assert max_concurrent_ok(3, max_concurrent=3) is False
