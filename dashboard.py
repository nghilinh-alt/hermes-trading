"""
dashboard.py -- local Hermes Trading dashboard for the ICT live worker.

Pulls state from the VPS over SSH, serves at http://localhost:8888.

Reads (read-only, never writes to the VPS):
  state-ict-live/heartbeat.json              account-level liveness + equity
  state-ict-live/<ASSET>/position.json       flat | resting_order | open_position
  state-ict-live/<ASSET>/context.json        market snapshot written each cycle
  state-ict-live/<ASSET>/trades.jsonl        closed trades
  state-ict-live/<ASSET>/NEEDS_MANUAL_REVIEW.flag
  live.log                                   worker log tail

The predecessor of this file (for the retired indicator-weight agent) is at
archive/dashboard-indicator-weight-2026-07-20.py. Nothing here is ported
from it except the SSH batching, the stale-state fallback and the theme --
the ICT worker shares no state schema with the old system.

Usage:
  python dashboard.py

Requires SSH key auth to the VPS (no password prompts).
"""
import http.server
import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

AEST = ZoneInfo("Australia/Sydney")

VPS      = "root@187.127.108.173"
VPS_BASE = "/opt/trading/hermes-trading"
STATE    = f"{VPS_BASE}/state-ict-live"
LOG_PATH = f"{VPS_BASE}/live.log"

# Matches tools/fetch_ict_live_data_bybit.ASSETS exactly. XRP is deliberately
# absent -- it is not part of the ICT universe (confirmed with Linh, s21).
ASSETS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "TAO/USDT"]

PORT      = 8888
POLL_SECS = 300  # the worker only cycles every 15 min; polling faster just re-reads identical files

# Palette
GREEN, RED, AMBER, BLUE, PURPLE = "#1D9E75", "#E24B4A", "#EF9F27", "#4A7DE2", "#8B5CF6"


def _slug(asset: str) -> str:
    """BTC/USDT -> BTC_USDT, matching AssetStateStore's own directory naming."""
    return asset.replace("/", "_")


# ── SSH ────────────────────────────────────────────────────────────────────────

def _ssh_batch(files: list[str]) -> dict[str, str]:
    """Fetch many remote files in one SSH connection. Returns {} on any failure."""
    SEP = "---HERMES_SEP---"
    # The bare `echo` between the file and the END marker is load-bearing:
    # the worker writes heartbeat.json / context.json / position.json via
    # json.dumps and the review flag via write_text, none of which end in a
    # newline. Without the extra echo, `cat`'s final line (e.g. a closing
    # `}`) glues directly onto `{SEP}END`, the marker never matches on its
    # own line, and that file's content is silently dropped -- which is
    # exactly what happened on first live render (2026-07-21): live.log and
    # trades.jsonl parsed (shell-written, newline-terminated) while every
    # JSON panel came back empty. The forced newline makes END always land
    # alone; the resulting blank line is removed by .strip() below.
    parts = [f"echo '{SEP}{p}'; cat {p} 2>/dev/null; echo; echo '{SEP}END'" for p in files]
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", VPS, "; ".join(parts)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        output = result.stdout if result.returncode == 0 else ""
    except Exception:
        return {}

    parsed, cur, lines = {}, None, []
    for line in output.splitlines():
        if line.startswith(SEP) and not line.endswith("END"):
            cur, lines = line[len(SEP):], []
        elif line == f"{SEP}END" and cur:
            parsed[cur] = "\n".join(lines).strip()
            cur = None
        elif cur is not None:
            lines.append(line)
    return parsed


def _json(text: str) -> dict:
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}


def _jsonl(text: str) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


# ── Fetch ──────────────────────────────────────────────────────────────────────

def _fetch(last_known: dict | None = None) -> dict:
    data = {
        "updated": datetime.now(AEST).strftime("%H:%M:%S AEST"),
        "assets": {}, "heartbeat": {}, "log_tail": [], "ssh_ok": True,
    }

    files = [f"{STATE}/heartbeat.json", LOG_PATH]
    for a in ASSETS:
        d = f"{STATE}/{_slug(a)}"
        files += [f"{d}/position.json", f"{d}/context.json", f"{d}/trades.jsonl",
                  f"{d}/NEEDS_MANUAL_REVIEW.flag"]
    cache = _ssh_batch(files)

    if not cache:
        data["ssh_ok"] = False
        if last_known:
            for k in ("assets", "heartbeat", "log_tail"):
                if k in last_known:
                    data[k] = last_known[k]
            data["updated"] = last_known.get("updated", "—") + " (stale — VPS unreachable)"
        return data

    data["heartbeat"] = _json(cache.get(f"{STATE}/heartbeat.json", ""))

    for a in ASSETS:
        d = f"{STATE}/{_slug(a)}"
        position = _json(cache.get(f"{d}/position.json", "")) or {"status": "flat"}
        data["assets"][a] = {
            "position": position,
            "context": _json(cache.get(f"{d}/context.json", "")),
            "trades": _jsonl(cache.get(f"{d}/trades.jsonl", "")),
            "review_flag": (cache.get(f"{d}/NEEDS_MANUAL_REVIEW.flag") or "").strip(),
        }

    data["log_tail"] = [l for l in (cache.get(LOG_PATH, "") or "").splitlines() if l.strip()][-40:]
    return data


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _px(v, dp=2) -> str:
    """Price formatting -- more decimals for low-priced assets like TAO/SOL."""
    if v is None:
        return "—"
    v = float(v)
    if v < 1:
        return f"${v:,.5f}"
    if v < 100:
        return f"${v:,.3f}"
    return f"${v:,.{dp}f}"


def _col(v) -> str:
    return GREEN if (v or 0) >= 0 else RED


def _sgn(v, suffix="", dp=2) -> str:
    if v is None:
        return "—"
    return f"{'+' if v >= 0 else ''}{v:.{dp}f}{suffix}"


def _ts_aest(ms, fmt="%m-%d %H:%M") -> str:
    if not ms:
        return "—"
    try:
        return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).astimezone(AEST).strftime(fmt)
    except Exception:
        return "—"


