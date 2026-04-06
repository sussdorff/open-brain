#!/bin/bash
# Test: install-hooks.sh must create a pre-commit hook that runs pytest
# RED test — verifies AK2: scripts/install-hooks.sh exists and produces a working hook

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_HOOKS="$SCRIPT_DIR/install-hooks.sh"

echo "=== Test: install-hooks.sh exists ==="
if [ ! -f "$INSTALL_HOOKS" ]; then
    echo "FAIL: scripts/install-hooks.sh does not exist"
    exit 1
fi
echo "PASS: scripts/install-hooks.sh exists"

echo "=== Test: install-hooks.sh is executable ==="
if [ ! -x "$INSTALL_HOOKS" ]; then
    echo "FAIL: scripts/install-hooks.sh is not executable"
    exit 1
fi
echo "PASS: scripts/install-hooks.sh is executable"

echo "=== Test: install-hooks.sh references pytest ==="
if ! grep -q 'pytest' "$INSTALL_HOOKS"; then
    echo "FAIL: install-hooks.sh does not reference pytest"
    exit 1
fi
echo "PASS: install-hooks.sh references pytest"

echo "=== Test: install-hooks.sh references 'not integration' marker ==="
if ! grep -q 'not integration' "$INSTALL_HOOKS"; then
    echo "FAIL: install-hooks.sh does not skip integration tests"
    exit 1
fi
echo "PASS: install-hooks.sh skips integration tests"

echo ""
echo "All tests passed."
