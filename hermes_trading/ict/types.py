"""
hermes_trading.ict.types -- shared dataclasses for the ICT detection package.

Every type here is an immutable (frozen) record produced by a pure detector
function in structure.py / liquidity.py / imbalance.py / bias.py. See
ict-strategy-plan-2026-07-18.md S:3 for the mechanical definitions.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class SwingKind(str, Enum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class Swing:
    """A confirmed fractal pivot. Spec S:3.1."""

    index: int
    price: float
    kind: SwingKind
    confirmed_index: int  # first bar index at which this pivot is usable (index + n)


class TrendState(str, Enum):
    UPTREND = "uptrend"
    DOWNTREND = "downtrend"
    RANGE = "range"


class BreakKind(str, Enum):
    BOS = "bos"  # continuation
    MSS = "mss"  # reversal, requires a prior liquidity sweep


@dataclass(frozen=True)
class StructureBreak:
    """A BOS or MSS event. Spec S:3.2."""

    index: int
    kind: BreakKind
    direction: Direction
    broken_swing: Swing
    close: float


class ZoneKind(str, Enum):
    SUPPORT = "support"
    RESISTANCE = "resistance"


@dataclass(frozen=True)
class Zone:
    """A clustered S/R zone. Spec S:3.3."""

    price_low: float
    price_high: float
    kind: ZoneKind
    touches: int
    strength: float
    member_indices: tuple[int, ...]


class LiquidityKind(str, Enum):
    BUYSIDE = "buyside"    # resting stops above resistance
    SELLSIDE = "sellside"  # resting stops below support


class LiquiditySource(str, Enum):
    SWING = "swing"
    EQUAL_HIGHS = "equal_highs"
    EQUAL_LOWS = "equal_lows"
    PDH = "pdh"
    PDL = "pdl"
    PWH = "pwh"
    PWL = "pwl"


@dataclass(frozen=True)
class LiquidityPool:
    """A mapped resting-liquidity level. Spec S:3.4."""

    price: float
    kind: LiquidityKind
    source: LiquiditySource
    index: int  # bar index the pool was established/last reinforced at
    member_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class Sweep:
    """A liquidity sweep / stop hunt. Spec S:3.5."""

    index: int  # bar the sweep (wick beyond + close-back) is confirmed on
    pool: LiquidityPool
    penetration: float
    direction: Direction  # bullish = swept sell-side liquidity (reversal up)


@dataclass(frozen=True)
class FVG:
    """A 3-candle Fair Value Gap. Spec S:3.7."""

    index: int  # index of the 3rd (gap-confirming) candle
    low: float
    high: float
    kind: Direction
    displacement: bool
    mitigated_index: Optional[int] = None

    @property
    def mitigated(self) -> bool:
        return self.mitigated_index is not None


@dataclass(frozen=True)
class OrderBlock:
    """An order block zone. Spec S:3.8."""

    index: int  # index of the OB candle itself
    low: float
    high: float
    kind: Direction
    break_index: int  # index of the displacement candle causing the BOS/MSS
    mitigated_index: Optional[int] = None

    @property
    def mitigated(self) -> bool:
        return self.mitigated_index is not None


@dataclass(frozen=True)
class Breaker:
    """A failed order block that flipped polarity. Spec S:3.8b."""

    order_block: OrderBlock
    flip_index: int
    kind: Direction  # polarity AFTER the flip
    mitigated_index: Optional[int] = None

    @property
    def mitigated(self) -> bool:
        return self.mitigated_index is not None


class BiasDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    NO_TRADE = "no_trade"


@dataclass(frozen=True)
class Bias:
    """HTF bias verdict. Spec S:4."""

    direction: BiasDirection
    weekly_trend: TrendState
    daily_trend: TrendState
    reason: str


@dataclass(frozen=True)
class DealingRange:
    """The swing-low -> swing-high range premium/discount is measured against. Spec S:3.9."""

    low: float
    high: float
    low_index: int
    high_index: int

    def retracement_pct(self, price: float) -> float:
        """0.0 at the range low, 1.0 at the range high."""
        span = self.high - self.low
        if span <= 0:
            return 0.5
        return (price - self.low) / span


class PremiumDiscountZone(str, Enum):
    PREMIUM = "premium"
    DISCOUNT = "discount"


@dataclass(frozen=True)
class PremiumDiscount:
    """Premium/discount + OTE verdict for a price within a dealing range. Spec S:3.9."""

    dealing_range: DealingRange
    retracement_pct: float
    zone: PremiumDiscountZone
    in_ote: bool
