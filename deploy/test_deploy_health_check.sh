#!/bin/bash
# Test: deploy.sh must exit with non-zero code when health check fails
# RED test — verifies AK1: curl failure causes deploy to fail (exit 1)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SH="$SCRIPT_DIR/deploy.sh"

echo "=== Test: health check failure causes deploy exit ==="

# Extract the health check line from deploy.sh (first match only)
HEALTH_LINE=$(grep -m1 'localhost:8091/health' "$DEPLOY_SH")

# Verify the health check line exits on failure (not || echo WARNING)
if grep -q 'exit 1' <<< "$HEALTH_LINE"; then
    echo "PASS: health check line exits on failure"
    exit 0
else
    echo "FAIL: health check does NOT exit on failure with curl -sf"
    echo "  Found: $HEALTH_LINE"
    exit 1
fi
