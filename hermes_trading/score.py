"""
score.py — scores a list of trades against goal.yaml.
Returns a float in [-1, +1]:
  +1  = perfectly on target
   0  = neutral
  -1  = catastrophic
"""
import math
from typing import Any


def score(trades: list[dict], goal: dict) -> float:
    """
    Composite score from three sub-scores:
      - return_score:   realised return vs target_return_30d
      - drawdown_score: max drawdown vs max_drawdown
      - sharpe_score:   Sharpe ratio vs min_sharpe

    Each sub-score is in [-1, +1]. Composite is their weighted mean.
    """
    if not trades:
        return 0.0

    pnl_pcts = [t.get("pnl_pct", 0.0) for t in trades]
    realised_return = sum(pnl_pcts)

    # Max drawdown: largest peak-to-trough decline in cumulative pnl
    cumulative = []
    running = 0.0
    for p in pnl_pcts:
        running += p
        cumulative.append(running)

    peak = cumulative[0]
    max_dd = 0.0
    for c in cumulative:
        if c > peak:
            peak = c
        dd = (peak - c) / (abs(peak) + 1e-9)
        if dd > max_dd:
            max_dd = dd

    # Sharpe (annualised, assuming 1-minute bars → ~525,600 periods/year)
    if len(pnl_pcts) > 1:
        mean_r = sum(pnl_pcts) / len(pnl_pcts)
        variance = sum((r - mean_r) ** 2 for r in pnl_pcts) / (len(pnl_pcts) - 1)
        std_r = math.sqrt(variance) if variance > 0 else 1e-9
        sharpe = (mean_r / std_r) * math.sqrt(525600)
    else:
        sharpe = 0.0

    target_ret = goal.get("target_return_30d", 0.05)
    max_dd_goal = goal.get("max_drawdown", 0.08)
    min_sharpe = goal.get("min_sharpe", 1.2)
    failure_below = goal.get("failure_below", -0.04)

    # Sub-score: return
    if realised_return >= target_ret:
        return_score = 1.0
    elif realised_return <= failure_below:
        return_score = -1.0
    else:
        return_score = (realised_return - failure_below) / (target_ret - failure_below) * 2 - 1

    # Sub-score: drawdown (lower is better)
    if max_dd <= 0:
        drawdown_score = 1.0
    elif max_dd >= max_dd_goal * 2:
        drawdown_score = -1.0
    else:
        drawdown_score = 1.0 - (max_dd / (max_dd_goal * 2)) * 2

    # Sub-score: Sharpe
    if sharpe >= min_sharpe:
        sharpe_score = min(1.0, sharpe / (min_sharpe * 2))
    elif sharpe <= 0:
        sharpe_score = -1.0
    else:
        sharpe_score = (sharpe / min_sharpe) * 2 - 1

    composite = (
        0.5 * return_score +
        0.3 * drawdown_score +
        0.2 * sharpe_score
    )

    return round(max(-1.0, min(1.0, composite)), 4)
