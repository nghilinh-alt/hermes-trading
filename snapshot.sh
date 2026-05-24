#!/usr/bin/env bash
# snapshot.sh — Nightly commit of evolved strategy state to the state-snapshots branch.
#
# What it saves (per asset):
#   strategy.yaml    — current indicator weights + thresholds after self-improvement
#   memory.md        — Hermes's running log of past reflection decisions
#   hypotheses.jsonl — full history of every strategy change with reasoning
#
# What it does NOT save:
#   trades.jsonl     — live trade records (sensitive financial data, stays on VPS)
#   heartbeat.json   — transient runtime state
#   .env             — secrets
#
# Usage:
#   bash snapshot.sh              # run manually
#   cron: 0 14 * * * /opt/trading/hermes_trading/snapshot.sh >> /opt/trading/hermes_trading/logs/snapshot.log 2>&1
#
# Setup (one-time, on VPS):
#   cd /opt/trading/hermes_trading
#   git checkout --orphan state-snapshots
#   git rm -rf .
#   git commit --allow-empty -m "init: state-snapshots branch"
#   git push origin state-snapshots
#   git checkout master

set -euo pipefail

REPO="/opt/trading/hermes_trading"
BRANCH="state-snapshots"
ASSETS=("btc_usdt" "eth_usdt" "sol_usdt" "tao_usdt")
DATE=$(date -u +"%Y-%m-%d %H:%M UTC")
SNAPSHOT_FILES=("strategy.yaml" "memory.md" "hypotheses.jsonl")

echo "[snapshot] Starting — $DATE"

cd "$REPO"

# Create a clean worktree for the snapshot branch
WTREE=$(mktemp -d)
trap 'git -C "$REPO" worktree remove --force "$WTREE" 2>/dev/null; rm -rf "$WTREE"' EXIT

if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
    git worktree add "$WTREE" "$BRANCH"
else
    # Branch doesn't exist yet — create it as an orphan inside the worktree
    git worktree add --orphan "$WTREE" "$BRANCH"
fi

# Sync snapshot files into the worktree
changed=0
for slug in "${ASSETS[@]}"; do
    src_dir="$REPO/state/$slug"
    dst_dir="$WTREE/state/$slug"
    mkdir -p "$dst_dir"
    for fname in "${SNAPSHOT_FILES[@]}"; do
        src="$src_dir/$fname"
        dst="$dst_dir/$fname"
        if [ -f "$src" ]; then
            # Only copy if content differs (avoid empty commits)
            if ! diff -q "$src" "$dst" > /dev/null 2>&1; then
                cp "$src" "$dst"
                changed=$((changed + 1))
                echo "[snapshot]   updated state/$slug/$fname"
            fi
        fi
    done
done

# Also snapshot the shared goal.yaml so we can see what the agent was optimising for
if [ -f "$REPO/state/goal.yaml" ]; then
    mkdir -p "$WTREE/state"
    if ! diff -q "$REPO/state/goal.yaml" "$WTREE/state/goal.yaml" > /dev/null 2>&1; then
        cp "$REPO/state/goal.yaml" "$WTREE/state/goal.yaml"
        changed=$((changed + 1))
        echo "[snapshot]   updated state/goal.yaml"
    fi
fi

# Commit and push only if there are actual changes
cd "$WTREE"
git add -A

if [ "$changed" -eq 0 ] || git diff --cached --quiet; then
    echo "[snapshot] No changes — skipping commit."
else
    git config user.email "hermes-bot@rogue-night.ai"
    git config user.name  "Hermes Trading Bot"
    git commit -m "snapshot: $DATE ($changed files updated)"
    if git push origin "$BRANCH"; then
        echo "[snapshot] Committed and pushed — $changed files updated."
    else
        echo "[snapshot] WARNING: local commit succeeded but push failed (check SSH key / network)."
        echo "[snapshot] Run manually: cd $REPO && git checkout $BRANCH && git push origin $BRANCH && git checkout master"
    fi
fi

echo "[snapshot] Done."
