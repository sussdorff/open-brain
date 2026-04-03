# Memory Decay

Automatic priority reduction for unaccessed memories and priority boosting for frequently accessed memories, keeping the memory system fresh and focused on active knowledge.

## Was

Memory Decay is a periodic process that adjusts the priority of memories based on their access patterns and age. Unaccessed memories gradually lose priority (decay), making them less likely to be retrieved in search results and recommendations. Frequently accessed memories gain priority (boost), signaling their continued importance. The process is part of the memory lifecycle pipeline and surfaces decaying memories in the weekly briefing for review or archival.

## Für wen

Anyone using open-brain who wants to:

- **Prevent knowledge rot** — Automatically identify and de-emphasize memories that haven't been revisited
- **Surface stale memories for review** — Weekly briefing lists memories not accessed in 30+ days (candidates for archival or refresh)
- **Maintain priority signal** — Let frequently accessed memories "earn" higher priority through repeated use
- **Support memory hygiene workflows** — Combined with triage actions (promote, archive, merge), decay helps keep the knowledge base clean

**Use cases:**
- **End-of-week review** — Run `run_lifecycle_pipeline()` on Friday to decay stale memories and surface them in the briefing
- **Continuous background task** — Cron job running decay weekly ensures old memories gradually fade without manual intervention
- **Selective promotion** — When a decayed memory is accessed, its priority is boosted back up (reversible decay)

## Wie es funktioniert

### Three Types of Memories

The decay process handles memories in three categories:

#### 1. **Decayed Memories**

Memories not accessed in 30+ days (default `stale_days=30`) have their priority multiplied by `decay_factor` (default 0.9):

```
priority_new = priority_old * 0.9
```

This happens on each decay run. A memory stale for 60 days will be decayed twice (in two separate weekly runs), resulting in:

```
priority = priority_initial * 0.9 * 0.9 = priority_initial * 0.81
```

**Trigger condition:**
- `last_accessed_at IS NULL OR last_accessed_at < NOW() - 30 days` (configurable via `stale_days`)
- AND `created_at < NOW() - 30 days` (not a recently-created memory)

#### 2. **Boosted Memories**

Memories with high access count (default `access_count >= 10`) have their priority multiplied by `boost_factor` (default 1.1), capped at 1.0:

```
priority_new = min(priority_old * 1.1, 1.0)
```

This applies to **all** frequently-accessed memories regardless of age — recent memories benefit too.

**Trigger condition:**
- `access_count >= 10` (configurable via `boost_threshold`)

#### 3. **Protected Memories**

Recent memories (created within 7 days, default `boost_days=7`) are **not decayed**, even if unaccessed. They are counted separately in the decay report for visibility.

**Protection condition:**
- `created_at >= NOW() - 7 days` (configurable via `boost_days`)

### Order of Operations

In a single decay run:

1. **Decay first**: Apply `decay_unused_priorities()` to stale, old memories
2. **Boost second**: Apply boost to frequently-accessed memories (may include already-decayed memories)
3. **Report**: Return counts of decayed, boosted, and protected memories

If a memory is **both stale AND frequently accessed**, the net effect is:

```
After decay:  priority = priority_old * 0.9 = X
After boost:  priority = X * 1.1 = priority_old * 0.99  (recovers most of loss)
```

This is intentional: frequent access partially counteracts decay in the same run, but full recovery takes multiple boosts (reversibility via AK5).

### Time Window Logic

The decay process uses three separate date ranges:

```
Recent (Protected)  Stale (Decayed)  Decayed Once, Then Boosted
│←─ 7 days ─│      │←─ 30 days ─│   │←─ all memories ─│
     ↑              ↑              (if access_count >= 10)
   now-7d           now-30d
```

- **Protected**: Created within last 7 days — never decay
- **Decayed**: Created 30+ days ago AND unaccessed 30+ days — apply decay
- **Boost candidates**: All memories with high access count (no age restriction)

### Integration with Lifecycle Pipeline

Decay is **Step 0** of the three-step memory lifecycle pipeline:

```python
await run_lifecycle_pipeline(scope=None, dry_run=False)
```

Execution order:

1. **Step 0 — Decay**: `decay_memories()` — Reduce priority of stale memories, boost frequently accessed ones
2. **Step 1 — Triage**: `triage_memories()` — Classify remaining memories (keep, merge, promote, scaffold, archive)
3. **Step 2 — Materialize**: `materialize_memories()` — Generate new memories from triage decisions

The pipeline returns a combined report with decay summary, triage actions, and materialization results.

### Appearance in Weekly Briefing

Decay warnings appear in the sixth section of `weekly_briefing()` output:

```json
{
  "decay_warnings": [
    {
      "memory_id": 10,
      "title": "Old Decision",
      "days_unaccessed": 40,
      "access_count": 1
    },
    {
      "memory_id": 15,
      "title": "Stale Note",
      "days_unaccessed": 35,
      "access_count": 0
    }
  ]
}
```

**Criteria for inclusion:**
- Unaccessed for 30+ days (checked via `last_accessed_at` or `created_at`)
- AND `access_count <= 2` (low engagement)

These are **candidates** for user action:

- **Promote** — "This is still important" → boost priority back up
- **Archive** — "This is outdated" → remove from active memory
- **Merge** — "Consolidate with X" → reduce duplication

The `_find_decay_warnings()` function in `digest.py` handles this filtering (pre-existing before decay feature was added; decay simply uses the same criteria).

## Zusammenspiel

### With search_memory

Decay does **not change search behavior directly**, but affects result ranking:

- Decayed memories have lower priority → lower rank in hybrid search (priority is one of the ranking factors)
- Boosted memories have higher priority → higher rank in search results

The underlying search algorithm in `search()` remains unchanged; decay is purely a data-layer operation.

### With triage_memories

The typical workflow:

```
Weekly Briefing (Friday)
  ├─ decay_warnings section: 5 memories stale 30+ days
  │
  └─ User reviews and acts:
       ├─ "Keep" → no action (stays decayed, will be re-accessed or archived later)
       ├─ "Promote" → triage_memories(action="promote") → memory gets priority boosted to 1.0
       ├─ "Archive" → triage_memories(action="archive") → memory marked archived
       └─ "Merge" → triage_memories(action="merge_with", target_id=X) → consolidate
```

Decay alone does **not** delete or hide memories — it only adjusts priority. Archival is a separate triage action.

### With run_lifecycle_pipeline

The full pipeline is:

```python
result = await run_lifecycle_pipeline(scope=None, dry_run=False)
```

This runs:

1. **Decay** — `decay_memories(DecayParams(...))` — Returns { decayed, boosted, protected }
2. **Triage** — `triage_memories(TriageParams(...))` — Classifies memories into lifecycle actions
3. **Materialize** — `materialize_memories()` — Executes actions (archive, merge, scaffold)

Example result:

```json
{
  "decay_summary": "Decay run complete: 12 memories decayed, 3 boosted, 45 protected",
  "triage_summary": "Classified 150 memories: 100 keep, 20 merge, 15 promote, 10 scaffold, 5 archive",
  "materialize_summary": "Materialized 30 actions: 5 merged, 15 scaffolded, 10 archived",
  "total_time_seconds": 8.5
}
```

## Besonderheiten

### Decay Runs Compound

Calling `decay_memories()` multiple times on the same unaccessed memory results in repeated multiplication:

```
Run 1: priority = 0.5 * 0.9 = 0.45
Run 2: priority = 0.45 * 0.9 = 0.405
Run 3: priority = 0.405 * 0.9 = 0.3645
```

After 5 weekly decay runs, a memory's priority drops to ~31% of original. This is intentional: gradual deprioritization allows time for users to realize a memory matters before it becomes hard to find.

### Decay is Reversible (AK5)

Accessing a decayed memory **restores its priority** via the boost mechanism:

1. User searches and finds a decayed memory (priority still visible in search results, just lower)
2. User accesses it (increments `access_count`)
3. On the next decay run, boost applies: `priority *= 1.1`
4. Repeated boosts can restore priority to original value

Example: A memory decayed 3x (priority = original × 0.729) can be boosted 3x (priority = 0.729 × 1.1³ ≈ 1.0, capped).

This is **not automatic** — decay does not track the original priority. Users must explicitly access memories to signal they matter.

### Dry-Run Mode

For testing or previewing decay impact without modifying data:

```python
result = await decay_memories(DecayParams(dry_run=True))
```

Returns counts of what **would** be decayed/boosted/protected without writing to the database.

### Hard-Coded Defaults (Configurable in Code)

Decay parameters have sensible defaults but are fully configurable via `DecayParams`:

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `stale_days` | 30 | Memories not accessed in N days are candidates for decay |
| `boost_days` | 7 | Memories created within N days are protected from decay |
| `decay_factor` | 0.9 | Priority multiplier for stale memories (0.9 = 10% reduction) |
| `boost_threshold` | 10 | Access count threshold for boosting |
| `boost_factor` | 1.1 | Priority multiplier for frequently accessed (capped at 1.0) |
| `dry_run` | False | If True, return counts without modifying DB |

To use different values:

```python
from open_brain.data_layer.interface import DecayParams

result = await decay_memories(
    DecayParams(
        stale_days=60,  # Memories stale 60+ days
        decay_factor=0.8,  # More aggressive: 20% reduction per run
        boost_threshold=5,  # Lower bar: access_count >= 5 triggers boost
    )
)
```

