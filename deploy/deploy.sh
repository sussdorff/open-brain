#!/bin/bash
# Deploy open-brain on the server
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
REPO_DIR="${OPEN_BRAIN_DIR:-/opt/open-brain}"

cd "$REPO_DIR"

echo "Pulling latest changes..."
git pull --ff-only

echo "Disabling old systemd unit (if present)..."
systemctl disable --now open-brain 2>/dev/null || true

echo "Building and starting Docker container..."
docker compose -f docker-compose.service.yml up --build -d

echo "Waiting for startup..."
sleep 5
curl -sf http://localhost:8091/health && echo "" || { echo "ERROR: health check failed — deploy aborted"; exit 1; }

echo "Running post-deploy smoke checks..."
# Verify OAuth metadata endpoint is reachable (unauthenticated, should return 200)
curl -sf http://localhost:8091/.well-known/oauth-authorization-server > /dev/null || { echo "ERROR: smoke check failed — OAuth metadata endpoint unreachable"; exit 1; }
echo "  OK: OAuth metadata endpoint"
# Verify OAuth protected resource endpoint is reachable (unauthenticated, should return 200)
curl -sf http://localhost:8091/.well-known/oauth-protected-resource > /dev/null || { echo "ERROR: smoke check failed — OAuth protected resource endpoint unreachable"; exit 1; }
echo "  OK: OAuth protected resource endpoint"
echo "All smoke checks passed."

echo ""
echo "Deploy complete."
echo "NOTE: MCP clients (e.g. Claude Code) must reconnect after deploy."
echo "  Run: /mcp reconnect open-brain"
