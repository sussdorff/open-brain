# Plan: Persist manual session-learning review decisions

Bead: `open-brain-kr3e`

## Context

The analyzer is intentionally read-only for memories, but reviewed derived clusters need durable operator state. The state belongs to the exact recurrent claim grouping, not to an entire session summary.

## Approaches Considered

### A. Mutate source-memory priority or lifecycle state

Rejected. One session summary contains several claims, so memory-level state is too coarse and would also couple review to recall ranking.

### B. Dedicated append-only cluster review ledger

Selected. A deterministic source-set key survives LLM wording and ordering changes. Each manual decision is appended with its evidence snapshot and reviewer metadata. Analysis reads the latest record for each key and separates reviewed from active items. Duplicate keys in one analysis fail open.

### C. Local JSON state beside the CLI

Rejected. It would not be shared across clients or deployments, would be difficult to audit, and would not work consistently through authenticated MCP.

## Developer Decisions

- Review keys use the transparent format `session-learning:v1:<sorted comma-separated source IDs>`.
- The table is append-only so a later reclassification preserves history; analysis uses the latest row per key.
- `accept`, `covered_obsolete`, `project_only`, and `dismiss` are classifications only. None performs promotion or mutation outside the ledger.
- A review write requires the key, decision, reason, and canonical-learning snapshot. Source IDs are parsed and validated from the key.
- The authenticated user ID is stored when OAuth provides it; direct and API-key callers may have a null reviewer.
- The analyzer reads review records without retrieval search and keeps `read_only=true` to mean memory-read-only. It reports `review_ledger_writes=false` for the analysis itself.
- Multiple clusters with the same key in one run are reported with `review_identity_conflict=true` and remain active even if a ledger decision exists.

## Implementation Tasks

1. Add failing tests for stable keys, validated review writes, queue partitioning, CLI dispatch, tool scope, and schema parity.
2. Add review dataclasses, validation, append/read queries, and runtime/bootstrap table definitions.
3. Add review keys to clusters and merge latest ledger records into report partitioning with collision-safe behavior.
4. Add the evolution-scoped MCP review tool and `ob learnings review` CLI command.
5. Update terminal rendering and operator documentation.
6. Run focused and complete verification, adversarial Opus review with one same-thread continuation, deploy, then manually mark the current production cluster.

## Test Plan

| Layer | Verification |
|---|---|
| Unit | Key order/membership invariants; decision/reason/key validation; report active/reviewed/ambiguous routing |
| SQL contract | Insert query touches only `session_learning_reviews`; latest-per-key read; runtime/bootstrap DDL assertions |
| CLI | Parser and remote tool dispatch for review; human analyzer output includes review keys and reviewed queue |
| MCP | Evolution scope exposure and runtime guard; reviewer identity forwarding |
| Integration | Existing real-Postgres bootstrap parity test when `DATABASE_URL` is available |
| Regression | Full non-integration pytest suite, Ruff, MyPy, and diff checks |
| Production | Before/after doctor and source snapshots; explicit `covered_obsolete` write; rerun queue invariant |

## Verification Commands

```bash
cd python
uv run pytest tests/test_session_learning_reviews.py tests/test_session_learning_analysis.py tests/test_cli.py tests/test_tools.py tests/test_scope_gated_tools.py tests/test_bootstrap_schema_parity.py -m 'not integration'
uv run ruff check src tests
uv run mypy src
uv run pytest -m 'not integration'
```

## Recommendation

Ready to implement. The append-only dedicated ledger is the smallest auditable design that preserves the manual-only rollout boundary and avoids destructive memory-level classification.
