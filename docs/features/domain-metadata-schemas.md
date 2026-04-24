# Domain Metadata Schemas

Structured metadata validation for domain-specific memory types (event, person, household, meeting, decision, mention, interaction). Enables type-aware field validation without blocking saves.

## Was

Domain Metadata Schemas define structured field definitions for seven domain-specific memory types. Each type has a TypedDict schema with optional datetime fields that are validated (but not enforced) when saving memories. The system validates datetime fields and appends warnings to the response if validation fails, but the memory is always saved successfully.

No new database tables are added — all metadata is stored in the existing `metadata` JSONB column alongside other metadata like `capture_template` and `entities`.

## Für wen

Teams and systems that want to enforce consistency and structure around specific memory types:

- **Event management** — Capture meetings, workshops, deadlines with validated `when` timestamps
- **People tracking** — Build a CRM layer on top of open-brain with structured `last_contact` dates for relationship management
- **Household inventory** — Track items and appliances with warranty expiry dates for maintenance alerts
- **Decision logging** — Maintain structured rationale and context for decisions made
- **Meeting notes** — Standardize meeting capture with attendees, topics, and action items

Type-aware validation enables:
- **Downstream filtering** — Find all "event" memories without a `when` field and prompt cleanup
- **MIRA knowledge base** — Support agent knows which structured fields to expect for each memory type
- **Triage logic** — Lifecycle rules can operate on type-specific metadata (e.g., archive old events)

## Wie es funktioniert

### Supported Domain Types and Schemas

Seven domain types are predefined with TypedDict schemas:

#### **event**
```python
class EventMetadata(TypedDict, total=False):
    when: str              # ISO datetime (required for events)
    who: list[str]         # Names of people involved
    where: str             # Location or venue
    recurrence: str        # Frequency or pattern (e.g., "weekly", "one-time")
```

**Validation**: `when` field is validated as ISO 8601 datetime. If missing or invalid, a warning is appended to the response but the memory is saved.

#### **person**
```python
class PersonMetadata(TypedDict, total=False):
    name: str              # Person's full name
    org: str               # Organization or company
    role: str              # Job title or role
    relationship: str      # How they relate to you (colleague, friend, mentor)
    last_contact: str      # ISO datetime of last interaction
```

**Validation**: `last_contact` is validated as ISO 8601 datetime if provided. Valid timestamps enable relationship tracking and outreach planning.

#### **household**
```python
class HouseholdMetadata(TypedDict, total=False):
    category: str          # Type of item (appliance, furniture, electronics)
    item: str              # Name of specific item
    location: str          # Where it's stored in home
    details: str           # Notes, model, serial number, etc.
    warranty_expiry: str   # ISO datetime of warranty end
```

**Validation**: `warranty_expiry` is validated as ISO 8601 datetime if provided. Used for maintenance alerts and purchase planning.

#### **meeting**
```python
class MeetingMetadata(TypedDict, total=False):
    attendees: list[str]   # Names of people present
    topic: str             # Main topic or agenda
    key_points: list[str]  # Important decisions and conclusions
    action_items: list[str] # Follow-up tasks and owners
    date: str              # ISO datetime of the meeting
```

**Validation**: `date` is validated as ISO 8601 datetime if provided. Enables timeline reconstruction of meetings and decision history.

#### **decision**
```python
class DecisionMetadata(TypedDict, total=False):
    what: str              # What was decided
    context: str           # Why this decision was needed
    owner: str             # Who made or owns the decision
    alternatives: list[str] # Other options considered
    rationale: str         # Why this option was chosen
```

**Validation**: No datetime fields — purely documentation of reasoning and context.

#### **mention**
```python
class MentionMetadata(TypedDict, total=False):
    person_ref: str        # stable identifier pointing to a person memory
    context: str           # short snippet from source
    source_memory_ref: str # memory id that contains the mention
    sentiment_hint: str    # positive|neutral|negative|ambiguous|unknown
```

**Validation**: `person_ref` is recommended. If missing, a warning is appended to the response but the memory is saved. All other fields are purely informational with no validation.

#### **interaction**
```python
class InteractionMetadata(TypedDict, total=False):
    person_ref: str        # stable identifier pointing to a person memory
    channel: str           # meeting|call|email|chat|unknown
    direction: str         # inbound|outbound|bidirectional
    summary: str
    occurred_at: str       # ISO 8601 datetime
    follow_up_needed: bool
```

**Validation**: `person_ref` is recommended — a warning is appended if missing. `occurred_at` is validated as ISO 8601 datetime if provided — a warning is appended if the value is not a valid datetime. Both warnings are advisory; the memory is always saved.

### Validation Flow

When `save_memory(type=..., metadata=...)` is called:

1. **Save proceeds immediately** — no blocking even if metadata is invalid
2. **Type and metadata checked** — call `validate_domain_metadata(memory_type, metadata)`
3. **Validation returns warnings** — a list of human-readable warning strings
4. **Warnings appended to response** — if list is non-empty, `response.warning` contains "; "-joined messages
5. **Memory persists unchanged** — validation is advisory only

