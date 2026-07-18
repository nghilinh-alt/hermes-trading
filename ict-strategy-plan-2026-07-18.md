# ICT Swing Strategy — Build Plan & Mechanical Specification (v0.1)

_Rogue Night — Hermes-Trading. 2026-07-18. Author: Hermes agent. **Internal — flag for Linh's review before any deploy.**_

## 0. Locked decisions (this session)

| Decision | Choice | Implication |
|---|---|---|
| Markets | **Phased — crypto now, futures-ready** | Build + validate on Bybit crypto (BTC/ETH/SOL/TAO). All broker calls go through an abstraction layer so ES/NQ/YM/Gold/Oil (CME) can plug in later without a rewrite. |
| End goal | **Full automation** | Every ICT concept must be codified to a mechanical, parameterized definition — no discretionary steps. This document is that codification. |
| Start point | **Lock mechanical spec first** | Section 3 is the deliverable to freeze before tooling. Nothing gets built until the definitions and default parameters here are signed off. |

Old indicator-weight strategies (BTC/ETH/SOL/TAO/XRP live+paper, plus the never-run scalp stubs) are archived at `archive/live-paper-archived-2026-07-18/`. Live VPS worker halts via the `pkill` in that folder's README.

---

## 1. Philosophy & scope

A top-down, higher-timeframe-driven **swing** system that takes only A+ setups: a HTF directional bias, a liquidity sweep against that bias, a market-structure shift back in favour of the bias, and an entry on the retracement into the resulting imbalance (FVG/OB) inside the correct premium/discount zone. Holds hours-to-days. No scalping. **Target cadence: 3–8 trades/month/market** — selective, but frequent enough to build a statistically meaningful sample. An over-tight filter that fires once a month is a "perfect-setup detector" with no sample size to validate the edge; selectivity is enforced by a scored grade (§9), not by demanding every possible confluence at once.

The whole system is deterministic: given the same candle history, it produces the same signals every time. That is what makes it backtestable and automatable.

---

## 2. Timeframe roles & data

| TF | Role | What it decides |
|---|---|---|
| Weekly | Macro bias | Long-term structure (HH/HL vs LH/LL); overall directional permission |
| Daily | Major levels | Key S/R, prior-day/week high & low (liquidity), dealing range for premium/discount |
| 4H | Setup zone | Where the OB/FVG of interest sits; intermediate structure |
| 1H | Confirmation | Structure shift (MSS) confirmation after the sweep |
| 15M | Execution | Entry trigger, precise FVG/OB, stop placement |

**Data gap vs current system:** today's `price.py` fetches 15m/1h/4h + a daily EMA. This strategy needs full **Weekly and Daily OHLCV** series (not just an EMA), and enough history for HTF swing detection (≥ 200 weekly, ≥ 300 daily bars). New fetch layer required; additive to the existing feed.

---

## 3. Mechanical definitions — THE SPEC TO LOCK

Every concept below is defined so code can decide true/false with no human judgment. Defaults are starting points to calibrate in backtest (Section 11). All tolerances scale with ATR so they work across assets/volatility.

### 3.1 Swing point (fractal pivot) — the foundation
- **Swing high (SH):** candle `i` where `high[i] > high[i±1..N]` for all `k` in `1..N`.
- **Swing low (SL):** candle `i` where `low[i] < low[i±1..N]`.
- **`swing_strength` N:** per-TF. Default 15M=2, 1H=2, 4H=3, Daily=3, Weekly=3.
- **Confirmation lag:** a pivot at bar `i` is only *confirmed* at bar `i+N` (needs N candles to its right). The engine only ever acts on confirmed pivots — enforced identically in live and backtest (no lookahead).

### 3.2 Market structure & trend
Maintain the ordered list of confirmed SH/SL per TF.
- **Uptrend:** latest confirmed SH > prior SH **and** latest confirmed SL > prior SL.
- **Downtrend:** latest SH < prior SH **and** latest SL < prior SL.
- **Range / no-bias:** anything else.
- **Break of Structure (BOS):** trend-continuation break — close beyond the most recent same-direction swing extreme.
- **Market Structure Shift (MSS):** trend-*reversal* break — a close beyond the most recent *opposing* swing point that occurs **after** a liquidity sweep (§3.5). MSS is the reversal trigger; BOS is continuation. The distinction is the sweep precondition.

