#!/bin/bash
# Test: nightly test runner script must exist and be properly configured
# RED test — verifies AK3: nightly full test suite setup

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NIGHTLY_SCRIPT="$SCRIPT_DIR/run-nightly-tests.sh"

echo "=== Test: run-nightly-tests.sh exists ==="
if [ ! -f "$NIGHTLY_SCRIPT" ]; then
    echo "FAIL: scripts/run-nightly-tests.sh does not exist"
    exit 1
fi
echo "PASS: scripts/run-nightly-tests.sh exists"

echo "=== Test: run-nightly-tests.sh is executable ==="
if [ ! -x "$NIGHTLY_SCRIPT" ]; then
    echo "FAIL: scripts/run-nightly-tests.sh is not executable"
    exit 1
fi
echo "PASS: scripts/run-nightly-tests.sh is executable"

echo "=== Test: run-nightly-tests.sh runs full pytest (including integration) ==="
if ! grep -q 'uv run pytest' "$NIGHTLY_SCRIPT"; then
    echo "FAIL: run-nightly-tests.sh does not run pytest"
    exit 1
fi
echo "PASS: run-nightly-tests.sh runs pytest"

echo "=== Test: nightly script does NOT exclude integration tests ==="
if grep -q "not integration" "$NIGHTLY_SCRIPT"; then
    echo "FAIL: run-nightly-tests.sh excludes integration tests (should run full suite)"
    exit 1
fi
echo "PASS: run-nightly-tests.sh runs full test suite including integration"

echo ""
echo "All tests passed."
