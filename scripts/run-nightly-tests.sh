#!/bin/bash
# Nightly full test suite — runs all tests including integration
# Requires: VOYAGE_API_KEY and DATABASE_URL environment variables set
#
# Cron setup (run once):
#   echo "0 2 * * * cd /opt/open-brain && bash scripts/run-nightly-tests.sh >> /var/log/open-brain-nightly.log 2>&1" | crontab -
#
# Or for Claude Code /schedule, trigger with:
#   /schedule "0 2 * * *" "cd /opt/open-brain && bash scripts/run-nightly-tests.sh"

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"

echo "[$TIMESTAMP] Starting nightly test run..."

cd "$REPO_ROOT/python"

# Caller must set required env vars before running this script:
#   DATABASE_URL  — Postgres connection string
#   VOYAGE_API_KEY — Voyage AI API key for embedding integration tests

# Run full test suite (including integration tests)
EXIT_CODE=0
uv run pytest --tb=short -q 2>&1 || EXIT_CODE=$?
TIMESTAMP_END="$(date '+%Y-%m-%d %H:%M:%S')"

if [ $EXIT_CODE -eq 0 ]; then
  echo "[$TIMESTAMP_END] Nightly tests PASSED."
else
  echo "[$TIMESTAMP_END] Nightly tests FAILED (exit $EXIT_CODE)."
fi

exit $EXIT_CODE
