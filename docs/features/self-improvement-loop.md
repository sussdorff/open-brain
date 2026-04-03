# Self-Improvement Loop

Analyzes briefing engagement patterns and automatically proposes one behavior change per week to optimize which briefing types the user finds most valuable.

## Was

The Self-Improvement Loop is a four-part workflow that tracks how often users respond to different types of briefings (e.g., "weekly_summary", "action_items", "decay_warnings") and generates weekly suggestions to either remove low-engagement briefing types or expand high-engagement ones.

The loop implements a classical feedback cycle: **capture response data → analyze engagement → propose change → log approval → evolve behavior**.

## Für wen

AI assistants and teams using open-brain who want to:

- **Adapt briefing preferences automatically** — Stop showing briefing types the user ignores
- **Amplify high-value signals** — Expand briefing types the user consistently engages with
- **Measure briefing utility** — See which briefing types produce the highest response rates
- **Track improvement over time** — View the history of behavior changes and their outcomes

**Use cases:**
- **Personal AI assistants** — Run weekly analysis to keep briefing formats fresh and relevant
- **Team memory systems** — Identify which briefing types different team members value most
- **Multi-project environments** — Suppress low-engagement briefing types per-project
- **Knowledge hygiene** — Continuously prune noise from the weekly briefing

## Wie es funktioniert

### The Four-Step Workflow

#### Step 1: Capture Engagement

When saving a briefing memory, include the required metadata fields:

```python
save_memory(
    text="Weekly briefing: top 3 entities...",
    type="briefing",
    metadata={
        "briefing_type": "decay_warnings",      # Required
        "user_responded": True                   # Required (True/False)
    }
)
```

- `briefing_type` — A string label for the briefing category (e.g., "decay_warnings", "action_items", "theme_trends")
- `user_responded` — A boolean: `True` if the user read/acted on the briefing, `False` if ignored

#### Step 2: Analyze Engagement

Call `analyze_briefing_engagement(days_back=7)` to compute response rates per briefing type:

```json
{
  "period_days": 7,
  "total_briefings": 28,
  "has_sufficient_data": true,
  "by_type": [
    {
      "briefing_type": "action_items",
      "total_count": 10,
      "responded_count": 9,
      "response_rate": 0.9
    },
    {
      "briefing_type": "decay_warnings",
      "total_count": 10,
      "responded_count": 2,
      "response_rate": 0.2
    },
    {
      "briefing_type": "theme_trends",
      "total_count": 8,
      "responded_count": 6,
      "response_rate": 0.75
    }
  ]
}
```

The analyzer looks back 7 days by default and requires:
- At least 3 distinct briefings across at least 2 different days (has_sufficient_data=true)
- At least 7 days of data collection

#### Step 3: Generate One Suggestion

Call `generate_evolution_suggestion(days_back=7)` to propose exactly one behavior change based on engagement:

```json
{
  "suggestion": {
    "action": "remove",
    "briefing_type": "decay_warnings",
    "reason": "Low engagement: only 20% response rate (2/10 briefings responded). Consider removing this briefing type to reduce noise.",
    "response_rate": 0.2
  }
}
```

**Decision logic:**

1. **Rate limit** — Only generate one suggestion per 7 days. Returns `null` if a suggestion was already made in the last 7 days.
2. **30-day rejection suppression** — If a briefing type was rejected (not approved) within the last 30 days, do NOT re-propose it. This prevents nagging the user about the same briefing type.
3. **Strategy**:
   - If ANY type has **< 30% response rate** → Propose removing the **lowest-engagement type**
   - If ALL types have **≥ 30% response rate** → Propose **expanding the highest-engagement type** (add more details, increase frequency, etc.)

Returns `null` if:
- Insufficient data (`has_sufficient_data=false`)
- A suggestion was already made in the last 7 days
- All briefing types have been suppressed by the 30-day rejection window

#### Step 4: Log Approval/Rejection

Call `log_evolution_approval(suggestion_id, approved=True/False, briefing_type="...")` to record the user's decision:

```python
# User approved removal of decay_warnings
await log_evolution_approval(
    suggestion_id=42,
    approved=True,
    briefing_type="decay_warnings"  # Enables 30-day suppression for future suggestions
)

# User rejected expansion of action_items
await log_evolution_approval(
    suggestion_id=43,
    approved=False,
    briefing_type="action_items"  # Suppresses re-proposal for 30 days
)
```

Each approval/rejection is saved as a memory with:
- `type="evolution"`
- `metadata.evolution_type="approval"`
- `metadata.approved="true"` or `"false"`
- `metadata.suggestion_id` and `metadata.briefing_type` (if provided)

### Time-Based Rate Limiting and Suppression

