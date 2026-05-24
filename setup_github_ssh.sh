#!/usr/bin/env bash
# setup_github_ssh.sh — One-time VPS setup to allow snapshot.sh to push to GitHub.
#
# Run once on the VPS as root:
#   bash /opt/trading/hermes_trading/setup_github_ssh.sh
#
# After running, copy the printed public key into GitHub:
#   GitHub → Settings → SSH and GPG keys → New SSH key
#   Title: hermes-vps
#   Paste the key → Add SSH key
#
# Then re-run snapshot.sh manually to verify the push works.

set -euo pipefail

KEY_FILE="$HOME/.ssh/id_ed25519_hermes"
REPO_DIR="/opt/trading/hermes_trading"

echo "=== Hermes GitHub SSH Setup ==="
echo ""

# 1. Generate SSH key if it doesn't exist
if [ -f "$KEY_FILE" ]; then
    echo "[✓] SSH key already exists at $KEY_FILE"
else
    echo "[+] Generating new ED25519 SSH key..."
    ssh-keygen -t ed25519 -C "hermes-vps@rogue-night.ai" -f "$KEY_FILE" -N ""
    echo "[✓] Key generated: $KEY_FILE"
fi

# 2. Ensure .ssh dir has correct permissions
chmod 700 "$HOME/.ssh"
chmod 600 "$KEY_FILE"
chmod 644 "${KEY_FILE}.pub"

# 3. Add to ssh-agent (best-effort — may not be running in all shell contexts)
if command -v ssh-agent &>/dev/null; then
    eval "$(ssh-agent -s)" 2>/dev/null || true
    ssh-add "$KEY_FILE" 2>/dev/null && echo "[✓] Key added to ssh-agent" || echo "[!] Could not add to ssh-agent (non-fatal)"
fi

# 4. Configure SSH to use this key for GitHub
SSH_CONFIG="$HOME/.ssh/config"
if ! grep -q "id_ed25519_hermes" "$SSH_CONFIG" 2>/dev/null; then
    cat >> "$SSH_CONFIG" <<EOF

# Hermes trading bot — GitHub SSH key
Host github.com
    HostName github.com
    User git
    IdentityFile $KEY_FILE
    IdentitiesOnly yes
EOF
    chmod 600 "$SSH_CONFIG"
    echo "[✓] Added GitHub host entry to $SSH_CONFIG"
else
    echo "[✓] GitHub entry already in $SSH_CONFIG"
fi

# 5. Switch repo remote from HTTPS to SSH
cd "$REPO_DIR"
CURRENT_REMOTE=$(git remote get-url origin 2>/dev/null || echo "none")
if echo "$CURRENT_REMOTE" | grep -q "https://"; then
    # Extract owner/repo from HTTPS URL and convert to SSH
    SSH_REMOTE=$(echo "$CURRENT_REMOTE" | sed 's|https://github.com/|git@github.com:|')
    git remote set-url origin "$SSH_REMOTE"
    echo "[✓] Remote updated: $CURRENT_REMOTE → $SSH_REMOTE"
elif echo "$CURRENT_REMOTE" | grep -q "git@github.com"; then
    echo "[✓] Remote is already SSH: $CURRENT_REMOTE"
else
    echo "[!] Unexpected remote URL: $CURRENT_REMOTE"
    echo "    Manually set it with: git remote set-url origin git@github.com:OWNER/REPO.git"
fi

echo ""
echo "============================================================"
echo "NEXT STEP — Add this public key to GitHub:"
echo "  GitHub → Settings → SSH and GPG keys → New SSH key"
echo "  Title: hermes-vps"
echo "============================================================"
echo ""
cat "${KEY_FILE}.pub"
echo ""
echo "============================================================"
echo ""
echo "After adding the key to GitHub, verify with:"
echo "  ssh -T git@github.com"
echo ""
echo "Then test snapshot push:"
echo "  bash $REPO_DIR/snapshot.sh"
echo ""
