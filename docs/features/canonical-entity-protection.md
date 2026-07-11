# Canonical Entity Protection

Stable, long-lived knowledge records — people, projects, organizations, concepts — can be flagged as canonical entities. Protected memories survive automated memory maintenance (decay, compaction, refine, triage, materialize/archive) intact, while remaining fully readable and explicitly updatable.

## What

A memory becomes a canonical entity by setting two metadata fields:

| Field | Value | Description |
|-------|-------|-------------|
| `metadata.canonical_entity` | `true` | Protection marker |
| `metadata.canonical_kind` | `person` / `project` / `organization` / `concept` | Entity classification |

These fields are orthogonal to the existing `type` and `stability` fields — any memory type can be a canonical entity.

## Why It Exists

Automated maintenance processes (decay, compaction, refine, triage, materialize/archive) operate on heuristics. For high-value knowledge nodes such as a person profile, a project description, or an organization record, heuristic deletion or merging can silently destroy institutional knowledge. Canonical entity protection makes protection explicit and auditable rather than implicit.

## For Whom

Anyone managing long-lived structured knowledge nodes:

- **People records** — Person memories with org, role, and relationship metadata
- **Project records** — Active project descriptions that must survive memory churn
- **Organization records** — Company, team, or institutional knowledge
- **Concept definitions** — Domain terminology or framework definitions that agents rely on

## How It Works

### Protection Predicate

A shared SQL predicate is applied before any automated mutation:

```sql
NOT (metadata->>'canonical_entity' = 'true')
```

All five automated maintenance paths check this predicate:

| Process | Behavior |
|---------|----------|
| `decay_memories` | Skips canonical entities in both decay and count; reports them in `protected_canonical_entities` |
| `compact_memories` | Excludes canonical entities from similarity cluster deletion |
| `refine_memories` | Skips merge, demote, and delete actions on canonical entities |
| `triage_memories` | Excludes canonical entities from archive and merge triage actions |
| `materialize_memories` / `run_lifecycle_pipeline` | Skips archive actions for canonical entities |

### Read Tool Identity Object

`search`, `timeline`, `get_observations`, and `search_by_concept` now include a `canonical_entity` key in each memory payload when the memory is protected:

```json
{
  "id": 42,
  "title": "Alice Müller",
  "type": "person",
  "canonical_entity": {
    "id": 42,
    "kind": "person"
  }
}
```

Callers can detect canonical entities without parsing raw metadata by checking for the presence of this key.

### Approved Update Tool

The `approved_canonical_entity_update` MCP tool is the only sanctioned path for modifying or archiving a canonical entity:

```python
approved_canonical_entity_update(
    id=42,
    actor="malte",
    note="Updated org affiliation after role change",
    operation="update",
    title="Alice Müller (Cognovis)",
    metadata={"org": "Cognovis GmbH"},
)
```

Required parameters:

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | int | Memory ID |
| `actor` | str | Who is making the change (required, must be non-empty) |
| `note` | str | Why the change is being made (required, must be non-empty) |
| `operation` | str | `update` (default) or `archive` |

Optional content parameters: `text`, `type`, `project`, `title`, `subtitle`, `narrative`, `metadata`.

Every call appends an entry to `metadata.audit`:

```json
{
  "metadata": {
    "canonical_entity": true,
    "canonical_kind": "person",
    "audit": [
      {
        "ts": "2026-07-11T10:00:00",
        "actor": "malte",
        "operation": "update",
        "note": "Updated org affiliation after role change"
      }
    ]
  }
}
```

Archive sets `metadata.status='archived'` and preserves the memory ID — the record is not deleted.

## Marking a Memory as Canonical

Use `save_memory` or `update_memory` with the protection metadata:

```python
# Create a new canonical entity
save_memory(
    title="Alice Müller",
    text="Senior engineer at Cognovis GmbH. Contact for pvs-x-isynet integration.",
    type="person",
    metadata={
        "canonical_entity": True,
        "canonical_kind": "person",
        "org": "Cognovis GmbH",
        "role": "Senior Engineer",
    }
)

# Promote an existing memory to canonical
update_memory(
    id=42,
    metadata={
        "canonical_entity": True,
        "canonical_kind": "person",
    }
)
```

## Observability

Processes that encounter canonical entities report them:

- `decay_memories` / `run_lifecycle_pipeline` — `protected_canonical_entities` count in result
- `compact_memories` — skipped entities logged in summary
- `refine_memories` / `triage_memories` / `materialize_memories` — skipped items included in action report

## Constraints

- `canonical_kind` must be one of `person`, `project`, `organization`, `concept`. Invalid values trigger a validation warning.
- `actor` and `note` are mandatory for `approved_canonical_entity_update` — the tool rejects empty strings.
- Canonical entities can still be modified via direct `update_memory` (no protection against deliberate manual edits; protection is against automated bulk operations).

## Testing

Tests in `python/tests/test_canonical_entity_retention.py` cover:

| Test | Acceptance Criterion | What It Verifies |
|------|----------------------|------------------|
| `test_canonical_entity_survives_decay` | AC2 | Decay skips canonical entities |
| `test_canonical_entity_survives_compaction` | AC2 | Compact skips canonical entities |
| `test_canonical_entity_survives_refine` | AC2 | Refine skips canonical entities |
| `test_canonical_entity_survives_triage` | AC2 | Triage skips canonical entities |
| `test_canonical_entity_survives_materialize` | AC2 | Materialize/archive skips canonical entities |
| `test_approved_update_records_audit` | AC3 | Approved update appends audit entry |
| `test_approved_archive_sets_status` | AC3 | Approved archive sets `status=archived` |
| `test_read_tools_expose_identity` | AC1 | Search/timeline/get_observations include identity object |