### 3.3 Support / Resistance zones
- Cluster confirmed pivots within a tolerance band `sr_tol = 0.15 × ATR(14, TF)`.
- **Resistance zone:** ≥ `min_touches` (default 2) swing highs within one band. **Support zone:** ≥ 2 swing lows.
- **Strength score:** `touches × recency_weight × volume_weight` (recency decays over `sr_lookback` bars, default 150).
- Always include reference levels as implicit S/R + liquidity: prior-day high/low (PDH/PDL), prior-week high/low (PWH/PWL), session high/low.

### 3.4 Liquidity pools (buy-side / sell-side)
- **Sell-side liquidity (SSL):** resting stops **below** support — prior lows, PDL/PWL, and **equal lows**.
- **Buy-side liquidity (BSL):** resting stops **above** resistance — prior highs, PDH/PWH, and **equal highs**.
- **Equal highs/lows:** ≥ 2 swing points within `eql_tol = 0.10 × ATR` of each other → flagged as a magnet.

### 3.5 Liquidity sweep / stop hunt
A sweep of level `L` occurs when, within one candle: price trades **beyond** `L` by ≥ `sweep_penetration = 0.10 × ATR`, then **closes back on the origin side** of `L` (bullish sweep of SSL: wick below, close above; bearish sweep of BSL: wick above, close below). Optional strictness: close-back must occur within `sweep_max_bars` (default 1). This is the "trap."

### 3.6 Displacement candle
A decisive move confirming intent: candle body `|close − open| ≥ disp_atr × ATR(14)` (default `disp_atr = 1.5`) **and** it is the candle that produces the BOS/MSS close. Usually leaves an FVG (§3.7).

### 3.7 Fair Value Gap (FVG) — 3-candle imbalance
- **Bullish FVG:** `low[i] > high[i−2]` → gap zone `[high[i−2], low[i]]`.
- **Bearish FVG:** `high[i] < low[i−2]` → gap zone `[high[i], low[i−2]]`.
- Valid only if gap size ≥ `min_fvg = 0.25 × ATR` and the middle candle is a displacement candle (§3.6).
- **Mitigation:** price returning into the zone. Entry targets the retrace into an *unmitigated* FVG.

### 3.8 Order Block (OB)
- **Bullish OB:** the last down-close candle **before** a bullish displacement that causes a BOS/MSS. Zone = that candle's `[low, high]` (or body-only, param `ob_body_only`, default false).
- **Bearish OB:** last up-close candle before bearish displacement causing the break.
- Must be **unmitigated** (untouched since formation) to be a valid entry zone. OB + overlapping FVG = highest-quality zone.

### 3.8b Breaker block (third entry-zone option)
A **failed order block that flips polarity.**
- **Bullish breaker:** a bearish OB (§3.8) whose zone price has **closed above** (violated it) during the MSS/displacement leg. Having failed as supply, on the retrace back down into it the zone acts as demand → long entry.
- **Bearish breaker:** a bullish OB that price has closed **below** during a bearish MSS; on the retrace up into it, it acts as supply → short entry.
- Same validity rule as OB: the flip must be caused by a displacement candle that produced the structure break, and the zone must be untested since the flip. A breaker is the preferred entry when no clean unmitigated FVG/OB remains after the MSS.

### 3.9 Premium / discount (equilibrium)
- **Dealing range:** the current significant swing low → swing high on the reference TF (default Daily for swing entries).
- **Equilibrium = 50%.** `< 50%` = **discount** (only longs), `> 50%` = **premium** (only shorts).
- **OTE (optimal trade entry):** retracement `0.62–0.79` of the dealing range — preferred entry band when it overlaps an OB/FVG.

---

## 4. HTF bias engine
1. Compute structure (§3.2) on Weekly and Daily.
2. **Bias = long** if both Weekly and Daily are uptrend, or Weekly uptrend + Daily range with price in discount. **Bias = short** = mirror. Otherwise **no-trade** (bias conflict is an A+ disqualifier).
3. Compute the Daily dealing range → premium/discount context.
4. Bias gates everything downstream: only long setups in a long bias, only in discount; vice-versa.

---

## 5. Setup state machine (per asset, per bias)
The engine is a state machine, not a per-tick score. States:

