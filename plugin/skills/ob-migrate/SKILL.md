---
name: ob-migrate
description: >
  Migrate knowledge into open-brain memory. Supports two modes:
  Interactive mode extracts facts from prior conversation context and saves them.
  Batch mode imports from a JSONL or Obsidian/Markdown file.
  Use when: "/ob-migrate", "ob-migrate", "migrate memories", "import memories",
  "migrate knowledge", "import into open-brain", "bootstrap memory",
  "migrate from Obsidian", "import JSONL", "Wissen importieren", "Memories importieren".
version: 0.1.0
---

# open-brain Memory Migration

Bootstrap or extend open-brain memory by migrating knowledge from prior context or external files.

Idempotent: re-running is safe. When `save_memory` returns `duplicate_of` in the response,
the item is counted as skipped (duplicate) — no new memory is created.

## Quick Start

```
/ob-migrate                          # Interactive mode: extract facts from prior context
/ob-migrate /path/to/export.jsonl    # Batch mode: import JSONL file
/ob-migrate /path/to/notes.md        # Batch mode: import Obsidian/Markdown file
/ob-migrate /path/to/notes.md project:my-project   # Batch with default project
```

---

## Mode 1: Interactive Mode

**Trigger:** `/ob-migrate` with no file argument.

### Workflow

**Step 1 — Extract facts from prior context**

Review the current conversation history and any loaded context (CLAUDE.md, standards, memory context).
Identify concrete facts, decisions, patterns, learnings, or observations about the user, their projects,
or their preferences that are worth preserving in long-term memory.

Focus on:
- Technical decisions and their rationale
- Patterns and conventions the user follows
- Project-specific knowledge (architecture, deployment, tools)
- Personal preferences and working styles
- Discoveries and learnings from the current session

**Step 2 — Present extraction plan**

Before saving, list the facts you identified. Example:

```
Found 5 items to migrate:
1. [learning] "Use uv run python for all Python commands in this project" (project: open-brain)
2. [observation] "Deploy script requires /mcp reconnect after completion" (project: open-brain)
3. [decision] "Voyage-4 embeddings chosen over text-embedding-3-small (14% better retrieval)"
4. [learning] "Always run tests with -m 'not integration' to skip external deps"
5. [observation] "pgvector cosine + tsvector FTS via RRF is the hybrid search strategy"

Proceed with migration? (y/n/edit)
```

**Step 3 — Save each item via save_memory**

For each fact, call `mcp__open-brain__save_memory` with appropriate fields:

```
save_memory(
    text="<the fact/knowledge>",
    type="<learning|observation|decision|discovery|...>",
    project="<project name or null>",
    title="<short title>",
    narrative="<optional: why this matters, context>",
)
```

The capture router is called automatically by `save_memory` — no manual routing needed.

**Step 4 — Track progress and report summary**

Track each response:
- Response has no `duplicate_of` → count as **migrated**
- Response has `duplicate_of` → count as **skipped (duplicate)**
- Exception or missing `id` → count as **error**

Print summary at the end (see Summary section below).

---

## Mode 2: Batch Mode

**Trigger:** `/ob-migrate <file-path>` — file path is the first argument.

Supports two file formats: JSONL and Obsidian/Markdown.

### JSONL Format

Each line is a JSON object. Required field: `text`. Optional: `type`, `project`, `title`, `narrative`, `metadata`.

```jsonl
{"text": "Use asyncpg for all DB access in this project.", "type": "learning", "project": "open-brain"}
{"text": "Deploy script drops MCP connection — run /mcp reconnect after.", "type": "observation", "project": "open-brain"}
{"text": "Voyage-4 chosen for embeddings (14% better retrieval vs text-embedding-3-small).", "type": "decision"}
```

**Parsing rules:**
- Skip blank lines silently
- Lines that are not valid JSON → count as **error**, continue
- Lines missing the `text` field → count as **error**, continue
- Unknown extra fields are passed through as `metadata`

### Obsidian/Markdown Format

Each file is treated as one memory, OR sections separated by `---` are treated as individual memories.

