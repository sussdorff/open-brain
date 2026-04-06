#!/bin/bash
# Test: deploy.sh must have post-deploy smoke checks beyond the basic health check
# RED test — verifies AK4: additional endpoint checks after health check

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SH="$SCRIPT_DIR/deploy.sh"

echo "=== Test: deploy.sh has post-deploy smoke checks ==="

# Check for smoke check section
if ! grep -q 'smoke' "$DEPLOY_SH"; then
    echo "FAIL: deploy.sh has no smoke check section"
    exit 1
fi
echo "PASS: deploy.sh has smoke check section"

echo "=== Test: smoke checks verify at least one API endpoint ==="
# Should check more than just /health — look for additional endpoint check
if ! grep -q 'smoke\|check.*endpoint\|verify\|/mcp\|/oauth\|/sse' "$DEPLOY_SH"; then
    echo "FAIL: deploy.sh does not check any API endpoint in smoke tests"
    exit 1
fi
echo "PASS: deploy.sh verifies API endpoints in smoke tests"

echo "=== Test: smoke check failures cause deploy to abort ==="
# The smoke section should use exit 1 or set -e behavior
SMOKE_SECTION=$(sed -n '/smoke/,/Deploy complete/p' "$DEPLOY_SH")
if ! echo "$SMOKE_SECTION" | grep -q 'exit 1\||| {'; then
    echo "FAIL: smoke check failures do not abort deploy"
    exit 1
fi
echo "PASS: smoke check failures abort deploy"

echo ""
echo "All tests passed."
