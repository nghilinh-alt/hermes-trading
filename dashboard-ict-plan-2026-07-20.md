# Dashboard rebuild for the ICT worker — plan

**Date:** 2026-07-20 (session 21)
**Author:** Consulting agent, Rogue Night
**Status:** **BUILT** — Linh answered the §10 questions and approved the build the same
session. Phases 1–5 are implemented and tested locally. **Not deployed** — deploy remains
gated on Linh's explicit sign-off (§9).
**Requested by:** Linh — "we have a new ICT Trading worker, update the Dashboard accordingly."

---

## 0. Decisions and build status (added after Linh's answers)

Linh's answers to §10:

| # | Question | Answer |
|---|---|---|
| 1 | XRP in the ICT universe? | **Leave XRP out** — `ASSETS` stays BTC/ETH/SOL/TAO |
| 2 | Show archived old-system trades? | **Fully retire from view** — old dashboard moved to `archive/` |
| 3 | Fix the partial-P&L bug now? | **Yes, fix it** — done, with regression tests |
| 4 | Dashboard poll interval | **5 minutes** (`POLL_SECS = 300`) |
| 5 | Alerts on arm/fill | **In scope** — implemented |

### What was built

| File | Change |
|---|---|
| `hermes_trading/ict/context.py` | **NEW.** Pure `build_market_context()` — bias, dealing range/OTE, FVG/OB/breaker/S-R/liquidity near price, qualified **and** near-miss candidates with score + failed gates, gate summary. No I/O, no broker calls |
| `hermes_trading/ict/live.py` | Partial-P&L fix; closed trades now record `realised_r`, `planned_rr`, `pnl_pct`, `risk_usd`, `pnl_source`, `legs`, `final_stop_price` |
| `tools/run_ict_live.py` | Per-asset atomic `context.json` dump after the trading cycle (try/except isolated); heartbeat extended with equity, busy count, circuit breaker, live `scan_params`, cycle duration |
| `dashboard.py` | **REWRITTEN** for ICT. Old file moved to `archive/dashboard-indicator-weight-2026-07-20.py` |
| `tests/ict/test_context.py` | **NEW.** 8 tests, including the scan_asset parity contract |
| `tests/ict/test_live.py` | +6 tests covering the partial-P&L fix and R recording |
| `tests/ict/fixtures/context_btc_near_miss.json` | Real recorded snapshot — a score-18/20 A+ setup blocked only by the `rr` gate |

### Verification performed

- 114 tests green (detection, setup, risk, brokers-base, context, no-lookahead, smoke) in 3.9s
- 11 targeted live-worker tests green (partial P&L, R recording, leg reconciliation)
- Context builder run against real BTC/ETH/SOL/TAO history: **4–5 KB per asset**, 0.2 s on
  8k candles, ~3 s on 30k
- Dashboard rendered against fixtures for all three states plus empty and VPS-unreachable
- Context-dump failure isolation proven by feeding it deliberately invalid input: it logged
  and continued rather than propagating

**Known limitation:** the real-data-heavy modules (`test_scanner.py`, `test_backtest.py`,
and the real-CSV half of `test_live.py`) take longer than this sandbox's 45-second
per-command limit and could not be run to completion here. **This is pre-existing** —
verified by stashing all of this session's changes and observing the identical timeout on
the untouched tree. They must be run on the VPS or Linh's machine before deploy; that is
step 1 of §9.

**Housekeeping for Linh:** a stale `.git/index.lock` is present and could not be removed
from the sandbox (`Operation not permitted` on the Windows mount). Also, five files
(`hermes_trading/ict/backtest.py`, `snapshot.sh`, `tests/ict/test_backtest.py`,
`tools/dedup_trades.py`, and an archived `goal.yaml`) show as modified but contain **zero
content change** — pure CRLF churn from the mount, confirmed via
`git diff --ignore-cr-at-eol`. Commit with explicit paths to keep them out.

---

## 1. Executive summary

The current `dashboard.py` is **completely disconnected from the live system**. Every
field it reads belongs to the indicator-weight agent that was halted in session 18 and
archived to `archive/live-paper-archived-2026-07-18/`. It is not stale — it is pointed at
a process that no longer runs and files that were `git mv`-ed away. This is a rewrite,
not a patch.

Separately, the two things Linh specifically asked for split cleanly:

- **"If in a trade, show entry / exit / SL / TP"** — the data exists. The worker already
  persists it in `state-ict-live/<asset>/position.json`. This is a plumbing job.
- **"If not in a trade, show the proposed scenario + FVG / Order Blocks / S&R"** — the data
  **does not exist anywhere.** The worker computes all of it every cycle inside
  `build_detection_context()` and then discards it. `scan_asset()` returns only
  fully-qualified, still-pending setups; everything else — every rejected candidate, every
  FVG, every order block, the current bias — is thrown away when the function returns. The
  cycle's only surviving output is a one-line human-readable string in `CycleResult.action`.

So the plan has two halves: a **worker-side change** to persist what it already sees, and a
**dashboard rewrite** to render it. Approved approach (per Linh, this session):

| Decision | Choice |
|---|---|
| Data source | Worker writes a per-asset `context.json` each cycle; dashboard renders only |
| Setup scope | Qualified setups **and** near-misses, with score and failed gates shown |
| Visualisation | HTML/CSS price ladder + numeric tables (no chart library, no candle transfer) |
| This session | Plan document only |

---

## 2. Current state — what is actually broken

`dashboard.py` (1011 lines) reads, over SSH from `root@187.127.108.173`:

```
/opt/trading/hermes_trading/state/goal.yaml
/opt/trading/hermes_trading/state/<slug>/trades.jsonl
/opt/trading/hermes_trading/state/<slug>/strategy.yaml
/opt/trading/hermes_trading/state/<slug>/heartbeat.json
/opt/trading/hermes_trading/state/<slug>/hypotheses.jsonl
/opt/trading/hermes_trading/logs/hermes.log
```

Every one of these is a dead path for the ICT system:

| Dashboard dependency | ICT reality |
|---|---|
| VPS dir `/opt/trading/hermes_trading/` (underscore) | ICT worker runs from `/opt/trading/hermes-trading/` (hyphen) |
| `logs/hermes.log` | Does not exist. ICT worker logs to `live.log`; memory.md already flags this as a repeat tripwire |
| Assets `btc/eth/sol/**xrp**_usdt` | ICT trades `BTC/ETH/SOL/**TAO**` — XRP is not in the ICT universe at all |
| `state/<slug>/` (lowercase slug) | `state-ict-live/<ASSET>/` where asset is `BTC_USDT` (uppercase, from `asset.replace("/", "_")`) |
| `strategy.yaml` → indicator weights, `min_confidence`, `position_pct` | No such concept. ICT has fixed `SCAN_PARAMS` in `tools/run_ict_live.py` and grade-based risk (A+ = 20%, B = 10%) |
| `hypotheses.jsonl` → reflection cards | No reflection loop in ICT. This entire dashboard section is obsolete |
| `indicators_fired` / `indicators_snapshot` / `confidence_at_entry` | Do not exist. ICT's analogue is the 20-point score, grade, and 7 mandatory gates |
| `goal.yaml` → target %, drawdown, reflect-every | Not used by the ICT worker. Its risk limits are `DEFAULT_DAILY_LOSS_LIMIT_PCT = -0.20` / `WEEKLY = -0.40` in `risk.py` |
| Trade fields `ts`, `pnl_pct`, `closed_pnl_usdt`, `rr_ratio`, `strategy_version`, `abandoned` | ICT `trades.jsonl` writes `entry_utc`/`exit_utc` (epoch **ms**, not ISO), `pnl_usd`, `grade`, `stop_price`, `target_price`, `close_reason`, `order_id`. **There is no `pnl_pct` and no R-multiple at all** |
| Price scraped from log lines via regex `price=([\d.]+)` | ICT's `live.log` does not emit that format |

**Consequence:** roughly 60% of the existing dashboard (indicator bars, reflections,
confidence column, signals-fired chips, strategy versioning, goal card) has no ICT
analogue and should be deleted rather than ported.

**Note on access:** SSH to the VPS from this sandbox failed this session
(`Network is unreachable`) — consistent with the pattern in memory.md that VPS
reachability from the sandbox is intermittent and must be re-verified each session, never
assumed. Everything above is derived from source code, not from a live inspection. Worth
one read-only confirmation pass before implementation.

---

## 3. Gap analysis — Linh's two requirements

### Requirement 1 — in-trade view (entry / exit / SL / TP)

**Data available today.** `state-ict-live/<ASSET>/position.json`, when
`status == "open_position"`, holds:

```
direction, entry_price (actual fill), initial_stop_price, current_stop_price,
target_price, grade, qty_total, qty_remaining, leverage, partial_taken,
partial_realized_pnl_usd, mss_timestamp, fill_timestamp, last_processed_bar_ts
```

That covers entry, SL (both initial and trailed) and TP directly. Two things are missing
and matter:

- **Exit** — there is no live exit price for an open position (correct: it hasn't exited).
  What Linh presumably wants is *current mark price* plus unrealised P&L and unrealised R.
  Neither is persisted. Mark price must come from the candle cache or a broker call.
- **The `resting_order` status is a distinct third state** the current dashboard has no
  concept of: an order placed at a limit price, not yet filled, which can still be
  cancelled as invalidated or expired. This deserves its own visual treatment — it is
  neither "in a trade" nor "looking for one."

### Requirement 2 — proposed scenarios + FVG / OB / S&R

**Data available today: none.** Tracing the call path:

`run_asset_cycle` → `_look_for_new_setup` → `scan_asset(...)` → `build_detection_context(...)`

`build_detection_context` computes and returns `exec_swings`, `sweeps`, `mss_events`,
`fvgs`, `order_blocks`, `breakers`, `atr_series`. `scan_asset` then loops candidate MSS
events, calls `build_setup`, and **`continue`s past anything where
`setup is None or not setup.qualified`** — discarding the `TradeSetup` object that carries
`score`, `grade`, `gate_failures`, `entry_price`, `stop_price`, `target_price` and `rr`.
When no alert results, `_look_for_new_setup` returns
`CycleResult(asset, "flat", "no qualified pending setup", False)` and the entire
detection context is garbage-collected.

Additionally, two things Linh asked for are **not computed at all** in the live path:

- **S/R zones** — `liquidity.sr_zones()` exists and is tested, but `build_detection_context`
  never calls it. Only `liquidity_pools()` is used.
- **Bias / dealing range** — computed inside `scan_asset`'s per-MSS loop only, so it exists
  only when there is a candidate MSS to evaluate. On a quiet asset there is no bias value
  at all.

This is why the worker change is unavoidable.

---

## 4. Architecture

```
VPS: /opt/trading/hermes-trading/
  tools/run_ict_live.py
    └─ run_full_cycle()                    [unchanged trading path]
    └─ dump_market_context()   ← NEW, after the cycle, wrapped in try/except
         └─ hermes_trading/ict/context.py  ← NEW pure module
              writes state-ict-live/<ASSET>/context.json

Local: dashboard.py  ← REWRITE
  SSH-fetches position.json + context.json + trades.jsonl + circuit_breaker.json
  + heartbeat.json per asset, renders. No computation, no ICT imports.
```

**Design rule, non-negotiable:** the context dump must not be able to affect trading. It
runs *after* `run_full_cycle()` returns, reads only already-fetched candles, writes only to
its own file, and is wrapped in a `try/except` that logs and swallows. A bug in the
dashboard's data feed must never cost money.

**Why not have the dashboard recompute locally:** it would duplicate the entire detection
pipeline in a second place and risk showing Linh zones the worker never saw — precisely
the drift `locate_pending_setup` was written to avoid. It would also mean moving ~70k
candles per asset per refresh over SSH.

---

## 5. Part A — worker-side change

### A1. New module `hermes_trading/ict/context.py`

One pure function, no I/O, no broker calls:

```python
def build_market_context(
    candles_15m, asset, equity, *, as_of_index=None, **scan_params
) -> dict
```

It reuses `build_detection_context()` — the *same* call the trading path makes, so what the
dashboard shows is by construction what the worker saw — then additionally computes
`sr_zones()`, `compute_bias()` and `dealing_range()` at the current bar, and re-runs
`build_setup()` over recent MSS events **keeping the rejected ones**.

Proposed output schema:

```jsonc
{
  "asset": "BTC/USDT",
  "generated_at_ms": 1753000000000,
  "last_bar_ts": 1752999900000,
  "exec_tf": "1h",
  "price": 118432.10,              // close of the most recent exec bar
  "atr": 640.25,

  "bias": {
    "direction": "long",           // long | short | no_trade
    "weekly_trend": "uptrend",
    "daily_trend": "range",
    "reason": "..."                // Bias.reason, straight through
  },

  "dealing_range": {
    "low": 112000.0, "high": 121500.0,
    "retracement_pct": 0.68,       // where price sits, 0 = low, 1 = high
    "zone": "premium",             // premium | discount
    "in_ote": true,
    "ote_band": [117890.0, 119695.0]   // direction-relative, per _ote_price_band
  },

  "zones": {
    "fvg":          [{"low": .., "high": .., "kind": "bullish", "index": .., "bar_ts": .., "displacement": true,  "mitigated": false}],
    "order_blocks": [{"low": .., "high": .., "kind": "bullish", "index": .., "bar_ts": .., "break_index": .., "mitigated": false}],
    "breakers":     [{"low": .., "high": .., "kind": "bullish", "flip_index": .., "bar_ts": .., "mitigated": false}],
    "sr":           [{"price_low": .., "price_high": .., "kind": "resistance", "touches": 3, "strength": 2.4}],
    //   note: Zone.strength is touches x recency_weight x volume_weight — unbounded,
    //   NOT 0-1. Normalise against the max strength in the set before mapping to opacity.
    "liquidity":    [{"price": .., "kind": "buyside", "source": "pdh", "bar_ts": ..}]
  },

  "recent_events": {
    "sweeps": [{"bar_ts": .., "pool_price": .., "kind": "sellside", "penetration": .., "direction": "bullish"}],
    "mss":    [{"bar_ts": .., "direction": "bullish", "broken_swing_price": .., "close": ..}]
  },

  "candidates": [                  // newest first; qualified AND near-miss
    {
      "mss_ts": 1752980000000,
      "direction": "bullish",
      "status": "pending",         // pending | filled | invalidated | expired
      "qualified": false,
      "score": 11,                 // out of 20
      "grade": "b",                // a_plus | b | none
      "gate_failures": ["session"],
      "entry_zone": {"kind": "fvg", "low": .., "high": ..},
      "entry_price": .., "stop_price": .., "target_price": .., "rr": 1.9,
      "swept_level": .., "mss_level": ..,
      "size": {"qty": .., "leverage": 4, "notional": .., "risk_usd": ..}   // null if ungraded
    }
  ],

  "gate_summary": {                // count of candidates failing each gate, this window
    "htf_bias": 0, "liquidity_event": 0, "mss": 0,
    "entry_zone": 2, "rr": 1, "session": 3, "risk_filter": 0, "score_below_b": 4
  }
}
```

**Bounding, so the file stays small.** Over a 2-year cache there are thousands of FVGs and
order blocks. Filter to what is actionable:

- zones: unmitigated as of the current bar, **and** within ±3×ATR of current price
- liquidity pools and S/R: within ±5×ATR of current price
- sweeps / MSS: last `state_ttl_bars` (40) bars only
- candidates: MSS events within the same lookback `scan_asset` already uses
  (`state_ttl_bars + max_bars_after_mss + 1` = 61 bars)

Expected size: a few KB per asset. Trivial over SSH.

**Cost:** `build_detection_context` currently runs twice per asset per cycle in the flat
path (once inside `scan_asset`, once here). Cycles currently take 88–142s against a 900s
budget, so there is ample headroom, but the clean fix is to hoist the context build and
pass it in. Worth doing, not worth blocking on.

### A2. Hook in `tools/run_ict_live.py`

In `run_once()`, after `run_full_cycle(...)` returns and results are printed:

```python
for asset in ASSETS:
    try:
        ctx = build_market_context(candles_by_asset[asset], asset, equity, **SCAN_PARAMS)
        _atomic_write(STATE_ROOT / asset.replace("/", "_") / "context.json", ctx)
    except Exception:
        print(f"[WARN] context dump failed for {asset} (trading unaffected)", flush=True)
        traceback.print_exc(file=sys.stdout)
```

Write atomically (`tmp` file + `os.replace`) so the dashboard can never read a half-written
file mid-poll.

`equity` needs one `broker.get_balance()` call per cycle — already made inside
`_look_for_new_setup`, but not for assets that are busy. One extra call per cycle is
negligible; fetch it once in `run_once` and pass it down.

### A3. Extend `heartbeat.json`

Currently `{ts, results[]}` with `ts` as a float epoch second. Add account-level facts the
dashboard needs and cannot otherwise get:

```jsonc
{
  "ts": 1753000000.0,
  "equity_usd": 808.03,
  "busy_count": 0,
  "max_concurrent": 1,
  "circuit_breaker": {"active": false, "daily_pnl_pct": -0.02, "weekly_pnl_pct": 0.05,
                      "daily_limit_pct": -0.20, "weekly_limit_pct": -0.40},
  "scan_params": { ... },          // so the dashboard shows the live params, never a stale copy
  "cycle_seconds": 94.2,
  "results": [ ... ]               // unchanged
}
```

Surfacing `scan_params` from the running process matters: they are currently duplicated by
hand across `run_ict_backtest.py`, `run_ict_scanner.py` and `run_ict_live.py`, with a
comment admitting they are kept in sync manually. A dashboard that reads them from the
heartbeat cannot drift.

### A4. Add realised R to closed trades

`_handle_position_closed` writes `pnl_usd` but no `pnl_pct` and no R-multiple. For an
R-based strategy, **realised R is the single most important number** and the dashboard
cannot reliably reconstruct it (the initial stop is known, but partial fills muddy it).
Add at write time:

```python
"risk_per_unit": abs(entry_price - initial_stop_price),
"planned_rr": ...,          # from the setup
"realised_r": total_pnl / (risk_per_unit * qty_total),
"pnl_pct": (exit_price - entry_price) / entry_price * (1 if long else -1),
```

Small change, permanently improves every downstream metric.

---

## 6. Part B — dashboard rewrite

Recommendation: **rewrite `dashboard.py` in place**, and move the current file to
`archive/dashboard-indicator-weight-2026-07-20.py` alongside the rest of the archived
system. Keeping a 1011-line dashboard for a halted worker in the project root invites the
exact confusion this session started with.

Retained from the old file (it is good and battle-tested): `_ssh_batch` multiplexing,
the last-known-state fallback with the "VPS unreachable" banner, the poll-thread +
`?refresh=1` pattern, AEST handling, and the CSS variable theme.

### Section 1 — Header + account bar

Mode badge (`live` / `dry-run`, from heartbeat), equity, busy `n/1`, circuit-breaker state
with the daily/weekly bars against their −20% / −40% limits, last cycle time and duration,
SSH-stale banner.

### Section 2 — Per-asset cards, one of three states

Every asset renders as a card whose entire shape is driven by `position.json.status`:

**(a) `open_position` — IN TRADE**

```
BTC/USDT   ● IN TRADE   LONG   A+   4x
─────────────────────────────────────
              price ladder
─────────────────────────────────────
entry (filled)   118,240.00
mark             118,432.10   +0.16%   +0.30R
initial stop     117,600.00   -0.54%
current stop     118,240.00   ← moved to BE      [BE ✓]
target           120,100.00   +1.57%   planned 2.9R
qty              0.0068 / 0.0068 remaining
partial 2R       not yet taken
risk at entry    $161.60 (20% of equity, A+)
opened           07-19 22:15 AEST · 14 bars ago
```

The stop row must show **both** initial and current, with a badge when the stop has moved
to breakeven or is trailing — that is the whole point of `replay_management_bars` and it is
currently invisible. Same for the 2R partial: taken / not taken, and remaining quantity.

**(b) `resting_order` — ORDER RESTING (not yet filled)**

Limit price, distance from mark in % and ATR, SL/TP that will attach on fill, grade, the
MSS timestamp it came from, bars elapsed against `state_ttl_bars = 40`, and a plain-English
line on what happens next: *"cancels if price closes back through the MSS level, or after
26 more bars."*

**(c) `flat` — WATCHING**

This is the section Linh specifically asked for and the one that does not exist today.

```
SOL/USDT   ○ WATCHING
bias  LONG   (weekly uptrend, daily range — discount)
range 62% retracement · in OTE ✓
─────────────────────────────────────
              price ladder
─────────────────────────────────────
PROPOSED SCENARIO           score 11/20   grade B   ✗ not armed
  direction     LONG
  entry zone    FVG   184.20 – 185.10   (mid 184.65)
  stop          181.90    -1.49%
  target        191.40    +3.65%
  R:R           2.4R      would risk $80.80 (B = 10%)
  blocked by    session  (outside kill zone)
  from MSS      07-20 04:00 AEST · 9 bars ago · expires in 31 bars

STRUCTURE NEAR PRICE
  resistance   190.80 – 191.60   3 touches
  bullish OB   183.90 – 185.40   unmitigated
  bullish FVG  184.20 – 185.10   unmitigated
  sellside liq 181.40            PDL
```

Show at most 2–3 candidates per asset, newest first, qualified ranked above near-misses.
When there are none: *"No MSS in the last 61 bars — nothing to propose."* — which is itself
informative, and different from *"3 candidates, all blocked by the session gate."*

### Section 3 — The price ladder

A single vertical CSS band per asset, min/max scaled to the union of all rendered levels
padded by 0.5×ATR, everything absolutely positioned by linear interpolation. Purely
presentational, no library:

- **Current price** — solid horizontal line, label pinned right
- **Entry zone** — filled band, blue
- **Stop** — red line; if the stop has moved, a faint red line at the initial level too
- **Target** — green line
- **FVG** — translucent amber band, hatched if mitigated
- **Order block** — translucent purple band
- **Breaker** — purple band, dashed border
- **S/R zone** — grey band, opacity scaled by `strength`
- **Liquidity pool** — dotted grey line, labelled `PDH` / `PDL` / `EQH` / `EQL`
- **OTE band** — thin bracket on the left gutter
- **Sweep** — small ✂ marker at the swept level

Overlapping labels are the main risk; keep labels in a right-hand gutter with a simple
collision nudge, and put full precision in `title=` tooltips.

### Section 4 — Closed trades

Columns: time (AEST), asset, direction, **grade**, entry, exit, stop, target, **planned R**,
**realised R**, P&L $, close reason, duration. Grade as a coloured chip. Realised R is the
column that should carry the eye.

### Section 5 — Performance, ICT-native

The old percentage-based P&L cards are wrong for this system — position sizing is
grade-dependent (20% vs 10% risk), so a raw percentage sum is meaningless. Replace with:

- Total realised R, and R by grade (does A+ actually outperform B? this is the single
  most valuable question the dashboard can answer, and it directly validates or refutes
  the 20-point scoring model)
- Win rate by grade, and by direction
- Average win R vs average loss R, expectancy in R
- Equity curve in R, sparkline
- Setup funnel: MSS detected → passed gates → armed → filled → closed, with the count lost
  at each stage. Given a base rate of ~0.5–0.9 trades/month/asset, **understanding why
  setups are rejected is more valuable than the P&L table**, and `gate_summary` makes it
  a straight render.

### Section 6 — Activity log

Tail `live.log` (not `hermes.log`), keeping cycle summaries, order placements, fills,
management actions, cancellations, errors, and any `NEEDS_MANUAL_REVIEW.flag`. A review
flag must be a loud red banner at the very top of the page — it means automated management
of that asset has stopped.

---

## 7. Correctness issues found while reviewing

These are separate from the dashboard work but were surfaced by it. Flagging, not fixing.

1. **`partial_realized_pnl_usd` is never populated.** In `live._manage_open_position`, the
   `partial_take` branch reduces `qty_remaining` and sets `partial_taken = True` but never
   accumulates the partial's realised P&L. `_handle_position_closed` then computes
   `trade["closed_pnl_usd"] + position.get("partial_realized_pnl_usd", 0.0)` — always adding
   zero. If Bybit logs the 2R partial as its own closed-PnL record, `matching[0]` may pick
   up either leg, and the recorded `pnl_usd` for a partialled trade will be wrong. Worth
   confirming against a real partialled trade before trusting any P&L display. **Recommend
   fixing before, or at least alongside, the dashboard** — otherwise the dashboard will
   faithfully display an incorrect number.

2. **No `pnl_pct` or R-multiple on closed trades** (covered in §A4).

3. **`SCAN_PARAMS` triplicated by hand** across the backtest, scanner and live tools, with
   only a comment enforcing sync. §A3 makes the dashboard read the live values, which at
   least makes a drift visible.

4. **`_write_heartbeat` uses `time.time()`**, wall-clock, while everything else is keyed to
   bar timestamps. Fine for a liveness check; do not use it for anything ordered.

---

## 8. Phasing

| Phase | Scope | Depends on | Rough effort |
|---|---|---|---|
| **0** | Verify VPS reachability; confirm the live worker is still cycling; snapshot the real on-disk shape of `position.json` / `trades.jsonl` / `heartbeat.json` before coding against assumptions | — | 15 min |
| **1** | `hermes_trading/ict/context.py` + unit tests against the existing real-data CSVs. Pure function, no deploy | Phase 0 | half session |
| **2** | Wire the dump into `run_ict_live.py` + extend heartbeat + §A4 trade fields. Test with `--dry-run` locally first | Phase 1 | half session |
| **3** | Dashboard rewrite: header, three-state asset cards, closed trades, log. No ladder yet — numbers first | Phase 2 (or against a hand-written fixture `context.json`, in parallel) | 1 session |
| **4** | Price ladder rendering | Phase 3 | half session |
| **5** | Performance section + setup funnel | Phase 3 | half session |
| **6** | Deploy to VPS, verify one clean cycle writes context files, verify dashboard renders live | all | 30 min |

Phases 3–5 can be built entirely offline against a fixture file, so dashboard work is not
blocked on the VPS being reachable.

---

## 9. Deploy notes

Per project doctrine — one change at a time, measure before stacking the next.

- The worker change requires a **restart of the live trading daemon**. Before any restart:
  confirm flat via `has_open_position()` on all 4 assets, exactly as session 18 did before
  the halt. Do not restart with a position open or an order resting — `attempted.json` and
  `position.json` survive a restart by design, but there is no reason to test that
  with real money on the line for a dashboard feature.
- Deploy target is `/opt/trading/hermes-trading/` (**hyphen**), via `git fetch` then a
  verified fast-forward. Memory.md records that this clone's `git status` has lied before
  when `git fetch` had not run — fetch first, always.
- The dashboard is local-only; it needs no deploy, but its SSH host/paths change and should
  be verified against the running system, not the plan.
- Rollback: the context dump is additive and try/except-wrapped. Reverting is `git revert`
  plus a restart; no state migration, no orphaned files that would confuse the worker.

---

## 10. Open questions for Linh — ANSWERED, see §0

1. **XRP** — dropped from the ICT universe (ICT trades BTC/ETH/SOL/TAO). Intentional, or
   should XRP be added to `fetch_ict_live_data_bybit.ASSETS`? That is a strategy change,
   not a dashboard one, and would need its own calibration check.
2. **Historical old-system trades** — the archived `trades.jsonl` files hold the entire
   indicator-weight trading history. Should the new dashboard show any of it (as a
   clearly-separated "archived system" tab), or is it fully retired from view?
3. **The `partial_realized_pnl_usd` bug (§7.1)** — fix it as part of this work, or raise it
   as its own item? My recommendation is to fix it first, since the dashboard's headline
   numbers depend on it.
4. **Refresh cadence.** The worker cycles every 15 minutes, so the dashboard's current 60s
   poll re-reads identical files ~15 times per cycle. Suggest 5 minutes, with the manual
   `↻ sync now` retained.
5. **Alerting.** Once the dashboard can see an armed setup, a push notification when an
   order goes resting or fills is a small increment. In scope, or later?

---

## 11. Handoff

**Next agent:** deploy agent, once Linh signs off on the deploy (the build is done).
**Read first:** §0 of this file, then `memory.md` (Last Updated + Handoffs → Session 21),
then `ict-strategy-plan-2026-07-18.md` §3 (mechanical definitions of FVG / OB / S&R) and
§9 (the gate and scoring model the dashboard renders).

**Start at:**

1. Run the full test suite somewhere without a 45-second command ceiling —
   `pytest tests/ict/` including the real-CSV modules. Do not deploy on the strength of
   the partial run recorded in §0.
2. Verify VPS reachability, then confirm the real on-disk shape of `position.json` /
   `trades.jsonl` / `heartbeat.json` matches what `dashboard.py` expects. SSH failed from
   the sandbox in session 21, so the schemas are still source-derived, not observed.
3. Deploy per §9 — flat-check all 4 assets first, `git fetch` before trusting the VPS
   clone's status, restart the worker, confirm one clean cycle writes four `context.json`
   files, then point the dashboard at it.

**Do not:** deploy or restart the live worker without Linh's explicit sign-off. Real money
is on this account ($808.03 as of session 20).

**Watch on first live cycle:** cycle duration. The context dump re-runs
`build_detection_context`, so a flat asset now builds it twice per cycle. Measured cost is
~3 s per asset on 30k candles, and the live cache is ~70k, against a 900 s budget with
cycles currently at 88–142 s — comfortable, but worth confirming rather than assuming. If
it ever matters, the fix is to hoist the detection context and pass it into both.
