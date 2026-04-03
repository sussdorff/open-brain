# Periodic Learnings Extraction

Automatic extraction of session learnings every 4 hours during work hours, enabling asynchronous knowledge capture without blocking the session-close workflow.

## Was

Periodic Learnings Extraction is a background service that automatically captures learnings from conversation histories on a rate-limited schedule (every 4 hours during work hours) without requiring explicit user intervention. The feature runs via the `memory-heartbeat` skill, which checks if learnings extraction is due and spawns the `learning-extractor` agent when the 4-hour interval has elapsed.

The system prevents duplicate learnings through content-hash deduplication at the database level (`SHA-256` of normalized content), ensuring that identical learnings captured from multiple sessions are stored only once.

## Für wen

Teams and individuals using Claude Code that want automatic knowledge capture across their entire project portfolio without:

- Waiting for session-close extraction (blocks the session)
- Manual calls to the learning-extractor agent
- Duplicate learnings from overlapping observations in multiple sessions

**Use cases:**
- **Long-running projects** — Continuous feedback capture during extended development (e.g., multi-day coding marathons)
- **Multi-session workflows** — Teams working across multiple concurrent projects benefit from dedicated periodic extraction
- **Knowledge gaps detection** — Asynchronous extraction identifies patterns across all projects, not just current session
- **Lean interruptions** — Run learnings extraction in the background without blocking the user's active session

## Wie es funktioniert

### The 4-Hour Window

The `memory-heartbeat` skill runs automatically during work hours (Mon–Fri, 6 AM–5 PM). Within each work-hour cycle, it checks whether periodic learnings extraction is due:

1. **Load processing state**: Check `~/.claude/learnings/processing-state.json` for the `last_learnings_run` timestamp
2. **Calculate elapsed time**: If `last_learnings_run` exists, compute seconds since that timestamp
3. **Check 4-hour interval**: If elapsed >= 14,400 seconds (4 hours), extraction is due; otherwise skip
4. **If due**: Spawn `Agent(subagent_type="learning-extractor")` with `scope=all-projects`
5. **Update state**: After agent completes, update `last_learnings_run` to the current UTC timestamp

### Processing State Management

The learnings state is stored in `~/.claude/learnings/processing-state.json`:

```json
{
  "version": "1.0",
  "processed_conversations": {
    "zahnrad/a1b2c3d4-...jsonl": "abc123def456..."
  },
  "last_learnings_run": "2026-04-03T10:15:23.456789+00:00"
}
```

| Field | Purpose | Type |
|-------|---------|------|
| `version` | Schema version for migration | string |
| `processed_conversations` | Map of conversation file → checksum for incremental processing | object |
| `last_learnings_run` | ISO timestamp of last periodic extraction run | string (ISO 8601 with timezone) |

The state file is managed by the `learnings_state.py` helper module:

- **`load_state(path)`** — Reads state from disk, returns `{}` if file missing or corrupt
- **`save_state(path, state)`** — Writes state atomically (write-to-temp, then rename)
- **`is_extraction_due(state, interval_hours=4.0)`** — Checks if last run was older than interval
- **`mark_extraction_ran(state)`** — Returns new state dict with `last_learnings_run` set to now

### Deduplication

When saving learnings, the system prevents duplicates using **content-hash deduplication** at the database level:

1. **Compute hash**: SHA-256 of `content.strip().lower()` (normalized content)
2. **Derive session ref**: `lrn-<content_hash[:8]>` — stable dedup key
3. **Search for existing**: Call `search(query=<content>, type='learning')` to find prior learnings
4. **Check metadata.content_hash**: Scan results for matching hash in `metadata.content_hash`
5. **If found**: Skip silently (count as "skipped duplicate" in summary)
6. **If not found**: Call `save_memory` to insert

The `server.py` response now surfaces the `duplicate_of` field when a dedup match is found:

```json
{
  "id": 1,
  "message": "Duplicate content detected",
  "duplicate_of": 42
}
```

This allows downstream tools to track which prior learning is the canonical version and suppress redundant alerts.

### Scope: All Projects

The periodic extractor runs with `scope=all-projects`, which means:

- Process all JSONL conversation files across all project directories (`~/.claude/projects/*/`)
- Skip files already in `processed_conversations` (by checksum comparison)
- Process only new or modified files since last run
- Update `processed_conversations` with new checksums

This prevents duplication even when running across multiple concurrent projects.