def _chip(text, colour, bg_alpha="0.14", title="") -> str:
    t = f" title='{_esc(title)}'" if title else ""
    return (f"<span{t} style='font-size:9px;padding:1px 6px;border-radius:4px;"
            f"background:{colour}{'' if colour.startswith('rgba') else ''};"
            f"background-color:color-mix(in srgb,{colour} {float(bg_alpha)*100:.0f}%,transparent);"
            f"color:{colour};font-weight:500;letter-spacing:0.02em'>{_esc(text)}</span>")


GRADE_LABEL = {"a_plus": ("A+", GREEN), "b": ("B", AMBER), "none": ("—", "#888")}


def _grade_chip(grade) -> str:
    label, colour = GRADE_LABEL.get(grade or "none", ("—", "#888"))
    return _chip(label, colour, title=f"setup grade: {grade}")


# Human-readable explanations of the 7 mandatory gates, so a blocked setup
# says *why* rather than printing an internal identifier at the operator.
GATE_TEXT = {
    "htf_bias": "HTF bias not aligned",
    "liquidity_event": "no liquidity sweep",
    "mss": "no market-structure shift",
    "entry_zone": "no unmitigated FVG/OB/breaker",
    "rr": "R:R below minimum",
    "session": "outside kill zone",
    "risk_filter": "position sizing rejected",
    "score_below_b": "score below B threshold",
}


def _gate_list(failures) -> str:
    if not failures:
        return f"<span style='color:{GREEN};font-size:11px'>all gates passed</span>"
    return " ".join(
        f"<span style='font-size:10px;padding:1px 6px;border-radius:4px;"
        f"background-color:color-mix(in srgb,{RED} 12%,transparent);color:{RED}' "
        f"title='gate: {_esc(g)}'>{_esc(GATE_TEXT.get(g, g))}</span>"
        for g in failures
    )


# ── Price ladder ───────────────────────────────────────────────────────────────

LADDER_H = 250  # px


def _ladder(price, bands, lines, height=LADDER_H) -> str:
    """
    Vertical price ladder: bands are (lo, hi, colour, label, dashed), lines are
    (price, colour, label, style). Everything is positioned by linear
    interpolation between the min and max of every level supplied, padded so
    nothing renders flush against an edge.

    Pure CSS -- no chart library, no candle data. The point is relative
    position (is the stop above or below that order block?), not a price
    chart.
    """
    levels = [p for (lo, hi, *_ ) in bands for p in (lo, hi) if p is not None]
    levels += [p for (p, *_) in lines if p is not None]
    if price is not None:
        levels.append(price)
    levels = [float(l) for l in levels if l is not None]
    if len(levels) < 2:
        return "<div style='font-size:11px;color:var(--muted);padding:8px 0'>Not enough levels to plot.</div>"

    lo_v, hi_v = min(levels), max(levels)
    pad = (hi_v - lo_v) * 0.08 or (hi_v * 0.001 or 1.0)
    lo_v, hi_v = lo_v - pad, hi_v + pad
    span = hi_v - lo_v

    def y(v):
        """Price -> px from top. Higher price = nearer the top, as on a chart."""
        return max(0.0, min(1.0, (hi_v - float(v)) / span)) * height

    out = [f"<div style='position:relative;height:{height}px;margin:10px 0 6px;"
           f"border-left:1px solid var(--border);border-right:1px solid var(--border)'>"]

    # Geometry (bands + lines) is drawn at EXACT price positions -- that's the
    # whole point of the ladder. Labels are collected separately and pushed
    # apart afterwards, so a cluster of nearby levels stays spatially honest
    # while its text stays legible. `ideal` is the label's true y; `size` and
    # `weight` let the price label sit a touch larger.
    labels: list[dict] = []

    for lo, hi, colour, label, dashed in bands:
        if lo is None or hi is None:
            continue
        top, bot = y(hi), y(lo)
        h = max(3.0, bot - top)
        border = f"1px dashed {colour}" if dashed else "none"
        out.append(
            f"<div title='{_esc(label)} · {_px(lo)}–{_px(hi)}' style='position:absolute;left:0;right:90px;"
            f"top:{top:.1f}px;height:{h:.1f}px;background-color:color-mix(in srgb,{colour} 16%,transparent);"
            f"border:{border};border-radius:2px'></div>"
        )
        labels.append({"ideal": top, "colour": colour, "text": label, "size": 9, "weight": 400})

    for lvl, colour, label, style in lines:
        if lvl is None:
            continue
        top = y(lvl)
        out.append(
            f"<div title='{_esc(label)} · {_px(lvl)}' style='position:absolute;left:0;right:90px;top:{top:.1f}px;"
            f"border-top:1.5px {style} {colour}'></div>"
        )
        labels.append({"ideal": top, "colour": colour, "text": label, "size": 9, "weight": 400})

    if price is not None:
        top = y(price)
        out.append(
            f"<div style='position:absolute;left:0;right:90px;top:{top:.1f}px;border-top:2px solid var(--fg)'></div>"
        )
        labels.append({"ideal": top, "colour": "var(--fg)", "text": _px(price), "size": 10, "weight": 600})

    # De-collision, two passes over labels sorted by true position:
    #   (1) top-down -- push each label to at least prev + ROW, so none
    #       overlaps the one above it;
    #   (2) bottom-up -- if that pushed the last past the bottom edge, pull
    #       each up to at most next - ROW.
    # Both passes only ever enforce the ROW gap, so together they settle to a
    # non-overlapping stack that stays within [0, height]. Labels remain in
    # price order; the title tooltip on each geometry element carries the
    # exact value, so a nudged label never misleads.
    ROW = 11.0
    labels.sort(key=lambda d: d["ideal"])
    prev = -ROW
    for d in labels:
        d["y"] = max(d["ideal"], prev + ROW)
        prev = d["y"]
    nxt = height
    for d in reversed(labels):
        d["y"] = min(d["y"], nxt - ROW)
        nxt = d["y"]
    for d in labels:
        yy = max(0.0, d["y"])
        out.append(
            f"<div style='position:absolute;right:0;width:88px;top:{yy:.1f}px;font-size:{d['size']}px;"
            f"font-weight:{d['weight']};color:{d['colour']};text-align:right;white-space:nowrap;"
            f"overflow:hidden;line-height:{ROW:.0f}px'>{_esc(d['text'])}</div>"
        )

    out.append("</div>")
    return "".join(out)


