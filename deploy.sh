#!/bin/bash
# Hermes Trading Agent - Deployment Script
# Usage: ./deploy.sh [github-username] [repo-name]

set -e

echo "=========================================="
echo "  Hermes Trading Agent - Deployment"
echo "=========================================="
echo ""

# Configuration
REPO_NAME="${1:-HerMES-Trading}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}Configuration:${NC}"
echo "  Working Directory: $SCRIPT_DIR"
echo "  Repository Name:   ${REPO_NAME:-HerMES-Trading}"
echo ""

# Step 1: Initialize git if not already done
echo -e "${YELLOW}[1/4] Checking Git setup...${NC}"
cd "$SCRIPT_DIR"

if [ ! -d ".git" ]; then
    echo "  Initializing new Git repository..."
    git init
    git config user.name "${USER:-$(whoami)}"
    git config user.email "$(cat ~/.git-email 2>/dev/null || echo $(git config user.email 2>/dev/null || email))"
else
    echo "  Git repository already exists."
fi

# Step 2: Add files (excluding .env and state)
echo -e "${YELLOW}[2/4] Adding project files...${NC}"
git add \
    .gitignore \
    pyproject.toml \
    Dockerfile \
    hermes_trading/ \
    README.md \
    requirements.txt 2>/dev/null || true

# Step 3: Commit changes
echo -e "${YELLOW}[3/4] Creating commit...${NC}"
git status --short

COMMIT_MESSAGE="Self-improving trading agent deployment

Features:
- Autonomous trading loop with reflection capabilities
- Multi-source data integration (macro, news, on-chain, price)
- Performance scoring and evaluation system
- Docker containerization for easy deployment"

git commit -m "$COMMIT_MESSAGE" 2>/dev/null || {
    echo "  No changes to commit (already up to date or no new changes)"
}

# Step 4: Setup remote if needed
echo -e "${YELLOW}[4/4] Setting up Git remote...${NC}"
if [ -z "$(git remote get-url origin 2>/dev/null)" ]; then
    read -p "  Enter your GitHub username (or press Enter to skip): " GH_USERNAME
    
    if [ ! -z "$GH_USERNAME" ]; then
        read -p "  Enter repo name (default: $REPO_NAME): " GH_REPO_NAME
        
        REPO_URL="${GH_USERNAME}/${GH_REPO_NAME:-$REPO_NAME}.git"
        
        echo "  Adding remote: ${REPO_URL}"
        git remote add origin "$REPO_URL"
    else
        echo "  No GitHub username provided - using local only"
    fi
fi

# Push to GitHub if remote exists
if [ -n "$(git remote get-url origin 2>/dev/null)" ]; then
    echo ""
    echo -e "${GREEN}Repository ready for push!${NC}"
    echo -e "${YELLOW}To deploy to GitHub, run:${NC}"
    echo ""
    echo "  git push -u origin $(git remote get-url origin | sed 's|https://[^/@]*@||' | sed 's|/|$|')\
    || gh repo create \
        ${REPO_NAME:-HerMES-Trading} \
        --source=. \
        --remote=origin \
        --push \
        --private"
    echo ""
else
    echo -e "${YELLOW}Git remote not configured. To deploy locally:${NC}"
    echo "  Use this script on your VPS after cloning:"
    echo ""
    echo "  curl -L https://raw.githubusercontent.com/nghil/HerMES-Trading/master/pyproject.toml > ./pyproject.toml"
fi

echo ""
echo -e "${GREEN}Deployment setup complete!${NC}"
echo ""
echo "Next steps:"
echo "1. Install dependencies: uv sync or pip install -e ."
echo "2. Configure .env with your API keys"
echo "3. Run: uv run python -m hermes_trading.run"
echo ""