### ISO 8601 Datetime Format

All datetime fields accept ISO 8601 format strings:

```
YYYY-MM-DDTHH:MM:SS                    # Local datetime
2026-04-15T10:00:00                    # Valid: April 15, 2026, 10:00 AM

2026-04-15T10:00:00Z                   # UTC with Z suffix
2026-04-15T10:00:00+02:00              # With timezone offset

2026-04                                 # Year + month (valid ISO)
2026                                    # Year only (valid ISO)
```

Validator accepts any ISO datetime that Python's `datetime.fromisoformat()` can parse.

### Example: Saving an Event

```json
{
  "text": "Quarterly planning session with finance team",
  "type": "event",
  "metadata": {
    "when": "2026-04-15T10:00:00",
    "who": ["Alice", "Bob", "Sarah"],
    "where": "Conference Room A"
  }
}
```

Response (if valid):
```json
{
  "id": 42,
  "message": "Memory saved"
}
```

Response (if `when` is missing):
```json
{
  "id": 42,
  "message": "Memory saved",
  "warning": "event metadata missing required field 'when' (expected ISO datetime, e.g. '2026-04-15T10:00:00')"
}
```

Response (if `when` is invalid):
```json
{
  "id": 42,
  "message": "Memory saved",
  "warning": "event metadata field 'when' is not a valid ISO datetime: 'April 15'"
}
```

### Unknown Types

Types not in the predefined list (event, person, household, meeting, decision, mention, interaction) pass through validation with **no warnings**:

```python
# Custom type not in the schema registry — no validation
await save_memory(type="custom_event", metadata={"foo": "bar"})
# Returns: {"id": 42, "message": "Memory saved"}
# (no validation because type is unknown)
```

This ensures backward compatibility — existing code with arbitrary type values is never broken by this feature.

## Zusammenspiel

### With search and filtering

Downstream consumers can search by domain type and filter by metadata:

```python
# Find all event memories
results = await search(type="event")

# Find all people memories created in 2025
results = await search(type="person", date_start="2025-01-01")

# Find events without a "when" field and prompt fix
results = await search(type="event")
events_without_when = [m for m in results if not m.metadata.get("when")]
```

### With capture_template

A memory can have both domain metadata AND capture_template metadata:

```python
metadata = {
  "when": "2026-04-15T10:00:00",      # From domain schema validation
  "who": ["Alice", "Bob"],
  "capture_template": "meeting",       # From capture_router
  "attendees": ["Alice", "Bob"],       # Template-specific field
  "entities": {                        # From entity extraction
    "people": ["Alice", "Bob"],
    "orgs": ["Finance"]
  }
}
```

All enrichment layers (domain schemas, capture router, entity extraction) compose together transparently.

### With triage and lifecycle rules

Triage logic can use domain metadata to classify memories:

```python
# Lifecycle rule: Archive old events (more than 1 year old)
old_events = [m for m in all_events if is_past(m.metadata.get("when"), years=1)]

# Lifecycle rule: Promote "person" memories with recent "last_contact"
recent_contacts = [m for m in people if is_recent(m.metadata.get("last_contact"), days=30)]

# Lifecycle rule: Alert on warranty expiry
expiring_soon = [m for m in household if is_expiring_soon(m.metadata.get("warranty_expiry"), days=90)]
```

## Besonderheiten

### Soft Validation (Never Blocks)

This feature is **permissive by design**:

- Invalid datetime fields → warning appended, memory saved anyway
- Missing required fields → warning appended, memory saved anyway
- Metadata entirely omitted → no error, memory saved (type is enough)
- Unknown types → no validation, memory saved

The system prioritizes reliability over correctness. Callers always get a saved memory; warnings guide gradual refinement.

### No Enforcement at Read Time

Validation runs only at **write time** (save_memory). There is no validation when:

- Searching with `search(type="event")`
- Reading with `get_observations(ids=[...])`
- Building timelines with `timeline(...)`

This means a memory saved without a `when` field will still appear in event searches — callers are responsible for filtering or prompting fixes.

### Warnings are Advisory

Warnings are returned in the response but are **not a failure**. Code like this is valid:

```python
result = await save_memory(type="event", metadata={})
# result.warning == "event metadata missing required field 'when'..."
# But result.id is valid and memory is persisted
```

A system can choose to:
- Log warnings for later cleanup
- Alert the user and prompt for correction
- Silently ignore warnings (if domain metadata is optional)

### TypedDict is Python-Only

The `EventMetadata`, `PersonMetadata`, etc. TypedDicts are defined in `python/src/open_brain/data_layer/interface.py` but are **not enforced by type checking at runtime**. They document expected structure for:

- IDE autocomplete when building metadata dicts
- Mypy/Pyright static analysis
- Documentation for API consumers

The actual validation is dynamic (via `validate_domain_metadata` function).

### No Default Values

If a caller provides a field with a value like `{"when": null}` or `{"name": ""}`, the validator treats them as provided:

```python
# null is treated as "value provided but is null"
await save_memory(type="event", metadata={"when": None})
# Still validates; warning returned because None is not a valid ISO datetime

# Empty string passes ISO datetime validation check
await save_memory(type="event", metadata={"when": ""})
# Still validates; warning returned because "" is not valid ISO
```