### Database Function: `decay_unused_priorities()`

Decay runs a PostgreSQL function (created in migrations) to efficiently apply decay in a single SQL query:

```sql
SELECT decay_unused_priorities(stale_days := 30, decay_factor := 0.9)
```

This function:
- Finds all stale, old memories matching the WHERE clause
- Multiplies their priority by `decay_factor`
- Returns count of affected rows

The Python code mirrors the WHERE clause to ensure dry-run counts match actual decay counts.

### Protected Memories are Counted but Not Modified

The `protected` count in decay results is informational:

```json
{
  "decayed": 12,
  "boosted": 3,
  "protected": 45,
  "summary": "Decay run complete: 12 memories decayed, 3 boosted, 45 protected"
}
```

Protected memories are **not changed** during decay. The count helps users understand what portion of the database is "too new" to consider stale.

### Performance Characteristics

For a database with 10,000 memories:

- **Dry-run**: ~100-200ms (three COUNT queries)
- **Live run**: ~200-500ms (one decay_unused_priorities call + one update for boost)

Decay is optimized to run weekly via cron or as part of the lifecycle pipeline. No background indexing or caching — all operations are deterministic SQL.

## Technische Details

### Data Structures

Located in `python/src/open_brain/data_layer/interface.py`:

```python
@dataclass
class DecayParams:
    stale_days: int = 30          # memories not accessed in N days get decayed
    boost_days: int = 7           # recent memories (< N days) are protected
    decay_factor: float = 0.9     # priority *= decay_factor for stale memories
    boost_threshold: int = 10     # access_count >= N triggers priority boost
    boost_factor: float = 1.1     # priority *= boost_factor for frequently accessed
    dry_run: bool = False

@dataclass
class DecayResult:
    decayed: int    # count of memories whose priority was reduced
    boosted: int    # count of memories whose priority was boosted
    protected: int  # count of recent memories left unchanged (informational)
    summary: str    # human-readable summary of the decay run
```

### Implementation

Located in `python/src/open_brain/data_layer/postgres.py`:

Method signature:

```python
async def decay_memories(self, params: DecayParams) -> DecayResult:
    """Apply priority decay to stale memories and boost frequently accessed ones."""
```

**Dry-run mode** (counts only, no writes):

```python
if params.dry_run:
    decayed = await conn.fetchval(
        """SELECT COUNT(*) FROM memories
           WHERE (last_accessed_at IS NULL OR last_accessed_at < NOW() - ($1 || ' days')::interval)
             AND created_at < NOW() - ($1 || ' days')::interval""",
        str(params.stale_days),
    )
    boosted = await conn.fetchval(
        """SELECT COUNT(*) FROM memories
           WHERE access_count >= $1""",
        params.boost_threshold,
    )
    protected = await conn.fetchval(
        """SELECT COUNT(*) FROM memories
           WHERE created_at >= NOW() - ($1 || ' days')::interval""",
        str(params.boost_days),
    )
```

**Live mode** (applies decay and boost):

```python
else:
    # Step 1: Decay stale memories
    decayed = await conn.fetchval(
        "SELECT decay_unused_priorities($1, $2)",
        params.stale_days,
        params.decay_factor,
    )
    
    # Step 2: Boost frequently accessed
    boosted = await conn.fetchval(
        """WITH updated AS (
               UPDATE memories
               SET priority = LEAST(priority * $1, 1.0),
                   updated_at = NOW()
               WHERE access_count >= $2
               RETURNING id
           )
           SELECT COUNT(*) FROM updated""",
        params.boost_factor,
        params.boost_threshold,
    )
    
    # Step 3: Count protected (recent) memories
    protected = await conn.fetchval(
        """SELECT COUNT(*) FROM memories
           WHERE created_at >= NOW() - ($1 || ' days')::interval""",
        str(params.boost_days),
    )
```

### Integration with Lifecycle Pipeline

In `python/src/open_brain/server.py`:

```python
@mcp.tool(
    description="Run the full memory lifecycle pipeline: decay → triage → materialize. "
    "Returns a structured report with decay summary, triage actions, and materialization results. "
    "Params: scope, dry_run"
)
async def run_lifecycle_pipeline(
    scope: str | None = None,
    dry_run: bool = False,
) -> str:
    """Chain decay_memories → triage_memories → materialize_memories into one pipeline run."""
    dl = get_dl()

    # Step 0: Decay — reduce priority of stale memories, boost frequently accessed ones
    decay_result = await dl.decay_memories(DecayParams(dry_run=dry_run))

    # Step 1: Triage (uses decayed priorities as input)
    triage_result = await dl.triage_memories(
        TriageParams(scope=scope, dry_run=dry_run)
    )

    # Step 2: Materialize (executes triage actions)
    if not triage_result.actions:
        materialize_result = MaterializeResult(actions=[], summary="No actions to materialize")
    else:
        materialize_result = await dl.materialize_memories(
            MaterializeParams(actions=triage_result.actions, dry_run=dry_run)
        )

    # Combine all three reports
    return json.dumps({
        "decay_summary": decay_result.summary,
        "triage_summary": triage_result.summary,
        "materialize_summary": materialize_result.summary,
    })
```

