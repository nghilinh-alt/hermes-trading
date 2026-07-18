# Diagnosis: "Still no trades" — session 14 (2026-07-08)

_Read-only. Sandbox still has no VPS network access (`/dev/tcp/187.127.108.173/22` → "Network is unreachable", re-confirmed this session, 3rd session in a row). This is a local-code-only deep dive to narrow the hypothesis list before Linh runs anything on the VPS — session 13's diagnostic was never run._

## Where this picks up

Session 13 (2026-07-06) wrote a diagnostic for the exact checkpoint the session 12 deploy doc asked for — re-check trade frequency 3–5 days after the trend-filter soft-gate fix (deployed and confirmed live 2026-07-03, 11:35 UTC). That diagnostic was never run; Linh is now asking again, 5 days post-deploy. Nothing has changed in git since `f3ae306` (session 12 wrap-up) — no code or config has moved. So this is the same open question, now further overdue, not a new regression.

## What this session actually checked (all local, no VPS needed)

**1. Is the soft-discount code actually correct, not just present?**
Read `loop.py` lines 321–960 directly. Confirmed: `_get_trend_direction()` returns `(direction, confirmed)`; when `confirmed` is `False` the code applies `entry_result["confidence"] *= discount` (0.7x) and only sets `fires = False` if the *discounted* confidence still misses `min_confidence` — this is a soft gate, not a hard skip, and matches the deploy doc's description exactly. The local repo's `loop.py` is the intended post-fix version. (This doesn't confirm the VPS is running this exact file — that still requires the `grep trend_4h_soft_discount` step from session 13's script — but rules out "the fix as designed doesn't actually work" as a cause.)

**2. Is there a gate the soft-discount fix didn't touch that could independently zero out trades?**
Yes — one, not previously called out this explicitly. The **daily EMA(20) ambiguous-band check stayed a hard gate** (deliberately, per the deploy doc — "this is what stopped the June losses, unchanged"):

```
if trend_allowed is None:   # price within ambiguous_band_pct (0.3%) of daily EMA20
    ... skip, no confidence math, no override possible
```

This fires *before* the soft-discount logic even runs. If BTC/XRP/ETH/SOL have spent the last 5 days chopping within 0.3% of their respective daily EMA20 — plausible in a range-bound regime — every tick gets skipped here regardless of whether the 4h soft-discount fix is working perfectly. The session 12/13 docs distinguish "daily/4h mismatch" (fixed) from "ambiguous band" (untouched) but the skip-reason greps used so far (`grep -iE "skip|trend filter|4h disagrees|confidence"`) would lump both under "trend filter" without separating them. **Session 14's diagnostic below splits these into two separate counts.**

**3. New hypothesis: could Hermes' own reflection loop have re-tightened the gate the fix just loosened?**
This wasn't checked in sessions 12 or 13. `reflect.py`'s tunable-variable list (lines 614–635) includes `entry.min_confidence` and `entry.min_indicators` — both are gates Hermes can autonomously change every reflection cycle (every 5 closed trades, or hourly fallback). `trend_4h_soft_discount` itself is **not** in the tunable list, so the fix's own parameter can't drift — but if trades have been rare, `min_confidence` could have drifted *upward* on the few reflections that did fire (e.g., after a loss), silently re-narrowing the exact gap the soft-discount was designed to open. Locally, `state/*/hypotheses.jsonl` are all 0 lines — but these are local mirrors that are not kept in sync with the VPS (this is the same sync gap already flagged for `trades.jsonl` since session 11), so this can't be ruled in or out without VPS data. **Added to the diagnostic below.**

**4. Anything changed in git or local working tree that could explain this independent of the VPS?**
No functional changes. `git log` shows nothing past `f3ae306`. The 5 uncommitted local diffs (`goal.yaml`, `snapshot.sh`, `dedup_trades.py`, the two `.md` docs) are line-ending-only rewrites from the Windows mount, except `memory.md` and the trend-filter deploy doc, which just carry session 13's already-known text updates. Nothing here is a live-behavior change.

**5. Reminder of what's still separately open (unchanged from session 13, restated for completeness):**
- BTC/ETH/SOL `trades.jsonl` reportedly 0 lines on the VPS as of 07-03 — if still true, Hermes has no data to reflect on for 3 of 4 assets regardless of gate tuning.
- SOL fails a distinct gate (`min_tp_pct` structural guard), unrelated to trend filter.
- Position-sizing/leverage fix (`d8a49c7`, `75ef76b`) implemented locally, still not deployed — correctly held back, does not affect trade *frequency* either way.

