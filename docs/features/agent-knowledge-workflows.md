# Agent Knowledge Workflows

Agents interact with Open Brain through the existing MCP tools and the thin `ob`
CLI wrappers. These operations replace Obsidian capture and periodic review
without adding a graphical note-taking surface.

## Capture

Use `save_memory` for all supported capture types. The primary content goes in
`text`; structured fields and provenance go in `metadata`.

Supported agent capture patterns:

- Idea capture: `save_memory(text=..., type="observation", project=..., metadata={"capture_template": "observation"})`
- Journal capture: `save_memory(text=..., type="journal", project=..., metadata={"capture_template": "journal", "entry_date": "2026-07-12T09:00:00"})`
- URL resource capture: `save_memory(text=..., type="resource", project=..., metadata={"capture_template": "resource", "url": "<resource-url>", "source_type": "web"})`
- structured knowledge item capture: use the canonical personal-knowledge types documented by `save_memory`, such as `concept`, `decision`, `meeting`, `project`, or `person`.

For deterministic agent-authored captures, include `metadata.capture_template`.
When entity extraction should not call the LLM, include `metadata.entities`.

## Inbox Review

New captures default to `metadata.capture_status="inbox"`.

Supported operations to review outstanding inbox captures:

- List outstanding captures with `search(capture_status="inbox", project=...)` or `ob inbox --project <project>`.
- Mark an item processed with `set_capture_status(memory_id=..., capture_status="processed")` or `ob capture set-status <id> processed`.
- Dismiss an item with `set_capture_status(memory_id=..., capture_status="dismissed")`.

Capture status transitions do not change the lifecycle status unless
`lifecycle_status` is explicitly supplied.

## Daily Review

Use `daily_review(date="YYYY-MM-DD", project=...)` or `ob daily [DATE] --project <project>`.

The daily review is a base memory-scope operation, like `search` and `timeline`.
It returns:

- Date-bounded entries for the selected calendar day.
- Source references from `metadata.url`, `metadata.source_ref`,
  `metadata.paperless_reference.document_id`, or `session_ref` metadata when present.
- Date-bounded unresolved inbox captures for the same day.
- Counts for total entries, unresolved captures, and entries by type.

## Weekly Review

Use the existing `weekly_briefing(weeks_back=..., project=...)` tool for weekly
review. Weekly briefing remains an evolution-gated MCP tool and includes memory
counts, top entities, trends, open loops, cross-project connections, decay
warnings, canonical entity state, and inbox state.

See [weekly-briefing.md](weekly-briefing.md) for the full field reference and
return-type schema.

## Related Documentation

- [capture-router.md](capture-router.md) — domain templates and structured field extraction used during `save_memory`
- [canonical-entity-protection.md](canonical-entity-protection.md) — how canonical entities are protected and surfaced in briefings
- [weekly-briefing.md](weekly-briefing.md) — full weekly briefing field reference including `canonical_entities` and `inbox_state`