## Zusammenspiel

### With memory-heartbeat Skill

The `memory-heartbeat` skill (in `/code/claude/malte/skills/memory-heartbeat/`) is the orchestrator:

```
memory-heartbeat runs (every ~8h)
  │
  ├─ WORK-HOURS window? → run_lifecycle_pipeline(scope=recent)
  │   │
  │   └─ After pipeline, check learnings extraction...
  │       │
  │       ├─ Is 4h interval due?
  │       │  ├─ YES → spawn learning-extractor(scope=all-projects)
  │       │  └─ NO → skip (log "last run < 4h ago")
  │       │
  │       └─ END-OF-DAY or WEEKLY? → run those pipelines
  │
  └─ Report results
```

### With learning-extractor Agent

The `learning-extractor` agent (in `/code/claude/malte/agents/learning-extractor/`) performs the actual extraction:

```
learning-extractor (scope=all-projects)
  │
  ├─ Load processing state from ~/.claude/learnings/processing-state.json
  │
  ├─ For each project directory:
  │   │
  │   └─ For each JSONL conversation file:
  │       │
  │       ├─ Skip if checksum in processed_conversations
  │       ├─ Extract user messages (filter system, meta, tool results)
  │       ├─ Classify feedback type + compute confidence
  │       ├─ Search for duplicates via save_memory
  │       │  └─ If dedup detected: skip (increment "skipped duplicates" count)
  │       ├─ Save via save_memory with metadata.content_hash
  │       └─ Update processed_conversations checksum
  │
  ├─ Update processing state:
  │   └─ Set last_learnings_run = now (ISO timestamp)
  │
  └─ Return summary (count by type, scope, confidence bands)
```

### With session-close Extraction

Periodic extraction is **independent** from `session-close` extraction:

- **Session-close**: Runs at session end, captures learnings from current session only, blocks session close
- **Periodic**: Runs in background on 4h interval, processes all projects, non-blocking

**Both use the same dedup mechanism** (`content_hash`), so:
- Session-close learnings saved at 10:00 AM
- Periodic extraction at 2:00 PM finds the same learning in another project → skipped (duplicate_of points to AM save)
- No redundant learnings in the system

## Besonderheiten

### Atomic State Updates

State file writes use a **write-to-temp-then-rename** pattern:

```python
tmp = Path(str(p) + ".tmp")
tmp.write_text(json.dumps(state, indent=2))
tmp.rename(p)  # atomic on POSIX filesystems
```

This prevents corruption if the process is interrupted mid-write (e.g., kill signal, power loss).

### Timezone-Aware Timestamps

The `last_learnings_run` field uses **UTC ISO 8601 with timezone info**:

```
2026-04-03T10:15:23.456789+00:00
```

This ensures:
- Consistent UTC reference across machines
- Timezone-aware comparison (no ambiguity on DST transitions)
- Machine-readable format for JSON

### Graceful Degradation

If processing-state.json is missing or corrupt:

- `load_state()` returns `{}` (empty dict)
- `is_extraction_due({})` returns `True` (treat as "never run")
- Next work-hour cycle will extract
- No error raised; system continues

### Rate Limiting

The 4-hour interval is **enforced in the heartbeat skill**, not in the agent. This means:

- User can manually invoke `learning-extractor` at any time (bypasses rate limit)
- Periodic heartbeat respects the 4h rule
- No database-level throttling needed

### Duplicate Detection Robustness

Content hash is computed from **normalized content** (`.strip().lower()`):

```python
content_hash = hashlib.sha256(content.strip().lower().encode()).hexdigest()[:16]
```

This means:
- Whitespace differences → same hash
- Case differences → same hash
- Truly identical learnings across sessions → detected as duplicates

But:
- Slight rewording → different hash (by design — allows capturing new framing)

### No Session Blocking

Unlike session-close extraction, periodic extraction:
- Runs in a spawned `Agent` (separate execution context)
- Does not block the current session
- Failure in periodic extraction does not affect user workflow

## Technische Details

### Implementation

The feature spans three components:

#### 1. learnings_state.py Module

Located in `python/src/open_brain/learnings_state.py`:

```python
load_state(path: Path | str) -> dict
save_state(path: Path | str, state: dict) -> None
is_extraction_due(state: dict, interval_hours: float = 4.0) -> bool
mark_extraction_ran(state: dict) -> dict
```