```
Now
  │
  ├─ Suggestion made 8 days ago ─ CAN propose new suggestion
  │
  ├─ Suggestion made 3 days ago ─ CANNOT propose (rate limit)
  │
  ├─ Type rejected 25 days ago ─ CAN propose (outside 30-day window)
  │
  └─ Type rejected 5 days ago ─ CANNOT propose (within 30-day suppression)
```

- **7-day rate limit** — At most one suggestion per 7 days, globally
- **30-day rejection suppression** — If a specific briefing_type is rejected, do not re-suggest it for 30 days

### Evolution History

Call `query_evolution_history(limit=20)` to view past suggestions and approvals:

```json
{
  "count": 5,
  "history": [
    {
      "id": 42,
      "title": "Evolution suggestion: remove decay_warnings",
      "metadata": {
        "evolution_type": "suggestion",
        "action": "remove",
        "briefing_type": "decay_warnings",
        "response_rate": "0.2"
      },
      "created_at": "2026-04-03T12:00:00+00:00"
    },
    {
      "id": 43,
      "title": "Evolution approved: suggestion #42",
      "metadata": {
        "evolution_type": "approval",
        "suggestion_id": "42",
        "approved": "true",
        "briefing_type": "decay_warnings"
      },
      "created_at": "2026-04-03T12:15:00+00:00"
    }
  ]
}
```

All evolution memories have `type="evolution"` and are queryable via the standard `search_memory` tool.

## Zusammenspiel

### With Weekly Briefing

The Self-Improvement Loop consumes metadata from the Weekly Briefing workflow:

```
Weekly Briefing (user saves briefing_type + user_responded)
  │
  ├─ action_items briefing, user_responded=True
  ├─ decay_warnings briefing, user_responded=False
  └─ theme_trends briefing, user_responded=True
        │
        ▼
analyze_briefing_engagement() → computes response rates
        │
        ▼
generate_evolution_suggestion() → proposes removal of "decay_warnings"
        │
        ▼
log_evolution_approval(approved=True) → stores approval
        │
        ▼
Next week's analysis excludes "decay_warnings" from proposal (30-day suppression)
```

### With save_memory and search_memory

Evolution memories are first-class: they are saved via `save_memory` with `type="evolution"` and queried via `search_memory`:

```python
# Query all evolution history
results = await search(
    type="evolution",
    limit=20,
    order_by="newest"
)

# Query only suggestions
results = await search(
    type="evolution",
    metadata_filter={"evolution_type": "suggestion"},
    limit=10
)

# Query rejections in the last 30 days
results = await search(
    type="evolution",
    metadata_filter={"evolution_type": "approval", "approved": "false"},
    date_start="2026-03-04T...",
    date_end="2026-04-03T...",
)
```

### With capture_router and entity extraction

While the Self-Improvement Loop does NOT rely on capture_router, it works well alongside it:

- **capture_router** classifies and extracts structured metadata from raw briefings
- **Self-Improvement Loop** analyzes aggregated response patterns across briefing types
- Together, they form a full feedback cycle: automatic classification → pattern analysis → behavior change

## Besonderheiten

### Metadata Validation

The module enforces that any memory saved with `type="briefing"` must include:

```python
BRIEFING_METADATA_REQUIRED_KEYS = ("briefing_type", "user_responded")
```

Calling `validate_briefing_metadata(metadata)` returns a list of errors (empty = valid):

```python
errors = validate_briefing_metadata({"briefing_type": "action_items"})
# Returns: ["briefing metadata missing required field 'user_responded'"]
```

This is a **soft validation** — the server does not reject saves that lack these fields, but the analyzer will ignore briefings without them.

### Rate Limit: One Per 7 Days

The suggestion generator enforces a global rate limit: at most **one suggestion per 7 days** across all projects and briefing types. This prevents proposal fatigue:

```python
# Day 1: Suggestion generated
# Day 3: Call generate_evolution_suggestion() → Returns None (rate-limited)
# Day 8: Call generate_evolution_suggestion() → Can generate new suggestion
```

The 7-day window is checked against memories with `type="evolution"` and `metadata.evolution_type="suggestion"`.

### 30-Day Rejection Suppression

If a user rejects a suggestion, that specific **briefing_type** is suppressed for 30 days. This prevents the system from repeatedly nagging about the same briefing type:

```python
# Day 1: Suggest removal of "decay_warnings"
# Day 2: User rejects with log_evolution_approval(approved=False, briefing_type="decay_warnings")
# Day 3-30: generate_evolution_suggestion() will NOT suggest removal of "decay_warnings"
# Day 31: "decay_warnings" eligible again
```

Rejected types are identified by searching `type="evolution"`, `metadata.evolution_type="approval"`, `metadata.approved="false"` in the last 30 days.

### Threshold: 30% Response Rate

The decision to remove vs. expand is based on a hard-coded **30% threshold**:

