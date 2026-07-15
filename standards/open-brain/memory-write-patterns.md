# open-brain Memory Write Patterns

Conventions every skill or agent must follow when writing to open-brain memory
via `save_memory` (CLI: `ob save`, MCP: `mcp__open-brain__save_memory`).

Applies to: `ob-migrate`, `ingest-content`, `ob-triage`, any custom writer.

## 1. Idempotency via `duplicate_of`

`save_memory` computes a SHA-256 hash of `text`. If an identical text was already
saved, the response includes a `duplicate_of` field pointing to the original:

```json
{"id": 456, "message": "Duplicate detected", "duplicate_of": 123}
```

**Rule**: When `duplicate_of` is present, count the item as **skipped** — never
as an error. Re-running the same write is always safe.

For bulk writers, accumulate three counters:

- `migrated` — response has no `duplicate_of`
- `skipped` — response has `duplicate_of`
- `errors` — exception, missing `id`, or malformed input

## 2. Preview-before-save

Any skill that writes more than one memory in a single run MUST present a
preview to the user before calling `save_memory`. Skip the preview only when the
user explicitly bypasses it (e.g. `--yes`, `dry-run` already passed once,
non-interactive batch with `--auto-apply`).

Preview format:

```
Found 42 items in <source>.
3 malformed entries will be skipped (errors).
Proceed with import? (y/n)
```

For single-item writers (one URL, one fact), preview is optional — go straight
to save unless the action has side effects beyond memory (e.g. external API
calls during extraction).

## 3. Summary at end

Every write run ends with a one-line summary in the format:

```
<action> complete: N migrated, M skipped (duplicates), K errors
```

If `errors > 0`, list the first 3-5 with location and reason:

```
Errors:
  Line 7: Invalid JSON — '{bad json'
  Line 15: Missing required field 'text'
```

## 4. Capture router is automatic

Do NOT manually classify a memory before calling `save_memory`. The server-side
capture router reads `type`, `project`, `metadata` and routes the memory to the
right storage path. Manual pre-classification breaks the router's invariants.

Pass through whatever fields the source provides (`type`, `project`, `title`,
`narrative`, `metadata`); let unknown extra fields land in `metadata`.

## 5. Dry-run is honored

If a writer accepts `dry-run` / `--dry-run`, it MUST parse, count, preview, and
return — without any `save_memory` calls. The summary still prints but reads:

```
Dry-run complete: N would be migrated, M would be skipped, K errors
```

## 6. Metadata is a JSON string (MCP)

When calling `mcp__open-brain__save_memory`, the `metadata` field is a JSON
**string**, not an object:

```
metadata: "{\"source_url\": \"...\", \"extraction_date\": \"2026-05-16\"}"
```

The `ob save` CLI accepts both forms transparently. Only the MCP signature
requires the string-encoding.

## 7. Canonical origin lineage

Every new write requires a `provenance` object with a non-blank producer and a
stable namespaced source reference:

```json
{"producer":"ingest-content","source_ref":"url:https://example.com/article"}
```

For `ob save`, pass `--source-ref`; its producer defaults to `ob-cli` and can be
overridden with `--producer`. For MCP, pass the object directly. Memories
derived from the same session, transcript, or document may share one
`source_ref`; it is lineage, not a uniqueness key.

Canonical origin lineage is separate from epistemic provenance such as
`source_label`, `expected_use`, or authorization. Both are stored under
`metadata.provenance`, and writers must not discard existing epistemic fields.

## 8. Searchable source metadata

Whenever the memory originates from an external source (URL, file, transcript,
user message), record provenance in `metadata`:

| Field | When | Example |
|---|---|---|
| `source_url` | URL ingestion | `"https://example.com/article"` |
| `source_file` | File import | `"/path/to/notes.md"` |
| `extraction_date` | Anything extracted | `"2026-05-16"` |
| `content_type` | Curated content | `"video"`, `"article"`, `"doc"` |

This is what makes memory searchable by origin later and enables the
"already-ingested" check in `ingest-content`.

## 9. Pre-write duplicate check (URL ingestion only)

For URL-based writers (single-item, expensive to re-extract), search for the URL
before extracting:

```
ob search "<URL>" --json
# or
mcp__open-brain__search(query="<URL>")
```

If a memory with matching `source_url` exists, ask the user whether to update,
skip, or save as duplicate. For bulk writers this check is too expensive — they
rely on `duplicate_of` instead.

## Consumers

Skills that write to open-brain memory declare:

```yaml
requires_standards:
  - open-brain/cli-routing
  - open-brain/memory-write-patterns
```
