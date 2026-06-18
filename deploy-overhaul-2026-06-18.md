# Hermes-Trading — Deploy Overhaul 2026-06-18
_Session 11 — Run these blocks in order. All commands target VPS root@187.127.108.173._
_**Flagged for Linh's review before deploying.**_

---

## Pre-flight: verify VPS is accessible

```bash
ssh root@187.127.108.173 "echo connected && date && whoami"
```

---

## Step 1: Stop running hermes workers

```bash
ssh root@187.127.108.173 "
  systemctl stop hermes-btc 2>/dev/null || true
  systemctl stop hermes-eth 2>/dev/null || true
  systemctl stop hermes-sol 2>/dev/null || true
  systemctl stop hermes-tao 2>/dev/null || true
  echo 'Workers stopped'
"
```

---

## Step 2: Backup current code on VPS

```bash
ssh root@187.127.108.173 "
  cp -r /opt/trading/hermes_trading /opt/trading/hermes_trading.bak-2026-06-18
  echo 'Backup at /opt/trading/hermes_trading.bak-2026-06-18'
"
```

---

## Step 3: Deploy updated Python source files

```bash
scp C:/Users/nghil/Projects/Hermes/Hermes-Trading/hermes_trading/adapters/price.py \
    root@187.127.108.173:/opt/trading/hermes_trading/hermes_trading/adapters/price.py

scp C:/Users/nghil/Projects/Hermes/Hermes-Trading/hermes_trading/loop.py \
    root@187.127.108.173:/opt/trading/hermes_trading/hermes_trading/loop.py

scp C:/Users/nghil/Projects/Hermes/Hermes-Trading/hermes_trading/adapters/execution.py \
    root@187.127.108.173:/opt/trading/hermes_trading/hermes_trading/adapters/execution.py

scp C:/Users/nghil/Projects/Hermes/Hermes-Trading/dashboard.py \
    root@187.127.108.173:/opt/trading/hermes_trading/dashboard.py
```

---

## Step 4: Deploy updated strategy YAMLs

```bash
# BTC, ETH, SOL — updated with trend_filter, position_pct, risk_per_trade
scp C:/Users/nghil/Projects/Hermes/Hermes-Trading/state/btc_usdt/strategy.yaml \
    root@187.127.108.173:/opt/trading/hermes_trading/state/btc_usdt/strategy.yaml

scp C:/Users/nghil/Projects/Hermes/Hermes-Trading/state/eth_usdt/strategy.yaml \
    root@187.127.108.173:/opt/trading/hermes_trading/state/eth_usdt/strategy.yaml

scp C:/Users/nghil/Projects/Hermes/Hermes-Trading/state/sol_usdt/strategy.yaml \
    root@187.127.108.173:/opt/trading/hermes_trading/state/sol_usdt/strategy.yaml

# TAO — trading_enabled: false
scp C:/Users/nghil/Projects/Hermes/Hermes-Trading/state/tao_usdt/strategy.yaml \
    root@187.127.108.173:/opt/trading/hermes_trading/state/tao_usdt/strategy.yaml

# XRP — new asset (paper mode)
ssh root@187.127.108.173 "mkdir -p /opt/trading/hermes_trading/state/xrp_usdt"
scp C:/Users/nghil/Projects/Hermes/Hermes-Trading/state/xrp_usdt/strategy.yaml \
    root@187.127.108.173:/opt/trading/hermes_trading/state/xrp_usdt/strategy.yaml
```

---

## Step 5: Validate syntax on VPS

```bash
ssh root@187.127.108.173 "
  cd /opt/trading/hermes_trading
  source .env 2>/dev/null || true
  python3 -m py_compile hermes_trading/adapters/price.py && echo 'price.py OK'
  python3 -m py_compile hermes_trading/loop.py && echo 'loop.py OK'
  python3 -m py_compile hermes_trading/adapters/execution.py && echo 'execution.py OK'
  python3 -m py_compile dashboard.py && echo 'dashboard.py OK'
"
```

---

## Step 6: Diagnose and fix cron + trades.jsonl

```bash
ssh root@187.127.108.173 "
  echo '=== CRON ===' && crontab -l
  echo '=== BACKFILL LOG ===' && tail -20 /var/log/hermes-backfill.log 2>/dev/null || echo 'no log'
  echo '=== TRADES.JSONL LINES ===' && wc -l /opt/trading/hermes_trading/state/*/trades.jsonl
"
```

