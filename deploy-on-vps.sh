#!/bin/bash
# HerMES-Trading - Quick Deploy Script for VPS
# Run this on your VPS: chmod +x deploy.sh && ./deploy.sh

set -e

echo "=========================================="
echo "  HerMES-Trading - VPS Deployment"
echo "=========================================="
echo ""

VPS_USER="${USER:-$(whoami)}"
INSTALL_DIR="/opt/trading/hermes-trading"

echo -e "${GREEN}[1/5] Creating trading directory...${NC}"
sudo mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo -e "${GREEN}[2/5] Cloning from GitHub...${NC}"
git clone https://github.com/nghilinh-alt/hermes-trading.git . 2>/dev/null || \
    git clone https://github.com/nghilinh-alt/hermes-trading.git . 

echo -e "${GREEN}[3/5] Installing dependencies...${NC}"
python3 -m venv venv 2>/dev/null || virtualenv venv

source venv/bin/activate || source /usr/local/bin/virtualenv  # or: ./venv/bin/activate
pip install --upgrade pip
pip install -e "."

echo -e "${GREEN}[4/5] Creating docker-compose.yml...${NC}"
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

echo -e "${GREEN}[5/5] Building and starting container...${NC}"
docker-compose up -d --build

echo ""
echo "=========================================="
echo "✅ Deployment Complete!"
echo "=========================================="
echo ""
echo "Agent is running at: https://github.com/nghilinh-alt/hermes-trading"
echo ""
echo "View logs:"
echo "  docker-compose logs -f trading-agent"
echo ""
echo "Restart agent:"
echo "  docker-compose restart"
echo ""
echo "Stop agent:"
echo "  docker-compose down"
echo ""
echo "Check status:"
echo "  docker-compose ps"
echo ""
