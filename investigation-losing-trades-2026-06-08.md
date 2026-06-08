# Losing Trades Investigation — 2026-06-08
_Rogue Night / Hermes-Trading. **Flagged for Linh's review.**_

---

## Scope & Data Limitations

The local `data/bybit-closed-pnl-2026-05-29.csv` covers 45 trades through May 29. VPS is authoritative for June 2–8 (not accessible from sandbox). This report covers what the local data and session history can establish, then flags the probable causes of the recent deterioration.

---

## Part 1 — What the May data actually shows

**Overall (45 trades, May 23–29):**

| Metric | Value |
|--------|-------|
| Win rate | 53.3% (24W / 21L) |
| Avg win | +$4.06 |
| Avg loss | -$2.52 |
| Win/loss ratio | 1.61x |
| Net PnL | +$44.41 |
| Total fees (open+close) | $19.72 |

On paper these are acceptable numbers. The problem is in the composition.

**Per-asset breakdown:**

| Asset | Trades | Win% | PnL |
|-------|--------|------|-----|
| BTC | 5 | 60% | +$10.41 |
| ETH | 7 | 57% | +$3.02 |
| SOL | 3 | 67% | +$7.40 |
| **TAO** | **30** | **50%** | **+$23.58** |

TAO accounts for 67% of all trades and is the main driver of volatility.

---

## Part 2 — Root Causes Identified

### Cause 1: TAO overtrading / fee bleed (most significant)

On May 27 alone, TAO fired **16 trades**. Result: 7W/9L, PnL = **-$9.78**, fees burned = **$8.79**.

The fee structure is fatal for small moves. Every TAO trade costs ~$0.55 in open+close fees. Several "winning" trades generated:
- $0.03, $0.09, $0.73, $0.58 — each one is a **net loser** after fees

In fact, 15 of 24 wins (62.5%) returned less than $5. Three TAO "wins" of <$0.10 each turned into ~$0.45 net losses after fees. This creates a **structural drag** where the strategy's true win rate at the fee-adjusted level is meaningfully below 53%.

**Fee-adjusted win threshold**: A trade needs to clear ~$0.55 just to break even. Any "win" below $1.10 is a net loser at the P&L level.

The `min_profit_usd: $5` guard was deployed May 28 (session 6) and **should** have caught this — but it arrived after the worst day (May 27). The key question for the current period is whether the guard is actually firing on VPS.

### Cause 2: Flawed Hermes reflection mutation on TAO (high confidence)

In session 6, TAO's first successful Hermes reflection produced mutation: `volume_spike.params.min_ratio 1.5 → 2.0`. The LLM's reasoning: "volume spike fired on 80% of losing trades."

The decision_context data tells a different story: **winning trades had HIGHER average volume_ratio (1.051) than losing trades (0.788)**. The LLM hallucinated the directional claim. This mutation suppresses entries when volume confirms the move — potentially filtering out exactly the right trades.

Status: TAO v03 mutation is LIVE on VPS. TAO v04 (June 5) applied `bb_squeeze.weight 1.0 → 1.25` with similarly garbled LLM reasoning (misread delta sign, correct direction by accident). Two consecutive mutations based on unreliable reasoning.

### Cause 3: Reflections were silently dead for 3+ days (May 28–June 5)

The `subprocess.run(timeout=120)` in `loop.py` was killing all Hermes calls before Ollama could respond (LLM timeout = 300s). Every reflection appeared to trigger but produced nothing. Fixed in session 8 (June 5, timeout raised to 360s).

Consequence: the strategy operated without any adaptive improvement for the most volatile period in the data. Fallback reflections still ran but those are rule-based, not LLM-driven.

### Cause 4: Confidence-scaled leverage — deployed June 6, currently active

Session 9 replaced fixed 5x leverage with a confidence-scaled 3x–10x range:

| Confidence | Old leverage | New leverage | Change |
|------------|-------------|-------------|--------|
| 30% | 5x | 5x | — |
| 40% | 5x | 6x | +20% |
| 55% | 5x | 7x | +40% |
| 70% | 5x | 8x | +60% |