`IDLE → BIAS_SET → LIQUIDITY_MAPPED → SWEEP_DETECTED → MSS_CONFIRMED → ENTRY_ARMED → IN_TRADE → MANAGING → (CLOSED | INVALIDATED)`

**Long path (short = mirror):**
1. `BIAS_SET` — HTF bias long (§4).
2. `LIQUIDITY_MAPPED` — identify SSL target below (prior lows / equal lows / PDL).
3. `SWEEP_DETECTED` — SSL swept on 1H/15M (§3.5).
4. `MSS_CONFIRMED` — 1H (or 15M) closes above the last lower-high with a displacement candle (§3.2/§3.6).
5. `ENTRY_ARMED` — locate the unmitigated bullish **FVG / OB / Breaker** left by the displacement, inside discount/OTE. Run the §9 gates + score; if all mandatory gates pass **and** score ≥ 11, place a limit order at the zone (size per grade, §7). Score < 11 → back to `IDLE`.
6. `IN_TRADE` — filled on retrace.
7. `MANAGING` — SL/TP/partial/trail per §6.

**Timeouts / invalidations:** each state has a max life in bars (`state_ttl`, default 20 on 1H). If price closes beyond the sweep extreme before entry → `INVALIDATED`. If MSS is retraced fully before fill → `INVALIDATED`.

---

## 6. Entry / stop / target
- **Entry:** limit order at the OB/FVG zone (retracement entry, maker-preferred to cut fees).
- **Stop loss:** beyond the sweep extreme (the wick low for longs) + `sl_buffer = 0.25 × ATR`. This is the invalidation point — if hit, the sweep/MSS read was wrong.
- **Target:** the next **external liquidity** pool in the bias direction (opposing prior high/low, PDH/PWH, weekly liquidity). Not a fixed %.
- **Gate:** reject unless `RR = (target − entry)/(entry − stop) ≥ min_rr` (default **2.0**; prefer ≥ 3.0). This is a hard A+ filter.
- **Management:** partial (default 50%) at 2R, move stop to break-even, trail remainder under each new 15M higher-low (long) toward the liquidity target.

---

## 7. Risk management
- **Risk per trade (hard):** **20% of account equity** on an **A+** setup = the dollar amount lost if the stop is hit. At **$1,000 equity → $200 per trade.** _(This is very aggressive — roughly 10–40× a professional 0.5–2% risk budget. It is a deliberate owner choice; the sizing, leverage policy, and circuit breakers below are built around it.)_
- **Grade-based size (§9):** **A+ (score ≥14) → 20% risk; B (11–13) → 10% risk (half); <11 → no trade.** The full stake rides only on top-conviction setups; B setups take reduced size to still contribute to the sample.
- **Position size (risk-based):** `stop_pct = |entry − stop| / entry` (structural stop, §6); `risk_$ = equity × 0.20`; `notional = risk_$ / stop_pct`; `qty = notional / entry`. Risk is fixed; size is derived from how far the stop is.
- **Leverage — derived, not fixed:** leverage is whatever holds that notional on the account: `leverage = clamp(ceil(notional / equity), 1, LEV_MAX)`, `LEV_MAX = 10×`. Isolated margin, one full-account position at a time. The **stop-loss order always sits far inside the liquidation price** (e.g. 10× → liquidation ≈ 8–9% away vs a ~2% stop), so the structural stop is the exit, never liquidation.
- **Cap behaviour:** if a setup's stop is tighter than ~2% (would need >10×), leverage caps at 10× and realized risk falls **below** $200 (safe under-risk) — the engine never exceeds the cap to force the full $200.
- **Worked example @ $1,000 equity ($200 risk):**

| Structural stop | Notional | Leverage | Loss if stopped |
|---|---|---|---|
| 2.0% | $10,000 | 10× | $200 |
| 3.0% | $6,667 | 7× | $200 |
| 4.0% | $5,000 | 5× | $200 |
| 6.0% | $3,333 | 4× | $200 |
| 1.5% (too tight) | $10,000 (capped) | 10× | $150 (capped) |

- **Caps:** `max_concurrent_trades = 1` at this account size — a single 20%-risk position consumes the full margin budget; raise only as equity grows or for wide-stop setups. Correlation is moot at 1 concurrent.
- **Circuit breakers:** daily loss stop **−20%** (one losing trade ends the trading day — kills revenge trades), weekly **−40%** (two losses ends the week) → flatten & stand down. Widened to match the 20%/trade stake; tunable.

