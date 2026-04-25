# People-Aware Memory

Hub document for the People-Aware Memory feature area of open-brain. Start here if you're new
to the system or want to understand how person-centric data flows through the stack.

---

## Architecture Overview

```
 +--------------------------------------------------------------+
 |  Sources                                                      |
 |  (MacWhisper transcripts, Emails, Matrix/WhatsApp)            |
 +---------------------------+----------------------------------+
                             | raw content
                             v
 +--------------------------------------------------------------+
 |  Ingest Adapters  (ADR-0001)                                  |
 |  IngestAdapter Protocol: list_recent(), ingest()              |
 |  Credential access via 1Password CLI  (ADR-0002)              |
 +---------------------------+----------------------------------+
                             | IngestResult
                             v
 +--------------------------------------------------------------+
 |  Ingest Pipeline                                              |
 |  save_memory() -> embed -> metadata extraction (Haiku 4.5)   |
 +-----------+----------------------------------+---------------+
             | memory row                       | relationship edges
             v                                  v
 +---------------------+         +----------------------------+
 |  Domain Schemas     |         |  Typed Relationships       |
 |  (cr3.1)            |         |  (cr3.10)                  |
 |  event, person,     |         |  attended_by, mentioned_in |
 |  meeting, decision  |         |  spawned_task, supersedes  |
 +---------------------+         +----------------------------+
             |                                  |
             +------------------+---------------+
                                v
 +--------------------------------------------------------------+
 |  People-Aware Query Layer  (cr3.9)                            |
 |  MCP tools: people_discussed_with, people_stale_contacts,    |
 |             people_mentions_window                            |
 |  Skill: people-query (Claude Code presentation layer)         |
 +--------------------------------------------------------------+
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

## Email Ingest CLI / MCP Tool

The `ingest_email_inbox` MCP tool and `ob ingest email` CLI command let you pull emails
directly from an IMAP inbox into open-brain memory.

### MCP Tool: `ingest_email_inbox`

Registers as a standard MCP tool on the server. Fetches the most recent N emails from
the configured IMAP INBOX, saves each as an `interaction` memory (with idempotency),
and returns a run summary.

```
ingest_email_inbox(
    config_ref: str,       # 1Password op:// reference for the IMAP password
    max_messages: int = 50 # How many recent emails to process (default 50)
) -> JSON
```

**Response:**
```json
{
  "ingested": 12,
  "skipped": 3,
  "run_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

- `ingested`: number of newly saved memories
- `skipped`: emails already ingested (idempotency hits)
- `run_id`: use with `ingest_rollback(run_id=...)` to undo the entire batch

**Example (in Claude):**
```
ingest_email_inbox(
    config_ref="op://Private/email-account/app-password",
    max_messages=100
)
```

### CLI Command: `ob ingest email`

```bash
ob ingest email --config <OP_REF> [--max-messages <N>]
```

**Arguments:**
- `--config OP_REF` (required): 1Password op:// reference for the IMAP password
- `--max-messages N` (optional, default 50): how many recent emails to process
- `--pretty` (global flag): pretty-print the JSON output

**Examples:**
```bash
# Ingest the last 50 emails (default)
ob ingest email --config "op://Private/email-account/app-password"

# Ingest the last 200 emails with pretty output
ob ingest email --config "op://Private/email-account/app-password" \
    --max-messages 200 --pretty
```

**Output:**
```json
{
  "ingested": 15,
  "skipped": 5,
  "run_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### How It Works

1. Connects to the IMAP server using credentials from the 1Password op:// reference.
2. Lists all UIDs in INBOX, takes the `max_messages` most recent.
3. For each UID: checks if `source_ref=imap:{server}:{uid}` already exists (idempotency).
4. New emails are summarized via Claude Haiku 4.5 (or stored raw if `EMAIL_STORE_RAW_BODIES=true`).
5. Each email is saved as an `interaction` memory in the `people` project.
6. All memories share the same `run_id` (auto-injected via `ingest_run` context manager).

### Configuration

The following environment variables control IMAP behavior:

| Variable | Description |
|----------|-------------|
| `IMAP_SERVER` | IMAP hostname (e.g. `imap.gmail.com`) |
| `IMAP_PORT` | IMAP port (default `993`) |
| `IMAP_USER` | IMAP login address |
| `IMAP_PASSWORD_OP` | Default 1Password op:// reference (overridden by `config_ref`) |
| `EMAIL_STORE_RAW_BODIES` | If `true`, skip LLM summarization and store raw body |
| `EMAIL_EXTRACTION_MODEL` | LLM model for summarization (default: `claude-haiku-4-5-20251001`) |

---

## Related Docs

- [Architecture Overview](../architecture.md) — system-wide context
- [Capture Router](capture-router.md) — how memories are routed to the right type on ingest
- [Entity Extraction](entity-extraction.md) — how Haiku 4.5 extracts people/place metadata

---

*This hub was created as part of cr3.17 (Docs consolidation). If you add a new people-aware
feature, update this document and add a row to the Feature Docs table above.*
