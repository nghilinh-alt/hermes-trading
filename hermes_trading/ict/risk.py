"""
hermes_trading.ict.risk -- position sizing, leverage derivation, circuit breakers.

Spec section: S:7. Risk is the input (fixed by grade); size and leverage
are both *derived* from how far the structural stop is, never the reverse.
"""
from __future__ import annotations

import math

from hermes_trading.ict.types import Grade, PositionSize

DEFAULT_LEV_MAX = 10
DEFAULT_RISK_PCT_A_PLUS = 0.20
DEFAULT_RISK_PCT_B = 0.10
DEFAULT_DAILY_LOSS_LIMIT_PCT = -0.20
DEFAULT_WEEKLY_LOSS_LIMIT_PCT = -0.40
DEFAULT_MAX_CONCURRENT_TRADES = 1


def position_size(
    equity: float,
    entry: float,
    stop: float,
    grade: Grade,
    *,
    lev_max: int = DEFAULT_LEV_MAX,
    risk_pct_a_plus: float = DEFAULT_RISK_PCT_A_PLUS,
    risk_pct_b: float = DEFAULT_RISK_PCT_B,
) -> PositionSize | None:
    """
    Risk-based position size. Spec S:7.

    stop_pct = |entry-stop|/entry (structural stop distance); risk_usd (the
    TARGET loss if stopped) = equity x risk_pct(grade); notional = risk_usd
    / stop_pct; qty = notional/entry. Leverage is whatever holds that
    notional: leverage = clamp(ceil(notional/equity), 1, lev_max).

    Cap behaviour (spec): if the stop is tight enough that the uncapped
    notional would need more than lev_max, leverage caps at lev_max and
    notional is capped at equity x lev_max too -- the REALIZED risk (this
    function's `risk_usd` field) falls below the target grade risk, safely
    under, never over. Matches the spec's own worked-example table exactly,
    including the 1.5%-stop capped case ($150 realized vs $200 target).

    Returns None for Grade.NONE (no trade).
    """
    if grade == Grade.A_PLUS:
        risk_pct = risk_pct_a_plus
    elif grade == Grade.B:
        risk_pct = risk_pct_b
    else:
        return None

    stop_pct = abs(entry - stop) / entry
    if stop_pct <= 0:
        raise ValueError("stop must differ from entry")

    target_risk_usd = equity * risk_pct
    notional = target_risk_usd / stop_pct
    leverage = max(1, min(lev_max, math.ceil(notional / equity)))
    notional = min(notional, equity * lev_max)
    qty = notional / entry
    realized_risk_usd = notional * stop_pct

    return PositionSize(risk_usd=realized_risk_usd, notional=notional, leverage=leverage, qty=qty, stop_pct=stop_pct)


def circuit_breaker_status(
    daily_pnl_pct: float,
    weekly_pnl_pct: float,
    *,
    daily_limit_pct: float = DEFAULT_DAILY_LOSS_LIMIT_PCT,
    weekly_limit_pct: float = DEFAULT_WEEKLY_LOSS_LIMIT_PCT,
) -> bool:
    """
    True = halt all trading (flatten & stand down). Spec S:7.
    Daily -20% (one losing A+ trade) or weekly -40% (two losses) trips it.
    """
    return daily_pnl_pct <= daily_limit_pct or weekly_pnl_pct <= weekly_limit_pct


def max_concurrent_ok(open_count: int, *, max_concurrent: int = DEFAULT_MAX_CONCURRENT_TRADES) -> bool:
    """True if another position can be opened. Spec S:7 -- 1 concurrent at this account size."""
    return open_count < max_concurrent
