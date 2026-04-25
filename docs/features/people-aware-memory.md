# People-Aware Memory

Hub document for the People-Aware Memory feature area of open-brain. Start here if you're new
to the system or want to understand how person-centric data flows through the stack.

---

## Architecture Overview

```
 ┌──────────────────────────────────────────────────────────────┐
 │  Sources                                                       │
 │  (MacWhisper transcripts · Emails · Matrix/WhatsApp)          │
 └─────────────────────────┬────────────────────────────────────┘
                           │ raw content
                           ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  Ingest Adapters  (ADR-0001)                                  │
 │  IngestAdapter Protocol: list_recent() · ingest()             │
 │  Credential access via 1Password CLI  (ADR-0002)              │
 └─────────────────────────┬────────────────────────────────────┘
                           │ IngestResult
                           ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  Ingest Pipeline                                              │
 │  save_memory() → embed → metadata extraction (Haiku 4.5)     │
 └─────────┬──────────────────────────────────┬────────────────┘
           │ memory row                        │ relationship edges
           ▼                                   ▼
 ┌─────────────────────┐         ┌────────────────────────────┐
 │  Domain Schemas     │         │  Typed Relationships       │
 │  (cr3.1)            │         │  (cr3.10)                  │
 │  event · person ·   │         │  attended_by · mentioned_in│
 │  meeting · decision │         │  spawned_task · supersedes │
 └─────────────────────┘         └────────────────────────────┘
           │                                   │
           └──────────────────┬────────────────┘
                              ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  People-Aware Query Layer  (cr3.9)                            │
 │  MCP tools: people_discussed_with · people_stale_contacts ·  │
 │             people_mentions_window                            │
 │  Skill: people-query (Claude Code presentation layer)         │
 └──────────────────────────────────────────────────────────────┘
```

---

## Feature Docs

| Document | Bead | What it covers |
|----------|------|----------------|
| [Domain Metadata Schemas](domain-metadata-schemas.md) | cr3.1 | TypedDict schemas for `event`, `person`, `meeting`, `decision`, `household` — validation rules and JSONB storage |
| [Typed Relationships](typed-relationships.md) | cr3.10 | Semantic edge labels (`attended_by`, `mentioned_in`, …), `create_relationship()` API, graph traversal |
| [People-Aware Queries](people-aware-queries.md) | cr3.9 | Three MCP tools for person-centric queries + the `people-query` Claude Code skill |

## Architecture Decision Records

| ADR | What it decides |
|-----|----------------|
| [ADR-0001: Ingest Adapter Interface](../adr/0001-ingest-adapter-interface.md) | Shared `IngestAdapter` Protocol, adapter registry, observability threading |
| [ADR-0002: Credentials and Privacy](../adr/0002-credentials-and-privacy.md) | 1Password CLI as single secret store, PII quarantine policy |

---

## Where Do I Start? (Adding a New Ingest Source)

1. **Read ADR-0001** — understand the `IngestAdapter` Protocol and how adapters are registered.

2. **Read ADR-0002** — understand credential handling (1Password CLI) and PII quarantine rules.
   Your adapter must not log or persist raw credentials.

3. **Create your adapter file:**
   ```
   python/src/open_brain/ingest/adapters/<source_name>.py
   ```
   Implement the three Protocol methods: `name`, `list_recent()`, `ingest()`.
   Optionally implement `credentials()` if the source needs secrets.

4. **Register the adapter:**
   Import and call `register()` in `python/src/open_brain/ingest/adapters/__init__.py`.

5. **Choose a domain type for the memory:**
   See [Domain Metadata Schemas](domain-metadata-schemas.md) to pick the right `type` value
   (`person`, `meeting`, `event`, `decision`, …) and which metadata fields to populate.

6. **Link to people:**
   After calling `save_memory()`, create typed relationships using `create_relationship()`.
   See [Typed Relationships](typed-relationships.md) for valid `link_type` values:
   - Meeting + attendees → `attended_by`
   - Transcript mention of a person → `mentioned_in`

7. **Verify with the query layer:**
   Use `people_discussed_with(person_id=…)` from [People-Aware Queries](people-aware-queries.md)
   to confirm your adapter's memories surface correctly in cross-client queries.

8. **Write a fixture** for your source type following the pattern in `tests/fixtures/` so future
   refactors can validate adapter output without live credentials.

---

## Related Docs

- [Architecture Overview](../architecture.md) — system-wide context
- [Capture Router](capture-router.md) — how memories are routed to the right type on ingest
- [Entity Extraction](entity-extraction.md) — how Haiku 4.5 extracts people/place metadata

---

*This hub was created as part of cr3.17 (Docs consolidation). If you add a new people-aware
feature, update this document and add a row to the Feature Docs table above.*
