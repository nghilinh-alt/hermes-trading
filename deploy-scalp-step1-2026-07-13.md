# Deploy — Scalp strategy, Step 1: indicators + regression test (2026-07-13)

_Step 1 of the scalp rollout in `plan-scalp-strategy-2026-07-13.md`. **Local + regression only — NO VPS deploy in this step.** The goal is to add the two net-new indicators the scalp needs and prove they don't perturb the 4 live swing strategies before anything ships. Nothing about the live worker changes here._

## Scope of this step (and only this step)

Build the indicator layer the scalp config depends on, entirely in the shared `price.py`, in a way that is **additive** for the live assets:

1. **`sma_200`** — new simple-moving-average field (SMA chosen over EMA as the slow trend anchor; confirm with Linh).
2. **`mtf_sr_zone`** — multi-timeframe support/resistance returned as tolerance **bands** + a confluence count, parameterized by a TF set.
3. **Generalize the EMA period plumbing** so `ema_trend` can carry an explicit `period` (already used at 50) without hard-coding.
4. **Raise the primary OHLCV fetch `limit` 100 → 250** so a 200-period MA has enough history.
5. **`reflect.py` guard rails** — honor a `reflect_bounds` block so Hermes can't drift scalp gates (`min_tp_pct`, `min_rr_ratio`, `min_confidence`) outside their scalp range. Additive: configs without `reflect_bounds` (the live ones) behave exactly as before.

Explicitly **out of scope for step 1**: the scalp worker itself, going live, maker/limit entries, any change to a live `state/*/strategy.yaml`, any VPS action.

## One-variable discipline

The only change with cross-asset blast radius is #4 (fetch `limit` 100 → 250). Everything else is a new dict key the live configs never read. So the whole risk of this step reduces to a single question: **does giving the shared feed 150 more candles change any indicator value the live assets already use?** That is exactly what the regression test below answers, and it must pass before this step is considered done.

(If we want literally zero blast radius instead, the alternative is to compute `sma_200` from resampled higher-TF data and leave `limit` at 100 — more code, no regression risk. Recommendation stands with the `limit` bump + regression test unless Linh prefers zero-touch.)

## Files touched (local)

- `hermes_trading/adapters/price.py` — add `sma_200`, `mtf_sr_zone`; generalize EMA period; raise fetch `limit`.
- `hermes_trading/reflect.py` — enforce `reflect_bounds` when present (additive).
- No `state/*/strategy.yaml` (live) touched. Scalp configs live under `state-scalp/` and are not loaded by the live worker.

Reminder (doctrine #94): on the Windows mount, edit `.py` files via bash heredoc (`cat > path << 'PYEOF'`), then `python3 -m py_compile`. Do NOT use the Edit/Write tools on Python files here.

## Regression test — MUST pass before proceeding

For each of BTC/ETH/SOL/XRP, on the same fetched candles, compute the full indicator dict **before and after** the `price.py` change and assert the pre-existing fields are byte-identical:

```
for asset in BTC/USDT ETH/USDT SOL/USDT XRP/USDT:
    old = indicators(asset)   # limit=100 baseline, saved before the change
    new = indicators(asset)   # limit=250 build
    assert all(old[k] == new[k] for k in old
               if k not in ("sma_200", "mtf_sr_zone"))   # new keys exempt
```

Fields to confirm unchanged: `vwap`, `ema_9`, `ema_50`, `rsi`, `macd`, `bollinger`, `atr`, `support_1h4h`, `resistance_1h4h`, `fvg`, `order_block`, candle/flag/trendline fields, `ema20_daily`, `ema50_4h`. Expected result: identical (EMA/RSI/MACD use trailing windows; swing S/R uses fixed lookback — extra history should not move them). If anything differs, stop and investigate before touching the VPS.

## Verification checklist

- [ ] `python3 -m py_compile` clean on `price.py` and `reflect.py`.
- [ ] `sma_200` and `mtf_sr_zone` present and sane on all 4 assets (200MA within recent price range; zones return bands + a confluence count).
- [ ] Regression assertion above passes for all 4 live assets.
- [ ] `reflect_bounds` respected: a synthetic out-of-range mutation is clamped/rejected; a config without the block is unaffected.
- [ ] Diff of `state/*/strategy.yaml` (live) shows **no changes**.

## Next steps (separate docs, do not bundle)

- **Step 2** — stand up the scalp worker in paper: `STATE_DIR=state-scalp` `setsid` process, copy `_scalp-strategy-template.yaml` into each `state-scalp/<slug>/strategy.yaml`, run 3–5 days, watch signals fire and trades log.
- **Step 3** — tune the scalp gate table in paper, one variable at a time.
- **Step 4** — go live on one asset, small size, measure; then widen.

## Sign-off

- **Status**: Not started — plan/draft only. No code written, no regression run, no VPS action.
- **Blocker**: Linh's calls on SMA-200 vs EMA-200, the `price.py` approach (limit bump vs higher-TF derive), and final scalp asset basket.
- **Flag**: Internal. No live deploy without Linh's explicit sign-off, per standing discipline.