## Technische Details

### Implementation

Located in `python/src/open_brain/data_layer/interface.py` and `python/src/open_brain/server.py`:

- **TypedDict definitions** — `EventMetadata`, `PersonMetadata`, etc. in `interface.py`
- **Validator function** — `validate_domain_metadata(memory_type: str | None, metadata: dict) -> list[str]`
- **ISO datetime checker** — `_is_iso_datetime(value: str) -> bool` using `datetime.fromisoformat()`
- **Integration point** — `save_memory()` MCP tool calls validator and appends warnings to response

### Validator Logic

```python
def validate_domain_metadata(memory_type: str | None, metadata: dict | None) -> list[str]:
    """Validate domain-specific metadata fields.
    
    Returns list of human-readable warning strings.
    Unknown types and None type return empty list (no warnings).
    """
    if memory_type is None:
        return []
    
    md = metadata or {}
    warnings = []
    
    if memory_type == "event":
        when = md.get("when")
        if when is None:
            warnings.append("event metadata missing required field 'when'...")
        elif not _is_iso_datetime(str(when)):
            warnings.append(f"event metadata field 'when' is not valid ISO datetime: {when!r}")
    
    # Similar branches for person, meeting, household
    # (decision has no validation)
    
    return warnings
```

**Key properties:**

- Validates only known types — unknown types skip validation
- Gracefully handles None type and None metadata
- Always returns a list (possibly empty)
- Never raises exceptions
- Converts values to string before datetime check

### ISO Datetime Validation

Uses Python's `datetime.fromisoformat()`:

```python
def _is_iso_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False
```

- Accepts ISO 8601 subsets (YYYY, YYYY-MM, YYYY-MM-DDTHH:MM:SS, etc.)
- Handles Z suffix by converting to +00:00 offset
- Rejects non-datetime strings like "April 15" or "tomorrow"

### save_memory Tool Description

The MCP tool's docstring currently documents the original five schemas (`event`, `person`, `meeting`, `decision`, `household`); `mention` and `interaction` are defined in the interface layer and documented here, pending a follow-up bead to update the server tool description. The complete target description (all seven schemas) is shown below:

```
DOMAIN SCHEMAS — structured metadata by type:
event: {when (ISO datetime, required), who: [str], where: str, recurrence: str}.
person: {name: str, org: str, role: str, relationship: str, last_contact: ISO datetime}.
meeting: {attendees: [str], topic: str, key_points: [str], action_items: [str], date: ISO datetime}.
decision: {what: str, context: str, owner: str, alternatives: [str], rationale: str}.
household: {category: str, item: str, location: str, details: str, warranty_expiry: ISO datetime}.
mention: {person_ref: str (recommended), context: str, source_memory_ref: str, sentiment_hint: str}.
interaction: {person_ref: str (recommended), channel: str, direction: str, summary: str, occurred_at: ISO datetime, follow_up_needed: bool}.
ISO datetime format: 'YYYY-MM-DDTHH:MM:SS' (e.g. '2026-04-15T10:00:00').
Invalid or missing required datetime fields produce a warning in the response but still save the memory.
```

This is the canonical specification for domain metadata. Once `server.py` is updated in a follow-up bead, this will also serve as the primary API documentation exposed to LLM agents.

### No OpenAPI Changes

Domain metadata is passed via the `metadata` dict parameter of `save_memory`, which already exists in the OpenAPI schema. No new routes, parameters, or endpoints were added — the feature is pure metadata enrichment.

### Related Features

- **Entity Extraction** (`docs/features/entity-extraction.md`) — Extracts named entities in parallel; both entity and domain metadata can coexist
- **Capture Router** (`docs/features/capture-router.md`) — Classifies memory type and populates template-specific metadata; domain validation is independent
- **Periodic Learnings Extraction** — Can use domain metadata (e.g., access `memory.metadata.get("when")` for event memories)

## Testing

Unit tests in `python/tests/test_domain_schemas.py` cover:

1. **Event validation** — Missing `when` field produces warning; invalid ISO datetime produces warning; valid ISO datetime passes
2. **Person validation** — Valid and invalid `last_contact` dates
3. **Household validation** — Valid and invalid `warranty_expiry` dates
4. **Meeting validation** — Valid and invalid `date` fields
5. **Decision validation** — No datetime fields, no validation (always passes)
6. **Mention validation** — Missing `person_ref` produces warning; with `person_ref` passes
7. **Interaction validation** — Missing `person_ref` produces warning; malformed `occurred_at` produces warning; valid data passes; both warnings together when both fields are invalid
8. **Unknown types** — Pass through with empty warning list
9. **None type** — Passes with empty warning list
10. **Integration** — `save_memory` appends warnings to response JSON

Tests mock `get_dl()` and verify:
- Correct warnings are returned for each invalid field
- Multiple warnings are joined with "; "
- Valid metadata produces no warnings
- Unknown types are never validated
- Metadata is saved successfully regardless of validation result
