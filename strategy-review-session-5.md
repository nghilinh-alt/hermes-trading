# Strategy review — session 5 (2026-05-27)

_Prepared for Linh's review. Discussion only — no code or YAML changes proposed for immediate execution. Order of operations matters; recommend implementing in the sequence at the bottom._

## Empirical context from session 5

We have **one** real live trade in the wild to learn from:

| Field | Value |
|---|---|
| Asset | TAO/USDT |
| Direction | Short @ $282.0 |
| SL | $283.849 (structural resistance + 0.3% buffer) |
| TP | $278.8 (structural support) |
| R:R | **1.73** |
| Confidence | 54.76% |
| Indicators fired | rsi (oversold check inverted for short), ema_trend, order_block, sr_zone |
| Notional | qty 1.773 × $282 ≈ $500 (MAX_POSITION_USD cap hit) |
| Leverage | 5x |

BTC and ETH also fired short signals on the same tick but their TPs were < 0.03% from the fill price, so Bybit rejected with retCode 10001 ("TakeProfit set for Sell position should be lower than base_price"). SOL had an existing open position from a prior run, so the `has_open_position` guard correctly skipped it.

So we have observed: structural SL/TP works when the swing levels are far enough from entry. It fails (gracefully — Bybit rejects, agent keeps running) when they're within slippage distance.

---

## Linh's six questions

### 1) R:R should be at least 2:1

**Recommend: yes, add a `min_rr_ratio: 2.0` guard.** This is small, safe, and natively complements the existing `max_sl_pct` guard.

Two ways to enforce it inside `_structural_sl_tp`:

- **Soft (extend TP):** if structural support/resistance gives R:R < 2.0, override the TP to `entry ± sl_dist × min_rr_ratio`. Trade still fires but at the calculated TP, accepting that you're projecting past the nearest structural level.
- **Hard (skip trade):** if structural R:R < 2.0, raise `ValueError("R:R below threshold")`. Loop catches it and logs "Entry skipped — R:R too thin", same pattern as the existing structural-SL-too-wide skip.

I'd recommend **hard skip**. Structural levels are the whole point of the SMC approach; trading past them defeats the model. Better to wait for setups where the swing levels naturally give 2:1.

Our TAO trade at R:R 1.73 would have been skipped under this rule. That's a feature, not a bug — over a thousand trades, accepting < 2:1 is statistically negative-expectancy unless win rate is high.

### 2) TP zones — TP1 / TP2 / TP3

**Recommend: defer this to a Phase 3 milestone.** It's worth doing eventually but it's a substantial change in surface area.

Why deferred:

- Bybit's create_order takes **one** TP. To get TP1/TP2/TP3 you have to either split the position into 3 sub-orders at order time (with separate TPs and proportional quantities) or actively manage a single position with limit-close orders placed when intermediate price targets are touched.
- The cleanest implementation is "place full position with TP3 attached as a hard ceiling; loop tick-by-tick checks current price vs. TP1/TP2 and submits market-close orders for sub-quantities when hit." That's a real refactor of `place_live_trade` plus a new "position manager" responsibility.
- Without trailing stops first, TP zones don't do much — the point of TP zones is usually to lock in partial profit at TP1 while the rest rides with a trailed stop. So #3 (trailing) is a prerequisite, not the other way around.

Phase mapping:

- **Phase 1 SMC** (done, session 4) — structural SL/TP, risk-based sizing, fixed leverage.
- **Phase 2 — recommended next**: min R:R guard (this session's #1), min-TP-distance guard (the 10001 fix), trailing stop (#3 below).
- **Phase 3**: TP zones with partial close on Bybit. Months of work, not weeks.

### 3) Trailing stops — SL → BE at TP1, → TP1 at TP2…

**Recommend: yes, but in a simplified form that doesn't require TP zones first.** This can be Phase 2's headline feature.

The simplest trail rule that captures the spirit of "lock in profit as it goes":

- Track `entry_price` and `sl_price` per open trade (already in trades.jsonl).
- Every tick, fetch current mark price for each open position.
- If price has moved favorably by `≥ 1× sl_dist` (i.e., reached R = 1), move SL to `entry_price` (breakeven).
- If price has moved by `≥ 2× sl_dist` (R = 2), move SL to `entry_price + 1× sl_dist` (lock 1R of profit).
- If price has moved by `≥ 3× sl_dist` (R = 3), move SL to `entry_price + 2× sl_dist` (lock 2R).

Each SL update calls Bybit's `private_post_v5_position_trading_stop` with the new `stopLoss` value. This endpoint is idempotent (you can call it with the same value repeatedly without error — unlike `set_leverage` 110043).

Implementation footprint:

- New function: `execution.update_position_sl(asset, new_sl)` — ~15 lines.
- New function: `execution.fetch_open_positions_with_marks()` — returns list of `{asset, side, entry, current_mark, sl_price, sl_dist}` — ~20 lines.
- New per-tick block in `loop.py` after the entry decision: iterate open positions, compute R-multiple, update SL if threshold crossed. ~30 lines.

Total ~65 lines of code. Doable in one well-scoped session. Tests can be done in paper mode (simulate price moves) before live.

**Important:** trailing stops on Bybit don't move automatically — you have to send the update each tick. That means if your bot is down for a tick when price reaches TP1, the trail doesn't fire. Acceptable risk for a 15m timeframe; not acceptable for scalping.

### 4) Dashboard missing pre-update SOL trade — is dashboard reading Bybit?

**Diagnosis: dashboard reads `state/<slug>/trades.jsonl` for the history table and calls `has_open_position`/`fetch_positions` only for the "Live Positions" panel (per memory.md session 3 notes).** It does NOT backfill closed trades from Bybit.

So a trade that:
- Opened on Bybit before the per-asset state structure existed, and
- Closed on Bybit without being in trades.jsonl,

…is invisible to the dashboard history. It would show up briefly in Live Positions while open, then disappear when closed without ever being recorded.

**Recommend a small backfill enhancement:** a new function `execution.fetch_recent_closed_trades(asset, since_ts, limit=50)` that calls Bybit's `private_get_v5_position_closed_pnl` (which we already use for the singular `fetch_last_closed_pnl`), iterates the result list, and returns N closed trades since `since_ts`. Dashboard reads trades.jsonl AND merges with Bybit's closed history for the asset, deduplicating by `order_id`.

This is also useful for the per-asset memory file Hermes-reflection writes — currently it only learns from trades it logged itself. Backfilling means reflection learns from trades the bot didn't even know it placed (probably zero-value, but defends against future state-corruption losing trade history).

Implementation footprint: ~40 lines (new function + dashboard merge logic). Phase 2.

### 5) Is it swing trading / smart money concepts?

**Honest take: it's an SMC-flavored intraday momentum strategy, not swing trading.** Here's the gap.

What's SMC about it:
- FVG (Fair Value Gap) indicator on 1h
- Order Block indicator on 1h
- S/R zones from 1h/4h swing points
- Structural SL/TP from these levels (Phase 1 SMC)

What's NOT SMC about it:
- 15m primary timeframe — true swing usually trades 4h/daily entries
- RSI, MACD, BB squeeze, volume spike — all classical TA, not SMC
- 8 of 9 indicators are momentum/mean-reversion oriented; SMC indicators (FVG, OB, S/R) are 3 of 9 by weight

Score-wise the SMC indicators carry: FVG (0.4) + OB (0.4) + sr_zone (0.3) = 1.1 weight out of 3.2 total optional weight = **34% SMC weighting**. So the strategy is roughly 1/3 SMC, 2/3 classical multi-indicator.

If you want it more swing/SMC pure, the changes are:
- Move primary timeframe from 15m to 1h or 4h
- Drop or reduce weight on RSI, MACD, BB squeeze (the mean-reversion stuff)
- Increase weight on FVG, OB, sr_zone
- Add liquidity sweep / break-of-structure (BoS) / change-of-character (ChoCH) detectors — these are the bread-and-butter SMC signals not yet implemented

This is a doctrine change, not a code change. Want to discuss separately — Phase 4 conceptual question.

### 6) Aim for ≥3% return per trade (15% with 5x leverage)

**Math first, then recommendation.** Three interlocking knobs: `risk_per_trade`, leverage, and TP placement.

Currently:
- `risk_per_trade: 0.10` (10% of balance at risk per full SL hit)
- `default_leverage: 5`
- TP comes from structural support/resistance — no target-return enforcement

For a long at $1000 with 5x leverage:
- If SL is 1% away ($990): position notional sized so 1% × notional = 10% × balance → notional = 10× balance. Capped at 5× leverage means actual notional is 5× balance, and a 1% SL move costs 5% of balance (half the 10% target risk).
- If TP is 2% away ($1020) — R:R = 2 — and full TP is hit: gain is 2% × 5× balance = 10% of balance.

To hit your stated goal — **15% of balance on a winning trade with 5x leverage** — you need:
- Price move to TP = 3%
- R:R = 2 → SL = 1.5% away → 1.5% × 5× = 7.5% loss on a loser (not 10%)

That's actually cleaner than current: 7.5% risk per loser, 15% gain per winner, R:R 2.0. Expected value per trade with 50% win rate = +3.75% per trade. That's aggressive but coherent.

**The catch:** the strategy doesn't currently *enforce* TP at 3% price move. Structural TP could be anywhere from 0.02% (the BTC failure case) to 5%+ from entry. To enforce "TP at 3% price move," you'd have to override structural TP — which removes the SMC premise of "exit at the next structural level."

Three options:

- **A. Hybrid:** TP = max(structural_TP, entry × 1.03). Use structural TP when it's at least 3% away; project past structural if it's closer. Loses some SMC purity, gains return-target consistency.
- **B. Filter:** only take trades where structural TP gives ≥ 3% price move *and* R:R ≥ 2. Trade frequency drops; quality theoretically goes up.
- **C. Calculated TP:** ignore structural for TP entirely; always set TP at `entry × (1 ± 0.03)`. Use structural only for SL. Pure target-return strategy with SMC stops.

I'd recommend **option B** — it keeps the strategy honest about what it is (SMC) and only takes setups where SMC and your return target coincide. Trade frequency will be low, but the trades that fire will be high-conviction.

---

## Proposed implementation order

Phase 2 (next 1–3 sessions):

1. **Min TP distance guard** (the 10001 fix) — 5 lines, blocks BTC/ETH from re-failing. Highest priority because it's actively rejecting orders right now.
2. **Min R:R guard** (#1) — `min_rr_ratio: 2.0` field + skip-on-failure. Combine with #1 above in one session.
3. **Target-return filter** (#6 option B) — additional `min_tp_pct: 3.0` strategy field + filter. Same session as 1+2 since they're all structural guards in the same function.
4. **Trailing stops** (#3) — separate session. New function in execution.py, new per-tick block in loop.py. Test in paper mode first.
5. **Bybit-backfill for closed trades** (#4) — extends `fetch_last_closed_pnl` into `fetch_recent_closed_trades`. Dashboard reads merged history. Separate small session.

Phase 3 (Phase 2+ months):

6. **TP zones with partial close** (#2) — substantial refactor.
7. **Strategy doctrine: timeframe + indicator weighting** (#5) — conceptual, can happen in parallel.

## What I am NOT recommending

- **Don't fix the 10001 issue and the R:R guard in separate sessions.** They're in the same function and the same conceptual change ("constrain what setups we accept"). One Write tool overwrite, one deploy, one observation period.
- **Don't implement TP zones before trailing stops.** TP zones without trailing is just three TPs with no profit-locking — worse than one TP with a trail.
- **Don't change leverage or risk_per_trade right now.** The current 5x / 10% pair is coherent; changing it without first understanding actual trade outcomes is premature.

## Open questions for Linh

1. **Min R:R = 2.0 — soft (extend TP) or hard (skip trade)?** I lean hard. You?
2. **Target return = 3% — option A, B, or C?** I lean B.
3. **Trailing stops — fixed R-step rules (BE → 1R → 2R) or ATR-based dynamic trail?** Fixed is simpler and easier to reason about. ATR adjusts to volatility but adds a parameter to tune.
4. **Timeframe — keep 15m or move higher?** Moving to 1h cuts tick frequency by 4×, reduces noise, but also means slower learning loops for Hermes-reflection. 
5. **For the duplicate-trade-record bug in trades.jsonl — fix it before or after Phase 2?** Pure bookkeeping; doesn't affect Bybit. I'd say after Phase 2 since reflection isn't using trades.jsonl heavily yet.

## Handoff

- **Status:** No code or YAML changes made this session. Strategy review document only.
- **Receives:** Linh.
- **Read:** `strategy-review-session-5.md` (this file). `diagnosis-session-5.md` and `memory.md` are the prior context.
- **Decision needed before Phase 2 starts:** answers to the 5 open questions above.