def _zone_bands(ctx: dict, limit_each=3) -> list:
    """Structure bands for the ladder, drawn from context.json's zones block."""
    z = (ctx or {}).get("zones") or {}
    bands = []
    for f in (z.get("fvg") or [])[:limit_each]:
        bands.append((f["low"], f["high"], AMBER, f"FVG {f['kind'][:4]}", False))
    for o in (z.get("order_blocks") or [])[:limit_each]:
        bands.append((o["low"], o["high"], PURPLE, f"OB {o['kind'][:4]}", False))
    for b in (z.get("breakers") or [])[:limit_each]:
        bands.append((b["low"], b["high"], PURPLE, f"BRK {b['kind'][:4]}", True))
    for s in (z.get("sr") or [])[:limit_each]:
        bands.append((s["price_low"], s["price_high"], "#888",
                      f"{'R' if s['kind'] == 'resistance' else 'S'} ×{s['touches']}", False))
    return bands


def _liquidity_lines(ctx: dict, limit=4) -> list:
    z = (ctx or {}).get("zones") or {}
    out = []
    for p in (z.get("liquidity") or [])[:limit]:
        out.append((p["price"], "#888", p["source"].upper(), "dotted"))
    return out


# ── Asset cards: open_position / resting_order / flat ──────────────────────────

def _row(label, value, colour=None, title="") -> str:
    c = f";color:{colour}" if colour else ""
    t = f" title='{_esc(title)}'" if title else ""
    return (f"<div{t} style='display:contents'>"
            f"<span style='font-size:11px;color:var(--muted);padding:2px 0'>{_esc(label)}</span>"
            f"<span style='font-size:12px;text-align:right;font-weight:500;padding:2px 0{c}'>{value}</span>"
            f"</div>")


def _grid(rows_html: str) -> str:
    return (f"<div style='display:grid;grid-template-columns:auto 1fr;gap:0 12px;"
            f"align-items:baseline'>{rows_html}</div>")


def _card_open(asset, pos, ctx) -> str:
    """In a live position: entry, mark, SL (initial + current), TP, partial state."""
    price   = (ctx or {}).get("price")
    entry   = pos.get("entry_price")
    init_sl = pos.get("initial_stop_price")
    cur_sl  = pos.get("current_stop_price")
    tp      = pos.get("target_price")
    is_long = pos.get("direction") == "bullish"
    sign    = 1 if is_long else -1

    risk_unit = abs(entry - init_sl) if (entry and init_sl) else None
    unreal_pct = (price - entry) / entry * sign * 100 if (price and entry) else None
    unreal_r   = (price - entry) * sign / risk_unit if (price and entry and risk_unit) else None
    unreal_usd = (price - entry) * sign * pos.get("qty_remaining", 0) if (price and entry) else None
    planned_rr = abs(tp - entry) / risk_unit if (tp and entry and risk_unit) else None

    def dist(level):
        return f" <span style='color:var(--muted);font-weight:400'>{_sgn((level - entry) / entry * 100, '%')}</span>" \
            if (level and entry) else ""

    # A stop that has moved off its initial level is the visible evidence of
    # breakeven/trailing management -- previously invisible anywhere.
    moved = cur_sl is not None and init_sl is not None and abs(cur_sl - init_sl) > 1e-9
    at_be = moved and entry is not None and abs(cur_sl - entry) <= abs(entry) * 1e-6
    sl_badge = ""
    if at_be:
        sl_badge = " " + _chip("BREAKEVEN", GREEN)
    elif moved:
        sl_badge = " " + _chip("TRAILING", GREEN)

    partial = _chip("2R PARTIAL TAKEN", GREEN) if pos.get("partial_taken") else \
        _chip("partial not yet taken", "#888")

    rows = (
        _row("entry (filled)", _px(entry))
        + _row("mark", _px(price), title="close of the most recent 1H bar the worker saw — not a live tick")
        + _row("unrealised", f"{_sgn(unreal_pct, '%')} &nbsp; <b>{_sgn(unreal_r, 'R')}</b>", _col(unreal_pct))
        + _row("unrealised $", _sgn(unreal_usd, dp=2) if unreal_usd is not None else "—", _col(unreal_usd))
        + _row("initial stop", _px(init_sl) + dist(init_sl), RED)
        + _row("current stop", _px(cur_sl) + dist(cur_sl) + sl_badge, RED)
        + _row("target", _px(tp) + dist(tp), GREEN)
        + _row("planned R:R", _sgn(planned_rr, "R") if planned_rr else "—")
        + _row("qty", f"{pos.get('qty_remaining', 0):.6g} / {pos.get('qty_total', 0):.6g}")
        + _row("leverage", f"{pos.get('leverage', '—')}x")
        + _row("opened", _ts_aest(pos.get("fill_timestamp")))
    )

    lines = [(entry, BLUE, "entry", "solid"), (tp, GREEN, "target", "solid"),
             (cur_sl, RED, "stop", "solid")]
    # Only draw the initial stop separately once it has actually moved --
    # otherwise it sits exactly under "stop" and just renders as clutter.
    if moved:
        lines.append((init_sl, RED, "initial SL", "dashed"))
    lines += _liquidity_lines(ctx, limit=2)

    return (f"<div style='margin-bottom:6px'>{partial}</div>"
            + _ladder(price, _zone_bands(ctx, limit_each=2), lines)
            + _grid(rows))


