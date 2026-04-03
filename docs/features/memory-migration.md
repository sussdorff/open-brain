# Memory Migration Skill: /ob-migrate

Bootstrap or extend open-brain memory by migrating knowledge from prior context or external files.

## Was

The Memory Migration Skill (`/ob-migrate`) is a Claude Code skill that ingests knowledge from two sources:
1. **Interactive mode** — Extracts facts from prior conversation context and memory
2. **Batch mode** — Imports from JSONL or Obsidian/Markdown files

Migrated items are automatically classified and routed through the capture router, which structures them according to domain templates (decision, meeting, insight, learning, etc.). The feature is idempotent: re-running with the same data creates no duplicates.

## Für wen

- **AI assistants bootstrapping memory** — Initialize open-brain with session learnings, decisions, and context without manual data entry
- **Knowledge base maintainers** — Bulk-import Obsidian notes, Notion exports, or JSONL datasets into a persistent memory system
- **Multi-session continuity** — Preserve project insights, technical decisions, and patterns across sessions
- **Memory migration from other systems** — Move existing knowledge from Obsidian, Notion, or custom databases into open-brain

## Wie es funktioniert

### Interactive Mode

When invoked as `/ob-migrate` with no arguments:

1. **Fact extraction** — The skill scans prior conversation history and context (CLAUDE.md, session standards, loaded memory) for concrete facts: technical decisions, patterns, learnings, observations, discoveries.

2. **Extraction plan** — Before saving, the skill displays a numbered list of extracted facts with their type and project context, and prompts for confirmation (y/n/edit).

3. **Save via capture router** — Each confirmed fact is sent to `save_memory(text, type, project, title, narrative)`. The capture router automatically classifies the text into domain templates (decision, meeting, insight, etc.) and extracts structured fields.

4. **Progress tracking** — The skill tracks each response:
   - `duplicate_of` present → counted as **skipped (duplicate)**
   - `id` present → counted as **migrated**
   - Exception → counted as **error**

5. **Summary** — At the end, a summary is printed: `Migration complete: N migrated, M skipped (duplicates), K errors`

### Batch Mode

When invoked as `/ob-migrate <file-path> [options]`:

#### JSONL Format

Each line is a JSON object with required field `text` and optional fields `type`, `project`, `title`, `narrative`, `metadata`:

```jsonl
{"text": "Use asyncpg for all DB access in this project.", "type": "learning", "project": "open-brain"}
{"text": "Deploy script drops MCP connection — run /mcp reconnect after.", "type": "observation", "project": "open-brain"}
```

Parsing rules:
- Blank lines are silently skipped
- Lines that are not valid JSON or missing `text` field are counted as **errors** (parsing continues)
- Unknown fields are passed through as `metadata`

#### Obsidian/Markdown Format

Markdown files can be imported in two ways:

**Single file → one memory:**
```bash
/ob-migrate /path/to/note.md
```
The entire file becomes one memory; the filename (without extension) becomes the title.

**Multi-section file (sections separated by `---`):**
Each section between `---` dividers is a separate memory. The first heading (`# ...`) in a section becomes the title.

Default values for Markdown imports:
- `type` defaults to `"observation"` (override via `type:learning`)
- `project` defaults to `null` (override via `project:my-project`)

#### Batch Workflow

1. **Parse file** — Read and parse all items according to format
2. **Preview** — Show item count, error count, and ask for confirmation before importing
3. **Save each item** — Call `save_memory()` for each valid item
4. **Check for duplicates** — Parse response for `duplicate_of` field to count skipped items
5. **Print progress** — For large imports (>10 items), print progress every 10 items
6. **Summary** — Final summary shows migrated, skipped, error counts

### Idempotency

The `save_memory` tool computes a SHA-256 hash of the `text` content. If an identical text was already saved, it returns:

```json
{"id": 456, "message": "Duplicate detected", "duplicate_of": 123}
```

When `duplicate_of` is present, the item is **skipped and not re-saved**. This makes re-running the same import file safe — only new items will be added.

## Zusammenspiel

The memory migration skill integrates with three core components:

- **Capture Router** — Each migrated item is automatically classified and structured (see [Capture Router](capture-router.md))
- **save_memory MCP Tool** — Performs embedding, deduplication, and persistence
- **Memory System** — Migrated items are stored in the same tables as manually-created memories and can be searched via hybrid search (RRF)

## Besonderheiten

- **Capture router runs automatically** — No manual template selection needed; the router classifies each item
- **Batch mode is fault-tolerant** — Malformed lines are counted as errors but don't halt the import
- **dry-run mode** — Pass `dry-run` argument to parse and preview without saving
- **Type and project override** — In batch/Markdown mode, use `type:learning` or `project:my-project` to override defaults for all items
- **Limit option** — Pass `limit:N` to import only the first N items (useful for testing)

## Technische Details

### Tool Invocation

The skill is triggered via Claude Code plugin system on phrases including:
- "/ob-migrate"
- "migrate memories", "import memories"
- "migrate knowledge", "import into open-brain"
- "bootstrap memory", "migrate from Obsidian", "import JSONL"
- German: "Wissen importieren", "Memories importieren"

### Backend

- **Batch import parsing** — `python/src/open_brain/migrate.py` provides `parse_jsonl_batch()` and `parse_jsonl_line()` helpers
- **Duplicate detection** — Implemented in `save_memory` via SHA-256 hash of text content
- **Classification** — The capture router receives `type` and `project` metadata and uses LLM-based templates to extract structured fields (see [Capture Router](capture-router.md))
- **Concurrency** — Classification runs in parallel with save/embed; total added latency <200ms

### Routes and APIs

Memory migration uses the existing `save_memory` MCP tool — no new routes are needed. The feature is entirely implemented as a Claude Code skill with helper functions in the Python package.

### Skill Definition

- **Location** — `plugin/skills/ob-migrate/SKILL.md`
- **Version** — 0.1.0
- **Handler** — Implements interactive + batch workflows with progress tracking and duplicate detection via `duplicate_of` response field
