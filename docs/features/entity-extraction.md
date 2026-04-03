# Entity Extraction on save_memory

Automatic extraction of named entities (people, organizations, technologies, locations, dates) from memory text using Claude Haiku, with parallel execution to minimize latency.

## Was

Entity Extraction is an optional metadata enrichment layer in `save_memory` that automatically recognizes and extracts named entities from incoming text. When called, the system identifies people, organizations, technologies, locations, and dates mentioned in the memory content and stores them in `metadata.entities` for faster downstream filtering, linking, and search.

The extraction runs in parallel with embedding and persistence using `asyncio.gather`, so it adds zero additional latency to the save operation.

## Für wen

Systems that want richer semantic indexing of memories without caller involvement:

- **Memory indexing** — Populate a searchable index of "people mentioned in memories", "tech stack we use", "locations we work in"
- **Graph building** — Connect memories to entity nodes (e.g., link all memories mentioning "Sarah" to Sarah's context node)
- **Triage automation** — Route memories based on extracted entities (e.g., "budget memories" from Finance org)
- **MIRA knowledge base** — Support agent can quickly identify context: "What do we know about this person/org/tech?"
- **Batch reporting** — Aggregate memories by extracted organization, location, or technology for business intelligence

## Wie es funktioniert

### Automatic Extraction Flow

When `save_memory(text=..., metadata=...)` is called:

1. **Check if caller pre-provided entities**
   - If `metadata.entities` already set → skip extraction (trust caller intent)
   - Otherwise → proceed to extraction

2. **Parallel execution**
   - Fork two async tasks using `asyncio.gather()`:
     - Task A: `_extract_entities(text)` — LLM entity recognition (Haiku)
     - Task B: `dl.save_memory(...)` — persist to database and embed
   - Both run concurrently; save operation is not blocked

3. **Extraction result**
   - If entities found → call `dl.update_memory(id, metadata={entities: {...}})` to enrich
   - If no entities → skip update (don't store empty dict)
   - If extraction fails (LLM error, malformed response) → gracefully degrade and log warning

4. **Return immediately** with saved memory ID (enrichment happens transparently)

### Entity Types

The system extracts five entity types:

| Type | Examples | Use Cases |
|------|----------|-----------|
| **people** | "Sarah", "John Smith", names of individuals | Context about who was involved, relationship tracking |
| **orgs** | "Acme Corp", "Google", "Finance Team", organizations and institutions | Segment memories by company or department |
| **tech** | "Python", "Docker", "Kubernetes", "PostgreSQL", tools/languages/frameworks/platforms | Track technology stack, learning domains |
| **locations** | "Berlin", "San Francisco", "Germany", geographic places and regions | Physical office locations, travel, geo-specific events |
| **dates** | "2026-04-03", "Q2", "last Tuesday", time references and periods | Timeline context, event dating |

### Prompt & LLM

The system uses **Claude Haiku 4.5** with a simple structured extraction prompt:

```
Extract named entities from the following text and return ONLY valid JSON:
{"people": [...], "orgs": [...], "tech": [...], "locations": [...], "dates": [...]}

Rules:
- people: named individuals only
- orgs: companies, organizations, institutions
- tech: programming languages, frameworks, tools, platforms, services
- locations: geographic places, cities, countries, regions
- dates: time references, periods, years
- Use empty arrays if no entities of that type found
```

The LLM response is parsed as JSON; markdown fences are stripped if present. Non-list values or malformed JSON is caught and logged.

## Zusammenspiel

### With save_memory

Entity extraction is **embedded into the `save_memory` tool**. No separate call needed:

```python
# Simple usage — caller doesn't need to know about entity extraction
result = await save_memory(
    text="Sarah from Acme Corp visited us at our Berlin office.",
    type="observation",
)
# Automatically enriches with: metadata.entities = {
#   "people": ["Sarah"],
#   "orgs": ["Acme Corp"],
#   "locations": ["Berlin"]
# }
```

### With Pre-provided Entities

If the caller has already computed entities externally, pass them in metadata to **skip extraction**:

```python
result = await save_memory(
    text="Sarah from Acme Corp visited...",
    metadata={"entities": {"people": ["Sarah"], "orgs": ["Acme Corp"]}},
)
# Entity extraction is skipped entirely (no LLM call)
# Pre-provided metadata is preserved as-is
```

This is useful for:
- Systems that compute entities differently (custom NER pipeline)
- Reducing LLM costs when entities are known upfront
- Explicit control over what entities are indexed

### With Capture Router

Entity extraction runs **independently and after** Capture Router classification. A single memory can have both:

```
metadata = {
  "capture_template": "meeting",     # From Capture Router
  "entities": {                       # From Entity Extraction
    "people": ["Alice", "Bob"],
    "orgs": ["HQ Team"]
  },
  "attendees": ["Alice", "Bob"],     # Template field from Capture Router
  ...
}
```

Both enrichment layers are transparent and non-blocking.

### With Search and Filtering

Downstream consumers (search, triage, MIRA) can filter by extracted entities:

```python
# Search for all memories mentioning a specific person
results = await search(metadata_filter={"entities.people": ["Sarah"]})

# Or use entities to build context lookups
# "Show me what we know about this company"
entity_memories = [m for m in all_memories if "Acme Corp" in m.metadata.get("entities", {}).get("orgs", [])]
```

## Besonderheiten

### Empty Text Handling

Empty or whitespace-only text returns `{}` (empty entities dict) immediately without calling the LLM. This avoids unnecessary API calls for no-content saves.

### LLM Failure Graceful Degradation

If the LLM call fails (timeout, rate limit, malformed response):
- Warning is logged (not an error — doesn't block the save)
- Memory is saved successfully without entities
- Caller continues normally

This ensures entity extraction is **never a failure point** for the core save operation.

### Empty Entity Arrays

If the LLM returns all empty arrays (no entities found), the enrichment step is skipped:
- `metadata.entities` is not set on the memory
- Avoids cluttering metadata with `{"people": [], "orgs": [], ...}`
- Saves database space

A memory either has `metadata.entities` with **at least one non-empty entity type**, or no `entities` key at all.

### No Deduplication

The system does not deduplicate or canonicalize entities. If the text says "Sarah" in one memory and "Sarah Smith" in another, both are stored as separate entity values. Callers are responsible for entity linking if needed.

### Caller Intent Preserved

Pre-provided entities in `metadata.entities` are **never overwritten** by automatic extraction. This respects explicit caller intent and allows hybrid workflows where different memory sources contribute different levels of structure.

## Technische Details

### Implementation

Located in `python/src/open_brain/server.py`:

- **`_extract_entities(text: str) -> dict`** — Helper function that calls Haiku, parses JSON response, returns dict with non-empty entity types
- **`save_memory(...)` modification** — Parallel execution via `asyncio.gather(_extract_entities, dl.save_memory)`, conditional `update_memory` call
- **`_ENTITY_EXTRACTION_PROMPT`** — Shared prompt template used by all extraction calls

### Model & Cost

- **Model**: `claude-haiku-4-5-20251001` (Haiku 4.5, low latency and cost)
- **Input tokens**: ~100–150 per call (prompt + text, 1-3K chars typical)
- **Output tokens**: ~100–150 per call (JSON array response)
- **Cost**: ~$0.0008 per call (1M input tokens at $0.80, 1M output at $0.80)

### Parallelization

Uses `asyncio.gather()` to run extraction and save concurrently:

```python
entities, result = await asyncio.gather(
    _extract_entities(text),
    dl.save_memory(SaveMemoryParams(...)),
)
```

Since embedding is already async within `dl.save_memory`, entity extraction adds minimal wall-clock latency (<50ms in typical scenarios).

### Error Handling

- **Empty/whitespace text**: Returns `{}` immediately (no LLM call)
- **LLM timeout**: Caught as `Exception`, logged as warning, continues with `return {}`
- **Malformed JSON**: `_parse_llm_json` strips markdown fences; if still invalid, `json.loads` raises, caught, logged, returns `{}`
- **Non-list values in response**: Filtered out by `{k: v for k, v in parsed.items() if isinstance(v, list) and v}`

The system is designed to **never block a memory save due to entity extraction**.

### Related Endpoints

**No dedicated entity extraction endpoint** — extraction is built into `save_memory` tool only. The `search`, `get_observations`, and `timeline` tools use extracted entities in metadata but don't trigger new extraction.

## Testing

Unit tests in `python/tests/test_entity_extraction.py` cover:

1. People + orgs extraction (Criterion 1)
2. Tech extraction (Criterion 2)
3. Location extraction (Criterion 3)
4. Pre-provided entities skip condition (Criterion 4)
5. Empty text graceful handling (Criterion 5)
6. Parallel execution via `asyncio.gather` (Criterion 6)
7. LLM failure graceful degradation

Tests mock `llm_complete`, `get_dl`, and verify:
- Correct entities are parsed from LLM response
- `update_memory` is called only when entities are found
- `update_memory` is skipped when entities are pre-provided
- Empty entity dicts don't trigger updates
- LLM failures don't raise exceptions
