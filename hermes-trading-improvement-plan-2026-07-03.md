# Hermes-Trading — Trade Frequency Diagnosis & Improvement Plan
_Session 12 — 2026-07-03. Rogue Night. Flagged for Linh's review before any VPS change._

## Executive Summary

Since session 11 (2026-06-18) the bot has produced roughly 1 trade in 15 days across 4 assets, against a target of 5–10 trades/week set at the end of that session. The gap isn't a mystery — it's arithmetic. Session 11 added a daily+4h trend filter and a session-hours block on top of an already-tight gate stack, and the same day (per git history, two commits after the session 11 doc was written) two more indicators were added and XRP was flipped from paper to live — without the planned 10-trade paper validation. Nobody re-measured the combined effect before letting it run for two weeks.

This document is a diagnosis, not a deploy. Local `trades.jsonl`/`heartbeat.json` files are all stale or empty (this machine has no live VPS state — SSH from this sandbox is network-unreachable), so before changing anything we need one read-only diagnostic pass on the VPS to rule out an outright bug (bot down, trend filter always returning "skip" due to a data issue) before treating this as a tuning problem.

## Part 1 — What Changed on 2026-06-18 (and why it matters)

Three commits landed same day, only the first was written up as "session 11":

| Commit | What | Logged in memory.md? |
|---|---|---|
| `a6ccb66` | Trend filter (daily EMA20 + 4h EMA50), session block 00:00–07:00 UTC, portfolio loss cap $40, new position sizing | Yes — session 11 |
| `7c93f05` | New `candle_pattern` + `trend_line` indicators added to BTC/ETH/SOL/XRP (weight 0.5, 0.4) | No |
| `b5d1718` | XRP `trading_mode: paper → live` | No |

The session 11 doc's own confirmed decision was: *"Wait period: run XRP in paper mode for the first 10 trades to confirm signal quality before switching to live."* That was reversed the same evening with no trade data to justify it. Worth flagging simply because it's the kind of thing that should have its own line in the Key Decisions table and doesn't.

## Part 2 — The Gate Stack (why frequency, not just quality, collapsed)

Every one of these is independently reasonable — each was added to fix a real problem from an earlier loss review. Stacked, they're close to prohibitive. Current config (BTC/ETH/SOL/XRP all identical):

| Gate | Setting | Effect |
|---|---|---|
| `min_confidence` | 0.5 | ≥50% of weighted indicator score must fire |
| `min_indicators` | 3 (of 11) | At least 3 indicators must literally fire in the same direction, independent of weight |
| `trend_filter` | daily EMA20 + 4h EMA50 must agree with each other AND with entry direction | Blocks any tick where daily/4h disagree or price is within 0.3% of daily EMA |
| `session_blocked_end_utc` | 7 | 7 of 24 hours/day (29%) blocked outright |
| `max_trades_per_day` | 3 | Caps upside even on a good day |
| `min_rr_ratio` / `min_tp_pct` | 2.0 / 3.0% | Structural TP must be ≥3% away and ≥2:1 R:R, or skip |
| `min_profit_usd` | $5.00 | Skip if expected profit too small |
| `max_portfolio_daily_loss_usd` | $40 | Halts all 4 assets for the rest of the day after a bad stretch |

Two of the eleven indicators (`candle_pattern`, `trend_line`) are pattern-based and fire rarely by design — hammer/engulfing/star patterns and price-within-1%-of-a-projected-trendline don't happen every 15m tick. Adding them to the same `min_indicators: 3` pool without raising the pool size doesn't help hit 3 more often; it just adds two indicators that mostly return `False` or `None`, diluting the confidence-weight denominator slightly and doing nothing for the count gate.

The trend filter and session filter are *time-based* — they don't just lower the odds a signal is good, they remove whole windows from consideration regardless of signal quality. Combined with the two per-tick indicator/confidence gates and the daily trade cap, the effective "tradeable surface" is a fraction of a fraction of a fraction. The session 11 doc estimated this would cut ~25 trades/week to 5–10. Getting ~0.1/week instead means either the estimate was too optimistic about how often daily+4h actually agree, or something is silently over-blocking (see Part 3).

