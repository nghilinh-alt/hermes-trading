# Plan: multi-timeframe S/R scalping strategy — session 15 (2026-07-13)

_Internal planning doc. Nothing here is implemented or deployed. Decisions locked with Linh this session: **15m timeframe**, **separate isolated worker**, **start on several assets at once**._

## What's being added

A new **scalping strategy** that enters off multi-timeframe support/resistance confluence, using **VWAP**, **50-period MA**, and **200-period MA** as its indicator set, aiming for many small, quick trades rather than the 3%+ swing moves the current strategies target.

The scalp runs on 15m (same candle feed as the live strategies) but as a **completely separate worker process** with its own state, its own config, and — critically — its own gate set. The current BTC/ETH/SOL/XRP 15m strategies are left untouched.

## Architecture facts that shape this design

Confirmed by reading `run.py`, `loop.py`, `price.py`, `execution.py`:

1. **A "strategy" today = one per-asset `state/<slug>/strategy.yaml`** — an indicator bag plus gate parameters. There is no framework for multiple named strategies on the same asset. The worker (`run.py::main`) does `asyncio.gather` over an assets list, one `loop.run()` per asset.
2. **Timeframe is global per process** (`HERMES_TIMEFRAME` env, set once from `goal.yaml`). Because the scalp is also 15m, this is a non-issue here — but it's the reason the scalp must be its own worker if we ever want a different TF, and the reason we're isolating from day one.
3. **No 200MA exists.** `price.py` computes EMA-9, EMA-50, VWAP, RSI, MACD, Bollinger, ATR, 1h/4h swing S/R, FVG, order blocks, candles, trendlines. `50MA` maps to the existing `ema_trend` (period 50); `VWAP` already exists. **200MA is net-new.**
4. **Only 100 candles are fetched** (`price.py` line ~33, `limit=100`). A 200-period MA on the primary TF needs **≥200 candles** — this is the one change with cross-asset blast radius (see Impact section).
5. **S/R is 1h+4h only** (`_support_resistance(ohlcv_1h, ohlcv_4h, ...)`, lookback 3, returns single nearest levels). "Multi-timeframe zones" means extending this to more TFs and treating levels as tolerance bands, not points.
6. **Every entry gate that killed 3,255 trades in the session-14/15 diagnosis is per-config.** So the scalp gets its own, inverted set — none of it touches the live configs.

## Proposed design

### 1. Isolation — a second worker

- New goal file, e.g. `state-scalp/goal.yaml`, with its own `assets:` list, `timeframe: 15m`, and its own `reflection_every`.
- Run with `STATE_DIR=state-scalp python -m hermes_trading.run` as a **separate `setsid` process** on the VPS (per the daemonization doctrine in `next-session-prompt.md`). It bootstraps its own `state-scalp/<slug>/` dirs via the existing `_bootstrap_asset` path — no code change needed for that.
- Result: two independent worker processes, two independent sets of trade logs / heartbeats / reflection state. The live worker is never restarted or reconfigured as part of this.

### 2. Indicator additions (shared `price.py` — additive only)

- **`ema_200` (or `sma_200`):** add a new computed field. Generalize `_ema` calls so period is config-driven rather than hard-coded (`ema_50` is already there; add `ema_200`). Decide MA type with Linh — "200MA" is ambiguous between SMA and EMA; SMA-200 is the more conventional trend anchor, EMA-200 reacts faster. **Recommend SMA-200 for the slow trend filter, keep the existing EMA-50.**
- **Raise the primary fetch `limit` to ≥250** so 200-period MAs have history. Purely additional history — see Impact.
- **Multi-timeframe S/R as zones:** a new/parameterized function that takes a configurable TF set (e.g. 15m + 1h + 4h) and returns support/resistance **bands** (level ± tolerance), plus a confluence count (how many TFs agree near a price). Reuses already-fetched 1h/4h; adds 15m swings. Exposed as a new indicator (e.g. `mtf_sr_zone`) the scalp config points at.

### 3. Scalp `strategy.yaml` template (the gate inversion)

This is where the scalp diverges hardest from the live strategies. Everything the session-15 diagnosis flagged as a trade-killer must be relaxed for scalping. Draft starting values (all to be tuned in paper):

| Parameter | Live strategies | Scalp strategy | Why |
|---|---|---|---|
| `min_tp_pct` | 3.0% | **0.4–0.8%** | The single biggest blocker. Scalps target fractions of a 15m ATR move; a 3% floor forbids scalping outright. |
| `min_rr_ratio` | 2.0 | **1.0–1.3** | Scalps live on hit-rate, not 2:1 payoff. |
| `min_profit_usd` | $5.0 | **$1–2, or raise notional** | A 0.5% target on a small position won't clear $5. Either lower the floor or size up. |
| `stop_loss_pct` / `max_sl_pct` | 2.0 / 5.0 | **0.4–0.6 / 1.0** | Tight stops to match tight targets. |
| `session_blocked_end_utc` | 7 (blocks ~29% of day) | **off, or narrower** | Don't discard a third of scalp opportunities by default. |
| `max_trades_per_day` | 3 | **10–20** | Scalping is a volume game; 3/day defeats the purpose. |
| `min_confidence` / `min_indicators` | 0.5 / 3 | **0.4 / 2** | Fewer indicators in the scalp set (VWAP, 50MA, 200MA, MTF-S/R); tune the bar to them. |
| Indicator bag | 11 mixed | **vwap, ema_trend(50), sma_200, mtf_sr_zone** | Exactly the set requested. |

