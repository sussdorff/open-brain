# ADR-0001: Ingest Adapter Interface

Date: 2026-04-24
Status: Accepted
Deciders: Malte Sussdorff

## Context

The open-brain memory system will receive content from multiple heterogeneous sources:
MacWhisper transcripts, email (IMAP/SMTP), and Matrix/WhatsApp-bridge messages. Each source
has different authentication models, data shapes, and polling semantics.

Before building any individual adapter, a shared interface must be defined so that:

- All adapters can be driven by a single orchestrator/scheduler without source-specific branching.
- New sources can be added without touching orchestration logic.
- Credential handling follows a uniform, secure pattern across all sources.
- Observability (run tracking via IngestRun, cr3.12) is threaded through every adapter call
  from the start.

## Decision

A shared `IngestAdapter` Protocol will be defined in
`python/src/open_brain/ingest/adapters/base.py`. The Protocol uses structural subtyping
(duck-typing via `typing.Protocol`) — no ABC inheritance is required.

Adapters are registered in a central `ADAPTERS: dict[str, IngestAdapter]` dictionary via a
`register()` helper that enforces name uniqueness at import time. The orchestrator always
looks up adapters through this registry rather than importing adapter classes directly.

## Protocol Shape

Pseudocode (documentation only — not executable Python):

```
Protocol IngestAdapter:
  name: str                                    # adapter identifier (unique, snake_case)
  list_recent(n: int) -> list[Ref]             # list N most recent items from the source
  ingest(ref: Ref, run_id: UUID) -> IngestResult  # ingest a single item identified by ref
  credentials() -> dict  (optional/default={}) # credential requirements (key → description)
```

- `Ref` is a source-specific opaque identifier (e.g. file path, message-id, event-id).
- `IngestResult` carries the ingested content, extracted metadata, and the `run_id` (see
  run_id Contract section below).
- `credentials()` is optional; adapters that need no credentials return an empty dict.

## Source-Adapter Registry Pattern

A module-level registry is maintained:

```
ADAPTERS: dict[str, IngestAdapter] = {}

def register(adapter: IngestAdapter) -> None:
    # Validates that adapter.name is unique in ADAPTERS.
    # Raises ValueError on name collision.
    # Inserts adapter into ADAPTERS under adapter.name.
```

Each adapter module calls `register(MyAdapter())` at import time. The orchestrator imports
`open_brain.ingest.adapters` (the package `__init__`) which in turn imports every adapter
submodule, triggering all registrations. This avoids any manual wiring in the orchestrator.

## Credential Strategy

- **Non-secret config** (URLs, ports, folder names): plain environment variables
  (`os.environ`), documented in `.env.example`.
- **Secrets** (API keys, passwords, tokens): retrieved at adapter startup via the
  1Password CLI: `op read op://VaultName/ItemName/field`. The adapter calls `op read` once
  on first use and caches the result for the process lifetime.
- **NEVER** store plaintext secrets in the repository, `.env` files committed to git, or
  any config file tracked by version control.
- Adapters must declare their credential requirements via `credentials() -> dict`
  (key = env var or op-path name, value = human-readable description) so that a setup
  command can validate the environment before the first run.

## run_id Contract

Every ingest operation is associated with an `IngestRun` record (defined in cr3.12).
The `run_id` (UUID) is created by the scheduler/orchestrator before any adapter is called
and must be propagated end-to-end:

1. Orchestrator creates an `IngestRun` and obtains `run_id`.
2. Orchestrator passes `run_id` to every `ingest(ref, run_id=run_id)` call.
3. Each adapter embeds `run_id` in the `IngestResult` it returns.
4. The data layer persists `run_id` alongside every memory record created from that result.

No adapter may create its own `run_id` or omit it from `IngestResult`. This threading enables
full observability: every memory can be traced back to the exact ingest run that produced it.

## Consequences

- **Loose coupling**: adapters implement the Protocol by structural subtyping; no shared base
  class or inheritance is required, keeping each adapter self-contained.
- **Credential uniformity**: the env-var + 1Password pattern applies consistently across all
  adapters, reducing the risk of accidental secret exposure.
- **Full observability**: mandatory `run_id` threading from orchestrator through every adapter
  call and into persisted memory records enables end-to-end traceability and simplifies
  debugging of ingest failures.
- **Easy extensibility**: adding a new source requires only implementing the Protocol and
  calling `register()` — zero changes to the orchestrator or existing adapters.