The one recorded entry (TAO Trade #1, conf=54.76%) would now get **7x instead of 5x**.

Position sizing is risk-based (`qty = balance × 10% / sl_dist_pct / entry_price`), so a clean SL hit still targets 10% of balance regardless of leverage. However:

- Higher leverage → less margin locked per unit of qty → a price move **to SL** corresponds to a larger percentage of margin → greater risk of **liquidation before SL fires** on Bybit isolated margin
- More important: the strategy has a 50% win rate at the current min_confidence thresholds. At 40–60% confidence entries (likely the majority), leverage is 6–7x. **Losing 50% of trades at 6-7x is worse than losing 50% at 5x** if liquidation price is ever hit before SL.

This change went live **June 6 — two days ago.** It coincides exactly with when the losing streak became noticeable.

### Cause 5: Low min_confidence thresholds (TAO + SOL still at 0.3)

The strategy requires only 2+ indicators firing (min_indicators: 2) at ≥30% confidence to enter. At 30% confidence, the confidence-scaled leverage is 5x — same as before. But at 35-40% confidence, it's 5-6x on what is still a low-conviction trade.

ETH was raised to 0.4 in session 8 specifically because "RSI + VWAP only" entries were losing. TAO and SOL have the same problem and are still at 0.3.

---

## Part 3 — What We Can't Determine Without VPS Access

The following require SSH + VPS log access:
- Win rate and PnL for June 2–8 (the period of the reported losing streak)
- Whether `min_profit_usd: $5` guard is actually firing (check VPS bot.log for "Expected TP profit too small" lines)
- Whether the confidence-scaled leverage is causing liquidations before SL (check for unexpected exit prices)
- Current open positions and unrealized PnL
- How many trades have fired under the new 3x–10x leverage regime

---

## Part 4 — Prioritised Actions

**Immediate (before next trade fires):**

1. **SSH to VPS and pull bot.log tail** — confirm $5 floor is firing and no unexpected liquidations
   ```bash
   ssh root@187.127.108.173 "tail -100 /opt/trading/hermes_trading/bot.log"
   ```

2. **Pull current trade counts per asset**
   ```bash
   ssh root@187.127.108.173 "for s in btc_usdt eth_usdt sol_usdt tao_usdt; do echo $s: $(wc -l < /opt/trading/hermes_trading/state/$s/trades.jsonl) trades; done"
   ```

3. **Consider reverting confidence-scaled leverage to fixed 5x** while the strategy win rate is uncertain. The change was deployed 2 days ago on a system that was already having issues. If the bot is winning 50% at 40-60% confidence, scaling leverage up at those confidence levels adds exposure without added edge.

**Short-term (this session):**

4. **Raise TAO and SOL min_confidence from 0.3 → 0.4** (same fix applied to ETH in session 8). Low-conviction TAO entries are the single biggest contributor to fee bleed.

5. **Add a sanity-check before any Hermes mutation applies** (Phase 2.8, already in backlog) — verify the LLM's directional claim against actual decision_context data before writing to strategy.yaml. The volume_spike mutation and bb_squeeze mutation both should have been caught by this.

6. **Consider reverting TAO volume_spike min_ratio from 2.0 → 1.5** — the mutation was based on a hallucinated LLM claim. Decision_context shows wins had higher volume, not lower. The tighter filter may be suppressing valid entries.

**This Week:**

7. **Run the Phase 2.9 historical audit** (`tools/audit_historical_pnl.py`) to understand the -$18k cumRealisedPnl. If it's concentrated in a specific period/strategy version, it informs whether the current strategy has ever had structural edge.

8. **Cap TAO trade frequency** — on May 27, TAO fired 16 trades in one day. Even if each trade has 53% win rate, 16 × $0.55 = $8.80 in fees is a guaranteed drain. Consider a `max_trades_per_day` cap (already in the ETH/SOL yamls at 10, may not be active on TAO).

---

## Summary

The losing streak has at least four contributing factors: (1) TAO overtrading with fee bleed from sub-$5 wins, which predates June but may be continuing if the $5 floor isn't working on VPS; (2) two consecutive Hermes mutations on TAO based on questionable LLM reasoning; (3) reflections were dead for 3+ days, preventing adaptive correction; and (4) the confidence-scaled leverage deployed June 6 is adding 20-40% more exposure on average-confidence trades exactly when the win rate is uncertain.

The most actionable immediate step is SSHing to check bot.log for the last 48h of trades under the new leverage regime.

---

_Next handoff: Linh to review, then SSH to VPS for current state. Refer to `next-session-prompt.md` for Phase 2.8 sanity-check implementation._
