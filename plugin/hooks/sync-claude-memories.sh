#!/usr/bin/env bash
# Claude Code Stop hook: sync Claude memory files to open-brain.
#
# Reads stdin JSON from Claude Code Stop hook, checks for loop prevention,
# verifies the SSH tunnel to open-brain Postgres is up, then runs
# migrate_claude_memories.py in the background (non-blocking).
#
# Loop prevention: exits 0 if stop_hook_active is true.
# Graceful degradation: exits 0 if SSH tunnel is not running.
# Logging: appends to ~/.claude/memory-sync.log with ISO timestamps.

LOG_FILE="${HOME}/.claude/memory-sync.log"
TUNNEL_PORT="${SYNC_TUNNEL_PORT:-15432}"
MIGRATE_SCRIPT="${CLAUDE_PLUGIN_ROOT}/scripts/migrate_claude_memories.py"

# ISO timestamp helper
ts() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log() {
  echo "$(ts) $*" >> "${LOG_FILE}"
}

# Read stdin JSON (Claude passes hook payload via stdin)
HOOK_INPUT="$(cat)"

# Loop prevention: if stop_hook_active is true, skip to avoid infinite loops
STOP_HOOK_ACTIVE="$(echo "${HOOK_INPUT}" | python3 -c "import sys,json; d=json.load(sys.stdin); print('true' if d.get('stop_hook_active') else 'false')" 2>/dev/null)"
STOP_HOOK_ACTIVE="${STOP_HOOK_ACTIVE:-false}"

if [ "${STOP_HOOK_ACTIVE}" = "true" ]; then
  log "stop_hook_active=true, skipping memory sync to prevent loop"
  exit 0
fi

# Check SSH tunnel is up
if ! nc -z localhost "${TUNNEL_PORT}" 2>/dev/null; then
  log "SSH tunnel not running on port ${TUNNEL_PORT}, skipping memory sync"
  exit 0
fi

# Tunnel is up — launch migration in background (non-blocking)
log "Starting memory sync in background (tunnel OK on port ${TUNNEL_PORT})"
nohup bash -c "uv run '${MIGRATE_SCRIPT}' >> '${LOG_FILE}' 2>&1 ; ec=\$?; echo \"\$(date -u +\"%Y-%m-%dT%H:%M:%SZ\") memory sync finished (exit=\${ec})\" >> '${LOG_FILE}'" </dev/null >/dev/null 2>&1 &

exit 0