**Single file → one memory:**
```
/ob-migrate /path/to/note.md
```
The entire file content becomes the `text`. The filename (without extension) becomes the `title`.

**Multi-section file (sections separated by `---`):**
Each section between `---` dividers is one memory. The first heading (`# ...`) in a section becomes the `title`.

**Default values for Markdown imports:**
- `type` defaults to `"observation"` (override via argument: `type:learning`)
- `project` defaults to `null` (override via argument: `project:my-project`)

### Batch Workflow

**Step 1 — Read the file**

Use the Read tool to read the file at the given path.

**Step 2 — Parse**

Parse all items from the file according to format rules above.
Count malformed lines/sections as errors immediately.

**Step 3 — Preview**

Show the user a preview before importing:

```
Found 42 items in /path/to/export.jsonl.
3 malformed lines will be skipped (errors).
Proceed with import? (y/n)
```

**Step 4 — Save each item via save_memory**

For each valid item, call:

```
save_memory(
    text=item["text"],
    type=item.get("type"),         # null if not specified
    project=item.get("project"),   # null if not specified
    title=item.get("title"),
    narrative=item.get("narrative"),
    metadata=item.get("metadata"),
)
```

The capture router runs automatically — each item is classified and routed.

**Step 5 — Check response for duplicate_of**

Parse the JSON response from `save_memory`:

```python
response = json.loads(result)
if "duplicate_of" in response:
    # Item already exists — count as skipped (duplicate)
    skipped += 1
else:
    # Successfully saved
    migrated += 1
```

**Step 6 — Print progress**

For large imports (>10 items), print progress every 10 items:
```
Progress: 10/42 processed (8 migrated, 2 skipped)
Progress: 20/42 processed (16 migrated, 4 skipped)
...
```

---

## Idempotency

Re-running ob-migrate is safe. The `save_memory` tool computes a SHA-256 hash of the `text` content.
If an identical text was already saved, it returns:

```json
{"id": 456, "message": "Duplicate detected", "duplicate_of": 123}
```

When `duplicate_of` is present in the response, the item is **skipped (not re-saved)**.
This means you can re-run the same import file and only new items will be migrated.

---

## Summary

At the end of every migration (interactive or batch), print:

```
Migration complete: N migrated, M skipped (duplicates), K errors
```

Example:
```
Migration complete: 38 migrated, 4 skipped (duplicates), 3 errors
```

If there were errors, list the first few with their line number and reason:
```
Errors:
  Line 7: Invalid JSON — '{bad json'
  Line 15: Missing required field 'text'
  Line 31: Invalid JSON — 'not a json object'
```

---

## Arguments Reference

| Argument | Description |
|----------|-------------|
| *(none)* | Interactive mode — extract from prior conversation context |
| `<file-path>` | Batch mode — import JSONL or Markdown file |
| `project:<name>` | Override/default project for all items |
| `type:<type>` | Override/default type for all items (useful for Markdown) |
| `limit:<n>` | Only import first N items (useful for testing) |
| `dry-run` | Parse and preview without saving anything |

---

## Rules

- **NEVER save without user confirmation** in interactive mode — always show the extraction plan first
- **Capture router runs automatically** — do not manually classify; `save_memory` handles it
- **Batch mode: continue on error** — malformed lines are counted, not fatal
- **Always print the summary** — migrated, skipped (duplicates), errors
- **duplicate_of = skip** — never treat a duplicate as an error
- **dry-run skips all save_memory calls** — only parse and count
- **Re-running is idempotent** — safe to run multiple times on the same file

---

## Type Reference

Common memory types for migration:

| Type | Use for |
|------|---------|
| `learning` | Technical lessons, best practices, conventions discovered |
| `observation` | Facts about a project, system, or environment |
| `decision` | Architectural or design decisions with rationale |
| `discovery` | New findings, surprising behaviors |
| `bugfix` | Bug found and fixed (document the root cause) |
| `feature` | Feature implemented or planned |
| `session_summary` | Summary of a work session |