---

## 8. Session filter (UTC)
- **Trade only** in kill zones: **London 07:00–10:00**, **New York 12:00–15:00** (defaults; per-asset overrideable). Crypto trades 24/7 but these windows carry the institutional volume ICT targets.
- **Avoid:** everything else (low-volume chop) and the first/last minutes around high-impact news (later enhancement via macro feed already stubbed in goal.yaml).

---

## 9. Setup qualification — mandatory gates + weighted score

Two-stage filter, replacing the old all-TRUE checklist. Stage 1 removes disqualified setups; Stage 2 grades the survivors and sizes by grade. This is the deliberate ~20–30% loosening for sample size.

### Stage 1 — mandatory gates (fail ANY → no trade, regardless of score)
1. **HTF bias** — at least **Daily** aligned and **Weekly not opposing** (Daily-only permitted; both-aligned scores higher in Stage 2).
2. **Liquidity event** — a sweep (§3.5) **or** a liquidity run into a mapped pool.
3. **Market structure shift** — confirmed MSS (§3.2).
4. **Entry zone** — at least **one** valid unmitigated **FVG / OB / Breaker** (§3.7–3.8b).
5. **RR ≥ 2.0** to external liquidity (§6).
6. **Session valid** — inside a kill zone (§8).
7. **Risk filter** — sizing/leverage/circuit-breaker rules satisfied (§7).

### Stage 2 — weighted score (survivors only)

| Condition | Points |
|---|---:|
| Weekly bias aligned | 2 |
| Daily bias aligned | 2 |
| Liquidity sweep (clean trap) | 3 |
| MSS confirmed | 3 |
| Displacement candle (≥1.5×ATR) | 2 |
| Valid FVG present | 2 |
| Valid OB present | 2 |
| Entry in OTE (0.62–0.79) | 2 |
| RR > 2 (strictly) | 2 |
| **Max** | **20** |

### Grade → action
- **A+ : score ≥ 14** → take at **full size** (20% risk, §7).
- **B : score 11–13** → take at **half size** (10% risk) — still trades, to build the sample.
- **< 11** → **ignore**, even if all mandatory gates passed.

Rationale: mandatory gates guarantee the setup is structurally valid; the score separates high-conviction (A+) from marginal (B) and sizes accordingly, so the aggressive stake rides mainly on the best setups while B setups still accumulate statistics.

---

## 10. Example trades (illustrative walk-throughs)

**A. BTC long.** Weekly + Daily both HH/HL (bias long); price pulls into Daily discount. Equal lows sit under a 4H support at 61,000. 15M wicks to 60,850 (sweeps the equal lows by ~0.1×ATR) and closes back at 61,150 — trap. 1H then closes above the prior lower-high at 61,600 on a 1.8×ATR displacement candle — MSS. That candle leaves a bullish FVG 61,200–61,450 overlapping an unmitigated OB. Limit long at 61,350; stop 60,750 (below sweep + buffer); target = prior 4H high / BSL at 63,900. RR ≈ 4.2. Managed: 50% off at 2R, BE stop, trail the rest toward 63,900.

**B. ETH short.** Weekly down / Daily lower-highs (bias short); price rallies into Daily premium (68% of range). BSL rests above equal highs at 1,905. Price spikes to 1,912 (sweeps), closes 1,896. 1H closes below the last higher-low at 1,878 on a 1.6×ATR displacement — MSS down. Bearish FVG + OB at 1,888–1,900. Limit short 1,893; stop 1,918; target = prior low / SSL at 1,815. RR ≈ 3.2.

---

