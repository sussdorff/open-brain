---
name: ob-migrate
description: >-
  use when: bootstrapping or extending open-brain memory by importing facts from
  prior conversation context, a JSONL export, or Obsidian/Markdown files.
  NOT for: ingesting URLs (use ingest-content), or saving a single observation
  (use `ob save` directly).
  boundary: ob-migrate is for batch + interactive memory writes; ingest-content
  is for one URL at a time; ob-search is read-only.
version: 0.2.0
requires_standards:
  - open-brain/cli-routing
  - open-brain/memory-write-patterns
requires:
  - prompt:english-only
  - mcp:open-brain
---

# open-brain Memory Migration

Bootstrap or extend open-brain memory by migrating knowledge from prior context
or external files. Idempotent (see write-patterns standard).

## Quick Start

```
/ob-migrate                          # Interactive: extract facts from prior context
/ob-migrate /path/to/export.jsonl    # Batch: import JSONL file
/ob-migrate /path/to/notes.md        # Batch: import Obsidian/Markdown file
/ob-migrate /path/to/notes.md project:my-project
```

## Mode 1: Interactive Mode

**Trigger**: `/ob-migrate` with no file argument.

1. Review current conversation history and loaded context (CLAUDE.md, standards,
   memory context). Identify concrete facts, decisions, patterns, learnings, or
   observations worth long-term preservation.
2. Present the extraction plan to the user as a numbered list with
   `[type] text (project)`, then ask `Proceed with migration? (y/n/edit)` —
   wait for the user's answer before saving.
3. For each approved fact, call:

   ```
   save_memory(text=..., type=..., project=..., title=..., narrative=...)
   ```

   The capture router runs automatically — pass through whatever fields apply.
4. Track and print the summary per the write-patterns standard.

Focus on: technical decisions and rationale, conventions, project-specific
knowledge (architecture, deployment, tools), preferences, session discoveries.

## Mode 2: Batch — JSONL

Each line is a JSON object. Required: `text`. Optional: `type`, `project`,
`title`, `narrative`, `metadata`.

```jsonl
{"text": "Use asyncpg for all DB access.", "type": "learning", "project": "open-brain"}
{"text": "Voyage-4 chosen for embeddings.", "type": "decision"}
```

Parsing rules:

- Skip blank lines silently
- Lines that are not valid JSON → count as **error**, continue
- Lines missing `text` → count as **error**, continue
- Unknown extra fields pass through into `metadata`

## Mode 2: Batch — Obsidian/Markdown

Single file → one memory; entire content becomes `text`, filename (without
extension) becomes `title`.

Multi-section file (sections separated by `---`): each section is one memory.
First heading (`# ...`) in a section becomes `title`.

Defaults for Markdown:

- `type` → `"observation"` (override via `type:learning`)
- `project` → `null` (override via `project:my-project`)

## Batch Workflow

1. Read the file via the Read tool.
2. Parse all items per the format rules above. Count malformed entries as
   errors immediately.
3. Preview to the user with the count and proposed action — wait for `y/n`.
4. For each valid item, call `save_memory` with pass-through fields.
5. Process the response per the write-patterns standard: `duplicate_of` →
   skipped, missing `id` or exception → error, otherwise → migrated.
6. For imports >10 items, print progress every 10.
7. Print the summary.

## Arguments

| Argument | Description |
|---|---|
| *(none)* | Interactive mode |
| `<file-path>` | Batch mode — JSONL or Markdown |
| `project:<name>` | Default project for all items |
| `type:<type>` | Default type for all items (Markdown) |
| `limit:<n>` | Import first N only |
| `dry-run` | Parse + preview without saving |

## Rules

- NEVER save without user confirmation in interactive mode
- Batch mode continues on error — malformed lines are counted, not fatal
- `dry-run` skips all `save_memory` calls (see write-patterns standard §5)
- All other write semantics (duplicate handling, summary, capture router,
  metadata encoding) inherit from `open-brain/memory-write-patterns`

## Type Reference

| Type | Use for |
|---|---|
| `learning` | Technical lessons, best practices, conventions |
| `observation` | Facts about a project, system, environment |
| `decision` | Architectural/design decisions with rationale |
| `discovery` | New findings, surprising behaviors |
| `bugfix` | Bug found and fixed (document root cause) |
| `feature` | Feature implemented or planned |
| `session_summary` | Summary of a work session |
