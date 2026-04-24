# Typed Relationships

Typed relationships allow memories to be connected with semantic edge labels, enabling structured graph traversal beyond the implicit `similar_to` edges created by the embedding dedup pipeline.

## Valid Link Types

| link_type | Meaning |
|---|---|
| `similar_to` | Legacy — auto-written by embedding dedup. Indicates semantic similarity. |
| `attended_by` | A meeting memory references a person memory (meeting → person). |
| `mentioned_in` | A person is mentioned in a memory (person → memory). |
| `spawned_task` | A meeting or mention caused a task to be created (meeting/mention → bd issue). |
| `supersedes` | One memory replaces an older memory (newer → older). |
| `contradicts` | One memory contradicts another. |
| `co_occurs` | Weak co-mention edge — two memories appear together frequently. |

## API

### `create_relationship(source_id, target_id, link_type, metadata=None) -> int`

Creates a typed edge between two memories. Returns the relationship row ID.

Raises `ValueError` if `link_type` is not in `VALID_LINK_TYPES`.

On conflict (same source, target, relation_type), the row is updated and `confidence` is set to `1.0`. Typed relationships are considered authoritative, overriding the auto-linked cosine similarity score written by `_embed_and_link`.

```python
rel_id = await dl.create_relationship(
    source_id=42,
    target_id=100,
    link_type="attended_by",
    metadata={"note": "confirmed attendee"},
)
```

### `traverse(anchor_id, link_types, depth=1, direction='outbound') -> list[dict]`

Traverses the relationship graph using iterative BFS starting from `anchor_id`.

Parameters:
- `anchor_id`: Starting memory ID.
- `link_types`: List of edge types to follow.
- `depth`: Number of hops (1 = direct neighbors, 2 = 2-hop, etc.).
- `direction`: `'outbound'` (source→target), `'inbound'` (target→source), or `'both'`.

Returns a list of dicts: `{id, link_type, depth, source_id, target_id}`.

```python
# Get all attendees of meeting 42 (direct neighbors)
neighbors = await dl.traverse(
    anchor_id=42,
    link_types=["attended_by"],
    depth=1,
)

# Get 2-hop graph: who attended meetings that person 100 also attended
graph = await dl.traverse(
    anchor_id=100,
    link_types=["attended_by"],
    depth=2,
    direction="both",
)
```

### `get_relationships(memory_id, link_types=None) -> list[dict]`

Returns all edges where `memory_id` is source or target.

```python
# All edges for memory 42
edges = await dl.get_relationships(memory_id=42)

# Only similar_to and attended_by edges
edges = await dl.get_relationships(memory_id=42, link_types=["similar_to", "attended_by"])
```

## MCP Tools

Two MCP tools are registered in `server.py`:

- **`create_relationship`** — available to any authenticated caller. Creates a typed relationship.
- **`traverse_relationships`** — available to any authenticated caller. Traverses the graph. Returns `{"results": [...], "count": N}`.

## Schema Migration

The `link_type` column is added to `memory_relationships` via an idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` with `DEFAULT 'similar_to'`. This migration runs automatically at server startup inside `get_pool()`.

Existing rows created before the migration will have `link_type = 'similar_to'` due to the column default.

## Backfill Script

`scripts/migrate_relationships_backfill.py` is a one-shot script that:

1. Runs the `ALTER TABLE` migration (idempotent).
2. Updates any rows where `link_type IS NULL` to `'similar_to'`.
3. Prints `"Backfill complete. Rows updated: N"`.

Running it a second time prints `"Rows updated: 0"` (idempotent).

```bash
DATABASE_URL=postgresql://... python scripts/migrate_relationships_backfill.py
```

## Backcompat

The existing `relation_type` column is untouched. The new `link_type` column is the semantic typed API layer. The auto-linking path in `_embed_and_link` continues to write `relation_type='similar_to'` as before, and those rows automatically get `link_type='similar_to'` via the column default.