def _card_resting(asset, pos, ctx, scan_params) -> str:
    """Limit order placed, not yet filled -- a state the old dashboard had no concept of."""
    price = (ctx or {}).get("price")
    atr   = (ctx or {}).get("atr")
    entry = pos.get("entry_price")
    sl, tp = pos.get("stop_price"), pos.get("target_price")
    risk_unit = abs(entry - sl) if (entry and sl) else None
    rr = abs(tp - entry) / risk_unit if (tp and entry and risk_unit) else None

    away_pct = (entry - price) / price * 100 if (price and entry) else None
    away_atr = abs(entry - price) / atr if (price and entry and atr) else None

    ttl = (scan_params or {}).get("state_ttl_bars", 40)
    rows = (
        _row("limit price", _px(entry), BLUE)
        + _row("mark", _px(price))
        + _row("distance", f"{_sgn(away_pct, '%')} &nbsp; <span style='color:var(--muted)'>{away_atr:.1f}×ATR</span>"
               if away_atr is not None else _sgn(away_pct, "%"))
        + _row("stop (on fill)", _px(sl), RED)
        + _row("target (on fill)", _px(tp), GREEN)
        + _row("R:R", _sgn(rr, "R") if rr else "—")
        + _row("qty", f"{pos.get('qty', 0):.6g}")
        + _row("leverage", f"{pos.get('leverage', '—')}x")
        + _row("risk if stopped", f"${pos.get('risk_usd', 0):,.2f}")
        + _row("from MSS", _ts_aest(pos.get("mss_timestamp")))
    )

    note = (f"<div style='font-size:11px;color:var(--muted);line-height:1.6;margin-top:8px;"
            f"border-top:0.5px solid var(--border);padding-top:8px'>"
            f"Cancels automatically if price closes back through the MSS level, "
            f"or after {ttl} bars without a fill.</div>")

    lines = [(entry, BLUE, "limit", "dashed"), (sl, RED, "stop", "solid"), (tp, GREEN, "target", "solid")]
    lines += _liquidity_lines(ctx, limit=2)
    return _ladder(price, _zone_bands(ctx, limit_each=2), lines) + _grid(rows) + note


def _candidate_block(c: dict, ttl_bars: int) -> str:
    """One proposed scenario -- qualified or near-miss."""
    armed = c.get("qualified") and c.get("status") == "pending"
    head_col = GREEN if armed else AMBER if c.get("grade") != "none" else "#888"
    state = "ENTRY ARMED" if armed else ("blocked" if c.get("gate_failures") else c.get("status", "—"))
    ez = c.get("entry_zone") or {}
    zone_txt = (f"{ez.get('kind', '—').replace('_', ' ').upper()} &nbsp;"
                f"{_px(ez.get('low'))} – {_px(ez.get('high'))}") if ez else "—"

    size = c.get("size") or {}
    risk_txt = f"${size.get('risk_usd', 0):,.2f}" if size else "—"

    rows = (
        _row("direction", f"<span style='color:{GREEN if c['direction'] == 'bullish' else RED}'>"
                          f"{'LONG' if c['direction'] == 'bullish' else 'SHORT'}</span>")
        + _row("entry zone", zone_txt)
        + _row("entry (mid)", _px(c.get("entry_price")), BLUE)
        + _row("stop", _px(c.get("stop_price")), RED)
        + _row("target", _px(c.get("target_price")), GREEN)
        + _row("R:R", _sgn(c.get("rr"), "R") if c.get("rr") else "—")
        + _row("would risk", risk_txt + (f" · {size.get('leverage')}x" if size else ""))
        + _row("swept level", _px(c.get("swept_level")))
        + _row("MSS level", _px(c.get("mss_level")))
        + _row("age", f"{c.get('bars_since_mss', '—')} bars · "
                      f"expires in {c.get('bars_until_expiry', '—')}")
    )

    return (
        f"<div style='border:0.5px solid var(--border);border-radius:8px;padding:10px;margin-top:8px'>"
        f"<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;flex-wrap:wrap;gap:6px'>"
        f"<div style='display:flex;align-items:center;gap:6px'>"
        f"<span style='font-size:11px;font-weight:600;color:{head_col}'>{_esc(state.upper())}</span>"
        f"{_grade_chip(c.get('grade'))}"
        f"<span style='font-size:10px;color:var(--muted)'>score {c.get('score', '—')}/20</span>"
        f"</div>"
        f"<span style='font-size:10px;color:var(--muted)'>{_ts_aest(c.get('mss_ts'))}</span>"
        f"</div>"
        f"{_grid(rows)}"
        f"<div style='margin-top:8px;display:flex;gap:4px;flex-wrap:wrap'>{_gate_list(c.get('gate_failures'))}</div>"
        f"</div>"
    )


def _card_flat(asset, ctx, scan_params) -> str:
    """Watching: bias, proposed scenarios (qualified + near-miss), structure near price."""
    if not ctx:
        return ("<div style='font-size:11px;color:var(--muted);padding:8px 0'>"
                "No market context yet — the worker writes it once per cycle. "
                "If this persists, the worker may be running a build without the context dump.</div>")

    price = ctx.get("price")
    bias  = ctx.get("bias") or {}
    dr    = ctx.get("dealing_range") or {}
    ttl   = (scan_params or {}).get("state_ttl_bars", 40)

    bd = bias.get("direction", "no_trade")
    bias_col = GREEN if bd == "long" else RED if bd == "short" else "#888"
    bias_html = (
        f"<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px'>"
        f"<span style='font-size:12px;font-weight:600;color:{bias_col}'>{_esc(bd.replace('_', ' ').upper())}</span>"
        f"<span style='font-size:10px;color:var(--muted)'>weekly {_esc(bias.get('weekly_trend', '—'))} · "
        f"daily {_esc(bias.get('daily_trend', '—'))}</span></div>"
        f"<div style='font-size:10px;color:var(--muted);line-height:1.5'>{_esc(bias.get('reason', ''))}</div>"
    )

    if dr:
        ote = " · <span style='color:%s'>in OTE</span>" % GREEN if dr.get("in_ote") else ""
        bias_html += (
            f"<div style='font-size:10px;color:var(--muted);margin-top:4px'>"
            f"range {_px(dr.get('low'))}–{_px(dr.get('high'))} · "
            f"{(dr.get('retracement_pct') or 0) * 100:.0f}% retracement · {_esc(dr.get('zone', '—'))}{ote}</div>"
        )

    bands = _zone_bands(ctx)
    lines = _liquidity_lines(ctx)
    cands = ctx.get("candidates") or []
    if cands:
        top = cands[0]
        lines += [(top.get("stop_price"), RED, "stop", "dashed"),
                  (top.get("target_price"), GREEN, "target", "dashed")]
        ez = top.get("entry_zone") or {}
        if ez.get("low") is not None:
            bands.append((ez["low"], ez["high"], BLUE, "entry", False))
    if dr.get("ote_band"):
        b = dr["ote_band"]
        bands.append((b[0], b[1], "#888", "OTE", True))

    ladder = _ladder(price, bands, lines)

    if cands:
        cand_html = "".join(_candidate_block(c, ttl) for c in cands[:3])
    else:
        cand_html = ("<div style='font-size:11px;color:var(--muted);padding:8px 0'>"
                     "No candidate setup in the last 61 bars — no MSS with a matching sweep to evaluate.</div>")

    # Structure list -- the raw zones behind the ladder, in numbers.
    z = ctx.get("zones") or {}
    struct_rows = []
    for s in (z.get("sr") or [])[:3]:
        struct_rows.append(_row(f"{s['kind']}", f"{_px(s['price_low'])} – {_px(s['price_high'])} "
                                                f"<span style='color:var(--muted);font-weight:400'>×{s['touches']}</span>"))
    for o in (z.get("order_blocks") or [])[:3]:
        struct_rows.append(_row(f"{o['kind']} OB", f"{_px(o['low'])} – {_px(o['high'])}"))
    for f in (z.get("fvg") or [])[:3]:
        d = " <span style='color:%s;font-weight:400'>disp</span>" % AMBER if f.get("displacement") else ""
        struct_rows.append(_row(f"{f['kind']} FVG", f"{_px(f['low'])} – {_px(f['high'])}{d}"))
    for b in (z.get("breakers") or [])[:2]:
        struct_rows.append(_row(f"{b['kind']} breaker", f"{_px(b['low'])} – {_px(b['high'])}"))
    for p in (z.get("liquidity") or [])[:4]:
        struct_rows.append(_row(f"{p['source'].upper()} liq", _px(p["price"])))

    struct_html = (
        f"<div style='margin-top:10px;border-top:0.5px solid var(--border);padding-top:8px'>"
        f"<div style='font-size:10px;color:var(--muted);margin-bottom:4px'>STRUCTURE NEAR PRICE</div>"
        f"{_grid(''.join(struct_rows))}</div>"
    ) if struct_rows else ""

    return bias_html + ladder + cand_html + struct_html


