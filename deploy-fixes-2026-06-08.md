# Deploy — Loss Investigation Fixes — 2026-06-08
_Rogue Night / Hermes-Trading. **Flagged for Linh's review before running.**_

Changes being deployed:
1. `min_confidence` raised to **0.5** for all 4 assets (was 0.3 BTC/SOL/TAO, 0.4 ETH)
2. Leverage fixed at **5x** for all assets (was confidence-scaled 3x–10x, deployed June 6 — identified as loss driver)
3. TAO `volume_spike.params.min_ratio` reverted **2.0 → 1.5** (VPS-side Hermes mutation based on hallucinated LLM reasoning)
4. TAO `bb_squeeze.weight` reverted **1.25 → 0.3** (VPS-side Hermes mutation with garbled LLM reasoning)
5. BTC `max_trades_per_day: 10` added (was missing from BTC yaml)

**One block per step. Do not combine.**

---

## Step 1 — Push local changes to GitHub (PowerShell)

```powershell
cd C:\Users\nghil\Projects\Hermes\Hermes-Trading
git add state/btc_usdt/strategy.yaml state/eth_usdt/strategy.yaml state/sol_usdt/strategy.yaml state/tao_usdt/strategy.yaml
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
ssh root@187.127.108.173 "STAMP=$(date +%s); for s in btc_usdt eth_usdt sol_usdt tao_usdt; do cp /opt/trading/hermes_trading/state/$s/strategy.yaml /tmp/${s}_strategy_bak_$STAMP.yaml; done; echo 'Backups:'; ls /tmp/*strategy_bak*"
```

Expected: 4 backup files listed.

---

## Step 4 — Diff hyphen vs running underscore yamls (sanity check)

```powershell
ssh root@187.127.108.173 "for s in btc_usdt eth_usdt sol_usdt tao_usdt; do echo '--- '$s' ---'; diff /opt/trading/hermes-trading/state/$s/strategy.yaml /opt/trading/hermes_trading/state/$s/strategy.yaml; done"
```

The diffs will show what the VPS has evolved vs what we're deploying. Confirm TAO shows `bb_squeeze weight: 1.25` and `volume_spike min_ratio: 2.0` in the running copy — these are the bad mutations we're reverting.

---

## Step 5 — Apply confidence + leverage changes to VPS yamls

Apply the common changes (min_confidence, min_leverage, max_leverage, max_trades_per_day) to all 4 running yamls on VPS using in-place Python edits (safe — no clobber of Hermes-evolved values, just the specific keys we want changed).

```powershell
ssh root@187.127.108.173 "cd /opt/trading/hermes_trading && source .venv/bin/activate && python3 - <<'PYEOF'
import yaml

assets = ['btc_usdt', 'eth_usdt', 'sol_usdt', 'tao_usdt']
base = 'state'

for asset in assets:
    path = f'{base}/{asset}/strategy.yaml'
    with open(path) as f:
        data = yaml.safe_load(f)
    
    # min_confidence
    if 'entry' in data:
        old = data['entry'].get('min_confidence')
        data['entry']['min_confidence'] = 0.5
        print(f'{asset}: min_confidence {old} -> 0.5')
    
    # leverage fixed at 5x
    data['min_leverage'] = 5
    data['max_leverage'] = 5
    print(f'{asset}: leverage fixed to 5x')
    
    # max_trades_per_day
    if 'max_trades_per_day' not in data:
        data['max_trades_per_day'] = 10
        print(f'{asset}: added max_trades_per_day 10')
    
    with open(path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

print('Done.')
PYEOF"
```

Expected: 4 assets each showing confidence and leverage change lines.

---

## Step 6 — Revert TAO Hermes mutations on VPS

Revert `bb_squeeze.weight 1.25 → 0.3` and `volume_spike.min_ratio 2.0 → 1.5` in the running TAO yaml. These mutations were based on hallucinated LLM reasoning (see investigation report).

```powershell
ssh root@187.127.108.173 "cd /opt/trading/hermes_trading && source .venv/bin/activate && python3 - <<'PYEOF'
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
PYEOF"
```

Expected: `bb_squeeze weight: 1.25 -> 0.3` and `volume_spike min_ratio: 2.0 -> 1.5`.

---

## Step 7 — Verify all VPS yamls look correct

```powershell
ssh root@187.127.108.173 "for s in btc_usdt eth_usdt sol_usdt tao_usdt; do echo '=== '$s' ==='; grep -E 'min_confidence|min_leverage|max_leverage|max_trades_per_day' /opt/trading/hermes_trading/state/$s/strategy.yaml; done && echo '--- TAO indicators ---' && grep -A3 'bb_squeeze\|volume_spike' /opt/trading/hermes_trading/state/tao_usdt/strategy.yaml"
```

Expected for each asset: `min_confidence: 0.5`, `min_leverage: 5`, `max_leverage: 5`, `max_trades_per_day: 10`.
Expected TAO: `bb_squeeze weight: 0.3`, `volume_spike min_ratio: 1.5`.

---

## Step 8 — Restart the bot

```powershell
ssh root@187.127.108.173 "pkill -f hermes_trading.run; sleep 2; cd /opt/trading/hermes_trading && set -a && source .env && set +a && nohup .venv/bin/python -m hermes_trading.run >> bot.log 2>&1 & sleep 3 && ps -ef | grep hermes_trading.run | grep -v grep && echo '--- last 20 log lines ---' && tail -20 bot.log"
```

Expected: fresh PID + 4 worker boot lines in log.

---

## Step 9 — Verify ticking (run ~16 minutes after Step 8)

```powershell
ssh root@187.127.108.173 "tail -40 /opt/trading/hermes_trading/bot.log"
```

Look for lines like `BTC/USDT | dir=both · conf=NN%`. Confirm conf% shown is based on the new 50% threshold (entries should be fewer and higher-quality than before).

---

## Expected behaviour after deploy

- Fewer trade entries overall — signals that previously fired at 33–48% confidence will now be blocked
- No entries at all under 50% confidence across all 4 assets
- Leverage fixed at 5x regardless of confidence score
- TAO volume_spike restores sensitivity to volume (min_ratio 1.5 vs the over-restrictive 2.0)
- Bot will accumulate trades toward the next Hermes reflection trigger; future mutations will be sanity-checked (old_value guard, deployed session 8)
