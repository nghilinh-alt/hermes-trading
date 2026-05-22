# HerMES-Trading - Quick Start Guide for VPS Deployment

## Option 1: Deploy from GitHub (Recommended)

### Step 1: Create Repository on GitHub

1. Go to https://github.com/new
2. Owner: `nghil`
3. Repository name: `HerMES-Trading`
4. Description: "Self-improving trading agent"
5. **Keep public unchecked** (private by default)
6. ✅ Don't add a README, .gitignore, or license (we'll have these)

### Step 2: Push your code to GitHub

```bash
cd /c/Users/nghil/projects/hermes/hermes-trading
git push -u origin master
```

If you get "repository not found" error, go back to Step 1 and create it first.

### Step 3: Clone on VPS

```bash
git clone https://github.com/nghil/HerMES-Trading.git /path/to/hermes-trading
cd hermes-trading
```

---

## Option 2: Direct Install Without Git (Advanced)

If you prefer not to use git on the VPS:

### Step 1: Download files from GitHub

```bash
# On your VPS, download the tarball
curl -L https://github.com/nghil/HerMES-Trading/archive/refs/heads/master.tar.gz | \
    tar -xzf - -C /tmp HerMES-Trading-master
mv /tmp/HerMES-Trading-master /path/to/hermes-trading

# Move the project to your preferred location
mkdir -p /opt/trading
mv /path/to/hermes-trading/* /opt/trading/
rmdir /path/to/hermes-trading 2>/dev/null || true
```

### Step 2: Create .env file on VPS

Edit `/opt/trading/.env`:
- Fill in API keys if using live trading
- Leave blank for paper trading/testing mode

---

## Installation on VPS

### Method 1: Using uv (Recommended - fast and modern)

```bash
# Install uv if not present
pip install uv

# Install the project
cd /opt/trading/hermes-trading
uv sync

# Configure environment
cp .env.example .env  # or edit .env directly

# Run the agent
uv run python -m hermes_trading.run
```

### Method 2: Using pip (Traditional)

```bash
cd /opt/trading/hermes-trading

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -e .

# Configure environment  
cp .env.example .env  # or edit .env directly

# Run the agent
python -m hermes_trading.run
```

---

## Docker Deployment (Simplest for VPS)

### Build and run with docker-compose

On your VPS:

```bash
cd /opt/trading/hermes-trading

# Create docker-compose.yml
cat > docker-compose.yml << 'EOF'
version: '3.8'
services:
  trading-agent:
    build: .
    volumes:
      - ./state:/app/state
      - ./logs:/app/logs
    environment:
      - CCXT_API_KEY=${CCXT_API_KEY}
      - CCXT_API_SECRET=${CCXT_API_SECRET}
    restart: unless-stopped
EOF

# Build and run
docker-compose up -d --build

# View logs
docker-compose logs -f trading-agent
```

---

## Quick Test on VPS

After deployment, test basic functionality:

```bash
cd /opt/trading/hermes-trading

# Check Python can import modules
uv run python -c "import hermes_trading; print('✅ Core module loaded')"

# Verify dependencies
uv run pip list | grep -E "(ccxt|pandas|numpy)"

# Run in debug mode first (no real trading)
uv run python -m hermes_trading.run --debug
```

---

## Production Checklist

- [ ] API keys set in `.env` (use secure secrets manager if possible)
- [ ] Running behind reverse proxy/HTTPS
- [ ] Log rotation configured
- [ ] Resource limits set (memory/CPU)
- [ ] Monitoring/alerting setup (Prometheus, Datadog, etc.)
- [ ] Database backups configured
- [ ] Disaster recovery plan documented

---

## Troubleshooting Common Issues

### "Permission denied" when running on VPS
```bash
chmod +x deploy.sh run.sh  # Make scripts executable
sudo chown -R $USER:$USER /opt/trading  # Fix ownership
```

### Port already in use
```bash
# Find the process using port
lsof -i :5175 | grep LISTEN
# Or change the port in your config
```

### Dependency version conflicts
```bash
# Use uv for better dependency resolution
pip install uv
uv sync --upgrade
```

---

## Support & Documentation

- Full README: See `README.md` in repo root
- API docs: Check GitHub Issues
- Bug reports: Open on GitHub at https://github.com/nghil/HerMES-Trading/issues
