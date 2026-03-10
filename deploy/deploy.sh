#!/bin/bash
# Deploy open-brain on the server (run via SSH or directly on LXC 116)
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
REPO_DIR="/opt/open-brain"

cd "$REPO_DIR"

echo "Pulling latest changes..."
git pull --ff-only

echo "Disabling old systemd unit (if present)..."
systemctl disable --now open-brain 2>/dev/null || true

echo "Building and starting Docker container..."
# Load OP service account token if available (server stores it in /etc/op-service-account-token)
if [ -f /etc/op-service-account-token ]; then
  export OP_SERVICE_ACCOUNT_TOKEN=$(cat /etc/op-service-account-token)
fi
op run --env-file=.env.tpl -- docker compose -f docker-compose.service.yml up --build -d

echo "Waiting for startup..."
sleep 5
curl -sf http://localhost:8091/health && echo "" || echo "WARNING: health check failed"

echo ""
echo "Deploy complete."
echo "NOTE: MCP clients (e.g. Claude Code) must reconnect after deploy."
echo "  Run: /mcp reconnect open-brain"
