# Deploy: Trend filter soft-gate (session 12)
_2026-07-03. One command per block — run in order._

## What changed and why

Live diagnostic (session 12) showed BTC and XRP skipping **every single tick** for 3+ hours on `Trend filter: ambiguous or daily/4h mismatch` — daily EMA(20) and 4h EMA(50) were on opposite sides of price the entire window, not briefly crossing. That's a common multi-hour regime, not an edge case, so the hard AND-gate between them was choking entries to near zero.

Fix: daily EMA(20) bias remains a **hard gate** (this is what actually stopped the June countertrend losses — unchanged). 4h EMA(50) disagreement now **discounts confidence by 0.7x** instead of hard-skipping the tick. A strong multi-indicator setup can still clear `min_confidence` after the discount; a marginal one won't. Config added: `trend_filter.trend_4h_soft_discount: 0.7` in all 4 active yamls (BTC/ETH/SOL/XRP), tunable later like any other Hermes variable.

**Files changed** (validated with `py_compile`, verified against the live diagnostic's exact BTC/XRP EMA values):
- `hermes_trading/loop.py` — `_get_trend_direction()` now returns `(direction, confirmed)` instead of `str | None`; call site applies the soft discount when `confirmed` is `False`
- `state/btc_usdt/strategy.yaml`, `state/eth_usdt/strategy.yaml`, `state/sol_usdt/strategy.yaml`, `state/xrp_usdt/strategy.yaml` — added `trend_4h_soft_discount: 0.7`

**Not changed** (deliberately, per the plan doc's "one variable at a time" discipline): session filter, min_indicators, min_rr_ratio, min_tp_pct, min_profit_usd, portfolio loss cap, max_trades_per_day. If entry frequency is still too low after this deploy, the plan doc's next lever is the session filter window, not another trend-filter change layered on this one.

## Step 1 — Pull latest and diff against running code

```powershell
ssh root@187.127.108.173 "cd /opt/trading/hermes-trading && git pull && echo DONE"
```

## Step 2 — Backup current running loop.py + yamls before overwrite

```powershell
ssh root@187.127.108.173 "cp /opt/trading/hermes_trading/state/btc_usdt/strategy.yaml /tmp/hermes-backup-20260703/btc_usdt-strategy.yaml && cp /opt/trading/hermes_trading/state/eth_usdt/strategy.yaml /tmp/hermes-backup-20260703/eth_usdt-strategy.yaml && cp /opt/trading/hermes_trading/state/sol_usdt/strategy.yaml /tmp/hermes-backup-20260703/sol_usdt-strategy.yaml && cp /opt/trading/hermes_trading/state/xrp_usdt/strategy.yaml /tmp/hermes-backup-20260703/xrp_usdt-strategy.yaml && rm -f /tmp/hermes-backup-20260703/.-strategy.yaml && ls /tmp/hermes-backup-20260703 && echo DONE"
```

(No `$` anywhere in this one — PowerShell interpolates `$varname` inside double-quoted strings even with a backslash in front, since backslash isn't its escape character. Explicit paths sidestep the whole problem. The stray `.-strategy.yaml` from the previous attempt gets cleaned up too.)

## Step 3 — Copy new loop.py to the running (underscore) install

```powershell
scp C:\Users\nghil\Projects\Hermes\Hermes-Trading\hermes_trading\loop.py root@187.127.108.173:/opt/trading/hermes_trading/hermes_trading/loop.py
```

## Step 4 — Copy the 4 updated yamls

```powershell
scp C:\Users\nghil\Projects\Hermes\Hermes-Trading\state\btc_usdt\strategy.yaml root@187.127.108.173:/opt/trading/hermes_trading/state/btc_usdt/strategy.yaml
scp C:\Users\nghil\Projects\Hermes\Hermes-Trading\state\eth_usdt\strategy.yaml root@187.127.108.173:/opt/trading/hermes_trading/state/eth_usdt/strategy.yaml
scp C:\Users\nghil\Projects\Hermes\Hermes-Trading\state\sol_usdt\strategy.yaml root@187.127.108.173:/opt/trading/hermes_trading/state/sol_usdt/strategy.yaml
scp C:\Users\nghil\Projects\Hermes\Hermes-Trading\state\xrp_usdt\strategy.yaml root@187.127.108.173:/opt/trading/hermes_trading/state/xrp_usdt/strategy.yaml
```

## Step 5 — Validate syntax on VPS

```powershell
ssh root@187.127.108.173 "cd /opt/trading/hermes_trading && .venv/bin/python -m py_compile hermes_trading/loop.py && echo SYNTAX_OK && .venv/bin/python -c \"import yaml; [yaml.safe_load(open(f'state/{a}/strategy.yaml')) for a in ['btc_usdt','eth_usdt','sol_usdt','xrp_usdt']]; print('YAML_OK')\" && echo DONE"
```

## Step 6 — Restart the bot

```powershell
ssh root@187.127.108.173 "pkill -f hermes_trading.run; sleep 2; cd /opt/trading/hermes_trading && set -a && source .env && set +a && nohup .venv/bin/python -m hermes_trading.run >> bot.log 2>&1 & echo launched && echo DONE"
```

## Step 7 — Verify heartbeats came back fresh

```powershell
ssh root@187.127.108.173 "sleep 20 && for f in /opt/trading/hermes_trading/state/*/heartbeat.json; do echo $f; cat $f; echo; done && echo DONE"
```

## Step 8 — Smoke-check the new log line shows up (may take up to 15 min for first tick)

```powershell
ssh root@187.127.108.173 "sleep 900 && tail -100 /opt/trading/hermes_trading/bot.log | grep -iE 'trend filter|4h disagrees|FIRE' && echo DONE"
```

Expect to see either normal skip lines without the old "ambiguous or daily/4h mismatch (price=..., ema20d=..., ema50_4h=...)" combined format, or new "4h disagrees with daily ... bias ... confidence discounted to X% < min Y% — skipping" lines when 4h disagrees but the discounted confidence still isn't enough. Absence of a same-direction skip on every single tick for BTC/XRP over that window is the actual signal that this worked — check trade counts again in a few days rather than expecting an instant trade.

## Known follow-up items (separate from this deploy, do not bundle in)

1. **BTC/ETH/SOL `trades.jsonl` are empty on VPS** — this was flagged as a to-do at the end of session 11 and never actually fixed. Hermes reflection has no data to learn from for these 3 assets. Needs its own diagnostic + cron fix session.
2. **SOL is failing a different gate** (`Structural TP too thin: 0.44% < min 3.00%`) — worth a look at SOL's S/R computation or whether `min_tp_pct: 3.0` is appropriate for SOL's current range; unrelated to this trend-filter fix, don't change without separate review.
3. **ETH didn't appear in the 500-line log sample at all** — re-check once the bot's been running a few hours post-deploy.

## Handoff

- **Status**: Code + yaml changes complete locally, validated with `py_compile` and manual function tests against the live diagnostic's exact BTC/XRP values (confirmed the new function reproduces `('short', False)` for both, i.e. would have applied the soft discount instead of hard-skipping). Not yet deployed.
- **Next agent**: read this file, run steps 1–8, then re-run the Part 3 diagnostic block from `hermes-trading-improvement-plan-2026-07-03.md` after 3–5 days to check whether trade frequency actually recovered.
- **Read next**: `hermes-trading-improvement-plan-2026-07-03.md` for the full diagnosis, `memory.md` session 12 entry.
