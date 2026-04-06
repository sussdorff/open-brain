#!/bin/bash
# Test: deploy.sh must exit with non-zero code when health check fails
# RED test — verifies AK1: curl failure causes deploy to fail (exit 1)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SH="$SCRIPT_DIR/deploy.sh"

echo "=== Test: health check failure causes deploy exit ==="

# Extract the health check line from deploy.sh
HEALTH_LINE=$(grep 'curl.*health' "$DEPLOY_SH")

# Verify it uses curl -sf with exit 1 on failure (not || echo WARNING)
if echo "$HEALTH_LINE" | grep -qE 'curl -sf.*/health.*exit 1|\{ echo.*exit 1'; then
    echo "PASS: health check line exits on failure"
    exit 0
else
    echo "FAIL: health check does NOT exit on failure with curl -sf"
    echo "  Found: $HEALTH_LINE"
    exit 1
fi
