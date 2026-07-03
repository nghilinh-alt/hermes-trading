# Deploy: Position sizing — 10% margin floor, scales up with confidence, leverage now actually applied (session 12)
_2026-07-03. Run AFTER the trend-filter soft-gate deploy is confirmed working — don't bundle these._

## ⚠ Magnitude of this change

This deploy fixes a real bug (see below), not just adds a feature. **Every trade's actual notional/exposure will be 3–8x bigger than it's been for the past several weeks** (matching leverage, which was being set on the exchange but never actually applied to position size). Dollar risk-per-trade stays at the designed ~2% of balance target regardless — that math is what's being fixed — but the position sizes you see on the exchange will look much larger than what you're used to. Read this whole doc before running it.

## What changed and why

Linh asked for a minimum trade size of 10% of available margin, then to scale it up on stronger signals. While implementing, we found the actual last XRP trade (2026-06-25) had `qty=68.3` at `entry=1.0373`, `leverage=4` → notional $70.85 on what backs out to a ~$708 balance — exactly `balance × position_pct` with **no leverage applied at all**. But the sizing docstring's own worked examples (written in session 11) always claimed leverage-scaled numbers like "$640 notional at 8x leverage" on an $80 base. The code never did that multiplication — `leverage` was passed to `exchange.set_leverage()` but had zero effect on `qty`. Real trades have been running at a fraction of the intended risk/exposure since session 11.

**Fixed formula** (`_position_based_sizing()` in execution.py):
```
margin              = balance × effective_pct     (effective_pct >= position_pct floor)
leverage            = clamp(risk_pct / (position_pct × sl_dist_pct), min_lev, max_lev)
position_notional    = margin × leverage           ← the fix: this multiplication was missing
qty                 = position_notional / entry_price
```
Verified against the docstring's own $800-balance worked examples — SL 2.5%→8x→$640 notional→$16 risk; SL 4.0%→5x→$400→$16; SL 5.0%→4x→$320→$16. All three now come out exactly right (previously all three would have been $80 regardless of SL distance or leverage).

**Confidence scaling** (the original ask): `position_pct` (10%) is now a floor on the *margin* commitment, not a fixed value. `_confidence_to_position_pct()` interpolates over `position_pct_curve` (same piecewise-linear pattern as the existing `leverage_curve`), clamped to never go below the base `position_pct`. Yamls without a `position_pct_curve` fall back to the flat floor.

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
At $800 balance, SL 2.5% (8x leverage): 50% confidence → $80 margin → $640 notional → $16 risk (floor, matches original design). 100% confidence → $160 margin → $1280 notional → $32 risk.

**Files changed** (`py_compile` clean; verified against 4 cases reproducing the docstring's own worked examples plus the confidence-scaling case — all exact):
- `hermes_trading/adapters/execution.py` — new `_confidence_to_position_pct()`; `_position_based_sizing()` now multiplies margin by leverage to get notional, takes a `confidence` param; `place_live_trade()` passes `ed.get("confidence", 0.0)` through
- `state/btc_usdt/strategy.yaml`, `state/eth_usdt/strategy.yaml`, `state/sol_usdt/strategy.yaml`, `state/xrp_usdt/strategy.yaml` — added `position_pct_curve`

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

- **Status**: Code + yaml changes complete locally, validated with `py_compile` and against both the docstring's own worked examples and the confidence-scaling case (all exact). Not deployed.
- **Depends on**: nothing — independent of the trend-filter deploy, but hold off until that one's confirmed stable so any behavior change is attributable to one fix at a time.
- **Before running**: re-read the magnitude warning at the top. Position notional will be 3–8x bigger than what's been live for weeks. Consider whether you want to watch the first trade closely after this deploys rather than walking away.
- **Next agent**: read this file, then check the actual per-trade notional once a trade fires — `qty × entry_price` in the trade record should now equal `(balance × effective_pct) × leverage`, not just `balance × effective_pct`.
