# Capture Router

Automatic LLM-based classification and structured field extraction for memory saving.

## Was

The Capture Router is a transparent LLM classification layer embedded in `save_memory` that classifies incoming text into domain-specific templates and auto-extracts structured fields. It runs concurrently with memory embedding to minimize latency.

## Für wen

Any system using open-brain that wants to automatically structure memories without manual template selection. Useful for:

- **Agent memory systems** — Automatically tag decisions, meetings, learnings without caller knowing the schema
- **Memory downstream processing** — Triage, refine, and materialize steps can rely on structured fields
- **Multi-agent workflows** — One agent captures raw text; another retrieves and uses structured templates
- **MIRA support layer** — Support agents can quickly identify memory type and context

## Wie es funktioniert

### Classification Step

When `save_memory(text=..., type=..., metadata=...)` is called:

1. **Async classification task** is created (unless bypassed — see below)
2. **LLM receives the text** along with a set of templates and their field schemas
3. **LLM classifies** the text into the best-matching template
4. **Structured fields** specific to that template are extracted from the text
5. **Classification result** is awaited, then merged into the memory's metadata
6. **Memory is persisted** with `metadata.capture_template` + extracted fields

### Concurrency

Classification runs as an `asyncio.Task` in parallel with `save` and `embed`:

```
save_memory(text)
  ├─ fork: asyncio.create_task(classify_and_extract)
  ├─ save to DB (embedding starts async)
  └─ await classification
     └─ update_memory(id, metadata={capture_template, ...fields})
```

This keeps added latency to <200ms in typical scenarios (save + embed takes 300-500ms).

### Templates and Fields

Each template defines a set of structured fields extracted by the LLM:

| Template | Trigger | Fields |
|----------|---------|--------|
| **project** | Project names, goals, status, roadmap | name, status, owner, goals (list), next_actions (list), repository, due_date |
| **resource** | URLs, references, articles, books | title, url, source_type, author, summary, published_at |
| **concept** | Ideas, definitions, domain knowledge | name, domain, summary, related_concepts (list) |
| **journal** | Daily notes, mood, personal reflection | entry_date, mood, themes (list), reflection |
| **correspondence** | Emails, messages, letters | with (list), channel, direction, subject, summary, occurred_at, follow_up_needed |
| **prompt** | Prompt templates, reusable instructions | purpose, prompt_text, target_model, variables (list), constraints (list), last_used_at |
| **decision** | "decided", "chosen", "selected", "option" | what (string), context (string), owner (string), alternatives (list), rationale (string) |
| **meeting** | "meeting", "attendee", "discuss", "action" | attendees (list), topic (string), key_points (list), action_items (list) |
| **person_context** | Names with roles/relationship | person (string), relationship (string), detail (string) |
| **insight** | "learned", "realized", "discovered" | realization (string), trigger (string), domain (string) |
| **event** | Dates, time references | what (string), when (string), who (string), where (string), recurrence (string) |
| **learning** | "feedback", "skill", "capability" | feedback_type (string), scope (string), affected_skills (list) |
| **observation** | Default / no match | (no special fields) |

### Type Aliases

Callers may pass informal type names that are automatically normalized before classification:

| Alias | Resolved to |
|-------|-------------|
| `note`, `diary` | `journal` |
| `reference` | `resource` |
| `idea` | `concept` |
| `email`, `letter` | `correspondence` |
| `prompt_template` | `prompt` |

Aliases are resolved in `normalize_memory_type()` inside `capture_router.py` before any LLM call.

### Type Column Persistence

For raw captures (no caller-supplied `capture_template` in metadata), the classified template name is persisted into the memory's `type` column after the LLM runs. This means type-based retrieval, `stats()`, and the people pipeline all see the classifier-inferred type instead of it living only in `metadata.capture_template`.

Two exceptions apply:

1. **`person_context`** — deliberately excluded from the column write. Incidental person mentions must not auto-activate the people pipeline; they remain `type=observation` in the `type` column.
2. **Explicit caller type** — if the caller supplied a `type` argument to `save_memory`, that value is always preserved regardless of what the classifier infers. A divergent classifier result updates metadata fields but never overwrites the column.

### Bypass Conditions

Classification is **skipped entirely** when:

1. **Pre-structured metadata**: `metadata.capture_template` is already set by the caller
   - Preserves explicit caller intent — no LLM overwrite
   - Useful for hybrid flows where some callers pre-structure data

2. **Session summary type**: `type="session_summary"`
   - Session summaries are treated as observations (no special template)
   - Prevents LLM from trying to extract meeting or decision fields from summary prose

In both cases, the raw metadata is returned unchanged and no LLM call is made.

## Zusammenspiel

### With save_memory

Capture Router is **embedded into `save_memory`**:

