# Hermes-Trading — Full Trade Analysis & Strategy Overhaul
_Session 11 — 2026-06-18. Rogue Night. **Flagged for Linh's review.**_
_Dataset: 155 trades, May 21 – June 18 2026 (Bybit ClosedPnL export)_

---

## Executive Summary

May was profitable (+$131, 57% win rate). June has been a sustained bleed (-$105, 37% win rate). Account sits at +$26 cumulative — almost entirely wiped out a strong start.

The data reveals one dominant root cause: **the bot is trading against the daily trend.** ETH fell from $2130 to $1595 in June. TAO fell from $282 to $192. SOL and BTC also trended down. The bot kept entering longs into these downtrends because it has no daily timeframe filter — RSI < 30 on 15m reads as "oversold" even when price is mid-fall on the daily.

Long win rates: TAO 34%, ETH 33%, SOL 37%, BTC 29%. Short win rates: TAO 46%, ETH 55%, SOL 54%, BTC 86%.

The strategy has real short-side edge. The losses come from the long side fighting the trend.

---

## Part 1 — The Numbers

### Overall (155 trades, May 21 – June 18)

| | Trades | Win% | Net PnL | Fees |
|---|---|---|---|---|
| **Total** | 155 | 43% | +$26.32 | $80.90 |
| May | 51 | **57%** | **+$131.37** | $24.52 |
| June | 104 | **37%** | **-$105.05** | $56.37 |

### By Asset (full period)

| Asset | Trades | Win% | Net PnL | Long Win% | Short Win% |
|-------|--------|------|---------|-----------|------------|
| TAO | 80 | 41% | +$40.08 | 34% | 46% |
| BTC | 14 | **57%** | **+$29.55** | 29% | **86%** |
| ETH | 29 | 41% | -$40.15 | 33% | 55% |
| SOL | 32 | 44% | -$3.15 | 37% | 54% |

### May vs June, asset by asset

| Asset | May Win% | May PnL | June Win% | June PnL |
|-------|----------|---------|-----------|---------|
| TAO | 50% | +$93.67 | 35% | -$53.60 |
| BTC | 60% | +$10.41 | 56% | +$19.14 |
| ETH | 67% | +$11.43 | **30%** | **-$51.58** |
| SOL | 80% | +$15.86 | **37%** | **-$19.01** |

BTC is the only asset that held its edge through June. ETH collapsed from 67% to 30%. SOL from 80% to 37%.

### Weekly equity curve

```
Week May 18:  +$83   (9 trades)
Week May 25:  +$48   (42 trades)  ← peak: $131 cumulative
Week Jun 01:  -$19   (66 trades)  ← begins bleed
Week Jun 08:  -$38   (17 trades)
Week Jun 15:  -$48   (21 trades)  ← still losing after tighter settings deployed
```

---

## Part 2 — Root Cause: Trading Against the Daily Trend

### The daily trend during the loss period

ETH daily price (first entry each active day):
- May 23: $2030 → May 24: $2117 → May 28: $2020 → Jun 2: $1931 → Jun 3: $1852 → Jun 7: $1595 → Jun 16: $1812

TAO daily:
- May 21: $271 → May 24: $282 (peak) → Jun 3: $224 → Jun 7: $192 (trough) → Jun 13: $263 → Jun 18: $248

Both assets were in sustained daily downtrends from late May into early June. The bot kept entering longs because its 15m RSI read these as oversold. They weren't — they were mid-fall on the daily.

### What the daily filter would have blocked

**June 2 (-$51):** BTC dropped 4.6%, dragging everything with it. Bot entered longs on ETH, SOL, SOL, SOL, ETH in rapid succession — all hit SL for ~$10 each. Daily trend was bearish on all assets → daily filter blocks all longs → disaster day is avoided.

**June 7 (-$104, worst day):** TAO was in a daily downtrend but had a massive intraday bounce (+11% in 12 hours). The bot shorted 5 times into this bounce and lost $20, $11, $3, $26, $10. A 4h trend check would have seen the 4h turning bullish mid-day and blocked those shorts. Daily filter alone doesn't catch this — need the 4h entry confirmation as well.

**June ETH longs (-$51.58 in June):** ETH daily clearly below its daily EMA from late May. Daily filter = short-only. 12 of ETH's 14 losing trades in June were longs. These would all be blocked.

