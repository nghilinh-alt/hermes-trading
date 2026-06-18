# Deploy — Loss Investigation Fixes — 2026-06-08
_Rogue Night / Hermes-Trading. **Flagged for Linh's review before running.**_

Changes being deployed:
1. `min_confidence` raised to **0.5** for all 4 assets (was 0.3 BTC/SOL/TAO, 0.4 ETH)
2. Leverage now **piecewise-interpolated** from a `leverage_curve` in the yaml: 50%→4x, 60%→5x, 75%→7x, 100%→8x (was linear 3x–10x)
3. TAO `volume_spike.params.min_ratio` reverted **2.0 → 1.5** (VPS-side Hermes mutation based on hallucinated LLM reasoning)
4. TAO `bb_squeeze.weight` reverted **1.25 → 0.3** (VPS-side Hermes mutation with garbled LLM reasoning)
5. `max_trades_per_day` set to **3** for all assets (was 10, or missing on BTC)
6. New `_confidence_to_leverage()` helper in `execution.py` implements the piecewise curve

**One block per step. Do not combine.**
**All SSH commands use `@'...'@ | ssh ... bash` to avoid PowerShell expanding `$(...)`.**

---

## Step 1 — Push local changes to GitHub (PowerShell)

```powershell
cd C:\Users\nghil\Projects\Hermes\Hermes-Trading
git add state/btc_usdt/strategy.yaml state/eth_usdt/strategy.yaml state/sol_usdt/strategy.yaml state/tao_usdt/strategy.yaml
git add hermes_trading/adapters/execution.py
git add deploy-fixes-2026-06-08.md investigation-losing-trades-2026-06-08.md memory.md
git status
```

```powershell
git commit -m "fix: raise min_confidence 0.5, fix leverage 5x, revert TAO Hermes mutations"
git push origin master
```

---

## Step 2 — Git pull on VPS hyphen clone

```powershell
ssh root@187.127.108.173 "cd /opt/trading/hermes-trading && git pull"
```

Expected: fast-forward showing the 4 strategy.yaml changes.

---

## Step 3 — Backup current VPS strategy.yamls

```powershell
@'
STAMP=$(date +%s)
for s in btc_usdt eth_usdt sol_usdt tao_usdt; do
  cp /opt/trading/hermes_trading/state/$s/strategy.yaml /tmp/${s}_strategy_bak_${STAMP}.yaml
done
echo Backups:
ls /tmp/*strategy_bak*
echo DONE
'@ | ssh root@187.127.108.173 bash
```

Expected: 4 backup files listed with a timestamp in the name.

---

## Step 4 — Diff hyphen vs running underscore yamls (sanity check)

```powershell
@'
for s in btc_usdt eth_usdt sol_usdt tao_usdt; do
  echo "--- $s ---"
  diff /opt/trading/hermes-trading/state/$s/strategy.yaml /opt/trading/hermes_trading/state/$s/strategy.yaml
done
echo DONE
'@ | ssh root@187.127.108.173 bash
```

The diffs will show what the VPS has evolved vs what we're deploying. Confirm TAO shows `bb_squeeze weight: 1.25` and `volume_spike min_ratio: 2.0` in the running copy — these are the bad mutations we're reverting. Also confirm confidence and leverage differences.

---

## Step 5a — Deploy updated execution.py to VPS

```powershell
ssh root@187.127.108.173 "cp /opt/trading/hermes-trading/hermes_trading/adapters/execution.py /opt/trading/hermes_trading/hermes_trading/adapters/execution.py && cd /opt/trading/hermes_trading && .venv/bin/python -m py_compile hermes_trading/adapters/execution.py && echo py_compile OK"
```

Expected: `py_compile OK`. If it errors, stop — do not proceed to Step 5b.

---

## Step 5b — Apply yaml changes to all 4 VPS running yamls

