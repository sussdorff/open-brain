#!/bin/bash
# Deploy open-brain on the server (run via SSH or directly on LXC 116)
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
REPO_DIR="/opt/open-brain"

cd "$REPO_DIR"

echo "Pulling latest changes..."
git pull --ff-only

echo "Syncing dependencies (Python 3.14)..."
cd python
uv sync

echo "Restarting service..."
systemctl restart open-brain

echo "Waiting for startup..."
sleep 2
systemctl status open-brain --no-pager -l

echo ""
echo "Deploy complete."
echo "NOTE: MCP clients (e.g. Claude Code) must reconnect after deploy."
echo "  Run: /mcp reconnect open-brain"