Entry logic in words: look for price reacting at a multi-TF S/R zone, filtered by trend context — e.g. only long near support when price is above the 200MA (or VWAP), only short near resistance when below. VWAP and the 50/200 MA relationship give the directional bias; the MTF zone gives the entry trigger; tight SL/TP make it a scalp.

### 4. Fee & slippage reality (must not skip)

Current execution places **market orders** (`execution.py::place_live_trade` → `create_order`), i.e. **taker fees ≈ 0.055%/side, ~0.11% round-trip** on Bybit USDT perps. At a 0.5% target that's ~22% of gross profit gone to fees before slippage. Implications baked into the numbers above:
- Keep `min_tp_pct` comfortably above the fee+slippage breakeven (~0.15–0.2%); 0.4%+ leaves real margin.
- Consider **maker/limit entries** for the scalp to cut fees — but that's an execution-path change (`create_order` type) and should be a **separate, later** step, not bundled into v1.
- This is why 15m (not 1m) is the sensible scalp TF: at 1m the fee drag on sub-0.3% targets is brutal.

## How this affects the current strategies

**Direct behavioral impact if isolated as planned: none.** The live worker's process, configs, timeframe, and gates are not touched. The scalp is a second process reading its own `state-scalp/` tree.

The only shared surfaces, and how each is managed:

1. **`price.py` (shared indicator module).** Adding `sma_200` / `mtf_sr_zone` is additive — new dict keys the live configs never reference, so their entry evaluation is unchanged. **The one real risk is raising the fetch `limit` 100 → 250**, which changes the data window every asset sees. EMA/RSI/MACD use trailing windows and are unaffected by extra history; swing S/R uses fixed lookback and is unaffected. Still, this must be **regression-tested against the 4 live assets** (compute indicators before/after the limit change on the same candles, confirm identical outputs) before anything deploys. If we want zero blast radius, alternative is to derive the 200MA from resampled higher-TF data instead of bumping primary `limit` — slightly more code, but leaves the live feed byte-identical.
2. **`reflect.py` (shared reflection code).** Each asset/strategy reflects on its own config, so live tuning is unaffected. But note the scalp will **actually trigger reflection** — it'll hit 5 closed trades quickly (unlike the live assets, which per the diagnosis have never reached it). That's a feature, but we must **bound the scalp's tunable ranges** so Hermes can't drift its `min_tp_pct` back up toward swing values and quietly re-strangle it — the same drift risk called out for the live gates, now actually live because trades will flow.
3. **Bybit API rate limits & VPS resources.** A second worker scalping several assets on 15m adds fetch/order call volume. Low risk at 15m cadence, but worth a sanity check against Bybit's rate limits and the VPS's load before scaling asset count.

**Net:** current strategies keep running exactly as they are; the diagnosis-15 `min_tp_pct` decision for the *live* strategies stays a separate, independent question. This plan does not resolve or touch it.

## Phased rollout (one variable discipline, per project doctrine)

1. **Build indicators** — `sma_200`, generalized EMA period, `mtf_sr_zone`; regression-test the 4 live assets against the `price.py` changes locally. Write a `deploy-*.md`. No VPS yet.
2. **Paper first** — scalp worker in `trading_mode: paper` on the chosen assets, 15m, own `state-scalp/`. Let it run 3–5 days. Confirm signals fire, trades log (watch the trade-file-writing issue flagged since session 11), and gate tallies look sane.
3. **Tune in paper** — adjust the gate table above off real skip/fill data, one variable at a time.
4. **Go live on one asset**, small size, measure. Only then widen to the rest.
5. **Later** — evaluate maker/limit entries to cut fee drag.

## Open decisions for Linh

- **SMA-200 vs EMA-200** for the slow MA (recommend SMA-200).
- **Which assets** for the "several" start — reuse BTC/ETH/SOL/XRP (isolated loops) or pick a different basket.
- **`price.py` approach:** bump primary `limit` to 250 (simpler, needs regression test) vs derive 200MA from higher-TF data (zero blast radius, more code). Recommend the bump + regression test.
- **Position sizing floor for scalp** — lower `min_profit_usd` vs size up notional (fee math favors slightly larger notional with tight stops).

## Handoff

- **Status**: Plan only. No code, config, or deploy. Architecture verified against source. Depends on nothing from the diagnosis-15 `min_tp_pct` decision — that's a separate live-strategy question.
- **Next**: Linh signs off on the open decisions above → implement indicators + scalp config → regression-test the 4 live assets → paper. Every step gets its own `deploy-*.md`, one variable at a time, per standing discipline.
- **Flag**: Internal. No client involvement. No live deploy without Linh's explicit sign-off.
