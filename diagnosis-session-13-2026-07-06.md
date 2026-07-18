# Diagnosis: "Still no trades" — session 13 (2026-07-06)

_Read-only diagnostic. This sandbox has no VPS network access (confirmed again this session — `/dev/tcp/187.127.108.173/22` unreachable), so as with session 12, Linh needs to run this and paste back the output._

## Context

3 days ago (2026-07-03, session 12) the trend-filter hard AND-gate was changed to a soft confidence discount and deployed live, specifically to fix BTC/XRP hard-skipping every tick. The deploy doc's own handoff said to re-check trade frequency after 3–5 days before touching anything else — Linh's "still no trades" question today is exactly that checkpoint, landing on day 3.

Also still separately pending from session 12, **not yet deployed**, and unrelated to trade frequency: the leverage/position-sizing fix (`d8a49c7`, `75ef76b` in local git history) was held back deliberately to isolate the trend-filter fix's effect. It would not explain zero trades either way.

## What this diagnostic checks

1. Bot actually running, heartbeats fresh (rule out a silent crash/outage first)
2. Trade counts per asset since the 07-03 deploy
3. Which gate is dominating skips now — confidence, indicator count, trend filter (should show the new soft-discount log format, not the old hard-skip), `min_tp_pct` (SOL's known separate issue), session filter, or `max_portfolio_daily_loss_usd`
4. Confirms the soft-discount code is actually the version running (not reverted by a restart pulling stale code)
5. BTC/ETH/SOL `trades.jsonl` — still flagged broken since session 11; check if still 0 lines

## Run this on the VPS

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
echo --- skip-reason sample, last 500 lines ---
tail -500 /opt/trading/hermes_trading/bot.log | grep -iE "skip|trend filter|4h disagrees|confidence|min_tp|portfolio.*loss|FIRE" | tail -80
echo DONE
'@ | ssh root@187.127.108.173 bash
```

(PowerShell here-string per doctrine — avoids the `$f`/`$varname` mangling that broke the quoted-string version of this same block twice in session 12.)

## What the answer determines

- **Bot down / heartbeats stale** → that's the whole story, fix the outage first, ignore everything below.
- **Trades firing now, just few** → soft-discount fix worked, this is a tuning/patience question, not a bug. Compare skip-reason tally against session 12's Part 2 gate-stack table (`hermes-trading-improvement-plan-2026-07-03.md`) to see which gate is now binding.
- **Still 100% skip on the same trend-filter line** → the soft-discount code didn't actually make it into the running process (grep for `trend_4h_soft_discount` in step 4 will show empty if so) — likely a deploy step was missed or a later restart pulled from the stale hyphen clone instead of picking up the underscore install's edited file.
- **Skips now dominated by a different gate** (`min_confidence`, `min_indicators`, `min_tp_pct`, session filter, portfolio loss cap) → trend filter fix worked as intended, next lever is whichever gate the tally shows, per Part 4 of the improvement plan doc — one variable at a time, not stacked.

## Handoff

- **Status**: Diagnostic written, not yet run. No code or config changed this session.
- **Next agent**: read this file + `hermes-trading-improvement-plan-2026-07-03.md` Part 2/4 once Linh pastes back the output. Do not change any gate until the skip-reason tally is in hand.
- **Flag**: Internal diagnosis only — nothing here goes to a client; standard sign-off from Linh applies before any subsequent deploy.
