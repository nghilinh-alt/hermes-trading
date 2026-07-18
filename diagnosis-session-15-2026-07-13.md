# Deep analysis: "why no trades" — session 15 (2026-07-13)

_Local code + config deep dive. Sandbox still has no VPS network access, so this re-derives the root cause from the source and the last confirmed live diagnostic (07-08) rather than re-running the VPS. **Nothing has been deployed since the 07-08 diagnosis** — `git log` shows no commits after `f3ae306`, so the running bot is byte-for-byte the same one those 07-08 counts came from. The problem is unchanged, not new._

## Bottom line

This is not an outage and not a signal-generation problem. The bot is alive and producing signals in volume — thousands per week clear confidence, indicators, trend filter, and session gates. They then die at the **last** gate before the order is placed: the structural take-profit floor **`min_tp_pct: 3.0%`** in `execution.py::_structural_sl_tp()`. Over the confirmed ~4d20h window (07-03 → 07-08) it rejected **3,255** entry attempts across all four live assets — more than the session-hours block (2,240) — and it is the single largest cross-asset bottleneck by a wide margin.

Config is identical on BTC/ETH/SOL/XRP: `min_tp_pct: 3.0`, `min_rr_ratio: 2.0`, `min_profit_usd: 5.0`, `max_sl_pct: 5.0`, `stop_loss_pct: 2.0`, `min_confidence: 0.5`, `min_indicators: 3`, `session_blocked_end_utc: 7`, `max_trades_per_day: 3`.

## Why this gate is almost self-defeating (the core finding)

The take-profit is set to the **nearest structural swing level** — `resistance_1h4h` for longs, `support_1h4h` for shorts (`execution.py` lines 128–147). By construction the nearest structural level is *close* to price. Then Guard 2 rejects the whole trade if that nearest level is less than 3% away:

```python
# Guard 2 (Option B filter): TP must be at least min_tp_pct from entry
tp_dist_pct = abs(tp_price - entry) / entry
if tp_dist_pct < min_tp_pct:            # min_tp_pct = 0.03
    raise ValueError("Structural TP too thin ...")
```

On a 15-minute timeframe, intraday 1h/4h swing levels are routinely well under 3% from current price. So the strategy picks the nearest structural target, then throws the trade away for the target being exactly what a structural target is — nearby. The gate is in direct tension with the structural-TP method it's guarding. In a range-bound regime (which the trend-filter diagnosis already suggested we're in) that tension becomes near-total, which is why the reject count is so high across every asset at once.

## New finding: the guard ordering makes it worse than intended

Guard 2 (the hard 3% reject) runs **before** Guard 3, the soft R:R extension:

```python
# Guard 3 (Soft R:R): extend TP if R:R below threshold
rr_ratio = abs(tp_price - entry) / sl_dist
if rr_ratio < min_rr_ratio:             # min_rr_ratio = 2.0
    tp_price = entry ± sl_dist * min_rr_ratio
```

With SL distance ≈ 2% and `min_rr_ratio` 2.0, Guard 3 would widen a thin structural TP out to ≈4% — comfortably past the 3% floor. **If the two guards were reversed** (extend for R:R first, then apply the 3% floor to the widened TP), the large majority of those 3,255 rejections would survive. As written, each trade is killed for a thin TP *before* the logic that would have widened that same TP ever runs. So a meaningful share of the "quality floor" is an ordering artifact, not a deliberate quality decision. This is the cleanest lever: it makes the gate internally consistent without abandoning a real 3% floor on the *final* TP.

## What was ruled out (so we don't re-chase it)

- **Outage / stale deploy** — 07-08 diagnostic confirmed one continuous process since the 07-03 11:35 deploy, no crashes, `trend_4h_soft_discount` live. (Local `state/heartbeat.json` reads `initializing` and local `trades.jsonl` are empty, but those are known-stale mirrors that don't sync from the VPS — not evidence of anything.)
- **Trend filter** — the session-12 fix works exactly as designed: ambiguous-band 59, 4h-disagree 6, confidence-skip 6 over the whole window. Negligible.
- **Reflection gate drift** — `min_confidence` (0.5) and `min_indicators` (3) are still at baseline on every asset; every `hypotheses.jsonl` is empty. No asset has reached the 5-closed-trade threshold, so Hermes has never mutated a gate. The thresholds are exactly as deployed.

## The gates queued up behind min_tp_pct (order of who bites next)

Even after `min_tp_pct` is loosened, these become the next binding constraints — worth anticipating so we don't declare victory prematurely:

1. **`min_profit_usd: 5.0`** (`_guard_min_profit_usd`, runs after sizing) — with ~10% position sizing on a small balance, a structurally valid setup can still be rejected if expected profit at TP is under $5. This is the most likely next wall once TPs start clearing.
2. **`session_blocked_end_utc: 7`** — blocks 00:00–07:00 UTC, ~29% of every day (2,240 skips, the current #2 blocker).
3. **`min_rr_ratio: 2.0`** combined with **`max_sl_pct: 5.0`** — a secondary shaping constraint on which setups survive.
4. **`max_trades_per_day: 3`** — caps the upside once trades actually start flowing; not a current blocker but relevant when judging any post-fix frequency recovery.

## Options for Linh (decision required — not a unilateral change)

Per standing discipline, no gate change ships without sign-off, one variable at a time, with a `deploy-*.md` and a 3–5 day measurement window before stacking the next.

- **A — Reorder guards (recommended technical fix).** Run the R:R extension before the min_tp_pct floor so the floor applies to the final TP. Recovers most of the 3,255 while keeping a genuine 3% floor. Lowest-risk, most principled — it fixes an inconsistency rather than lowering a standard.
- **B — Lower `min_tp_pct` to 1.5–2.0%.** Blunt but effective; frequency recovers sharply since this gap is essentially the whole story. Weakens the quality floor across the board.
- **C — Keep 3% as a deliberate quality floor** and instead widen where structural targets come from (larger swing lookback), accepting fewer, higher-quality setups. Lowest frequency recovery.

A and B are not mutually exclusive, but they should still be shipped and measured **one at a time** so the effect of each is attributable.

## Handoff

- **Status**: Diagnosis re-confirmed from source. Root cause unchanged from session 14: `min_tp_pct: 3.0%` structural-TP guard, now with two additions — (1) a mechanistic explanation of *why* it rejects so heavily on a 15m structural-TP strategy, and (2) a new guard-ordering finding (Guard 2 before Guard 3) that offers a cleaner fix than simply lowering the number. No code or config changed this session.
- **Next**: Linh to choose A / B / C. Whoever implements → one variable, write `deploy-*.md` first, measure 3–5 days, re-check `min_profit_usd` as the likely next binding gate.
- **Flag**: Internal only — nothing here goes to a client. No deploy without Linh's explicit sign-off.