```powershell
@'
cd /opt/trading/hermes_trading
source .venv/bin/activate
python3 - <<'PYEOF'
import yaml

assets = ['btc_usdt', 'eth_usdt', 'sol_usdt', 'tao_usdt']
base = 'state'
curve = [[0.5, 4], [0.6, 5], [0.75, 7], [1.0, 8]]

for asset in assets:
    path = f'{base}/{asset}/strategy.yaml'
    with open(path) as f:
        data = yaml.safe_load(f)

    if 'entry' in data:
        old = data['entry'].get('min_confidence')
        data['entry']['min_confidence'] = 0.5
        print(f'{asset}: min_confidence {old} -> 0.5')

    data['leverage_curve'] = curve
    data.pop('min_leverage', None)
    data.pop('max_leverage', None)
    print(f'{asset}: leverage_curve set (50%->4x 60%->5x 75%->7x 100%->8x)')

    data['max_trades_per_day'] = 3
    print(f'{asset}: max_trades_per_day = 3')

    with open(path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

print('Done.')
PYEOF
echo DONE
'@ | ssh root@187.127.108.173 bash
```

Expected: 4 assets each showing confidence, leverage_curve, and max_trades_per_day lines.

---

## Step 6 — Revert TAO Hermes mutations on VPS

```powershell
@'
cd /opt/trading/hermes_trading
source .venv/bin/activate
python3 - <<'PYEOF'
import yaml

path = 'state/tao_usdt/strategy.yaml'
with open(path) as f:
    data = yaml.safe_load(f)

for ind in data.get('indicators', []):
    if ind['name'] == 'bb_squeeze':
        old = ind['weight']
        ind['weight'] = 0.3
        print(f'bb_squeeze weight: {old} -> 0.3')
    if ind['name'] == 'volume_spike':
        old = ind.get('params', {}).get('min_ratio')
        ind.setdefault('params', {})['min_ratio'] = 1.5
        print(f'volume_spike min_ratio: {old} -> 1.5')

with open(path, 'w') as f:
    yaml.dump(data, f, default_flow_style=False, sort_keys=False)
print('Done.')
PYEOF
echo DONE
'@ | ssh root@187.127.108.173 bash
```

Expected: `bb_squeeze weight: 1.25 -> 0.3` and `volume_spike min_ratio: 2.0 -> 1.5`.

---

## Step 7 — Verify all VPS yamls look correct

```powershell
@'
for s in btc_usdt eth_usdt sol_usdt tao_usdt; do
  echo "=== $s ==="
  grep -E 'min_confidence|min_leverage|max_leverage|max_trades_per_day' /opt/trading/hermes_trading/state/$s/strategy.yaml
done
echo "--- TAO indicators ---"
grep -A3 'bb_squeeze\|volume_spike' /opt/trading/hermes_trading/state/tao_usdt/strategy.yaml
echo DONE
'@ | ssh root@187.127.108.173 bash
```

Expected for each asset: `min_confidence: 0.5`, `max_trades_per_day: 3`, `leverage_curve` list present.
Expected TAO: `weight: 0.3` under bb_squeeze, `min_ratio: 1.5` under volume_spike.

---

## Step 8 — Restart the bot

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

Expected: fresh PID + 4 worker boot lines in log.

---

## Step 9 — Verify ticking (run ~16 minutes after Step 8)

```powershell
ssh root@187.127.108.173 "tail -40 /opt/trading/hermes_trading/bot.log"
```

Look for lines like `BTC/USDT | dir=both · conf=NN%`. With min_confidence 0.5, most ticks should show `conf=XX% < 50% — skip` until a genuine signal fires.

---

## Expected behaviour after deploy

- Fewer trade entries overall — signals that previously fired at 33–48% confidence will now be blocked
- No entries at all under 50% confidence across all 4 assets
- Leverage piecewise: 50%→4x, 55%→5x, 60%→5x, 70%→6x, 75%→7x, 90%→8x, 100%→8x
- Max 3 trades per day per token
- TAO volume_spike restores sensitivity to volume (min_ratio 1.5 vs the over-restrictive 2.0)
- Bot will accumulate trades toward the next Hermes reflection trigger; future mutations will be sanity-checked (old_value guard, deployed session 8)