## 11. Backtesting framework (design)
- **Event-driven bar replay**, not vectorized — walks candles left to right, so no-lookahead is structural.
- **Multi-TF alignment:** resample all higher TFs from one base series (15M) so HTF bars only "close" at the right wall-clock — prevents peeking at unformed HTF candles.
- **Fills:** limit entries fill only if a later bar's range touches the level; market fills at next-open. **Costs:** maker/taker fees (Bybit ~0.055%/0.055% per side, param) + `slippage_ticks`.
- **Metrics:** expectancy (R), win rate, profit factor, Sharpe, max drawdown, avg holding time, trades/month, MAE/MFE distribution.
- **Validation:** in-sample calibration → out-of-sample + **walk-forward** windows. Parameter sensitivity sweep on the §3 defaults. Guardrail: reject the strategy if OOS expectancy ≤ 0 — do **not** repeat the "deploy and hope" pattern.
- **Labeled fixtures:** hand-tag ~30 historical sweeps/MSS/FVG/OB per asset as unit-test ground truth for the detectors before trusting the backtest.

---

## 12. Scanner logic (detection + alerts)
Reusable pure functions (also used live): `find_swings`, `market_structure`, `sr_zones`, `liquidity_pools`, `detect_sweep`, `detect_mss`, `find_fvg`, `find_order_blocks`, `premium_discount`. The scanner runs them each closed bar and emits an alert when the §9 checklist passes:
```
{asset, tf, direction, state, entry_zone:[lo,hi], stop, target, rr,
 checklist:{...booleans}, swept_level, mss_level, timestamp}
```
Alert sinks: dashboard panel + log now; auto-execution later (same object drives the order).

---

## 13. Architecture & implementation plan
New package, isolated from the archived system:
```
hermes_trading/ict/
  structure.py    # swings, trend, BOS/MSS
  liquidity.py    # pools, equal H/L, sweeps
  imbalance.py    # FVG, order blocks
  bias.py         # HTF bias + premium/discount
  setup.py        # state machine, checklist, RR gate
  risk.py         # sizing, caps, circuit breakers
  scanner.py      # bar-close detection + alert schema
brokers/
  base.py         # BrokerAdapter interface (get_ohlcv, balance, place_order, positions)
  bybit.py        # crypto impl now
  # cme_*.py      # futures impl later (phased)
```
- **Broker abstraction** is the key futures-ready seam: strategy code never calls Bybit directly, only `BrokerAdapter`. Adding ES/NQ/Gold later = one new adapter, zero strategy changes.
- **Reuse:** existing `price.py` FVG/OB/SR helpers seed `imbalance.py`/`structure.py` (rewritten to the §3 spec + tests).
- **Config:** `state-ict/<asset>/strategy.yaml` holds the §3/§6/§7/§8 params; `state-ict/goal.yaml` the objective. Separate state tree — never touches archived configs.

### Phases
0. **Spec lock** (this doc) — sign off §3 defaults. ← *we are here*
1. **Primitives + unit tests** on labeled fixtures (structure/liquidity/imbalance/bias).
2. **Backtest engine** + first expectancy run across BTC/ETH/SOL/TAO history.
3. **Scanner + dashboard alerts** (manual-tradeable, no money).
4. **Automated paper worker** (`state-ict`, paper mode) once backtest shows edge.
5. **Live, per-asset**, 20%-risk / stop-derived leverage ≤10× (§7), after clean paper-forward.
6. **Futures adapter** (phased) — add CME broker for ES/NQ/YM/Gold/Oil.

---

## 14. Parameters to calibrate (defaults above; tune in Phase 2)
`swing_strength` (per TF), `sr_tol`, `min_touches`, `eql_tol`, `sweep_penetration`, `sweep_max_bars`, `disp_atr`, `min_fvg`, `ob_body_only`, OTE band, `min_rr`, `sl_buffer`, `state_ttl`, session windows, risk %, caps, circuit-breaker levels, **§9 score weights (per condition), A+ threshold (14), B threshold (11), grade→size map (A+ 20% / B 10%)**. The score weights and thresholds are prime calibration targets — tune them in backtest to hit the 3–8 trades/month/market target while keeping A+ win-rate/expectancy meaningfully above B.

---

## 15. Next steps / decisions for Linh
1. **Sign off §3 mechanical defaults** (or flag any concept you define differently — ICT has dialects; better to lock yours now).
2. Confirm the **crypto basket** for Phase 1 (BTC/ETH/SOL/TAO as specced, or trim to the most liquid 2 for cleaner data).
3. Confirm **maker/limit entries** are acceptable on Bybit for this (fee model assumes it).
4. Approve moving to **Phase 1** (primitives + tests) once §3 is locked.

**Handoff:** next agent reads this file. Nothing is built yet — spec-lock gate is deliberate.
