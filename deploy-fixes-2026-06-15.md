# Deploy — Reflection Overhaul + Daily Limit Fix — 2026-06-15
_Rogue Night / Hermes-Trading. **Flagged for Linh's review before running.**_

Changes being deployed:
1. `reflect.py` — **Priority 0 directional triage**: if one direction has <30% win rate vs >45% on the other, `entry.direction` flips to the winning side. Re-enables `both` when both directions recover to >45%. All existing priorities (1–4) now guard on `changed_var is None` so they don't fire when triage already acted.
2. `loop.py` — **`max_trades_per_day` now enforced**: new `_count_todays_trades()` helper counts non-abandoned today-UTC trades and gates `_execute_trade` when the limit is reached. Previously the field existed in YAML but was ignored.
3. `state/*/strategy.yaml` — **`min_indicators` raised 2 → 3** on all four assets.

**One block per step. Do not combine.**
**All SSH commands use `@'...'@ | ssh ... bash` to avoid PowerShell expanding `$(...)`.**

---

## Step 1 — Push local changes to GitHub (PowerShell)

```powershell
cd C:\Users\nghil\Projects\Hermes\Hermes-Trading
git add hermes_trading/reflect.py hermes_trading/loop.py
git add state/btc_usdt/strategy.yaml state/eth_usdt/strategy.yaml state/sol_usdt/strategy.yaml state/tao_usdt/strategy.yaml
git add deploy-fixes-2026-06-15.md
git status
```

```powershell
git commit -m "fix: directional triage in reflect, enforce max_trades_per_day, min_indicators 3"
git push origin master
```

---

## Step 2 — Git pull on VPS hyphen clone

```powershell
ssh root@187.127.108.173 "cd /opt/trading/hermes-trading && git pull"
```

Expected: fast-forward showing `hermes_trading/reflect.py`, `hermes_trading/loop.py`, and the 4 strategy.yaml files.

---

## Step 3 — Copy code files to running dir + syntax check

```powershell
@'
cp /opt/trading/hermes-trading/hermes_trading/reflect.py /opt/trading/hermes_trading/hermes_trading/reflect.py
cp /opt/trading/hermes-trading/hermes_trading/loop.py /opt/trading/hermes_trading/hermes_trading/loop.py
cd /opt/trading/hermes_trading
.venv/bin/python -m py_compile hermes_trading/reflect.py && echo "reflect.py OK"
.venv/bin/python -m py_compile hermes_trading/loop.py && echo "loop.py OK"
echo DONE
'@ | ssh root@187.127.108.173 bash
```

Expected: `reflect.py OK` then `loop.py OK`. If either errors, stop — do not proceed.

---

## Step 4 — Apply min_indicators 3 to all running VPS yamls

```powershell
@'
cd /opt/trading/hermes_trading
source .venv/bin/activate
python3 - <<'PYEOF'
import yaml

assets = ['btc_usdt', 'eth_usdt', 'sol_usdt', 'tao_usdt']
for asset in assets:
    path = f'state/{asset}/strategy.yaml'
    with open(path) as f:
        data = yaml.safe_load(f)
    entry = data.get('entry', {})
    old = entry.get('min_indicators', '?')
    entry['min_indicators'] = 3
    data['entry'] = entry
    with open(path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    print(f'{asset}: min_indicators {old} -> 3')
print('Done.')
PYEOF
echo DONE
'@ | ssh root@187.127.108.173 bash
```

Expected: 4 lines, one per asset, showing `min_indicators X -> 3`.

---

## Step 5 — Verify running yamls

```powershell
@'
for s in btc_usdt eth_usdt sol_usdt tao_usdt; do
  echo "=== $s ==="
  grep -E 'min_indicators|min_confidence|max_trades_per_day' /opt/trading/hermes_trading/state/$s/strategy.yaml
done
echo DONE
'@ | ssh root@187.127.108.173 bash
```

Expected for each asset: `min_indicators: 3`, `min_confidence: 0.5`, `max_trades_per_day: 3`.

---

## Step 6 — Restart the bot

```powershell
@'
pkill -f hermes_trading.run
sleep 2
cd /opt/trading/hermes_trading
set -a; source .env; set +a
nohup .venv/bin/python -m hermes_trading.run >> bot.log 2>&1 &
sleep 3
ps -ef | grep hermes_trading.run | grep -v grep
echo "--- last 20 log lines ---"
tail -20 bot.log
echo DONE
'@ | ssh root@187.127.108.173 bash
```

Expected: fresh PID + 4 worker boot lines.

---

## Step 7 — Verify ticking (~16 min after Step 6)

```powershell
ssh root@187.127.108.173 "tail -40 /opt/trading/hermes_trading/bot.log"
```

Look for entries that skip due to `Daily limit reached (3/3)` for assets that already had 3 trades today, and fewer entry fires overall from the tighter `min_indicators: 3` gate.

---

## Expected behaviour after deploy

- TAO (and any asset with a lopsided short vs long win rate) will have `entry.direction` flipped to the winning side on its next reflection trigger (every 5 closed trades). The bleed of repeated-same-direction SL hits stops at the reflection point instead of running indefinitely.
- Daily trade cap of 3 per asset now actively enforced — was previously dead config.
- `min_indicators: 3` filters out the weakest 2-indicator combos that were clearing the 50% confidence gate by pairing only RSI + ema_trend.