```python
if lowest.response_rate < 0.30:
    # Propose removal
else:
    # Propose expansion (even if response rate is 30-50%)
```

This reflects the assumption that < 30% engagement indicates the briefing type should be reconsidered, while >= 30% suggests potential for growth.

### Empty Database Handling

If no briefings exist or insufficient data is collected:

- `analyze_briefing_engagement()` returns `has_sufficient_data=False`
- `generate_evolution_suggestion()` returns `None`
- No suggestion is generated until 7+ days of data are collected

This allows the system to be deployed early without immediately nagging users with incomplete signals.

### Per-Project Evolution

All methods accept an optional `project` parameter, allowing per-project evolution:

```python
# Analyze engagement for project "alpha" only
report = await analyze_engagement(dl, days_back=7, project="alpha")

# Generate suggestion for project "alpha" only
suggestion = await generate_suggestion(report, dl, project="alpha")

# Log approval scoped to project "alpha"
await log_evolution_approval(dl, suggestion_id=42, approved=True, project="alpha")

# Query evolution history for project "alpha"
history = await query_evolution_history(dl, project="alpha", limit=20)
```

When a project filter is applied, only briefings saved with matching `metadata.project` are analyzed.

### Metadata Flexibility

Brief types are arbitrary strings — no pre-defined enumeration. Supported types include:

- "decay_warnings" (stale memories needing review)
- "action_items" (unresolved meeting items)
- "theme_trends" (emerging/declining entities)
- "open_loops" (cross-project connections)
- Custom types defined by your briefing system

The analyzer automatically adapts to any briefing_type value.

## Technische Details

### Data Structures

Located in `python/src/open_brain/evolution.py`:

```python
@dataclass
class BriefingEngagement:
    """Engagement stats for a single briefing type."""
    briefing_type: str
    total_count: int
    responded_count: int
    response_rate: float  # 0.0 - 1.0

@dataclass
class EngagementReport:
    """Aggregated engagement report over a time period."""
    period_days: int
    by_type: list[BriefingEngagement]
    total_briefings: int
    has_sufficient_data: bool  # True if >= 7 days and >= 3 briefings across 2+ days

@dataclass
class EvolutionSuggestion:
    """A single behavior-change suggestion."""
    action: str  # "remove" | "expand"
    briefing_type: str
    reason: str
    response_rate: float
```

### Core Functions

All logic is in `python/src/open_brain/evolution.py`:

| Function | Purpose |
|----------|---------|
| `analyze_engagement(dl, days_back=7, project=None)` | Compute response rates by briefing type |
| `generate_suggestion(report, dl, project=None)` | Generate ONE suggestion based on engagement (rate-limited) |
| `log_evolution_approval(dl, suggestion_id, approved, project, briefing_type)` | Record approval/rejection |
| `query_evolution_history(dl, limit=20, project=None)` | Retrieve past suggestions and approvals |
| `validate_briefing_metadata(metadata)` | Validate that briefing memories include required fields |

### MCP Tool Definitions

In `python/src/open_brain/server.py`:

```python
@mcp.tool(description="Analyze briefing engagement: response rates by type over last N days.")
async def analyze_briefing_engagement(days_back: int = 7, project: str | None = None) -> str:
    ...

@mcp.tool(description="Generate ONE self-improvement suggestion based on engagement (rate-limited to 1 per 7 days).")
async def generate_evolution_suggestion(days_back: int = 7, project: str | None = None) -> str:
    ...

@mcp.tool(description="Log approval/rejection of an evolution suggestion.")
async def log_evolution_approval(suggestion_id: int, approved: bool, project: str | None = None, briefing_type: str | None = None) -> str:
    ...

@mcp.tool(description="Query evolution history: past suggestions and approvals.")
async def query_evolution_history_tool(limit: int = 20, project: str | None = None) -> str:
    ...
```

### Memory Storage

Evolution data is stored as regular memories in the `memories` table with:

```sql
type = 'evolution'
metadata.evolution_type = 'suggestion' | 'approval'
```

**Suggestions** (type='evolution', evolution_type='suggestion'):
```json
{
  "evolution_type": "suggestion",
  "action": "remove" | "expand",
  "briefing_type": "decay_warnings",
  "response_rate": "0.2"
}
```

**Approvals** (type='evolution', evolution_type='approval'):
```json
{
  "evolution_type": "approval",
  "suggestion_id": "42",
  "approved": "true" | "false",
  "briefing_type": "decay_warnings"  # Optional, enables 30-day suppression
}
```

This design leverages the existing memory storage layer and makes evolution data queryable via standard `search_memory` calls.

### Parameters

