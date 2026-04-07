#!/bin/bash
# ============================================================
# Faiba WiFi Captive Portal - Docker Setup Script
# ============================================================
#
# Quick start (copy-paste this entire block):
#
#   wget -qO- https://github.com/kibsalt/captive-portal/archive/refs/heads/main.tar.gz | tar xz
#   cd captive-portal-main
#   chmod +x setup.sh && ./setup.sh
#
# Or clone and run:
#
#   git clone https://github.com/kibsalt/captive-portal.git
#   cd captive-portal
#   chmod +x setup.sh && ./setup.sh
#
# ============================================================

set -e

PORTAL_PORT="${PORTAL_PORT:-8480}"
IMAGE_NAME="faiba-captive-portal"
CONTAINER_NAME="faiba-captive-portal"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}"
echo "  ============================================"
echo "    Faiba WiFi Captive Portal - Docker Setup"
echo "  ============================================"
echo -e "${NC}"

# --- Check Docker ---
if ! command -v docker &> /dev/null; then
    echo -e "${RED}Docker is not installed.${NC}"
    echo "Install Docker first:"
    echo "  curl -fsSL https://get.docker.com | sh"
    echo "  sudo usermod -aG docker \$USER"
    exit 1
fi

echo -e "${GREEN}[1/5]${NC} Docker found: $(docker --version)"

# --- Create .env if missing ---
if [ ! -f .env ]; then
    echo -e "${YELLOW}[2/5]${NC} Creating .env from .env.example..."
    cp .env.example .env

    # Generate a random secret key
    SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || openssl rand -hex 32 2>/dev/null || echo "change-me-$(date +%s)")
    sed -i "s/faiba-captive-portal-secret-change-in-production/$SECRET/" .env

    echo -e "${YELLOW}  Edit .env to configure your environment:${NC}"
    echo "    nano .env"
    echo ""
    echo "  Key settings to review:"
    echo "    PORTAL_HOST     - Your server IP (for BRAS redirect)"
    echo "    RADIUS_SERVER   - Your RADIUS/BRAS IP"
    echo "    RADIUS_SECRET   - RADIUS shared secret"
    echo "    MPESA_ENV       - 'sandbox' or 'production'"
    echo ""
else
    echo -e "${GREEN}[2/5]${NC} .env file exists"
fi

# --- Stop existing container ---
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo -e "${YELLOW}[3/5]${NC} Stopping existing container..."
    docker stop "$CONTAINER_NAME" 2>/dev/null || true
    docker rm "$CONTAINER_NAME" 2>/dev/null || true
else
    echo -e "${GREEN}[3/5]${NC} No existing container to remove"
fi

# --- Build ---
echo -e "${GREEN}[4/5]${NC} Building Docker image..."
docker build -t "$IMAGE_NAME" .

# --- Run ---
echo -e "${GREEN}[5/5]${NC} Starting container on port ${PORTAL_PORT}..."
docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    --env-file .env \
    -e PORTAL_PORT="$PORTAL_PORT" \
    -p "${PORTAL_PORT}:${PORTAL_PORT}" \
    -v faiba_portal_data:/app/data \
    "$IMAGE_NAME"

# --- Wait for health check ---
echo ""
echo -n "Waiting for portal to start"
for i in $(seq 1 15); do
    if curl -sf "http://localhost:${PORTAL_PORT}/health" > /dev/null 2>&1; then
        echo ""
        echo -e "${GREEN}Portal is running!${NC}"
        break
    fi
    echo -n "."
    sleep 1
done

echo ""
echo -e "${CYAN}  ============================================"
echo -e "    Portal URL:  http://localhost:${PORTAL_PORT}"
echo -e "    Health:      http://localhost:${PORTAL_PORT}/health"
echo -e "    RADIUS Auth: POST http://localhost:${PORTAL_PORT}/api/radius/auth"
echo -e "    RADIUS Acct: POST http://localhost:${PORTAL_PORT}/api/radius/acct"
echo -e "  ============================================${NC}"
echo ""
echo "Useful commands:"
echo "  docker logs -f ${CONTAINER_NAME}        # View logs"
echo "  docker restart ${CONTAINER_NAME}        # Restart"
echo "  docker stop ${CONTAINER_NAME}           # Stop"
echo "  docker exec -it ${CONTAINER_NAME} bash  # Shell access"
echo ""
echo "To change port, run:  PORTAL_PORT=9090 ./setup.sh"
echo "To use docker-compose: docker compose up -d"
