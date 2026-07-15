# Plan: Require canonical origin provenance on every new memory write

## Context

Open Brain accepts memories through several agent and ingestion writers, but their origin metadata is fragmented. This change introduces one minimal canonical contract, preserves existing external identifiers, and adds a read-only report for planning a later legacy cleanup. It deliberately does not migrate historical rows or enable scheduled processing.

## Approaches Considered

### A. Keep provenance as an untyped metadata convention

This minimizes code changes, but each writer could continue to produce a different shape and the persistence boundary could not fail closed. Rejected because it repeats the current fragmentation.

### B. Add typed provenance to `SaveMemoryParams` and enforce it at persistence

Every runtime writer supplies `producer` and namespaced `source_ref`; the data layer validates and persists the canonical nested shape. A repository-wide AST test prevents future omissions. Recommended because it centralizes the invariant without changing the database schema.

### C. Add dedicated database columns and migrate all existing rows now

This would make SQL reporting direct, but couples a new-write invariant to a risky historical migration and requires provenance guesses. Rejected for this bead; legacy classification remains read-only.

## Break Analysis

- Direct MCP/API callers that omit provenance will receive a typed validation failure.
- Internal writers must derive stable source references before the fail-closed boundary lands.
- Session-summary append must reconcile existing and incoming provenance explicitly.
- Portable restore must remain byte-faithful and bypass new-write validation.
- The reporting path must not reuse recall helpers because they mutate usage and ranking state.

## Developer Decisions

- The only required origin fields are non-blank `producer` and a namespaced `source_ref`.
- Canonical storage is `metadata.provenance`; legacy `metadata.source` remains readable but is insufficient for new writes.
- Transcript and Second Brain writers retain their flat `source_ref` and dual-write the same identity canonically.
- `source_ref` is lineage, not a uniqueness or upsert key; many memories may share one session reference.
- Session Close records the logical session being closed. Bead IDs remain optional context.
- Append adopts incoming provenance only when the existing row is legacy, preserves an exact match, and rejects conflicts.
- Explicit `is_test` non-persistent calls and the id-preserving portable restore transaction are exempt.
- The legacy report exposes explicit, deterministic-backfill, inferred, and unresolved cohorts through a manual CLI/tool surface using read-only SQL.
- No scheduler, background lifecycle action, backfill, priority change, or historical mutation is part of this implementation.

## Step by Step Tasks

1. Add the typed provenance value, dedicated validation errors, early persistence-boundary validation, and canonical nested metadata persistence.
2. Define and test session-summary append reconciliation and preserve portable-restore and legacy-read compatibility.
3. Update every repository-owned `SaveMemoryParams` writer with stable canonical provenance and add a type-agnostic AST inventory test.
4. Add the server-side read-only provenance report plus documented CLI/tool access and prove it performs no writes or usage logging.
5. Update provenance documentation, run focused and full quality gates, perform the final Opus adversarial review, and address its findings.

## Test Plan

### Test Framework

- Unit and integration-style tests: pytest through `uv`
- Linter and formatter: Ruff through `uv`
- Type checking: project-configured command if present

### Unit Tests

| Behavior | Test surface | Command |
|---|---|---|
| Validation, persistence, append, restore compatibility | `python/tests/test_postgres.py`, `python/tests/test_portable_backup.py` | `uv run pytest python/tests/test_postgres.py python/tests/test_portable_backup.py` |
| API/tool failures and read-only report | `python/tests/test_tools.py`, CLI tests | `uv run pytest python/tests/test_tools.py python/tests/test_capture_inbox_cli.py` |
| Complete writer inventory and writer-specific identity | session, ingest, evolution, import tests | `uv run pytest python/tests/test_session_summary_ast_allowlist.py python/tests/test_session_summary_sources.py python/tests/test_worktree_summary.py python/tests/test_email_ingest.py python/tests/test_evolution.py python/tests/test_transcript_ingest.py python/tests/test_second_brain_import.py` |

### Expected Results

- Before change: writers can omit provenance; append drops incoming metadata; no safe legacy-origin report exists.
- After change: all new runtime writes fail closed without valid canonical provenance, compatible writers dual-write external identity, append reconciles provenance explicitly, and the report is demonstrably read-only.

## Means of Compliance

| # | Acceptance Criterion | MoC | Planned Evidence |
|---|---|---|---|
| 1 | Canonical persistence and early typed failure | unit | Data-layer and tool tests in `test_postgres.py` and `test_tools.py` |
| 2 | Every runtime writer supplies provenance | unit | Type-agnostic AST inventory plus writer-family tests |
| 3 | Session Close and append semantics | unit | Session-summary source and Postgres append tests |
| 4 | Legacy, external identity, and portable restore compatibility | integ | Postgres, portable backup, transcript, and Second Brain tests |
| 5 | Manual report is useful and read-only | smoke | Tool and CLI tests with a write-failing database guard |
| 6 | Contract, distinction, mapping, and rollout documented | doc | Updated session-summary writer and memory-write-judge docs |

## Verification Commands

```bash
uv run pytest python/tests/test_postgres.py python/tests/test_portable_backup.py
uv run pytest python/tests/test_tools.py python/tests/test_capture_inbox_cli.py
uv run pytest python/tests/test_session_summary_ast_allowlist.py python/tests/test_session_summary_sources.py python/tests/test_worktree_summary.py python/tests/test_email_ingest.py python/tests/test_evolution.py python/tests/test_transcript_ingest.py python/tests/test_second_brain_import.py
uv run ruff check python/src python/tests
uv run pytest python/tests
```

## Recommendation

Ready to implement.
