# Deploy: Position sizing — 10% floor, scales up with confidence (session 12)
_2026-07-03. Run AFTER the trend-filter soft-gate deploy is confirmed working — don't bundle these._

## What changed and why

Linh asked for a minimum trade size of 10% of available margin (e.g. $80 floor at $800 balance), then asked to make it scale up on stronger signals rather than stay fixed.

Previously `_position_based_sizing()` always used exactly `position_pct` (0.10) — never more, never less. Now `position_pct` is a **floor**: `_confidence_to_position_pct()` interpolates over a new `position_pct_curve` (same piecewise-linear pattern as the existing `leverage_curve`) and clamps the result to never go below the base `position_pct`. Yamls without a `position_pct_curve` behave exactly as before (flat 10%) — this only activates where the curve is present.

Curve added to all 4 active yamls:
```
position_pct: 0.10
position_pct_curve:
- - 0.5
  - 0.10
- - 0.75
  - 0.15
- - 1.0
  - 0.20
```
At $800 balance: 50% confidence → $80 notional (floor, unchanged), 60% → $96, 75% → $120, 100% → $160.

**Files changed** (`py_compile` clean; `_confidence_to_position_pct` unit-checked against 6 cases including the floor clamp and the no-curve fallback):
- `hermes_trading/adapters/execution.py` — new `_confidence_to_position_pct()`; `_position_based_sizing()` takes a `confidence` param; `place_live_trade()` passes `ed.get("confidence", 0.0)` through
- `state/btc_usdt/strategy.yaml`, `state/eth_usdt/strategy.yaml`, `state/sol_usdt/strategy.yaml`, `state/xrp_usdt/strategy.yaml` — added `position_pct_curve`

**Not changed**: the leverage formula still keys off the base `position_pct` (10%), not the scaled-up effective value — leverage is set for margin efficiency and doesn't affect realized exposure/risk in this code's model (qty is derived straight from notional), so this was left alone deliberately.

## Step 1 — Backup current running files

```powershell
ssh root@187.127.108.173 "cp /opt/trading/hermes_trading/hermes_trading/adapters/execution.py /tmp/hermes-backup-20260703/execution.py.bak && cp /opt/trading/hermes_trading/state/btc_usdt/strategy.yaml /tmp/hermes-backup-20260703/btc_usdt-strategy-presizing.yaml && cp /opt/trading/hermes_trading/state/eth_usdt/strategy.yaml /tmp/hermes-backup-20260703/eth_usdt-strategy-presizing.yaml && cp /opt/trading/hermes_trading/state/sol_usdt/strategy.yaml /tmp/hermes-backup-20260703/sol_usdt-strategy-presizing.yaml && cp /opt/trading/hermes_trading/state/xrp_usdt/strategy.yaml /tmp/hermes-backup-20260703/xrp_usdt-strategy-presizing.yaml && ls /tmp/hermes-backup-20260703 && echo DONE"
```

## Step 2 — Copy the updated execution.py

```powershell
scp C:\Users\nghil\Projects\Hermes\Hermes-Trading\hermes_trading\adapters\execution.py root@187.127.108.173:/opt/trading/hermes_trading/hermes_trading/adapters/execution.py
```

## Step 3 — Copy the 4 updated yamls

```powershell
scp C:\Users\nghil\Projects\Hermes\Hermes-Trading\state\btc_usdt\strategy.yaml root@187.127.108.173:/opt/trading/hermes_trading/state/btc_usdt/strategy.yaml
scp C:\Users\nghil\Projects\Hermes\Hermes-Trading\state\eth_usdt\strategy.yaml root@187.127.108.173:/opt/trading/hermes_trading/state/eth_usdt/strategy.yaml
scp C:\Users\nghil\Projects\Hermes\Hermes-Trading\state\sol_usdt\strategy.yaml root@187.127.108.173:/opt/trading/hermes_trading/state/sol_usdt/strategy.yaml
scp C:\Users\nghil\Projects\Hermes\Hermes-Trading\state\xrp_usdt\strategy.yaml root@187.127.108.173:/opt/trading/hermes_trading/state/xrp_usdt/strategy.yaml
```

## Step 4 — Validate syntax on VPS

```powershell
ssh root@187.127.108.173 "cd /opt/trading/hermes_trading && .venv/bin/python -m py_compile hermes_trading/adapters/execution.py && echo SYNTAX_OK && echo DONE"
```

## Step 5 — Restart the bot

```powershell
ssh root@187.127.108.173 "pkill -f hermes_trading.run; sleep 2; cd /opt/trading/hermes_trading && set -a && source .env && set +a && nohup .venv/bin/python -m hermes_trading.run >> bot.log 2>&1 & echo launched && echo DONE"
```

## Step 6 — Verify heartbeats

```powershell
ssh root@187.127.108.173 "sleep 20 && for f in /opt/trading/hermes_trading/state/*/heartbeat.json; do echo $f; cat $f; echo; done && echo DONE"
```

## Handoff

- **Status**: Code + yaml changes complete locally, validated with `py_compile` and manual tests of `_confidence_to_position_pct`. Not deployed.
- **Depends on**: nothing — independent of the trend-filter deploy, but hold off until that one's confirmed stable so any behavior change is attributable to one fix at a time.
- **Next agent**: read this file, then check the actual per-trade notional against expectation once a trade fires (qty × entry_price in the trade record should be ≥ 10% of whatever the account's free USDT balance was at that moment — see the open question in chat about the $20 XRP trade before assuming $800 is the current balance).