- **No external dependencies** beyond stdlib (json, datetime, pathlib)
- **Handles missing/corrupt files gracefully**
- **Atomic writes** via temp+rename pattern

#### 2. server.py Dedup Response

In `python/src/open_brain/server.py`, the `save_memory` tool response now includes:

```python
class SaveMemoryResult(BaseModel):
    id: int
    message: str
    duplicate_of: int | None = None  # Set when dedup detected
```

When the data layer detects a duplicate (via `content_hash`), the server surfaces:

```json
{
  "id": 42,
  "message": "Duplicate content detected",
  "duplicate_of": 42
}
```

#### 3. memory-heartbeat Skill Integration

In `/code/claude/malte/skills/memory-heartbeat/SKILL.md`, the WORK-HOURS section now includes:

```bash
# After running lifecycle pipeline...

# Check if learnings extraction is due
python3 -c "
import json, datetime, pathlib
state_file = pathlib.Path.home() / '.claude/learnings/processing-state.json'
state = json.loads(state_file.read_text()) if state_file.exists() else {}
last_run = state.get('last_learnings_run', '')
if not last_run:
    print('due')
else:
    delta = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(last_run)
    print('due' if delta.total_seconds() >= 14400 else 'skip')
"

# If output is 'due', spawn learning-extractor with scope=all-projects
Agent(subagent_type="learning-extractor")
```

#### 4. learning-extractor Agent Update

In `/code/claude/malte/agents/learning-extractor/prompt.md`, Step 6 now includes:

```bash
python3 -c "
import json, datetime, pathlib
state_file = pathlib.Path.home() / '.claude/learnings/processing-state.json'
state = json.loads(state_file.read_text()) if state_file.exists() else {}
state['last_learnings_run'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
state_file.write_text(json.dumps(state, indent=2))
"
```

This ensures that after periodic extraction completes, the next heartbeat cycle (4h later) will not re-run.

### No New API Endpoints

Periodic learnings extraction uses **existing MCP tools only**:

- `save_memory` (existing tool; now returns `duplicate_of` field)
- `search` (existing tool; used for dedup detection)
- `update_memory` (existing tool; used for enrichment)

No new HTTP routes or MCP endpoints were added.

### Database-Level Dedup

The dedup mechanism is enforced at the **data layer**, not in the server:

```python
# In postgres.py:
# When save_memory is called, check if content_hash exists
# If found: return SaveMemoryResult(id=prior_id, message="...", duplicate_of=prior_id)
# If not found: insert new memory
```

This ensures:
- Session-close extraction (direct `save_memory` call) gets dedup signal
- Periodic extraction (agent calling `save_memory`) gets dedup signal
- All paths benefit from dedup, regardless of caller

### Timestamps in UTC

All timestamps are stored in **UTC ISO 8601** format:

```python
datetime.now(timezone.utc).isoformat()
# Returns: 2026-04-03T10:15:23.456789+00:00
```

This avoids timezone ambiguity when:
- Extracting across machines in different timezones
- Comparing timestamps for interval checks
- Debugging state transitions

## Testing

Tests in `python/tests/test_periodic_learnings.py` cover:

### State Management (AK3)
- `test_load_state_returns_empty_dict_when_file_missing` — File doesn't exist
- `test_load_state_returns_empty_dict_on_corrupt_file` — Invalid JSON
- `test_load_state_returns_existing_content` — Normal read
- `test_save_state_writes_json` — Normal write
- `test_save_state_is_atomic` — No .tmp file left behind
- `test_is_extraction_due_when_never_run` — Empty state returns True
- `test_is_extraction_due_when_last_run_4h_ago` — Just past threshold returns True
- `test_is_extraction_due_when_last_run_recently` — Recent run returns False
- `test_is_extraction_due_custom_interval` — Custom interval_hours parameter
- `test_mark_extraction_ran_sets_timestamp` — Timestamp is set + timezone-aware
- `test_mark_extraction_ran_preserves_existing_keys` — Existing state preserved

### Deduplication (AK2)
- `test_periodic_learnings_dedup_prevents_duplicates` — Identical content twice → second call signals `duplicate_of`
- `test_periodic_learnings_different_content_inserts_new` — Different content → both inserted

Tests mock the data layer and verify:
- Correct state transitions
- Proper timestamp formatting
- Content-hash dedup signal passed through server response
- Both learnings reach the data layer (no short-circuit filtering)
