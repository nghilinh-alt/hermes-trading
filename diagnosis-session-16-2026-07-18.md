# Diagnosis — Session 16 (2026-07-18)

**Trigger:** Linh asked "any trading yet, review the last 3 days."
**Answer up front: no. Zero trades opened across all assets since the 2026-07-14 guard-reorder deploy. BTC/ETH/SOL have never opened a single live trade.**

The guard reorder is live and correct, the bot is healthy and ticking — but trading is still fully blocked. The reason is now clearer than in sessions 13–15: **two different gates are binding, split by asset.** The reorder was a half-measure that did not address either.

---

## What was measured

Two read-only SSH blocks run by Linh on the VPS (`root@187.127.108.173`), 08:35–08:41 UTC 2026-07-18. This sandbox still has no network route to the VPS (confirmed again).

### Health / deploy status — all good
- Process `2433063` alive (the 07-14 22:56 guard-reorder deploy).
- Heartbeats current to the second: BTC/ETH/SOL/XRP `status:ok`, TAO `disabled`, `last_tick` = 08:41:46 UTC, `consecutive_failures:0`.
- Reorder confirmed in the running `execution.py`: Guard 2 (Soft R:R) runs **before** the `min_tp_pct` floor (Guard 3), floor applied to the final widened TP. Deploy is exactly as intended.

### Trades — zero
- `state/*/trades.jsonl` line counts: btc 0, eth 0, sol 0, xrp 1, tao 7. The xrp (paper) and tao (disabled) records are old. **No asset opened a trade in the last 3 days.**
- Reflections: 0 hypotheses across all assets — no asset has reached the 5-closed-trade threshold, so the self-improvement loop has still never fired.

### Correction to my first read
My first pull reported "TP-too-thin rejects: 0" and I wrongly inferred signals had stopped reaching the execution guards. **That was a grep artifact.** The `Structural TP too thin` reject lines carry **no timestamp prefix** (only the `No entry` lines are stamped `HH:MM UTC`), so a date-filtered `grep "2026-07"` count missed all of them. They are in fact firing on nearly every tick. Corrected below.

---

## Root cause — two binding gates, by asset

### SOL + XRP → die at `min_tp_pct` (the same gate since session 13)
Live log, every tick:
```
[SOL/USDT] Entry skipped — Structural TP too thin: 0.68% < min 3.00% (entry=74.97, tp=74.46)
[XRP/USDT] Entry skipped — Structural TP too thin: 1.37% < min 3.00% (entry=1.0916, tp=1.0766)
```
Observed final TP distances this window: SOL 0.68–0.87%, XRP 0.94–1.37%. All land far below the 3% floor. The Soft-R:R reorder cannot rescue them — the nearest-swing 15m TP is an order of magnitude below the floor; no reasonable R:R extension bridges 0.7% → 3%. **Session 15's own note — "the floor is near self-defeating on a 15m structural-TP strategy" — was correct. The reorder only helped the marginal SL≈2% / TP≈4% case, which SOL/XRP never present.**

### BTC + ETH → die upstream at `min_confidence` (never reach execution)
Live log, every tick:
```
[BTC/USDT] No entry · dir=long · conf=35% · price=64004.0
[ETH/USDT] No entry · dir=long · conf=25% · price=1845.13
```
These never reach the TP guard at all. Confidence sits at 25–43% against the **50% `min_confidence` gate raised in session 10**. Critically, **confidence never exceeds ~49% anywhere in the window** (BTC caps ~43%, ETH ~35%, XRP ~49%). The 50% threshold looks close to mathematically unreachable given the current indicator weights — worth checking whether the weights can even sum past 0.5 on a real setup.

### Whole-log skip tally (context)
`No entry` 7937 (dominant — confidence/indicator gate), `session` 3360 (00:00–07:00 UTC block), `ambiguous` 1815, `trend` 1060, `SL too wide` 175. Plus the timestamp-less `Structural TP too thin` stream (uncounted, but every tick for SOL/XRP).

---

## Assessment

We have chased `min_tp_pct` since session 13 and finally have the full shape: it is one of **two** independent zero-trade causes, and the fix we shipped addresses neither at the root. The system is working exactly as coded — every gate is doing its job — but the gate *values* are collectively incompatible with the entry logic:

- A **3% TP floor** is fundamentally at odds with **nearest-swing structural TPs on 15m**, which are routinely <1.5%.
- A **50% confidence floor** is at odds with an **indicator set that tops out near 49%.**

No single-variable tweak clears both. This is a strategy-design decision, not a bug.

---

## Decisions needed from Linh (live money — your call, presented as options)

**A. SOL/XRP — the TP floor.** Options, roughly least→most invasive:
1. **Lower `min_tp_pct`** to something 15m-realistic (e.g. 1.0–1.5%). At ~0.11% taker round-trip, a 1.5% TP nets ~1.4%; a 0.7% TP nets ~0.6% (marginal). Fastest, one variable.
2. **Change TP target selection** — target the *next* structural level out (not the nearest), or an ATR-multiple target, so TP distance is meaningfully larger by construction. More work in `price.py`/`execution.py`.
3. **Stand up the isolated scalp worker** already planned in session 15 (`state-scalp/`, inverted gate set) instead of bending the main loop further. Biggest scope, but it's the purpose-built home for thin-TP 15m trades.

**B. BTC/ETH — the confidence floor.** Recommend *diagnosing before changing*: confirm *why* confidence caps ~43% (indicator weights / normalization) before touching the gate. Then either lower `min_confidence` (e.g. 0.5 → 0.4) or rebalance weights so genuine setups can exceed 0.5. Changing the gate blind risks re-opening the low-conviction fee-bleed that session 10 raised it to stop.

My suggestion: pick **one** variable to move first (per the one-variable-at-a-time discipline), measure 3–5 days. If forced to choose, B's diagnostic (confidence ceiling) is the cheaper, safer first look; A.1 is the fastest lever if you want trades flowing sooner and accept thinner targets.

Nothing changed this session — code and config untouched, awaiting your decision.

---

## Handoff
- **Next agent:** whoever takes the fix — read this file + `diagnosis-session-15-2026-07-13.md` + `plan-scalp-strategy-2026-07-13.md` (option A.3 lives there).
- **Pending Linh decisions:** (A) TP-floor approach for SOL/XRP; (B) confidence-ceiling diagnosis + gate for BTC/ETH.
- **Baseline for next measurement:** process 2433063; confidence ceiling observed ≤49%; SOL/XRP TP distances 0.68–1.37%.
- **Flag:** internal, live-money change — deploy consciously.