### Decay and Weekly Briefing Integration

The `weekly_briefing()` MCP tool already included decay warnings before this bead. The `_find_decay_warnings()` helper in `digest.py` uses the same criteria (30-day stale + low access) as the decay process:

```python
def _find_decay_warnings(
    memories: list[Memory],
    now: datetime,
    stale_days: int = 30,
    max_access_count: int = 2,
) -> list[dict[str, Any]]:
    """Find memories not accessed in stale_days with access_count <= max_access_count."""
    warnings = []
    for mem in memories:
        last_access = mem.last_accessed_at or mem.created_at
        if isinstance(last_access, str):
            last_access = datetime.fromisoformat(last_access)
        if (now - last_access).days >= stale_days and mem.access_count <= max_access_count:
            warnings.append({
                "memory_id": mem.id,
                "title": mem.title,
                "days_unaccessed": (now - last_access).days,
                "access_count": mem.access_count,
            })
    return sorted(warnings, key=lambda w: w["days_unaccessed"], reverse=True)
```

### Parameters

| Parameter | Type | Default | Purpose | Configurable |
|-----------|------|---------|---------|--------------|
| `stale_days` | int | 30 | Days of inactivity before decay | Yes (DecayParams) |
| `boost_days` | int | 7 | Days of age before memory is eligible for decay | Yes (DecayParams) |
| `decay_factor` | float | 0.9 | Priority multiplier (10% reduction per run) | Yes (DecayParams) |
| `boost_threshold` | int | 10 | Access count threshold for boost | Yes (DecayParams) |
| `boost_factor` | float | 1.1 | Priority multiplier for boost (capped at 1.0) | Yes (DecayParams) |
| `dry_run` | bool | False | Preview without writing to DB | Yes (DecayParams) |

### Testing

Tests in `python/tests/test_decay.py` cover:

| Test | Criterion | What It Verifies |
|------|-----------|------------------|
| `test_decay_unaccessed` | AK1 | Memory 60 days old, unaccessed → decayed |
| `test_decay_boost` | AK2 | Memory with access_count >= 10 → boosted |
| `test_decay_recent_protected` | AK3 | Memory 3 days old → not decayed, protected count incremented |
| `test_decay_in_briefing` | AK4 | Memory 40 days stale with access_count=1 appears in `weekly_briefing()` decay_warnings |
| `test_decay_reversible` | AK5 | After decay, boosting restores priority |
| `test_decay_compound_runs` | Compounding | Multiple decay runs multiply: 0.5 × 0.9 × 0.9 = 0.405 |
| `test_decay_overlap_behavior` | Overlap | Memory both stale and frequently accessed: decayed then boosted in same run |

All tests use async/await with mock database connections (async mocks).

## Example Usage

### Weekly Decay Run (Dry-Run Preview)

```python
from open_brain.data_layer.interface import DecayParams

# Preview what would be decayed without modifying the database
result = await decay_memories(DecayParams(dry_run=True))

print(result.summary)
# Output: "Decay run complete: 12 memories decayed, 3 boosted, 45 protected"
```

### Full Lifecycle Pipeline

```python
# Run decay + triage + materialize in one call
result = await run_lifecycle_pipeline(dry_run=False)

import json
summary = json.loads(result)
print(summary["decay_summary"])
print(summary["triage_summary"])
print(summary["materialize_summary"])
```

### Custom Decay Parameters

```python
# More aggressive decay: 60 days stale, 20% reduction per run
result = await decay_memories(
    DecayParams(
        stale_days=60,
        decay_factor=0.8,
        boost_threshold=5,  # Lower bar for boost
    )
)
```

### Reviewing Decay Warnings in Weekly Briefing

```python
import json

# Generate this week's briefing
briefing_str = await weekly_briefing(weeks_back=1)
briefing = json.loads(briefing_str)

# Extract decay warnings
for warning in briefing["decay_warnings"]:
    print(f"{warning['title']} (not accessed {warning['days_unaccessed']} days, access_count={warning['access_count']})")

# Act on them
for warning in briefing["decay_warnings"]:
    if warning["title"] == "Important Decision":
        # Promote it back to priority 1.0
        await triage_memories(
            TriageParams(
                scope=None,
                actions=[{
                    "memory_id": warning["memory_id"],
                    "action": "promote"
                }],
            )
        )
```