```python
result = await save_memory(
    text="Decided to use async for better scalability",
    type="decision",
    metadata={...}  # capture_template not set → will be classified
)
# Metadata now includes: capture_template="decision", what="...", rationale="..."
```

### With triage_memories

Structured fields set by Capture Router become **available in triage recommendation**:

- Triage LLM can read `memory.metadata.capture_template` to know the memory's domain
- Action items from meetings are available for "create issue" action
- Decision rationale is preserved for "promote to standards"

### With refine_memories

Refined memories **preserve capture_template** — they are not reclassified:

- If a meeting memory is merged with another, the resulting memory keeps `capture_template="meeting"`
- Stability and priority adjustments don't touch the capture template

### With search and retrieval

Capture template and extracted fields are **returned in search results** via memory metadata:

```python
results = search("meetings from last week")
# results[0].metadata = {
#   capture_template: "meeting",
#   attendees: ["Alice", "Bob"],
#   action_items: ["Alice to send docs", ...]
# }
```

## Besonderheiten

### Fallback Behavior

If the LLM returns invalid JSON or fails to classify:
- Memory is still saved successfully
- `capture_template` defaults to `"observation"`
- No error is raised to the caller (graceful degradation)

### Markdown Stripping

The LLM response parser automatically strips markdown code fences:

```json
// LLM might return:
```json
{"capture_template": "decision", ...}
```

// Parser handles this correctly
```

### Null Fields

For extracted fields that cannot be determined from the text, the LLM fills them with `null`:

```json
{
  "capture_template": "decision",
  "what": "Migrate to async",
  "context": null,
  "owner": null,
  "alternatives": [],
  "rationale": "For scalability"
}
```

### Large Text Handling

The classification prompt includes the **full memory text**. For very large texts (>4KB), consider:
- Summarizing the text before calling `save_memory`
- Or pre-setting `capture_template` to skip expensive LLM calls

## Technische Details

### Routes and API

`save_memory` is an MCP tool (not a REST endpoint). Signature:

```python
async def save_memory(
    text: str,
    type: str | None = None,
    title: str | None = None,
    project: str | None = None,
    metadata: dict[str, Any] | None = None,
    ...
) -> str  # JSON response
```

**Metadata handling**:
- `metadata.capture_template` bypasses classification if present
- All other metadata fields are preserved
- Classified fields are **merged** into metadata (no overwrite)

### Implementation

**Location**: `python/src/open_brain/capture_router.py`

Key functions:

- `classify_and_extract(text, existing_metadata, memory_type)` — Main classification function
  - Returns dict with `capture_template` + extracted fields
  - Handles bypass conditions
  - Gracefully falls back to observation on errors
  
- `_parse_json(text)` — JSON response parser
  - Strips markdown fences
  - Handles malformed responses

**Integration in `server.py`**:

```python
# During save_memory:
classify_task = asyncio.create_task(
    classify_and_extract(text, existing_metadata=metadata, memory_type=type)
)

# ... save happens ...

classification = await classify_task
merged_metadata = {**(metadata or {}), **classification}
await dl.update_memory(UpdateMemoryParams(id=result.id, metadata=merged_metadata))
```

### LLM Provider

Uses the configured LLM provider (default: Anthropic Claude Haiku):
- Model: `claude-haiku-4-5-20251001`
- Max tokens: 512 (sufficient for JSON response)
- Temperature: default (0.0 for deterministic classification)

### Database Schema

The `metadata` column (JSONB) stores capture router results:

```sql
UPDATE memories SET metadata = metadata || '{
  "capture_template": "decision",
  "what": "...",
  "context": "...",
  "owner": "...",
  "alternatives": [...],
  "rationale": "..."
}'
WHERE id = ...;
```

No separate schema changes required — all fields live in the existing `metadata` JSONB column.

### Testing

**Unit tests** in `python/tests/test_capture_router.py`:

- Decision classification (AK1)
- Meeting classification + attendees/action_items (AK2)
- Person context extraction (AK3)
- Bypass when capture_template already set (AK4)
- Bypass for session_summary type
- Fallback to observation on parse/LLM errors (AK6)
- Integration test: save_memory → classify → update_memory (AK5)
- **Latency test** (mark: integration): <200ms added latency via concurrency

Run tests:

```bash
cd python
uv run pytest tests/test_capture_router.py -v
uv run pytest tests/test_capture_router.py::TestServerIntegration::test_classification_latency_under_200ms -v
```

### Performance Notes

- **Classification overhead**: ~50-150ms per call (depends on LLM latency + text length)
- **Concurrency**: Async task runs in parallel with embedding, so total latency addition is minimal
- **Token usage**: ~100-200 tokens per classification (prompt + response)
- **Cost**: ~$0.00001 per classification at Haiku pricing