### The two-layer filter approach

1. **Daily trend** — sets the direction bias for the session:
   - Price > daily EMA(20) → long bias only, no shorts
   - Price < daily EMA(20) → short bias only, no longs
   - Within 0.3% of daily EMA → ambiguous, skip

2. **4h trend confirmation** — gates the actual entry:
   - Long entry only when 15m signal fires AND 4h trend is also bullish (price > 4h EMA(50))
   - Short entry only when 15m signal fires AND 4h trend is also bearish

The price adapter already fetches 1h and 4h OHLCV. Adding daily requires one new Bybit API call (`timeframe: 'D'`, last 50 candles) to compute daily EMA(20). The 4h EMA(50) is already available.

---

## Part 3 — Confirmed Decisions

### 1. Swap TAO for XRP

**TAO is out.** 80 trades, 41% win rate overall, 35% in June, -$53.60 in June. Its downtrend from $282 → $192 attracted repeated failed long entries. Its high volatility and lower liquidity make structural levels unreliable.

**Replace with XRP/USDT.** Reasons:
- Top 5 global market cap — deeper liquidity, more reliable structural levels
- One of the highest volume perpetuals on Bybit
- Moderate volatility: moves well enough to trade, but not as erratic as TAO
- Often diverges from BTC on its own news (Ripple/XRP legal developments), giving opportunities when broader market is flat
- Responds well to SMC structure — clear order blocks and S/R levels
- Same setup as other assets: add `state/xrp_usdt/` on VPS, copy from ETH or SOL yaml as template

Wait period: run XRP in **paper mode** for the first 10 trades to confirm signal quality before switching to live.

### 2. Daily trend filter (no trading against the trend)

Implemented as two checks in `loop._evaluate_entry`:
- **Daily EMA(20)**: direction bias. Long only above, short only below.
- **4h EMA(50)**: entry confirmation. Entry in direction of daily bias only when 4h agrees.

If daily and 4h disagree → skip. This removes countertrend entries at both the macro and intraday level.

Implementation: add `daily` timeframe fetch to `adapters/price.py` (20 candles, `timeframe='D'`), compute `ema20_daily`. Use existing 4h OHLCV for `ema50_4h`. Gate in `loop._evaluate_entry` before confidence scoring. ~30 lines total.

### 3. Position sizing: 2% risk + 10% of balance minimum

**New formula:**
```
position_notional = balance × 0.10          # 10% of balance
leverage = risk_pct / (position_pct × sl_dist)  # dynamic
leverage = clamp(leverage, 3, 8)            # cap at 3–8x
```

**What this gives at $800 balance:**

| SL distance | Leverage | Leveraged pos | Risk per trade | Win at 2:1 |
|------------|----------|---------------|----------------|------------|
| 2.5% | 8x | $640 | $16 (2%) | $32 |
| 3.0% | 6.7x | $533 | $16 (2%) | $32 |
| 4.0% | 5x | $400 | $16 (2%) | $32 |
| 5.0% | 4x | $320 | $16 (2%) | $32 |

Risk is consistently $16 (2% of balance) for most real-world SL distances. Wins at 2:1 R:R = $32. Position scales automatically as balance grows.

Compared to current (10% risk, $500 notional cap): current SL hits cost $10-25 with no consistency. New model: consistent $16 max loss, meaningful $32 target at 2:1.

**Implementation:** replace `risk_per_trade` in execution.py with the new formula. Remove `MAX_POSITION_USD` hard cap (replaced by the 10% of balance floor + leverage cap). Add `position_pct: 0.10` to strategy yaml. `min_leverage: 3`, `max_leverage: 8` already exist.

### 4. Fix empty trades.jsonl + dead cron

Run this to diagnose:
```powershell
ssh root@187.127.108.173 "crontab -l && echo '---' && tail -20 /var/log/hermes-backfill.log && echo '---' && wc -l /opt/trading/hermes_trading/state/*/trades.jsonl"
```