## Ranked hypotheses for zero/near-zero trades over the 07-03 → 07-08 window

1. **Bot or a worker is down** — heartbeats stale. Rules out everything else if true. Check first, as always.
2. **Ambiguous-band hard gate (point 2 above)** — untouched by the fix, could be dominating if price has been range-bound near daily EMA20. Newly distinguished from the 4h-mismatch skip in this session's diagnostic.
3. **Reflection-driven `min_confidence`/`min_indicators` drift (point 3 above)** — new hypothesis, unconfirmed, needs the `hypotheses.jsonl` tail added below.
4. **Soft-discount code didn't actually make it to the running process** — same "stale restart" risk flagged in session 13 (unlikely given session 12's "confirmed live at 11:35 UTC" note, but 5 days is enough time for an unrelated restart to have pulled a stale checkout).
5. **Fix works, just genuinely low signal frequency** — a tuning/patience outcome, not a bug. Distinguishable from #2–#4 only by the actual skip-reason tally.
6. **SOL-specific `min_tp_pct` issue** — already known, doesn't explain BTC/ETH/XRP.

## Updated diagnostic — run this on the VPS

```powershell
@'
echo --- process ---
pgrep -af hermes_trading.run
echo --- heartbeats ---
for f in /opt/trading/hermes_trading/state/*/heartbeat.json; do echo $f; cat $f; echo; done
echo --- trade counts since 07-03 ---
for f in /opt/trading/hermes_trading/state/*/trades.jsonl; do echo $f: $(wc -l < $f); done
echo --- confirm soft-discount code is live ---
grep -n "trend_4h_soft_discount" /opt/trading/hermes_trading/hermes_trading/loop.py
echo "--- skip tally: ambiguous daily-EMA band, hard gate untouched by the fix ---"
grep -c "ambiguous band of daily EMA" /opt/trading/hermes_trading/bot.log
echo "--- skip tally: 4h disagreement, soft-discounted post-fix path ---"
grep -c "4h disagrees with daily" /opt/trading/hermes_trading/bot.log
echo "--- current live min_confidence / min_indicators per asset, drift check ---"
for f in /opt/trading/hermes_trading/state/*/strategy.yaml; do echo $f; grep -A2 "^entry:" $f; done
echo "--- reflection mutation history since 07-03, did Hermes retune confidence/indicators ---"
for f in /opt/trading/hermes_trading/state/*/hypotheses.jsonl; do echo $f; grep -iE "min_confidence|min_indicators" $f | tail -10; done
echo "--- skip-reason sample, last 500 lines, general ---"
tail -500 /opt/trading/hermes_trading/bot.log | grep -iE "skip|trend filter|4h disagrees|confidence|min_tp|portfolio.*loss|FIRE" | tail -80
echo DONE
'@ | ssh root@187.127.108.173 bash
```

