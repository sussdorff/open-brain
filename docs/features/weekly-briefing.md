# Weekly Briefing

Generates structured summaries of memory activity over time windows, surfacing emerging themes, stale memories, and unresolved action items across all memory types and projects.

## Was

Weekly Briefing is an MCP tool that aggregates memories across a configurable time window (default: 1 week) and generates a structured digest with six sections:

1. **Memory Counts** — Total memories in current/previous period, broken down by type, with deltas
2. **Top Entities** — Most-frequently-mentioned people, organizations, technologies, locations, dates (aggregated from all memories in the window)
3. **Theme Trends** — Emerging (new or 2x+ growth) and declining (0x or 2x loss) entities across current vs. previous period
4. **Open Loops** — Unresolved action items from meetings, decisions, and other memory types (sorted by age)
5. **Cross-Project Connections** — Shared entities and memory counts across different projects (if project metadata present)
6. **Decay Warnings** — Memories not accessed in 30+ days with access_count ≤ 2 (candidates for archival or review)

The tool works with empty databases and gracefully handles mixed memory types.

## Für wen

Teams and individuals using open-brain who want:

- **Weekly accountability** — Quantified summary of what was discussed, decided, and actioned
- **Emerging topics detection** — Know when a technology, person, or decision pattern is growing (or fading)
- **Stale memory alerts** — Identify memories that haven't been revisited and may be at risk of being forgotten
- **Cross-project insights** — See which entities and topics span multiple projects
- **Action item tracking** — Surface all unresolved meeting action items in one place

**Use cases:**
- **End-of-week ritual** — Run `weekly_briefing(weeks_back=1)` on Friday to review the week
- **Multi-project teams** — Detect when a technology (e.g., "Rust") is emerging across multiple team projects
- **Knowledge hygiene** — Regular review of decay warnings helps prevent knowledge rot
- **Project retrospectives** — Use theme trends to understand how the project's focus shifted over time

## Wie es funktioniert

### The Six Sections

#### 1. Memory Counts

```json
{
  "current": 42,
  "previous": 35,
  "by_type": {
    "observation": 15,
    "meeting": 8,
    "decision": 10,
    "person": 5,
    "event": 4
  },
  "delta": {
    "observation": 2,
    "meeting": 1,
    "decision": 3,
    "person": 0,
    "event": 1
  }
}
```

- `current` — Total memories in the current period
- `previous` — Total memories in the previous period (same duration)
- `by_type` — Breakdown of current-period counts by memory type
- `delta` — Change in count from previous to current period (positive = more, negative = fewer)

#### 2. Top Entities

```json
{
  "people": [
    {"name": "Alice", "freq": 5},
    {"name": "Bob", "freq": 3},
    {"name": "Charlie", "freq": 1}
  ],
  "tech": [
    {"name": "Python", "freq": 7},
    {"name": "PostgreSQL", "freq": 4}
  ],
  "organizations": [
    {"name": "Acme Corp", "freq": 2}
  ]
}
```

Aggregated from `metadata.entities` in all memories in the current period. Each category (people, tech, organizations, etc.) shows the most-frequent entities, up to top 10 per category.

#### 3. Theme Trends

```json
{
  "emerging": [
    {"name": "Rust", "category": "tech", "current_freq": 2, "previous_freq": 0},
    {"name": "Alice", "category": "people", "current_freq": 5, "previous_freq": 2}
  ],
  "declining": [
    {"name": "Java", "category": "tech", "current_freq": 0, "previous_freq": 3},
    {"name": "Bob", "category": "people", "current_freq": 1, "previous_freq": 3}
  ]
}
```

- **Emerging**: Entities that are new (previous_freq = 0) OR have grown by 2x+ (current_freq > 2 × previous_freq)
- **Declining**: Entities that disappeared (current_freq = 0) OR have shrunk by 2x+ (previous_freq > 2 × current_freq)

Computed by comparing entity frequencies across the two periods.

#### 4. Open Loops

```json
[
  {
    "memory_id": 5,
    "title": "Sprint Planning",
    "action_items": ["Fix bug #123", "Deploy to prod"],
    "age_days": 10
  }
]
```

Sorted by age (oldest first = most overdue). Only memories with non-empty `metadata.action_items` appear. Top 10 are returned.

#### 5. Cross-Project Connections

