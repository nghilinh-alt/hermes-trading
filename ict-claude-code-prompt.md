# Claude Code prompt — build the ICT swing strategy (Phase 1)

_Copy everything below the line into Claude Code, run from the repo root
`C:\Users\nghil\Projects\Hermes\Hermes-Trading`._

---

You are a senior quant developer working in the existing **Hermes-Trading** Python repo. We are building a new **ICT (Inner Circle Trader) swing trading strategy** from scratch, replacing a retired indicator-weight system. Work carefully, test-first, and do not touch live money or the VPS.

## 0. Read first (source of truth)
Before writing any code, read these in full and treat the first as the authoritative spec — if anything I say here conflicts with it, ask:
- `ict-strategy-plan-2026-07-18.md` — the complete mechanical specification (concept definitions, parameters, risk model, scoring, architecture, phases). **This is the spec. Implement to it exactly.**
- `memory.md` (top section) — project history, VPS layout, doctrine.
- `hermes_trading/adapters/price.py` — existing OHLCV + FVG/OB/S-R helpers you may reuse/adapt.
- `archive/live-paper-archived-2026-07-18/` — the retired configs. **Do not modify, import, or re-enable anything under `archive/` or the old `state/` and `state-scalp/` trees.**

## 1. Scope of THIS task — Phase 1 only
Implement the **mechanical detection primitives** from spec §3 and §4, plus a comprehensive unit-test suite proving they match hand-labeled ground truth. **Stop after Phase 1 and report — do not build the backtester, scanner, live worker, or any order placement yet.** No network calls to Bybit, no trading, no VPS actions.

## 2. Architecture (create this, isolated from the old system)
```
hermes_trading/ict/
  __init__.py
  structure.py    # swing pivots (fractals), market structure/trend, BOS vs MSS  (spec §3.1, §3.2)
  liquidity.py    # S/R zones, liquidity pools, equal highs/lows, sweeps         (spec §3.3–3.5)
  imbalance.py    # displacement, FVG, order blocks, breaker blocks              (spec §3.6–3.8b)
  bias.py         # HTF bias engine + premium/discount/OTE                        (spec §3.9, §4)
  types.py        # dataclasses: Swing, Zone, LiquidityPool, FVG, OrderBlock, Breaker, Bias, etc.
brokers/
  __init__.py
  base.py         # BrokerAdapter ABC (get_ohlcv, get_balance, place_order, get_positions) — interface ONLY this phase
tests/ict/
  fixtures/       # hand-labeled OHLCV slices (see §4)
  test_structure.py  test_liquidity.py  test_imbalance.py  test_bias.py
```
- All detectors are **pure functions** over an OHLCV series (pandas DataFrame or list of typed candles — pick one and be consistent). No I/O, no globals, deterministic.
- **`brokers/base.py`** defines the abstract interface only — this is the futures-ready seam so a CME adapter can be added later. Do NOT implement a concrete broker this phase.

## 3. Hard requirements
- **No lookahead, ever.** A swing pivot at bar `i` is only *confirmed* at bar `i + N` (needs N candles to its right). Every detector must operate as if streaming — never reference future bars relative to the decision bar. Add an explicit test that asserts each detector's output at bar `i` is unchanged whether or not bars after `i` exist.
- **ATR-scaled tolerances** exactly as specified in §3 (defaults given there). Expose every parameter as a function arg with the spec default; no magic numbers inline.
- **Determinism:** same input → same output, always.
- Python 3.10+, full type hints, docstrings citing the spec section each function implements. Keep functions small and single-purpose.

## 4. Testing (this is the real deliverable)
- Build `tests/ict/fixtures/` from real historical crypto OHLCV (BTC/ETH/SOL) — you may fetch history via the existing price adapter OR commit small static CSV slices; prefer committed CSVs so tests are hermetic and offline.
- **Hand-label ~20–30 instances per concept** (swing points, a trend state sequence, S/R zones, sweeps, MSS vs BOS, displacement, FVG, OB, breaker, premium/discount) as expected outputs, and assert the detectors reproduce them.
- Include edge cases: no-signal windows, ranging/choppy data, equal highs/lows within tolerance, gaps.
- Add the no-lookahead invariance test described in §3.
- Everything must pass `pytest` and `python -m py_compile`. Report coverage.

## 5. Do NOT (out of scope / safety)
- Do not implement §9 scoring/state-machine execution, §11 backtester, §12 scanner, or any live/paper trading this phase (those are Phase 2+).
- Do not place orders, call exchange trading endpoints, read API keys, or touch `.env`.
- Do not modify `archive/`, the old `state/`/`state-scalp/` configs, `loop.py`, `run.py`, or `execution.py`.
- Do not deploy anything to the VPS.

## 6. Working method
1. Restate your understanding of the Phase 1 scope and the primitives list, and flag any spec ambiguity **before** coding.
2. Propose the `types.py` dataclasses and one detector signature for my quick review, then proceed.
3. Implement module by module, writing tests alongside each.
4. Run the full suite; iterate until green.
5. Report: files added, test results/coverage, any spec definitions that were ambiguous and how you resolved them, and a short readiness note for Phase 2 (backtester). Do not proceed to Phase 2 without sign-off.

## 7. Notes
- Reuse `price.py` FVG/OB/S-R logic only if it already matches the §3 definitions; otherwise reimplement to spec and note the difference. The spec definitions win.
- Risk/sizing (§7: 20% risk/trade, stop-derived leverage ≤10×) and the §9 A+/B scoring are Phase 2 concerns — you may add typed stubs/interfaces for them, but no live behavior.
- Keep a running list of any parameter you think should be recalibrated; we tune in the backtest phase.

Begin with step 6.1 (restate scope + flag ambiguities). Do not start coding until you've done that.