# ── Closed trades + R-based performance ────────────────────────────────────────

def _all_trades(assets: dict) -> list[dict]:
    out = []
    for a in ASSETS:
        for t in (assets.get(a) or {}).get("trades", []):
            out.append({**t, "_asset": t.get("asset", a)})
    out.sort(key=lambda t: t.get("exit_utc") or 0, reverse=True)
    return out


def _realised_r(t: dict):
    """
    Prefer the R the worker recorded at close; fall back to deriving it for
    trades written before realised_r existed. Returns None when the initial
    stop distance isn't recoverable.
    """
    if t.get("realised_r") is not None:
        return float(t["realised_r"])
    entry, stop, qty, pnl = t.get("entry_price"), t.get("stop_price"), t.get("qty"), t.get("pnl_usd")
    if None in (entry, stop, qty, pnl):
        return None
    risk = abs(float(entry) - float(stop)) * float(qty)
    return (float(pnl) / risk) if risk > 0 else None


def _trades_table(trades: list[dict]) -> str:
    if not trades:
        return ("<div style='font-size:12px;color:var(--muted);margin-bottom:1.5rem'>"
                "No closed trades yet.</div>")

    head = "".join(
        f"<th style='padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left'>{h}</th>"
        for h in ("closed (AEST)", "asset", "side", "grade", "entry", "exit", "stop",
                  "planned", "realised", "P&L $", "source")
    )

    rows = []
    for t in trades[:50]:
        r = _realised_r(t)
        pnl = t.get("pnl_usd")
        src = t.get("pnl_source", "—")
        # local_estimate means the exchange records didn't reconcile against
        # the position's qty -- flag it rather than presenting it as truth.
        src_html = (f"<span style='color:{AMBER}' title='closed-pnl records did not reconcile; "
                    f"figure includes a locally-estimated partial'>estimate</span>"
                    if src == "local_estimate" else
                    "<span style='color:var(--muted)' title='summed from Bybit closed-pnl records'>exchange</span>")
        partial = " " + _chip("P", GREEN, title="2R partial was taken on this trade") if t.get("partial_taken") else ""
        cells = [
            f"<span style='color:var(--muted)'>{_ts_aest(t.get('exit_utc'))}</span>",
            _esc(t.get("_asset", "—")),
            f"<span style='color:{GREEN if t.get('direction') == 'bullish' else RED}'>"
            f"{'long' if t.get('direction') == 'bullish' else 'short'}</span>",
            GRADE_LABEL.get(t.get("grade"), ("—", "#888"))[0] + partial,
            _px(t.get("entry_price")),
            _px(t.get("exit_price")),
            _px(t.get("stop_price")),
            _sgn(t.get("planned_rr"), "R") if t.get("planned_rr") else "—",
            f"<b style='color:{_col(r)}'>{_sgn(r, 'R')}</b>" if r is not None else "—",
            f"<span style='color:{_col(pnl)};font-weight:500'>"
            f"{'+' if (pnl or 0) >= 0 else '-'}${abs(pnl or 0):,.2f}</span>",
            src_html,
        ]
        rows.append("<tr style='border-top:0.5px solid var(--border)'>"
                    + "".join(f"<td style='padding:5px 8px;font-size:11px'>{c}</td>" for c in cells)
                    + "</tr>")

    return (f"<div style='background:var(--bg);border:0.5px solid var(--border);border-radius:8px;"
            f"overflow:auto;margin-bottom:1.5rem'><table style='width:100%;border-collapse:collapse;"
            f"white-space:nowrap'><thead><tr style='background:var(--surface)'>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def _stat(label, value, sub="", colour=None) -> str:
    c = f";color:{colour}" if colour else ""
    return (f"<div style='background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px'>"
            f"<div style='font-size:11px;color:var(--muted);margin-bottom:4px'>{_esc(label)}</div>"
            f"<div style='font-size:20px;font-weight:500{c}'>{value}</div>"
            f"{f'<div style=font-size:10px;color:var(--muted);margin-top:3px>{sub}</div>' if sub else ''}</div>")


def _performance(trades: list[dict]) -> str:
    """
    R-based, not percentage-based. Position size is grade-dependent (A+ risks
    20% of equity, B risks 10%), so summing raw percentage returns across
    grades would be comparing different-sized bets. R is the only unit that
    makes them comparable.
    """
    closed = [t for t in trades if _realised_r(t) is not None]
    if not closed:
        return ("<div style='font-size:12px;color:var(--muted);margin-bottom:1.5rem'>"
                "No closed trades yet — performance appears here after the first close.</div>")

    rs = [_realised_r(t) for t in closed]
    total_r = sum(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    win_rate = len(wins) / len(rs) * 100
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    expectancy = total_r / len(rs)
    total_usd = sum(float(t.get("pnl_usd") or 0) for t in closed)

    cards = (
        _stat("total R", f"<b>{_sgn(total_r, 'R')}</b>", f"{len(rs)} closed trades", _col(total_r))
        + _stat("expectancy", _sgn(expectancy, "R"), "per trade", _col(expectancy))
        + _stat("win rate", f"{win_rate:.0f}%", f"{len(wins)}W / {len(losses)}L")
        + _stat("realised P&L", f"{'+' if total_usd >= 0 else '-'}${abs(total_usd):,.2f}",
                f"avg win {_sgn(avg_w, 'R')} · avg loss {_sgn(avg_l, 'R')}", _col(total_usd))
    )

    # By grade -- this is the question that validates or refutes the 20-point
    # scoring model: does A+ actually outperform B?
    by_grade = []
    for g in ("a_plus", "b"):
        sub = [t for t in closed if t.get("grade") == g]
        if not sub:
            continue
        sub_r = [_realised_r(t) for t in sub]
        w = sum(1 for r in sub_r if r > 0)
        label, colour = GRADE_LABEL[g]
        by_grade.append(
            f"<div style='display:flex;justify-content:space-between;font-size:11px;padding:4px 0;"
            f"border-top:0.5px solid var(--border)'>"
            f"<span style='color:{colour};font-weight:600'>{label}</span>"
            f"<span style='color:var(--muted)'>{len(sub)} trades · {w / len(sub) * 100:.0f}% win · "
            f"<b style='color:{_col(sum(sub_r))}'>{_sgn(sum(sub_r), 'R')}</b> total · "
            f"{_sgn(sum(sub_r) / len(sub_r), 'R')} avg</span></div>"
        )
    grade_html = (
        f"<div style='background:var(--bg);border:0.5px solid var(--border);border-radius:8px;"
        f"padding:12px;margin-bottom:1.5rem'>"
        f"<div style='font-size:10px;color:var(--muted);margin-bottom:2px'>BY GRADE "
        f"<span style='font-weight:400'>· does A+ actually outperform B?</span></div>"
        f"{''.join(by_grade)}</div>"
    ) if by_grade else ""

    return (f"<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));"
            f"gap:10px;margin-bottom:1rem'>{cards}</div>{grade_html}")


def _funnel(assets: dict) -> str:
    """
    Where setups die, aggregated across assets. At ~0.5-0.9 trades/month/asset
    this is usually more informative than the P&L table -- it answers "why
    isn't it trading?" directly from gate_summary.
    """
    totals: dict[str, int] = {}
    candidates = 0
    qualified = 0
    for a in ASSETS:
        ctx = (assets.get(a) or {}).get("context") or {}
        for gate, n in (ctx.get("gate_summary") or {}).items():
            totals[gate] = totals.get(gate, 0) + n
        for c in ctx.get("candidates") or []:
            candidates += 1
            if c.get("qualified"):
                qualified += 1

    if not totals and not candidates:
        return ("<div style='font-size:12px;color:var(--muted);margin-bottom:1.5rem'>"
                "No candidate setups in the current window across any asset.</div>")

    worst = max(totals.values()) if totals else 1
    bars = []
    for gate, n in sorted(totals.items(), key=lambda kv: -kv[1]):
        w = max(4, int(n / worst * 180))
        bars.append(
            f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:3px' "
            f"title='{_esc(gate)}'>"
            f"<span style='font-size:10px;width:170px;color:var(--muted)'>{_esc(GATE_TEXT.get(gate, gate))}</span>"
            f"<div style='height:6px;width:{w}px;background:{RED};border-radius:3px;opacity:0.6'></div>"
            f"<span style='font-size:10px;color:var(--muted)'>{n}</span></div>"
        )

    return (f"<div style='background:var(--bg);border:0.5px solid var(--border);border-radius:8px;"
            f"padding:12px;margin-bottom:1.5rem'>"
            f"<div style='font-size:11px;margin-bottom:8px'>"
            f"<b>{candidates}</b> candidate setup(s) in window · "
            f"<b style='color:{GREEN}'>{qualified}</b> fully qualified</div>"
            f"<div style='font-size:10px;color:var(--muted);margin-bottom:6px'>REJECTED BY GATE</div>"
            f"{''.join(bars)}</div>")


def _log_html(lines: list[str]) -> str:
    keep = ("placed", "filled", "closed", "cancel", "ERROR", "WARN", "Traceback",
            "needs_review", "circuit breaker", "partial", "trail", "[cycle]", "starting")
    shown = [l for l in lines if any(k.lower() in l.lower() for k in keep)][-25:]
    if not shown:
        shown = lines[-10:]
    if not shown:
        return "<div style='font-size:11px;color:var(--muted)'>No log output.</div>"
    out = []
    for l in shown:
        c = ""
        low = l.lower()
        if "error" in low or "traceback" in low:
            c = f"color:{RED}"
        elif "filled" in low or "closed" in low or "placed" in low:
            c = f"color:{GREEN}"
        elif "warn" in low:
            c = f"color:{AMBER}"
        elif l.startswith("[cycle]"):
            c = "color:var(--muted)"
        out.append(f"<div style='font-size:11px;line-height:1.8;{c}'>{_esc(l)}</div>")
    return "".join(out)


# ── Alerts ─────────────────────────────────────────────────────────────────────

def _alert_keys(assets: dict) -> list[dict]:
    """
    Stable identities for events worth notifying about. The browser keeps the
    set it has already shown in localStorage, so a key only fires once even
    though the page reloads on a timer.
    """
    events = []
    for a in ASSETS:
        st = assets.get(a) or {}
        pos, ctx = st.get("position") or {}, st.get("context") or {}
        status = pos.get("status")

        if st.get("review_flag"):
            events.append({"key": f"{a}:review", "title": f"{a} needs manual review",
                           "body": st["review_flag"][:180]})
        if status == "resting_order":
            events.append({"key": f"{a}:resting:{pos.get('mss_timestamp')}",
                           "title": f"{a} order resting",
                           "body": f"{'long' if pos.get('direction') == 'bullish' else 'short'} "
                                   f"limit @ {_px(pos.get('entry_price'))}"})
        elif status == "open_position":
            events.append({"key": f"{a}:filled:{pos.get('fill_timestamp')}",
                           "title": f"{a} position open",
                           "body": f"{'long' if pos.get('direction') == 'bullish' else 'short'} "
                                   f"filled @ {_px(pos.get('entry_price'))}"})
        for c in (ctx.get("candidates") or []):
            if c.get("qualified") and c.get("status") == "pending":
                events.append({"key": f"{a}:armed:{c.get('mss_ts')}",
                               "title": f"{a} setup armed",
                               "body": f"{c.get('grade', '').upper()} · "
                                       f"{'long' if c['direction'] == 'bullish' else 'short'} · "
                                       f"{_sgn(c.get('rr'), 'R')}"})
        for t in (st.get("trades") or [])[-3:]:
            r = _realised_r(t)
            events.append({"key": f"{a}:closed:{t.get('exit_utc')}",
                           "title": f"{a} trade closed",
                           "body": f"{_sgn(r, 'R')} · ${float(t.get('pnl_usd') or 0):,.2f}"})
    return events


# ── Page ───────────────────────────────────────────────────────────────────────

def _render(d: dict) -> str:
    assets  = d.get("assets", {})
    hb      = d.get("heartbeat", {})
    ssh_ok  = d.get("ssh_ok", True)
    updated = d.get("updated", "—")
    sp      = hb.get("scan_params") or {}

    banners = ""
    if not ssh_ok:
        banners += (f"<div style='background-color:color-mix(in srgb,{RED} 10%,transparent);"
                    f"border:0.5px solid {RED};border-radius:8px;padding:10px 14px;margin-bottom:1rem;"
                    f"font-size:12px;color:{RED}'>&#9888; VPS unreachable — showing last known state</div>")

    for a in ASSETS:
        flag = (assets.get(a) or {}).get("review_flag")
        if flag:
            banners += (f"<div style='background-color:color-mix(in srgb,{RED} 14%,transparent);"
                        f"border:1px solid {RED};border-radius:8px;padding:12px 14px;margin-bottom:1rem;"
                        f"font-size:12px;color:{RED}'><b>&#9888; {_esc(a)} NEEDS MANUAL REVIEW</b> — "
                        f"automated management of this asset is stopped.<br>"
                        f"<span style='font-size:11px'>{_esc(flag)}</span></div>")

    # Worker liveness: heartbeat ts is wall-clock seconds (see run_ict_live).
    hb_age = None
    if hb.get("ts"):
        hb_age = time.time() - float(hb["ts"])
    if hb_age is not None and hb_age > 30 * 60:
        banners += (f"<div style='background-color:color-mix(in srgb,{AMBER} 12%,transparent);"
                    f"border:0.5px solid {AMBER};border-radius:8px;padding:10px 14px;margin-bottom:1rem;"
                    f"font-size:12px;color:{AMBER}'>&#9888; Last worker cycle was "
                    f"{hb_age / 60:.0f} min ago — expected every 15 min. The worker may be stopped.</div>")

    equity = hb.get("equity_usd")
    cb = hb.get("circuit_breaker") or {}
    cb_active = cb.get("active")
    cb_col = RED if cb_active else GREEN
    account = (
        _stat("equity", f"${equity:,.2f}" if equity else "—",
              f"cycle {hb.get('cycle_seconds', '—')}s")
        + _stat("positions", f"{hb.get('busy_count', 0)} / {hb.get('max_concurrent', 1)}",
                "open + resting")
        + _stat("circuit breaker", "TRIPPED" if cb_active else "ok",
                f"day {(cb.get('daily_pnl_pct') or 0) * 100:+.1f}% / "
                f"{(cb.get('daily_limit_pct') or 0) * 100:.0f}% · "
                f"week {(cb.get('weekly_pnl_pct') or 0) * 100:+.1f}%", cb_col)
        + _stat("last cycle", _ts_aest((hb.get("ts") or 0) * 1000, "%H:%M") if hb.get("ts") else "—",
                f"{hb_age / 60:.0f} min ago" if hb_age is not None else "")
    )

    # Asset cards
    cards = ""
    for a in ASSETS:
        st = assets.get(a) or {}
        pos = st.get("position") or {"status": "flat"}
        ctx = st.get("context") or {}
        status = pos.get("status", "flat")

        if status == "open_position":
            label, colour, body = "IN TRADE", GREEN, _card_open(a, pos, ctx)
        elif status == "resting_order":
            label, colour, body = "ORDER RESTING", BLUE, _card_resting(a, pos, ctx, sp)
        else:
            label, colour, body = "WATCHING", "#888", _card_flat(a, ctx, sp)

        side = ""
        if status in ("open_position", "resting_order"):
            long_ = pos.get("direction") == "bullish"
            side = _chip("LONG" if long_ else "SHORT", GREEN if long_ else RED)
        grade = _grade_chip(pos.get("grade")) if pos.get("grade") else ""

        stale = ""
        if ctx.get("last_bar_ts"):
            stale = (f"<span style='font-size:10px;color:var(--muted)'>"
                     f"bar {_ts_aest(ctx['last_bar_ts'], '%H:%M')}</span>")

        cards += (
            f"<div style='background:var(--bg);border:0.5px solid var(--border);border-radius:12px;padding:1rem'>"
            f"<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;"
            f"gap:6px;flex-wrap:wrap'>"
            f"<div style='display:flex;align-items:center;gap:6px;flex-wrap:wrap'>"
            f"<span style='color:{colour}'>&#9679;</span>"
            f"<span style='font-size:14px;font-weight:600'>{_esc(a)}</span>"
            f"{_chip(label, colour)}{side}{grade}</div>{stale}</div>"
            f"{body}</div>"
        )

    trades = _all_trades(assets)
    alerts_json = json.dumps(_alert_keys(assets))

    kz = sp.get("kill_zones")
    params_line = " · ".join(f"{k} {v}" for k, v in sp.items() if k != "kill_zones") if sp else "—"

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes ICT</title>
<meta http-equiv="refresh" content="{POLL_SECS}">
<style>
  :root{{--bg:#fff;--fg:#111;--border:rgba(0,0,0,0.12);--muted:#888;--surface:#f6f6f4}}
  @media(prefers-color-scheme:dark){{:root{{--bg:#1a1a18;--fg:#eee;--border:rgba(255,255,255,0.12);--muted:#888;--surface:#232320}}}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--surface);
        color:var(--fg);padding:1.5rem;max-width:1100px;margin:0 auto}}
  h2{{font-size:12px;font-weight:500;color:var(--muted);margin:1.5rem 0 8px;letter-spacing:0.04em}}
  table{{color:var(--fg)}}
</style></head><body>

<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:1.5rem;gap:10px;flex-wrap:wrap">
  <div>
    <div style="font-size:18px;font-weight:600">Hermes Trading &middot; ICT</div>
    <div style="font-size:12px;color:var(--muted);margin-top:3px">
      synced {updated} &nbsp;·&nbsp; <span id="clk"></span> &nbsp;·&nbsp; refreshes every {POLL_SECS // 60} min
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:8px">
    <button id="notify" style="font-size:11px;padding:4px 10px;border-radius:6px;background:var(--surface);
      border:0.5px solid var(--border);color:var(--muted);cursor:pointer">&#128276; alerts</button>
    <a href="/?refresh=1" style="font-size:11px;padding:4px 10px;border-radius:6px;background:var(--surface);
      border:0.5px solid var(--border);color:var(--muted);text-decoration:none">&#8635; sync now</a>
    <span style="font-size:11px;padding:4px 10px;border-radius:6px;
      background-color:color-mix(in srgb,{RED} 15%,transparent);color:{RED}">
      {'DRY RUN' if hb.get('dry_run') else 'LIVE'}</span>
  </div>
</div>

{banners}

<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:1.5rem">
{account}
</div>

<h2>ASSETS</h2>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:12px;margin-bottom:1.5rem">
{cards}
</div>

<h2>WHY SETUPS ARE BEING REJECTED</h2>
{_funnel(assets)}

<h2>PERFORMANCE <span style="font-weight:400">· measured in R, since A+ and B risk different amounts</span></h2>
{_performance(trades)}

<h2>CLOSED TRADES <span style="font-weight:400">(last 50, newest first)</span></h2>
{_trades_table(trades)}

<h2>WORKER LOG</h2>
<div style="background:var(--bg);border:0.5px solid var(--border);border-radius:8px;padding:12px;
     font-family:ui-monospace,monospace;overflow-x:auto;margin-bottom:1rem">
{_log_html(d.get('log_tail', []))}
</div>

<div style="font-size:10px;color:var(--muted);text-align:center;padding-bottom:1.5rem;line-height:1.7">
  {_esc(VPS)} &nbsp;·&nbsp; kill zones {_esc(kz)} &nbsp;·&nbsp; {_esc(params_line)}<br>
  Params read live from the worker heartbeat — never a local copy.
</div>

<script>
(function tick(){{
  var el=document.getElementById('clk');
  if(el) el.textContent=new Date().toLocaleTimeString('en-AU',{{timeZone:'Australia/Sydney',hour12:false}})+' AEST';
  setTimeout(tick,1000);
}})();

// Alerts. Keys are stable per event, and the shown-set lives in localStorage,
// so a reload every {POLL_SECS // 60} min doesn't re-notify for the same thing.
var EVENTS = {alerts_json};
var SEEN_KEY = 'hermes_seen_alerts';

function seen(){{ try {{ return JSON.parse(localStorage.getItem(SEEN_KEY)) || []; }} catch(e) {{ return []; }} }}
function markSeen(keys){{ try {{ localStorage.setItem(SEEN_KEY, JSON.stringify(keys.slice(-300))); }} catch(e) {{}} }}

function fire(){{
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  var s = seen(), fresh = EVENTS.filter(function(e){{ return s.indexOf(e.key) === -1; }});
  // First run after enabling: adopt the current state silently rather than
  // firing a burst of notifications for things that already happened.
  if (s.length === 0) {{ markSeen(EVENTS.map(function(e){{ return e.key; }})); return; }}
  fresh.forEach(function(e){{ new Notification(e.title, {{ body: e.body }}); }});
  markSeen(s.concat(fresh.map(function(e){{ return e.key; }})));
}}

var btn = document.getElementById('notify');
function paint(){{
  if (!('Notification' in window)) {{ btn.textContent = 'alerts unsupported'; btn.disabled = true; return; }}
  btn.textContent = Notification.permission === 'granted' ? '\\uD83D\\uDD14 alerts on' : '\\uD83D\\uDD14 enable alerts';
}}
btn.addEventListener('click', function(){{
  if (Notification.permission === 'granted') return;
  Notification.requestPermission().then(function(){{ paint(); fire(); }});
}});
paint(); fire();
</script>
</body></html>"""


# ── Server ─────────────────────────────────────────────────────────────────────

_cache = {"data": {}, "lock": threading.Lock()}


def _poll_loop() -> None:
    while True:
        try:
            with _cache["lock"]:
                last = dict(_cache["data"])
            fresh = _fetch(last_known=last or None)
            with _cache["lock"]:
                _cache["data"] = fresh
        except Exception as e:
            print(f"[poll] error: {e}")
        time.sleep(POLL_SECS)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        params = parse_qs(urlparse(self.path).query)

        if "refresh" in params:
            with _cache["lock"]:
                last = dict(_cache["data"])
            fresh = _fetch(last_known=last or None)
            with _cache["lock"]:
                _cache["data"] = fresh

        with _cache["lock"]:
            data = _cache["data"]

        body = (_render(data).encode() if data else
                b"<html><body style='font-family:sans-serif;padding:2rem'>Loading&hellip;</body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    print("Hermes Trading Dashboard — ICT")
    print(f"Connecting to {VPS} ...")
    threading.Thread(target=_poll_loop, daemon=True).start()
    time.sleep(3)
    print(f"Dashboard at http://localhost:{PORT}\nPress Ctrl+C to stop.\n")
    import webbrowser
    webbrowser.open(f"http://localhost:{PORT}")
    with http.server.HTTPServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