```json
[
  {
    "project": "alpha",
    "memory_count": 12,
    "common_entities": ["Rust", "PostgreSQL", "async/await"]
  },
  {
    "project": "beta",
    "memory_count": 8,
    "common_entities": ["Python", "FastAPI"]
  }
]
```

Extracted from `metadata.project` field in memories. Shows top 5 most-common entities per project. Projects are sorted by memory count (descending).

#### 6. Decay Warnings

```json
[
  {
    "memory_id": 10,
    "title": "Old Decision",
    "days_unaccessed": 40,
    "access_count": 1
  }
]
```

Memories that:
- Haven't been accessed in 30+ days (checked via `last_accessed_at` or `created_at` if not accessed), AND
- Have access_count ≤ 2 (low engagement)

Sorted by days_unaccessed (longest first). These are candidates for review, archival, or consolidation.

**Note**: These memories have also been decayed by the `run_lifecycle_pipeline()` (Step 0 — Decay), which reduces their priority each week they remain unaccessed. Accessing a decayed memory restores its priority via the boost mechanism (see memory-decay feature for details).

### Time Window Logic

The tool operates on three overlapping time ranges:

```
Current Period    Previous Period       All-Time (Decay)
│←── weeks_back ──│←── weeks_back ──│   │←─ all memories ─│
      (1 week)       (1 week)              (decay checking)
      ↑              ↑                      ↑
      now            now-1w                 all
```

For example, with `weeks_back=1`:
- **Current**: Last 7 days
- **Previous**: 7-14 days ago (same duration)
- **Decay**: All memories in the database (30-day window and access_count check applied)

### Computation Pipeline

1. **Fetch current period** — Query memories with `date_start=now-weeks_back`, `date_end=now`
2. **Fetch previous period** — Query memories with `date_start=now-2*weeks_back`, `date_end=now-weeks_back`
3. **Fetch all memories** — Query full database (for decay warnings and open loops)
4. **Aggregate entities** — Extract and count all entities from metadata.entities across all memories
5. **Compute counts** — Group by type, compute deltas
6. **Compute trends** — Compare current vs. previous entity frequencies
7. **Find open loops** — Filter for action_items, sort by age
8. **Find decay warnings** — Filter for stale + low-access, sort by age
9. **Find cross-project** — Group by project metadata, extract top entities
10. **Return WeeklyBriefing** — All 6 sections in a single JSON structure

## Zusammenspiel

### With search_memory

The briefing tool uses the same `search()` interface as the `search_memory` MCP tool. This means:

- Same date filtering (ISO 8601 timestamps)
- Same project filtering
- Same limit/pagination (200 memories per query)
- Consistent entity extraction from metadata

### With save_memory and triage_memories

The briefing is a **read-only digest** of memories saved via `save_memory`. It does not modify, archive, or merge memories. However, the decay warnings section surfaces candidates for the `triage_memories` workflow:

```
Weekly Briefing (Friday)
  ├─ Decay Warnings: [10 memories not accessed 30+ days]
  │
  └─ User reviews warnings
       │
       ├─ "This is still important" → triage_memories(action: "promote")
       ├─ "This is outdated" → triage_memories(action: "archive")
       └─ "Merge this with X" → triage_memories(action: "merge_with", target_id=X)
```

### With capture_router

The briefing benefits from structured metadata created by the capture router:

- **Meetings** → action_items extracted → appear in open loops
- **Decisions** → decision metadata preserved → trend analysis includes decision entities
- **Persons** → people extracted → top entities and cross-person connections
- **Events** → dates + locations extracted → timeline analysis

But the briefing tool does NOT trigger capture router; it only reads pre-classified metadata.

## Besonderheiten

### Empty Database Handling

If the database has no memories, the briefing returns:

```json
{
  "period": { "weeks_back": 1, "from": "...", "to": "..." },
  "memory_counts": { "current": 0, "previous": 0, "by_type": {}, "delta": {} },
  "top_entities": {},
  "theme_trends": { "emerging": [], "declining": [] },
  "open_loops": [],
  "cross_project_connections": [],
  "decay_warnings": []
}
```

No errors are raised. This allows the tool to be used even during onboarding.

### Entity Category Flexibility

Entity categories are **not pre-defined**. Whatever categories appear in `metadata.entities` are automatically handled:

```json
{
  "metadata": {
    "entities": {
      "people": ["Alice"],
      "tech": ["Python"],
      "custom_domain": ["value1", "value2"]  // arbitrary category
    }
  }
}
```

The briefing will include `custom_domain` in top_entities, trends, and cross-project analysis automatically.

### Trend Thresholds

The 2x multiplier for emerging/declining is **fixed in code**:

```python
if c_freq > 0 and (p_freq == 0 or c_freq > 2 * p_freq):
    # emerging
elif p_freq > 0 and (c_freq == 0 or p_freq > 2 * c_freq):
    # declining
```

This means:
- A technology mentioned 1x → 3x is emerging (3 > 2×1)
- A technology mentioned 5x → 2x is NOT declining (2 ≤ 5/2)
- A technology mentioned 5x → 10x is NOT emerging (10 ≤ 2×5, it's growth but not explosive)

### Open Loops Ranking

Open loops are **sorted by age (oldest first)**, not by importance. A 100-day-old action item appears before a 10-day-old one, even if the newer one might be more critical.

This reflects the assumption that older action items are at higher risk of being forgotten.

### Decay Thresholds

Hard-coded in the implementation:

- **Stale threshold**: 30 days since last access or creation
- **Access count threshold**: ≤ 2 (memories accessed once or twice)

These values are **not configurable** via tool parameters. To change them, you must modify the `_find_decay_warnings()` function in `digest.py`.

### Project Metadata Requirement

Cross-project connections **only appear if memories have `metadata.project` set**. If no memories have project metadata, `cross_project_connections` returns an empty list.

```python
# This memory will be grouped under "project": "alpha"
{
  "metadata": {
    "project": "alpha",
    "entities": { "tech": ["Python"] }
  }
}

# This memory will be ignored for cross-project analysis
{
  "metadata": {
    "entities": { "tech": ["Python"] }
  }
}
```

### Response Format

The tool returns a **JSON string** (not a Pydantic model):

```python
# Server response:
return json.dumps(asdict(result), default=str)
```

The `default=str` ensures datetime objects and other non-JSON-serializable types are converted to strings. When parsed, this yields the full WeeklyBriefing structure.

### Performance Characteristics

For a typical database with 1,000 memories:
- Current period fetch: ~50-100 memories (1 query)
- Previous period fetch: ~50-100 memories (1 query)
- Decay warning fetch: all 1,000 memories (1 query, filtered client-side)

**Total**: 3 queries, each returning up to 200 results, O(n) entity aggregation.

No database-level aggregation (no SQL GROUP BY); all computation happens in Python after fetch. This is safe for databases up to ~10k memories; larger datasets would benefit from SQL aggregation.

## Technische Details

### Data Structures

Located in `python/src/open_brain/digest.py`:

```python
@dataclass
class WeeklyBriefing:
    period: dict[str, Any]
    memory_counts: dict[str, Any]
    top_entities: dict[str, list[dict[str, Any]]]
    theme_trends: dict[str, list[dict[str, Any]]]
    open_loops: list[dict[str, Any]]
    cross_project_connections: list[dict[str, Any]]
    decay_warnings: list[dict[str, Any]]
```

### Helper Functions

All aggregation logic is in `digest.py`:

| Function | Purpose |
|----------|---------|
| `_parse_dt(value)` | Parse ISO datetime string to UTC-aware datetime |
| `_aggregate_entities(memories)` | Extract and count entities from metadata.entities |
| `_top_entities(counters, top_n=10)` | Convert entity counters to sorted lists |
| `_compute_trends(current, previous)` | Find emerging/declining entities |
| `_find_open_loops(memories, now, top_n=10)` | Find memories with action_items |
| `_find_decay_warnings(memories, now, stale_days=30, max_access_count=2)` | Find stale low-access memories |
| `_count_by_type(memories)` | Group memories by type |
| `_find_cross_project_connections(memories)` | Group by project metadata |

### MCP Tool Definition

In `python/src/open_brain/server.py`:

```python
@mcp.tool(
    description="Generate weekly briefing: memory counts, top entities, theme trends (emerging/declining), "
    "open loops (unresolved action items), cross-project connections, and decay warnings (stale memories). "
    "Params: weeks_back (default 1), project (optional filter)"
)
async def weekly_briefing(
    weeks_back: int = 1,
    project: str | None = None,
) -> str:
    """Generate a structured weekly briefing with cross-type time-bridged insights."""
    from open_brain.digest import generate_weekly_briefing
    dl = get_dl()
    result = await generate_weekly_briefing(dl, weeks_back=weeks_back, project=project)
    return json.dumps(asdict(result), default=str)
```

### Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `weeks_back` | int | 1 | How many weeks to include in the current period (previous period is same duration, 1 period ago) |
| `project` | str | None | Optional project filter (filters all three queries: current, previous, decay) |

### Return Type

JSON string containing:

```typescript
{
  period: {
    weeks_back: number,
    from: string,        // ISO timestamp
    to: string           // ISO timestamp
  },
  memory_counts: {
    current: number,
    previous: number,
    by_type: Record<string, number>,
    delta: Record<string, number>
  },
  top_entities: Record<string, Array<{name: string, freq: number}>>,
  theme_trends: {
    emerging: Array<{name: string, category: string, current_freq: number, previous_freq: number}>,
    declining: Array<{name: string, category: string, current_freq: number, previous_freq: number}>
  },
  open_loops: Array<{
    memory_id: number,
    title: string,
    action_items: string[],
    age_days: number
  }>,
  cross_project_connections: Array<{
    project: string,
    memory_count: number,
    common_entities: string[]
  }>,
  decay_warnings: Array<{
    memory_id: number,
    title: string,
    days_unaccessed: number,
    access_count: number
  }>
}
```

### No New API Endpoints

The weekly briefing is an **MCP tool only**. It does not add any HTTP routes:

- No GET `/api/briefing`
- No POST `/api/briefing`

Users access it via the MCP tool interface only: `weekly_briefing(weeks_back=1, project="alpha")`.

### Timestamps

All timestamps in the briefing are **ISO 8601 with UTC timezone**:

```
2026-04-03T10:15:23.456789+00:00
```

Datetime parsing handles both timezone-aware and naive ISO strings (naive strings are assumed UTC).

### Testing

Tests in `python/tests/test_briefing.py` cover:

| Test Class | Acceptance Criterion |
|------------|---------------------|
| `TestBriefingSections` | AK1 — All 6 required sections present |
| `TestBriefingEntities` | AK2 — Entity frequency correctly aggregated |
| `TestBriefingTrends` | AK3 — Emerging/declining trends computed correctly |
| `TestBriefingOpenLoops` | AK4 — Open loops detected and sorted by age |
| `TestBriefingDecay` | AK5 — Decay warnings identify 30-day stale, low-access memories |
| `TestBriefingEmpty` | AK6 — Empty database returns valid zero-filled structure |
| `TestBriefingFullScenario` | Full integration — 20+ mixed-type memories produce non-zero counts |
| `TestBriefingSingleType` | Single-type database still produces valid structure |
| `TestBriefingCrossProject` | Cross-project connections aggregated correctly |

All tests use async/await with mock DataLayer.

## Example Usage

### End-of-Week Summary

```python
# Generate briefing for the past 7 days
result = await weekly_briefing(weeks_back=1)

# Parse result
import json
briefing = json.loads(result)

# Print summary
print(f"This week: {briefing['memory_counts']['current']} memories")
print(f"Last week: {briefing['memory_counts']['previous']} memories")
print(f"Change: {briefing['memory_counts']['delta']}")

# Check for stale memories
for warning in briefing['decay_warnings']:
    print(f"Stale: {warning['title']} (not accessed {warning['days_unaccessed']} days)")

# Check emerging technologies
for entity in briefing['theme_trends']['emerging']:
    if entity['category'] == 'tech':
        print(f"New tech: {entity['name']}")
```

### Multi-Project Cross-Check

```python
# See which entities span multiple projects
result = await weekly_briefing(weeks_back=2)
briefing = json.loads(result)

for connection in briefing['cross_project_connections']:
    print(f"Project {connection['project']}: {connection['memory_count']} memories")
    print(f"  Common tech: {', '.join(connection['common_entities'])}")
```

### Track Action Items

```python
# Find all unresolved action items
result = await weekly_briefing(weeks_back=4)
briefing = json.loads(result)

for loop in briefing['open_loops']:
    print(f"{loop['title']} ({loop['age_days']} days old)")
    for action in loop['action_items']:
        print(f"  - {action}")
```