## Part 3 — Rule Out a Bug Before Retuning

Before loosening anything, confirm this is a tuning problem and not a bug or an outage. Run this on the VPS (one block, per doctrine — no combined diff+cp steps):

```bash
ssh root@187.127.108.173 "
echo '--- process ---'
pgrep -af hermes_trading.run
echo '--- heartbeats ---'
for f in /opt/trading/hermes_trading/state/*/heartbeat.json; do echo \$f; cat \$f; echo; done
echo '--- trade counts (14d) ---'
for f in /opt/trading/hermes_trading/state/*/trades.jsonl; do echo \$f: \$(wc -l < \$f); done
echo '--- skip-reason sample (last 500 lines) ---'
tail -500 /opt/trading/hermes_trading/bot.log | grep -iE 'skip|trend filter|SKIP:' | tail -60
echo '--- live yaml vs git (drift check) ---'
diff /opt/trading/hermes_trading/state/btc_usdt/strategy.yaml <(git -C /opt/trading/hermes-trading show master:state/btc_usdt/strategy.yaml)
echo DONE
"
```

What this tells us:
- **Process/heartbeat**: confirms the bot is actually running on all 4 workers (not a silent crash since the 18th).
- **Skip-reason grep**: the log line format already distinguishes `confidence X% < min Y%`, `fired N < min_indicators M`, and the trend-filter dim-text skip line. Counting which reason dominates tells us which single gate to loosen first, rather than guessing.
- **Drift check**: rules out a live Hermes reflection mutation (or a manual Linh edit) having pushed a gate even tighter than what's in git.

If the bot is down or heartbeats are stale, that's the whole story — fix that first, don't touch any gate.

## Part 3 Results — Confirmed via live diagnostic (2026-07-03)

Linh ran the diagnostic block above. Results:

- **Bot is alive**: process running (PID 1911222), all 5 heartbeats fresh (`2026-07-03T10:01` UTC), zero consecutive failures. Not an outage.
- **Root cause confirmed and narrowed**: BTC and XRP skipped on `Trend filter: ambiguous or daily/4h mismatch` on every single tick across the full 3-hour log sample (07:16–10:01 UTC). The underlying values show why: BTC price ($61,732) sat below daily EMA20 ($62,162, → daily bias = short) but above 4h EMA50 ($60,737, → 4h says long) for the entire window — not a brief crossover, a sustained multi-hour regime. Same exact pattern for XRP. The AND-gate between two EMAs with different lookback windows (20 daily periods ≈ 20 days vs 50 four-hour periods ≈ 8.3 days) turns out to disagree far more often, and for far longer, than assumed when the filter was designed.
- **SOL has a separate, unrelated problem**: it's clearing the trend filter fine but hitting `Structural TP too thin: 0.44% < min 3.00%` repeatedly — current price structure doesn't have resistance/support far enough away for the 3% min-TP guard. Needs its own look, not a trend-filter fix.
- **ETH didn't appear in the log sample** — unresolved, check again post-deploy.
- **BTC/ETH/SOL `trades.jsonl` are empty (0 lines) on the VPS** — confirms session 11's own flagged-and-never-fixed action item ("Fix empty trades.jsonl + dead cron"). Still broken 15 days later. XRP's file has exactly 1 line — the trade Linh saw.
- **Drift check is not dangerous**: the *running* code has the correct session-11 config (v03, trend filter, etc.); the git staging clone at `/opt/trading/hermes-trading` (hyphen) is just stale — nobody `git pull`ed it since before session 11. Needs a pull before it's used as a diff baseline again, but the live bot itself was running the intended config.