| Method | Parameter | Type | Default | Purpose |
|--------|-----------|------|---------|---------|
| `analyze_briefing_engagement()` | `days_back` | int | 7 | Number of days to analyze |
| | `project` | str | None | Optional project filter |
| `generate_evolution_suggestion()` | `days_back` | int | 7 | Number of days to analyze (default 7) |
| | `project` | str | None | Optional project filter |
| `log_evolution_approval()` | `suggestion_id` | int | — | ID of the suggestion being approved/rejected |
| | `approved` | bool | — | True = approve, False = reject |
| | `project` | str | None | Optional project filter |
| | `briefing_type` | str | None | Briefing type (enables 30-day suppression if provided) |
| `query_evolution_history()` | `limit` | int | 20 | Max number of records to return |
| | `project` | str | None | Optional project filter |

### Return Types

All MCP tools return **JSON strings**:

#### analyze_briefing_engagement()
```json
{
  "period_days": 7,
  "total_briefings": 28,
  "has_sufficient_data": true,
  "by_type": [
    {
      "briefing_type": "action_items",
      "total_count": 10,
      "responded_count": 9,
      "response_rate": 0.9
    }
  ]
}
```

#### generate_evolution_suggestion()
```json
{
  "suggestion": {
    "action": "remove",
    "briefing_type": "decay_warnings",
    "reason": "Low engagement: only 20% response rate...",
    "response_rate": 0.2
  }
}
```
or `{"suggestion": null}` if rate-limited or insufficient data.

#### log_evolution_approval()
```json
{
  "status": "logged",
  "suggestion_id": 42,
  "action": "approved"
}
```

#### query_evolution_history()
```json
{
  "count": 5,
  "history": [
    {
      "id": 42,
      "title": "Evolution suggestion: remove decay_warnings",
      "metadata": {...},
      "created_at": "2026-04-03T12:00:00+00:00"
    }
  ]
}
```

### Testing

Tests in `python/tests/test_evolution.py` cover:

| Test Class/Function | Acceptance Criterion |
|---|---|
| `test_validate_briefing_metadata` | Metadata validation detects missing fields |
| `test_analyze_engagement_basic` | Response rates computed per briefing type |
| `test_analyze_engagement_multiple_days` | Engagement correctly aggregates across days |
| `test_analyze_engagement_sufficient_data` | has_sufficient_data=True when 7+ days, 3+ briefings, 2+ distinct days |
| `test_generate_suggestion_remove` | Proposes removal when type has < 30% response rate |
| `test_generate_suggestion_expand` | Proposes expansion when all types have >= 30% response rate |
| `test_generate_suggestion_rate_limit` | Rate limited to 1 per 7 days |
| `test_generate_suggestion_30day_suppression` | Rejected types suppressed for 30 days |
| `test_log_evolution_approval_approve` | Approval recorded correctly |
| `test_log_evolution_approval_reject` | Rejection recorded correctly |
| `test_query_evolution_history` | Query retrieves suggestions and approvals |
| `test_empty_database` | Returns sensible defaults when no data |
| `test_per_project_isolation` | Project filter isolates analysis |

All tests use async/await with mock DataLayer.

### No New API Endpoints

The Self-Improvement Loop is an **MCP tool only**. It does not add any HTTP REST routes:

- No GET `/api/evolution`
- No POST `/api/evolution`

Users access it via the MCP tool interface only.

### Timestamps

All timestamps are **ISO 8601 with UTC timezone**:

```
2026-04-03T10:15:23.456789+00:00
```

## Example Usage

### Weekly Evolution Check-In

```python
# Step 1: Analyze engagement
engagement = await analyze_briefing_engagement(days_back=7)

# Step 2: Generate suggestion
suggestion = await generate_evolution_suggestion(days_back=7)

if suggestion:
    print(f"{suggestion['action'].upper()}: {suggestion['briefing_type']}")
    print(f"Reason: {suggestion['reason']}")
    
    # Step 3: User approves
    user_approves = ask_user(f"Apply this change? Yes/No")
    if user_approves:
        await log_evolution_approval(
            suggestion_id=suggestion['id'],
            approved=True,
            briefing_type=suggestion['briefing_type']
        )
```

### Track Engagement Trends Over Time

```python
# Query 10 weeks of history
history = await query_evolution_history(limit=100)

# Analyze which briefing types were removed/expanded
for memory in history:
    if memory['metadata']['evolution_type'] == 'approval':
        action = "Approved" if memory['metadata']['approved'] == 'true' else "Rejected"
        brief_type = memory['metadata'].get('briefing_type', 'unknown')
        print(f"{action}: {brief_type} on {memory['created_at']}")
```

### Multi-Project Evolution

```python
# Analyze engagement per project
for project in ["alpha", "beta", "gamma"]:
    report = await analyze_briefing_engagement(days_back=7, project=project)
    print(f"{project}: {report['total_briefings']} briefings")
    for bt in report['by_type']:
        print(f"  {bt['briefing_type']}: {bt['response_rate']:.0%} engagement")
```
