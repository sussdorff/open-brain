# Session Knowledge Migration

Contract: `legacy-session-knowledge-migration.v1`
(`standard://open-brain/contracts/legacy-session-knowledge-migration.v1`)

Transforms historical `session_summary` and legacy `learning` memories into the
structured EKN session-knowledge model without losing source evidence, review
history, rollback ability, or retrieval quality.

This implementation **does not** switch `ccore session-close`. Live producers
remain on their existing writers until they adopt
`session-knowledge-capture.v1`.

## Operator flow

1. **Inventory / dry-run** (zero database writes):

```bash
ob --json session-knowledge-migration inventory
ob --json session-knowledge-migration dry-run > dry-run.json
```

The report includes route counts, unresolved/quarantine examples (bounded, no
source bodies), configured provider document/token/cost estimates, cohort
watermark + digest, catch-up plan, proposed operation ID, review-ledger
before digest/count, structured-record skip count, and `evidence_digest`. It
also binds one retrieval-control baseline per source. Apply derives each
bounded batch's baseline from exactly those approved source measurements, so a
full-cohort MAX score cannot make a later subset batch fail indefinitely.
`inventory` never calls a provider and therefore marks retrieval controls as
unmeasured. `dry-run` uses the configured embedding and rerank instruments when
provider credentials are present; without credentials it still inventories the
cohort, but its unmeasured baseline cannot pass the Human Decision Gate.

2. **Portable backup + restore verification** on a disposable target. Keep the
verified receipt (`verified: true` plus bundle digest).

3. **Human Decision Gate** (owner: Malte Sussdorff). Default outcome is
**BLOCK**. Allowed outcomes: `ALLOW`, `BLOCK`, `REVISE`, `ESCALATE`.

4. Only after ALLOW with the exact evidence bundle, run bounded apply:

```bash
ob --json session-knowledge-migration apply \
  --apply \
  --gate-evidence-file ./gate-allow.json \
  --dry-run-report-file ./dry-run.json \
  --operation-id <proposed_operation_id>
```

5. Status / reconcile:

```bash
ob --json session-knowledge-migration status --operation-id <uuid>
ob --json session-knowledge-migration reconcile --operation-id <uuid>
```

`--apply` without the full gate evidence returns a typed `status=blocked`
result and performs **zero writes**. There is no production shortcut.

## Human Decision Gate (exact)

Trigger: immediately before the first non-dry-run batch against production.

Minimum evidence (all required for ALLOW):

| Field | Rule |
|---|---|
| `decision` | Exact string `ALLOW` |
| `operation_id` | Exact proposed operation UUID (never synthesized by CLI flags) |
| `dry_run_report_digest` | Exact digest of the approved dry-run report |
| `cohort_digest` | Exact cohort digest from that report |
| `cohort_watermark` | Exact watermark object from that report |
| `batch_scope` | `{ "limit": 1..200, "after_id": N }` inside evidence |
| `backup_restore_receipt.verified` | `true` |
| `retrieval_control_baseline` | Measured `{ instrument, lexical, vector, rerank }` baseline; unmeasured or instrument mismatch blocks |
| `unresolved_acknowledgement` | `true` |
| `provider_metadata` | Must match configured embedding/rerank model + dimension |

`BLOCK` / `REVISE` / `ESCALATE` / missing / mismatched evidence → no writes.

Non-waivable: backup verification, no-silent-promotion, lineage preservation,
replay safety, initial no-hard-delete.

## Transition rules

- `session_summary` → compact `session_event` + optional `session_decision` +
  inferred `session_learning` candidates + unfinished-work **classification**
  (not persisted). Unsupported/empty shapes → `unresolved` / `quarantine`.
- Legacy `learning` → inferred evidence-only unless an existing confirmed label
  is preserved; expected use is never newly raised to instruction.
- Records already conforming to `session-knowledge-capture.v1` are counted and
  excluded. The historical cutover never clones or archives live EKN records.
- Never promote by type, repetition, review acceptance, or model output.
- Canonical `metadata.provenance.origin` is preserved.
- Inferred legacy origins are normalized to the canonical
  `{ producer, source_ref }` shape before persistence; inference details stay
  in migration provenance rather than inside `origin`.
- Derived records retain the source memory's project through their physical
  `memory_indexes` assignment, including idempotent replay.
- Review ledger rows are untouched; before/after digests are recorded.

## Semantic rebuild mapping ("Cohere / re-embed")

Operator language that says "Cohere refresh" or "re-embed" maps to the
repository's **configured** embedding/rerank abstractions (today: document/query
embeddings and optional rerank). Migration code paths inject adapters and record
provider/model/dimension/token/cost metrics without hard-coding vendor names in
orchestration. Existing configured providers remain unchanged.

Order: transform → embed → scoped reconcile/refine → final embed/relink →
retrieval verification. Only the migrated cohort is touched.

## Rollback

Initial apply archives/supersedes legacy rows **reversibly** only after derived
outputs and lineage are complete. Original content, provenance, relationships,
and review rows remain. Rollback metadata is stored under
`metadata.session_knowledge_migration.rollback`. No hard deletes.

## Safety

- Production apply is not authorized by this repository change alone.
- Dry-run writes nothing (no DB mutations, sequences, relationships,
  embeddings, lifecycle changes, or run-ledger rows).
- Receipts never include source contents or secrets.
- CLI inventory/status/reconcile, credential-free dry-run, and blocked apply
  suppress runtime DDL. Credential-free dry-run is explicitly unmeasured.
- CLI `--apply` never synthesizes `operation_id` / `batch_scope`. It validates
  the approved report and gate before provider calls, then uses the configured
  document embedding, rerank, retrieval-control, and scoped reconciliation
  adapters. Missing credentials block without writes.
- Archived/superseded legacy rows are excluded from `hybrid_search` and browse
  lifecycle filters; archival happens only after verification succeeds.
  Capture-inbox browse (`capture_status` set) may still surface archived
  lifecycle captures, but migration-superseded sources remain excluded.

## Real-Postgres integration (K1-06)

Marked `@pytest.mark.integration` in
`python/tests/test_session_knowledge_migration.py::TestMigrationSchemaIntegration`.

Root must provision a disposable Postgres+pgvector DB and run the class **twice**
on the same populated database:

```bash
export DATABASE_URL=postgresql://open_brain:test@localhost:55432/open_brain_test
cd python
uv run pytest -m integration \
  tests/test_session_knowledge_migration.py::TestMigrationSchemaIntegration -v
# immediately re-run against the same DB:
uv run pytest -m integration \
  tests/test_session_knowledge_migration.py::TestMigrationSchemaIntegration -v
```

Coverage includes runtime/bootstrap JSONB parity, full gated apply with persisted
content embeddings, hybrid_search exclusion of archived sources, same-op replay,
mutated-payload conflict, concurrent resume attempts, no hard delete, and
twice-rerunnable operation IDs. No production apply and no paid providers.