**Decision (Linh, 2026-07-03)**: fix the trend filter by making the 4h EMA a soft confidence discount instead of a hard AND-gate, keeping the daily EMA bias as a hard gate (unchanged — this is what stopped the June losses). Implemented this session; see `deploy-trend-filter-soft-gate-2026-07-03.md`.

## Part 4 — Proposed Loosening (pending Part 3 confirmation)

_Superseded by the Part 3 Results above for the trend filter specifically — items 2 and 4 below remain open follow-ups if frequency is still low after the trend-filter deploy._

If diagnostics confirm the gates themselves are the bottleneck, change **one variable at a time** and measure over a fixed window before stacking the next change — the same discipline the project already uses for Hermes reflections, applied manually here since this is a multi-variable rollback, not a single-variable reflection.

Suggested order, highest expected impact first:

1. **Session filter**: narrow the block from 7 hours to 2–3 (e.g. 02:00–05:00 UTC) or drop it entirely and rely on the trend+confidence gates to filter low-quality Asian-session signals instead of blocking the window outright. This alone returns ~20% of the day.
2. **`min_indicators` 3 → 2**: keep `min_confidence: 0.5` as the primary quality gate; use indicator count as a sanity check, not an independent hard gate. With 11 indicators most already-firing setups clear confidence before they clear count-of-3.
3. **Trend filter ambiguous band**: widen `ambiguous_band_pct` 0.3% → 0.5–0.75%, or relax the daily/4h *agreement* requirement to a *bias* (daily sets direction, 4h just can't be strongly opposed, rather than must-actively-confirm). This was the biggest lever added in session 11, so it's the most likely single cause and the one to test most carefully.
4. **`max_trades_per_day` 3 → 5**: only matters once 1–3 are already producing more signals; raise the ceiling once there's something to hit it.

Do **not** touch `min_rr_ratio`, `min_tp_pct`, `min_profit_usd`, or the portfolio loss cap — those are quality/risk controls unrelated to the June loss pattern that caused the trend filter to be added in the first place. Loosening those would reopen the actual problem session 11 fixed.

## Part 5 — XRP-specific

XRP went live same-day without the agreed 10-paper-trade check. Recommend: pull XRP's actual trade count and win rate from the VPS diagnostic above. If it's under 10 trades, either it hasn't traded yet either (same gate-stack problem) or a few live trades already happened without the intended validation gate. Either way, worth a one-line decision recorded in Key Decisions once we see the data — not blocking the rest of this plan.

## Part 6 — Immediate Action Items

| # | Action | Owner |
|---|--------|-------|
| 1 | Run Part 3 diagnostic block on VPS | Linh |
| 2 | Reconcile uncommitted local changes (`snapshot.sh`, `state/goal.yaml`, `tools/dedup_trades.py` — modified but not committed, unclear if intentional) | Linh / next session |
| 3 | Share diagnostic output back | Linh |
| 4 | Implement Part 4 change #1 (session filter) locally, deploy, measure 3–5 days | Next code session |
| 5 | If frequency still low, implement change #2, then #3 — one at a time | Following sessions |

## Handoff

- **Status**: Diagnosis confirmed via live VPS diagnostic. Trend-filter soft-gate fix implemented locally and validated (`py_compile` + manual function tests reproducing the exact live BTC/XRP values). **Not yet deployed** — see `deploy-trend-filter-soft-gate-2026-07-03.md` for the 8-step deploy block.
- **Next agent**: read this file, then `deploy-trend-filter-soft-gate-2026-07-03.md`, then `memory.md` (session 12 entry). Run the deploy, then re-check trade frequency after 3–5 days before touching anything else.
- **Still open** (do not bundle into this deploy): BTC/ETH/SOL empty `trades.jsonl` / dead cron (flagged since session 11, still broken), SOL's `min_tp_pct` structural guard, ETH not appearing in the log sample, hyphen-clone `git pull` before next diff-based check.
- **Flag**: This entire document is for internal review — nothing here goes to a client, but per standing instruction it's flagged for Linh's sign-off before deploying.