Expected fix:
- If cron is missing: re-add `0 */4 * * * /opt/trading/hermes_trading/tools/cron_backfill.sh`
- Run manual backfill for all 4 assets (7-day window): `cd /opt/trading/hermes_trading && set -a && source .env && set +a && python -m tools.backfill_trades --asset ALL --lookback-days 7`
- Until trades.jsonl is populated, Hermes reflection is completely offline

### 5. BTC: keep current settings, no changes

BTC: 57% win rate, 86% short win rate, +$29.55 net. It's working. Apply the daily trend filter and new position sizing formula, but don't touch the indicators or strategy settings.

---

## Part 4 — Full Change List

### Immediate (YAML + VPS config, no code deploy)

| Change | Asset(s) | How |
|--------|----------|-----|
| Pause TAO | TAO | `trading_enabled: false` in VPS yaml |
| Add XRP in paper mode | XRP | New `state/xrp_usdt/` dir, copy ETH yaml, set `trading_mode: paper` |
| Check + fix cron | All | SSH diagnostic block above |
| Run manual backfill | All | `python -m tools.backfill_trades` after cron fix |

### Next code session (in order of impact)

**1. Daily + 4h trend filter — `adapters/price.py` + `loop.py`** (~35 lines)
- `price.py`: add `daily` OHLCV fetch, compute `ema20_daily`
- `loop._evaluate_entry`: add gate — entry direction must match daily EMA(20) and 4h EMA(50)
- Add to strategy yaml: `trend_filter.enabled: true`, `trend_filter.daily_ema_period: 20`, `trend_filter.ema_4h_period: 50`, `trend_filter.ambiguous_band_pct: 0.3`

**2. New position sizing — `adapters/execution.py`** (~20 lines)
- Replace risk-based qty formula with: `position_notional = balance × position_pct`
- Compute leverage: `lev = clamp(risk_pct / (position_pct × sl_dist), min_lev, max_lev)`
- Add `position_pct: 0.10` to all strategy yamls

**3. Portfolio daily loss cap — `loop.py`** (~15 lines)
- Sum closed PnL across all assets for current UTC day
- If total < -`max_portfolio_daily_loss_usd` (suggest $40), halt new entries for rest of day
- Prevents the June 2 / June 7 style multi-asset simultaneous meltdowns

**4. ATR volatility gate — `loop.py`** (~10 lines)
- Skip entry if 15m ATR > `max_atr_pct` (suggest 0.8% of price)
- Blocks entries during news spikes and high-chop sessions

**5. Session filter — `loop.py`** (~5 lines)
- Skip entries 00:00–07:00 UTC (Asian low-volume session)
- Most of the worst TAO/ETH entries clustered in this window

**6. XRP paper mode → live switch**
- After 10+ paper trades show positive win rate, set `trading_mode: live`
- Compare XRP signal quality vs SOL/ETH in first 2 weeks

---

## Part 5 — Expected Behaviour After Full Deploy

| Metric | Current | Target |
|--------|---------|--------|
| Win rate | 43% | ≥ 55% |
| Long win rate | 29–37% | ≥ 50% (trend-filtered) |
| Max single-day loss | -$104 | < $40 (portfolio cap) |
| Trades per week | ~25 | 5–10 |
| Risk per trade | $10–25 (inconsistent) | $16 (consistent 2%) |
| Active assets | 4 (TAO, BTC, ETH, SOL) | 4 (XRP paper, BTC, ETH, SOL) |

The trend filter will reduce entry frequency significantly — many days the daily EMA direction won't align with 4h conditions, so nothing fires. That's correct behaviour. Fewer, aligned trades beat frequent countertrend ones.

---

## Part 6 — Known Issues to Also Fix

| Issue | Priority | Fix |
|-------|----------|-----|
| trades.jsonl empty → Hermes blind | Critical | Cron fix + manual backfill |
| qwen2.5:3b unreliable mutations | Medium | Replace with rules-based reflection or Claude Haiku API |
| min_rr_ratio soft (extends TP artificially) | Medium | Make it a hard skip in execution.py |
| Indicator stack has correlated duplicates (RSI+MACD+EMA all momentum) | Low | Simplify to 5 indicators in a future session |

---

_Next handoff: Linh runs cron diagnostic + initiates XRP paper state dir. Code session implements items 1–3 in priority order._
_Files to read at start of code session: this document, `memory.md`, `deploy-fixes-2026-06-15.md`._
