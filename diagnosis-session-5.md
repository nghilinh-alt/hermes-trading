# Hermes-Trading — Diagnosis (session 5, 2026-05-27)

_Prepared for Linh's review. No code or YAML has been modified. This is read-only analysis + a proposed safe next step._

## TL;DR

There are **two independent bugs** producing the "0% confidence, no entries" symptom, and **one cosmetic exchange error** for leverage. None of them require touching execution.py.

| # | Bug | Severity | Where | Fix path |
|---|-----|----------|-------|----------|
| 1 | ETH/SOL/TAO `state/<asset>/strategy.yaml` are in a **legacy flat schema** — no `indicators:` registry. loop.py falls into a hard-coded long-only RSI-below-30 fallback that can never fire while RSI sits at 60–75. | Critical (3 of 4 assets permanently idle) | `state/eth_usdt/strategy.yaml`, `state/sol_usdt/strategy.yaml`, `state/tao_usdt/strategy.yaml` | Apply `patch_strategies.py` (YAML only — no Python touched) |
| 2 | BTC `state/btc_usdt/strategy.yaml` v02 still has `rsi.required: true, threshold: 30`. The "long" leg can never fire while RSI > 30. Memory.md claimed session 3 demoted RSI to optional, but the local file disagrees. | High (BTC long entries blocked) | `state/btc_usdt/strategy.yaml` lines 8–12 | Apply `patch_strategies.py` |
| 3 | `set_leverage` raises on retCode 110043 ("leverage not modified") because `execution.py:243` only catches `ccxt.AuthenticationError`. 110043 is informational — it means leverage is already at the requested value. | Low (cosmetic; doesn't block trades) | `execution.py:237–246` | Idempotent wrapper (small, isolated edit) |

## Evidence

### Bug 1 — ETH/SOL/TAO strategies are the wrong shape

`hermes_trading/loop.py:240`–`261` (`_evaluate_entry`):

```python
indicators = strategy.get("indicators", [])
...
if not indicators:
    rsi       = price_data.get("rsi_14", 50.0)
    threshold = float(entry.get("threshold", 30))
    fired     = rsi < threshold if direction == "long" else rsi > threshold
    indicators_fired["rsi"] = fired
    return _result(fired, 1.0 if fired else 0.0)
```

`state/eth_usdt/strategy.yaml` (and sol/tao — same shape) has:

```yaml
asset: ETH/USDT
position_size_r: 0.8
risk_per_trade: 0.10
stop_loss_pct: 2.0
max_sl_pct: 5.0
take_profit_pct: 5.0
trailing_stop_r: 0.5
max_leverage: 5
use_fbb: false
risk_adjustment: "fixed"
rsi_entry_overbought: 70
rsi_entry_oversold: 30
max_trades_per_day: 10
```

There is **no `indicators:` block and no `entry:` block**. So:
- `strategy.get("indicators", [])` → `[]` → falls into the legacy branch
- `entry = {}` → `direction` defaults to `"long"` (the `or` in `direction = force_direction or entry.get("direction", "long")`)
- `threshold` defaults to **30**
- `rsi_14` reported as 60–75 → `rsi < 30` is False → `fires=False`, `confidence=0.0`

Since `direction` defaults to `"long"` (not `"both"`), the loop never even evaluates the short side for these three assets. This is exactly the "RSI 60–75, confidence 0%, no entries" pattern described.

`patch_strategies.py` is the intended remediation (memory.md sessions 3 + 4), but these YAML files clearly never received that patch. Most likely cause: the file was scaffolded from a different template after the patch was attempted, or the script was never run in the directory containing the per-asset state dirs.

### Bug 2 — BTC has the registry but RSI is still hard-gated

`state/btc_usdt/strategy.yaml`:

```yaml
version: "02"
entry:
  direction: both
  min_confidence: 0.3
indicators:
  - name: rsi
    required: true          # ← session 3 claimed to remove this; local file still True
    weight: 1.0
    params:
      threshold: 30
  ...
```

In `_evaluate_entry`, when `required: true` and the result is False, `fires` is set False (line 281). For the long leg, `rsi < 30` is False at RSI 60–75 → long entry blocked outright.

For the short leg, `rsi > 30` is trivially True at RSI 60–75 → the required gate passes, but it's not a meaningful short filter (a real short signal wants RSI > 70). The short leg then needs `optional_passed / 3.2 ≥ 0.3` (i.e. ≥0.96 weight worth of optionals firing) **and** `min_indicators ≥ 1`. In a sideways-to-bullish tape (RSI mid-range, price > VWAP, price > EMA, MACD line > signal), the long-biased optional indicators score high, but they're being evaluated in *short* direction where the comparisons invert and most return False. Net result: confidence often does come out near zero on the short leg too.

`min_indicators` is also missing from the BTC YAML — defaults to 1 in loop.py, but `patch_strategies.py` would set it to 2.

### Bug 3 — leverage 110043 not idempotent

`hermes_trading/adapters/execution.py:237–244`:

```python
leverage_val = strategy.get("default_leverage")
if exchange.id != "bybit" or leverage_val:
    try:
        leverage = int(leverage_val or 5)
        exchange.set_leverage(leverage, symbol)
    except ccxt.AuthenticationError:
        pass
```

Bybit retCode 110043 (`leverage not modified`) surfaces through ccxt as `ccxt.ExchangeError`, not `AuthenticationError`. So the second and subsequent calls per asset (once the value has been set once) raise and propagate up to the loop's broad `except`, getting logged as red error spam. Trade placement itself isn't affected — the exception fires before `create_order` — but the loop currently treats it as a tick failure and bumps the consecutive-failure counter.

There's also a small latent bug on line 246: `leverage_display = leverage or strategy.get("default_leverage", 5)` references `leverage` outside the `try` block, so if the `if` was False (no `default_leverage` set, Bybit), `leverage` is undefined and this line will `NameError`. This is currently masked because `default_leverage` exists on the VPS strategies after patch_strategies.py ran there.

## Why memory.md and the live state disagree

`patch_strategies.py` is correct and idempotent. The discrepancy is that:

1. The local working tree's YAML files were not modified by it (the script needs to be invoked, not just committed).
2. The VPS may have been patched in session 3, but if a later deploy SCP'd unpatched files over the patched ones, the VPS would be back to the old shape too. The session-4 deploy block in memory.md does not include the per-asset `state/<slug>/strategy.yaml` files — it only copies `.py` files and `state/goal.yaml`. So the VPS strategies should still be the patched versions if `patch_strategies.py` was actually executed there.

We don't currently know whether the VPS strategies match the local ones. That's an empirical question — checking `/opt/trading/hermes_trading/hermes_trading/state/btc_usdt/strategy.yaml` on the VPS will answer it.

## Proposed safe next step (3 small, reversible actions)

I am **not** doing any of these without Linh's go-ahead.

### Step 1 — Verify VPS strategy state (read-only, zero risk)

```bash
ssh root@187.127.108.173 \
  'for a in btc_usdt eth_usdt sol_usdt tao_usdt; do
     echo "=== $a ===";
     head -15 /opt/trading/hermes_trading/hermes_trading/state/$a/strategy.yaml;
   done'
```

This tells us whether the VPS is also unpatched, or only the local tree is.

### Step 2 — Re-apply `patch_strategies.py` (YAML only; Python untouched)

If Step 1 confirms the VPS is also unpatched (or for safety even if it's patched, since the operation is idempotent):

Local first:
```bash
cd C:\Users\nghil\Projects\Hermes\Hermes-Trading
python patch_strategies.py
git diff state/                # review the YAML diff before committing
```

Then VPS:
```bash
cd /opt/trading/hermes_trading/hermes_trading
python3 patch_strategies.py
pkill -f hermes_trading.run
cd /opt/trading/hermes_trading && source venv/bin/activate
nohup python3 -m hermes_trading.run >> logs/hermes.log 2>&1 &
```

Expected effect within one 15m tick:
- ETH/SOL/TAO start logging `dir=both` and a real confidence number instead of always `dir=long, conf=0%`.
- BTC long leg becomes possible again whenever 2+ optional indicators fire (RSI no longer hard-blocks).
- Entries should become sporadic but non-zero.

### Step 3 — Idempotent leverage handler (small `execution.py` edit, gated on review)

Proposed minimal change to lines 237–246 — written as a unified diff so you can see exactly what changes before approving:

```diff
     leverage_val = strategy.get("default_leverage")
+    leverage = int(leverage_val or 5)
     if exchange.id != "bybit" or leverage_val:
         try:
-            leverage = int(leverage_val or 5)
             exchange.set_leverage(leverage, symbol)
-        except ccxt.AuthenticationError:
-            pass  # API key may not have leverage permission
-
-    leverage_display = leverage or strategy.get("default_leverage", 5)
+        except ccxt.AuthenticationError:
+            pass  # API key lacks leverage permission
+        except ccxt.ExchangeError as e:
+            # retCode 110043 ("leverage not modified") is benign — Bybit just confirms
+            # the value is already set. Anything else re-raises.
+            if "110043" not in str(e) and "not modified" not in str(e).lower():
+                raise
```

What this does:
- Lifts `leverage` out of the `try` so it's always defined (kills the latent `NameError`).
- Catches the specific 110043 case and swallows it silently.
- Removes the unused `leverage_display` (it's never read after this point — verify by grep before applying).
- Leaves `create_order` and everything downstream untouched.

**Importantly:** this is a 7-line change in one function, with one clear test ("bot tick stops spamming red leverage warnings on the second tick after a set_leverage call"). It does not match the pattern of unsafe regex/sed patching that previously broke execution.py. Recommend applying via `Edit` tool with the exact `old_string`/`new_string`, not via shell `sed`.

### What I am **not** recommending right now

- Removing leverage logic entirely. It's needed the *first* time a position is opened on an account, and it's the only place position-sizing leverage gets aligned with `risk_per_trade`. Removing it would make first-ever orders fall on the exchange's last-set leverage, which could be anything.
- Lowering `min_confidence` or relaxing `min_indicators`. After Steps 1–2 we'll have real signal data and can tune from evidence, not guesses.
- Adding asymmetric RSI thresholds (long_threshold / short_threshold). It would help BTC, but it's a logic change to `_check_indicator` and should wait until Step 2 proves the simpler fix isn't enough.

## Open questions for Linh

1. **Run Step 1 first, or skip straight to Step 2?** Step 1 is purely diagnostic (one SSH command). Step 2 is the actual fix and is idempotent — running it without Step 1 is safe but loses the "did the VPS drift?" answer.
2. **Apply Step 3 in the same session as Step 2, or separate them?** I'd recommend separate sessions so that if entry frequency changes unexpectedly after Step 2, we know it's the YAML and not an interaction with the leverage code.
3. **VPS deploy block: should `state/<slug>/strategy.yaml` ever be SCP'd from local?** Right now memory.md's deploy blocks never copy them. The VPS-evolved strategies (Hermes reflection writes to them) are the source of truth on VPS. We should explicitly document "never overwrite VPS strategy.yaml from local; only run `patch_strategies.py` in-place." This belongs in memory.md if you agree.

## Handoff

- **Receives:** Linh (review & approve Step 1 / Step 2 / Step 3).
- **Read:** `diagnosis-session-5.md` (this file).
- **No client deliverable** until Linh signs off.
- **Status:** Diagnosis complete. Awaiting decision before any change.

---

## Session 5 — Applied (2026-05-27)

Linh approved "do all". The following changes are on disk in the local working tree, validated, but **not yet committed or pushed** (sandbox can't release `.git/index.lock`).

### What changed

1. **All 4 `state/<asset>/strategy.yaml`** — `patch_strategies.py` applied + indicator registry copied into ETH/SOL/TAO (which previously had none).
   - `entry.direction: both`, `min_indicators: 2`, `min_confidence: 0.3`
   - 9-indicator registry (rsi, ema_trend, macd, vwap, volume_spike, bb_squeeze, fvg, order_block, sr_zone)
   - `rsi.required: false`
   - `default_leverage: 5`, `sl_buffer_pct: 0.3`, `max_sl_pct: 5.0`
   - **Caveat:** PyYAML stripped the human-written comments from ETH/SOL/TAO YAMLs. Structural content is preserved.
   - **Caveat:** BTC had `default_leverage:` commented out with a note about Bybit's 12.5x default. The patch re-set it to 5. The new idempotent handler (below) makes this safe regardless.

2. **`hermes_trading/adapters/execution.py`** — 7-line edit to `place_live_trade`:
   - `leverage` is now defined before the `try` (kills latent NameError on non-Bybit exchanges).
   - Added `except ccxt.ExchangeError` that swallows retCode 110043 / "leverage not modified" and re-raises everything else.
   - Removed dead `leverage_display = ...` line (confirmed unused via grep before deleting).
   - **All other functions untouched.** AST parse OK. `py_compile` OK.

3. **Pre-existing untouched:** there is an uncommitted `DEBUG [_fetch_usdt_balance]` print at line 73–79 from a prior session. Left as-is — not my code to revert.

### What got recovered mid-session

The `Edit` tool truncated the last 8 lines of `fetch_last_closed_pnl` while applying the leverage change (only the leverage block was supposed to change, but the file lost its trailing `except` block too). Detected by `py_compile` failing. Restored via a small Python script that re-attached the original tail verbatim, then re-validated with AST parse. **Lesson:** `Edit` is not safe for files where the sandbox sees a Windows-mounted view. Future Python edits should be followed immediately by `python -m py_compile`.

### Deploy block — run from Windows PowerShell

```powershell
cd C:\Users\nghil\Projects\Hermes\Hermes-Trading

# 1. Clear the stale lock if present
Remove-Item .git\index.lock -Force -ErrorAction SilentlyContinue

# 2. Sanity-check what changed (visual review before committing)
git status
git diff hermes_trading/adapters/execution.py

# 3. Stage exactly what session 5 changed (note: state/goal.yaml has CRLF noise,
#    pre-existing — do NOT stage it)
git add hermes_trading/adapters/execution.py `
        state/btc_usdt/strategy.yaml `
        state/eth_usdt/strategy.yaml `
        state/sol_usdt/strategy.yaml `
        state/tao_usdt/strategy.yaml `
        diagnosis-session-5.md `
        memory.md

git commit -m "fix(session5): apply patch_strategies + add indicators registry to eth/sol/tao + idempotent leverage handler (110043)"
git push origin master
```

### VPS deploy block — run after `git push` succeeds

```bash
ssh root@187.127.108.173

# On VPS:
cd /opt/trading/hermes-trading && git pull

# Copy the one Python file that changed
cp hermes_trading/adapters/execution.py \
   /opt/trading/hermes_trading/hermes_trading/adapters/execution.py

# Copy the 4 patched strategy.yamls — these now have the full indicator registry
# AND the new SMC fields. If the VPS-evolved versions are richer than these,
# diff first before overwriting.
for slug in btc_usdt eth_usdt sol_usdt tao_usdt; do
  diff /opt/trading/hermes_trading/hermes_trading/state/$slug/strategy.yaml \
       state/$slug/strategy.yaml | head -30
done

# If the diffs look right (no loss of evolved indicator weights from Hermes reflection):
for slug in btc_usdt eth_usdt sol_usdt tao_usdt; do
  cp state/$slug/strategy.yaml \
     /opt/trading/hermes_trading/hermes_trading/state/$slug/strategy.yaml
done

# Restart the agent
pkill -f hermes_trading.run
cd /opt/trading/hermes_trading && source venv/bin/activate
nohup python3 -m hermes_trading.run >> logs/hermes.log 2>&1 &

# Watch the first few ticks — should now show real confidence numbers and dir=both
tail -f logs/hermes.log
```

### What to expect after deploy

- Within one 15m tick, log lines should switch from `dir=long · RSI=72 · conf=0%` to `dir=both · RSI=72 · conf=NN%` for all 4 assets.
- BTC long entries become possible whenever ≥2 optional indicators fire (no more RSI hard-gate).
- Leverage warnings on the second-and-subsequent ticks per asset should disappear (110043 swallowed silently).
- Entry frequency may go up. Watch `state/<slug>/trades.jsonl` for the first few entries; verify R:R looks sane on the dashboard.

### Important diff caveat for VPS

If the VPS strategy.yaml files have been evolved by Hermes reflection since the last local sync (e.g. tuned `min_ratio`, `tolerance_pct`, indicator weights), copying our local versions will **overwrite that learning**. Run the diff loop in the deploy block first. If any VPS file has evolved values you want to keep, do not blindly overwrite — instead, hand-merge by:

1. SCP the VPS file to local
2. Add the missing `indicators:` block / `entry:` block fields from our local version
3. SCP the merged file back

The local files are a **schema baseline** — they restore the missing structure but use default weights/params from `patch_strategies.py`.

### Handoff

- **Status:** All on-disk edits complete and validated. Awaiting Linh to run the Windows PowerShell block (which is the manual `git push`) and the VPS deploy block.
- **Receives:** Linh.
- **Read:** this file. Memory.md is also updated.
