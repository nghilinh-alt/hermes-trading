#!/usr/bin/env python3
"""
patch_guard_reorder.py — session 15 (2026-07-13)

Reorders the two structural guards in execution.py::_structural_sl_tp() so the
soft R:R extension runs BEFORE the min_tp_pct hard floor. Content-based and
idempotent: it matches the guard blocks by their exact text, so it applies
cleanly to the VPS's current execution.py WITHOUT pulling in any other local
change (notably the still-undeployed session-12 position-sizing fix). This keeps
the deploy to exactly one variable.

Usage on the VPS (underscore clone = running agent):
    python3 tools/patch_guard_reorder.py /opt/trading/hermes_trading/hermes_trading/adapters/execution.py
Writes a .bak beside the target, applies the patch, then py_compiles the result.
Re-running after a successful patch is a safe no-op.
"""
import io, sys, py_compile

OLD_BODY = '''    # Guard 2 (Option B filter): TP must be at least min_tp_pct from entry
    tp_dist_pct = abs(tp_price - entry) / entry
    if tp_dist_pct < min_tp_pct:
        raise ValueError(
            f"Structural TP too thin: {tp_dist_pct:.2%} < min {min_tp_pct:.2%} "
            f"(entry={entry}, tp={tp_price}) — Option B target-return filter"
        )

    # Guard 3 (Soft R:R): extend TP if R:R below threshold
    if sl_dist > 0:
        rr_ratio = abs(tp_price - entry) / sl_dist
        if rr_ratio < min_rr_ratio:
            if direction == "long":
                tp_price = round(entry + sl_dist * min_rr_ratio, 4)
            else:
                tp_price = round(entry - sl_dist * min_rr_ratio, 4)
'''

NEW_BODY = '''    # Guard 2 (Soft R:R): extend TP if R:R below threshold. Runs BEFORE the
    # min_tp_pct floor (session 15, 2026-07-13) so the floor is applied to the
    # FINAL take-profit, not the raw structural level. Previously this ran after
    # the floor, so a signal whose nearest structural TP was thin got rejected
    # before the extension that would have widened it past the floor could run.
    if sl_dist > 0:
        rr_ratio = abs(tp_price - entry) / sl_dist
        if rr_ratio < min_rr_ratio:
            if direction == "long":
                tp_price = round(entry + sl_dist * min_rr_ratio, 4)
            else:
                tp_price = round(entry - sl_dist * min_rr_ratio, 4)

    # Guard 3 (Option B filter): FINAL TP must be at least min_tp_pct from entry
    tp_dist_pct = abs(tp_price - entry) / entry
    if tp_dist_pct < min_tp_pct:
        raise ValueError(
            f"Structural TP too thin: {tp_dist_pct:.2%} < min {min_tp_pct:.2%} "
            f"(entry={entry}, tp={tp_price}) — Option B target-return filter"
        )
'''

def main():
    if len(sys.argv) != 2:
        sys.exit("usage: patch_guard_reorder.py <path-to-execution.py>")
    path = sys.argv[1]
    src = io.open(path, encoding="utf-8").read()

    if NEW_BODY in src and OLD_BODY not in src:
        print("Already patched — no change."); return
    n = src.count(OLD_BODY)
    if n != 1:
        sys.exit(f"ABORT: expected exactly 1 match of the old guard block, found {n}. "
                 "File differs from expectation — do not force; inspect manually.")

    io.open(path + ".bak", "w", encoding="utf-8").write(src)
    io.open(path, "w", encoding="utf-8").write(src.replace(OLD_BODY, NEW_BODY))
    py_compile.compile(path, doraise=True)
    print(f"Patched OK. Backup at {path}.bak ; py_compile clean.")

if __name__ == "__main__":
    main()