If cron is missing (no backfill entry), re-add it:
```bash
ssh root@187.127.108.173 "
  (crontab -l 2>/dev/null; echo '0 */4 * * * /opt/trading/hermes_trading/tools/cron_backfill.sh >> /var/log/hermes-backfill.log 2>&1') | crontab -
  echo 'Cron re-added'
"
```

Run manual 7-day backfill (all assets):
```bash
ssh root@187.127.108.173 "
  cd /opt/trading/hermes_trading
  set -a && source .env && set +a
  python3 -m tools.backfill_trades --asset ALL --lookback-days 7
"
```

---

## Step 7: Add XRP worker and restart all workers

```bash
ssh root@187.127.108.173 "
  # Start existing workers (BTC, ETH, SOL)
  systemctl start hermes-btc
  systemctl start hermes-eth
  systemctl start hermes-sol
  echo 'BTC/ETH/SOL started'
  
  # TAO stays stopped (trading_enabled: false in yaml, loop now checks this)
  # Do NOT start hermes-tao
  
  echo '--- Status ---'
  systemctl is-active hermes-btc hermes-eth hermes-sol
"
```

Start XRP worker (create systemd unit if it doesn't exist yet):
```bash
# Check if hermes-xrp service exists
ssh root@187.127.108.173 "systemctl status hermes-xrp 2>&1 | head -5"
```

If hermes-xrp service does NOT exist, create it (copy from hermes-eth template):
```bash
ssh root@187.127.108.173 "
  # Read eth unit as template, create xrp variant
  cp /etc/systemd/system/hermes-eth.service /etc/systemd/system/hermes-xrp.service
  sed -i 's/eth_usdt/xrp_usdt/g; s/ETH\\/USDT/XRP\\/USDT/g' /etc/systemd/system/hermes-xrp.service
  systemctl daemon-reload
  systemctl enable hermes-xrp
  systemctl start hermes-xrp
  echo 'hermes-xrp started'
"
```

---

## Step 8: Verify workers are healthy

```bash
ssh root@187.127.108.173 "
  sleep 5
  for slug in btc_usdt eth_usdt sol_usdt xrp_usdt; do
    hb=/opt/trading/hermes_trading/state/\$slug/heartbeat.json
    if [ -f \$hb ]; then
      echo \"\$slug: \$(cat \$hb)\"
    else
      echo \"\$slug: no heartbeat yet\"
    fi
  done
"
```

---

## Step 9: Smoke-check new features in logs

```bash
ssh root@187.127.108.173 "
  tail -50 /opt/trading/hermes_trading/logs/hermes.log | grep -E 'Trend filter|Session filter|Portfolio daily|ema20_daily|XRP'
"
```

Expected log patterns after deploy:
- `Trend filter: ambiguous` or `Trend filter: LONG` → daily EMA gate working
- `Session filter: 0x:xx UTC < 07:00` → session filter working (if checked before 07:00 UTC)
- No `Trend filter` lines after 07:00 UTC = normal (filter passed, trade evaluated normally)

---

## Rollback (if anything breaks)

```bash
ssh root@187.127.108.173 "
  systemctl stop hermes-btc hermes-eth hermes-sol hermes-xrp 2>/dev/null
  rm -rf /opt/trading/hermes_trading
  mv /opt/trading/hermes_trading.bak-2026-06-18 /opt/trading/hermes_trading
  systemctl start hermes-btc hermes-eth hermes-sol
  echo 'Rolled back to pre-session-11'
"
```

---

## Summary of what changed

| File | Change |
|------|--------|
| `adapters/price.py` | Added daily OHLCV fetch; `ema20_daily` and `ema50_4h` in price dict |
| `loop.py` | Session filter (skip 00:00–07:00 UTC); portfolio daily loss cap (-$40); daily+4h trend filter gate; `trading_enabled: false` support |
| `adapters/execution.py` | New `_position_based_sizing`: 10% balance position, 2% risk, dynamic leverage 3–8x |
| `state/btc_usdt/strategy.yaml` | `risk_per_trade: 0.02`, `position_pct: 0.10`, `trend_filter`, session + loss cap config |
| `state/eth_usdt/strategy.yaml` | Same as BTC |
| `state/sol_usdt/strategy.yaml` | Same as BTC |
| `state/tao_usdt/strategy.yaml` | `trading_enabled: false` |
| `state/xrp_usdt/strategy.yaml` | New file, paper mode |
| `dashboard.py` | TAO → XRP; trend filter badge; PAPER/PAUSED badge; long/short win rate; portfolio daily loss; sizing display |