(Adds two things session 13's script didn't have: a count that separates the untouched ambiguous-band hard gate from the fixed 4h-disagreement soft gate, and a grep of each asset's `hypotheses.jsonl` for any reflection-driven change to `min_confidence`/`min_indicators` since the deploy — directly tests hypothesis #3.)

## What the answer determines

- **Heartbeats stale** → outage, fix that, ignore the rest.
- **Ambiguous-band count high, 4h-disagreement count low** → the fix is working as designed but the *other* hard gate (never touched, deliberately) is now the binding constraint — a genuinely new finding, next lever is `ambiguous_band_pct` (0.3% → wider) per the improvement plan's Part 4 item 3, not re-touching the 4h logic again.
- **`hypotheses.jsonl` shows `min_confidence` raised since 07-03** → Hermes' own reflection quietly re-tightened the gate the fix loosened. Fix: either exclude `entry.min_confidence`/`entry.min_indicators` from the tunable list temporarily while frequency is still being diagnosed, or manually reset to the 07-03 values.
- **`trend_4h_soft_discount` grep comes back empty** → deploy didn't take / got reverted by a restart, same as session 13's read.
- **Trade counts >0 but still low, no gate is at 90%+ of skips** → this really is a low-frequency-by-design outcome; compare against the improvement plan's Part 4 remaining levers (session filter, `min_indicators` 3→2) one at a time.

## Results — confirmed via live VPS diagnostic (2026-07-08)

Linh ran the diagnostics above, restart-scoped to the currently running process (`PID 2248353`, started `2026-07-03 11:35:09` — this is the exact session-12 deploy restart, running continuously ever since with no crashes in between, so every count below is a clean read of the full ~4d20h post-fix window, not contaminated by older log history).

**The session 12 trend-filter fix worked exactly as intended and is not the bottleneck:**
- `ambiguous_band`: 59 (the untouched hard gate — hypothesis #2 above)
- `4h_disagree_skip`: 6
- `confidence_skip` (soft-discount pushing a signal below `min_confidence`): 6

All three are small. Hypothesis #2 (ambiguous-band gate dominating) and hypothesis #5 (soft-discount code not actually live) are both ruled out.

**Hypothesis #3 (reflection-driven gate drift) was already ruled out earlier this session** — `hypotheses.jsonl` is empty for all 5 assets on the live VPS; reflection has never fired (needs 5 closed trades per `reflection_every`, and no asset has reached that).

**The actual dominant gate — not previously identified as the primary cause — is `min_tp_pct: 3.0%`,** the structural-TP guard in `execution.py::_structural_sl_tp()`. This only runs *after* a signal has already cleared confidence, indicators, trend filter, and session filter and is attempting to place the trade — so a high count here means signals ARE being generated in volume, they're dying at the very last step:

| Asset | `Structural TP too thin` skips (4d20h) |
|---|---|
| ETH | 1057 |
| BTC | 982 |
| SOL | 673 |
| TAO | 332 (disabled for live, but still shadow-evaluated) |
| XRP | 211 |
| **Total** | **3255** |

For comparison, `session_filter` (guaranteed ~29% of all ticks) totaled 2240 over the same window. `min_tp_pct` is rejecting *more* attempts than the time-based session block, and it's hitting every active asset — this was previously flagged in the session 12/13 docs as "SOL's separate, unrelated problem," but the data shows it's actually the single largest cross-asset bottleneck, bigger than everything the trend-filter fix targeted combined.

Minor tooling note: `entry_skipped_total` (grep on `"Entry skipped —"`) returned 0 despite `min_tp_skip` being 3255 — almost certainly an em-dash encoding mismatch between the source's `—` character and how it round-trips through the SSH/PowerShell pipe, not a real discrepancy. The per-asset `min_tp_skip` breakdown grepped on plain-ASCII `"Structural TP too thin"` instead and is reliable.

**Conclusion**: this is not a bug and not an outage. The bot is alive, generating signals at a healthy rate, clearing confidence/indicator/trend/session gates thousands of times over 5 days — and then failing `min_tp_pct: 3.0%` on the large majority of those attempts, across all 4 live assets. The improvement plan's Part 4 explicitly said not to touch `min_tp_pct` without separate review, written before this data existed. That's exactly what this is: a separate review, not a bundle-in. **This needs Linh's decision, not a unilateral change** — options are lowering `min_tp_pct` (e.g. 3.0% → 1.5–2.0%), or leaving it as-is if 3% TP distance is a deliberate quality floor Linh wants kept regardless of frequency cost.

## Handoff

- **Status**: Diagnosis complete and confirmed via live, restart-scoped VPS data (full ~4d20h post-07-03-deploy window, clean single process run, no crashes in between). Root cause identified: `min_tp_pct: 3.0%` structural-TP guard, not the trend filter (which is confirmed working) and not reflection drift (confirmed not happening — no asset has reached 5 closed trades to trigger reflection). No code or config changed this session.
- **Next agent / Linh**: decide whether to lower `min_tp_pct` (and by how much) or leave it as a deliberate quality floor. This is a genuine trade-off — lower it and frequency should recover sharply (3255 rejected attempts over 5 days across 4 assets is the whole gap), but it was originally set at 3.0% for a reason (per `min_tp_pct`'s docstring, "Option B target-return filter") that predates this diagnostic and should be reviewed, not just overridden by the frequency data alone.
- **Flag**: Internal diagnosis only, nothing here goes to a client. Per standing instruction, no `min_tp_pct` change gets implemented or deployed without Linh's explicit sign-off first.
