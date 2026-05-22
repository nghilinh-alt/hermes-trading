# HerMES-Trading - Direct Copy to VPS (Alternative Method)

## Option A: Upload via SFTP/SCP from Windows PowerShell

```powershell
# On your Windows machine, connect to VPS and copy files:
scp hermes-trading-deploy.tar.gz root@187.127.108.173:/opt/trading/

# Then on VPS run:
cd /opt/trading && tar -xzf hermes-trading-deploy.tar.gz

# Install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

---

## Option B: Copy Files via `scp` from Your Windows Terminal

```cmd
cd C:\Users\nghil\projects\hermes\hermes-trading

# Copy the package directory (not the archive)
scp hermes_trading/ pyproject.toml requirements.txt Dockerfile docker-compose.yml .env .gitignore root@187.127.108.173:/opt/trading/
```

---

## Option C: Direct Deploy on VPS (Copy-Paste Method)

If you have SSH access to your VPS, SSH in and run these commands:

```bash
# 1. Create directory structure
sudo mkdir -p /opt/trading/hermes-trading
cd /opt/trading/hermes-trading

# 2. Clone from GitHub (or use git pull if already cloned)
git clone https://github.com/nghilinh-alt/hermes-trading.git . || git pull origin master

# 3. Create virtual environment
python3 -m venv venv

# 4. Activate and install
source venv/bin/activate
pip install --upgrade pip
pip install ccxt yfinance pyyaml httpx aiofiles numpy pandas rich uv

# 5. Set up docker-compose.yml (if not present)
cat > docker-compose.yml << 'EOF'
version: '3.8'
services:
  trading-agent:
    build: .
    container_name: hermes-trading-agent
    volumes:
      - ./state:/app/state
    environment:
      - CCXT_API_KEY=${CCXT_API_KEY}
      - CCXT_API_SECRET=${CCXT_API_SECRET}
EOF

# 6. Build and start
docker-compose up -d --build

# 7. View logs
docker-compose logs -f trading-agent
```

---

## Option D: Direct Python Run (No Docker)

If you don't have Docker on your VPS:

```bash
cd /opt/trading/hermes-trading
source venv/bin/activate
python -m hermes_trading.run
```

---

## Quick Commands to Run on VPS After Copying Files

```bash
# Verify installation
source venv/bin/activate
pip install -e .
python -c "import hermes_trading; print('✅ Installed!')"

# Configure .env (edit as needed)
nano .env  # or vim .env, echo > .env
# Add API keys if using live trading, leave blank for paper trading

# Start the agent
source venv/bin/activate
uv run python -m hermes_trading.run
# OR: python -m hermes_trading.run
```

---

## Deployment Checklist

- [ ] VPS has Python 3.8+ installed
- [ ] Docker & docker-compose installed (for container mode)
- [ ] SSH access with root or deploy user
- [ ] Git clone/pull from https://github.com/nghilinh-alt/hermes-trading
- [ ] Virtual environment created and activated
- [ ] Dependencies installed (`pip install -e .`)
- [ ] `.env` file configured (or left blank for paper trading)
- [ ] Agent started: `python -m hermes_trading.run` or `docker-compose up -d --build`

---

## Quick Start After File Copy

```bash
# On your VPS after files are copied to /opt/trading/hermes-trading/

cd /opt/trading/hermes-trading
python3 -m venv venv && source venv/bin/activate
pip install -e .
nano .env  # Edit: leave blank for paper trading mode
python -m hermes_trading.run
```

---

## Monitoring Commands

```bash
# View agent logs
tail -f state/*.json
docker-compose logs -f trading-agent

# Check container status
docker-compose ps
docker ps | grep hermes

# Resource usage
top
htop  # if installed
```
