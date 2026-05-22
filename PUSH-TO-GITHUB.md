========================================
PUSH HERMES-TRADING TO GITHUB
Step-by-Step Instructions
========================================

STEP 1: CREATE THE REPOSITORY
-------------------------------
1. Open your web browser
2. Go to: https://github.com/new
3. Fill in:
   - Repository name: HerMES-Trading
   - Description: Self-improving trading agent
   - Keep "Public/Private": SELECT PRIVATE (recommended for API keys)
   - ❌ Disable: Initialize with README
   - ❌ Disable: Add .gitignore
   - ❌ Disable: Add license
   
4. Click "Create repository"

STEP 2: PUBLISH YOUR CODE
--------------------------
Run these commands on Windows:

cd /c/Users/nghil/projects/hermes/hermes-trading
git push -u origin master

If successful, you'll see:
"Enumerating objects..."
"Counting objects..."
"Writing objects..."
"Total ..."
"remote: Repository update hook succeeded"
"To finish setting up your fork, run: git push -f origin main (or master)"

Then:
git push -u origin master

STEP 3: CLONE ON YOUR VPS
--------------------------
SSH into your VPS and run:

git clone https://github.com/nghil/HerMES-Trading.git /path/to/hermes-trading
cd hermes-trading

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -e ".[dev]"

# OR for simpler install:
pip install ccxt yfinance pyyaml httpx aiofiles numpy pandas rich

STEP 4: CONFIGURE .ENV
-----------------------
Copy the example env file:
cp .env.example .env  # or just edit .env directly

Edit .env and add your API keys if needed. Leave blank for paper trading.

STEP 5: RUN THE AGENT
----------------------
uv sync && uv run python -m hermes_trading.run

# OR without uv:
python -m hermes_trading.run

========================================
ALTERNATIVE: DOCKER DEPLOYMENT
========================================

If you prefer Docker, create docker-compose.yml:

cat > docker-compose.yml << 'EOF'
version: '3.8'
services:
  trading-agent:
    build: .
    volumes:
      - ./state:/app/state
    environment:
      - CCXT_API_KEY=${CCXT_API_KEY}
      - CCXT_API_SECRET=${CCXT_API_SECRET}
EOF

Build and run:
docker-compose up -d --build

View logs:
docker-compose logs -f trading-agent

========================================
TROUBLESHOOTING
========================================

Issue: "Repository not found"
Fix: You MUST complete STEP 1 first. Go to https://github.com/new manually.

Issue: Authentication required for push
Fix: Run 'gh auth login' or set GITHUB_TOKEN environment variable.

Issue: Permission denied on VPS
Fix: chmod +x *.sh && sudo chown -R $USER:$USER /path/to/hermes-trading

Issue: Port already in use
Fix: Find and kill process, or change port in config.

========================================