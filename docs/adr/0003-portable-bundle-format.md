# ADR-0003: Portable Bundle Format

Date: 2026-07-13
Status: Accepted
Deciders: Malte Sussdorff

## Context

The portable knowledge bundle contract was shipped by bead `open-brain-jhg` after the
architecture review on 2026-07-11. This ADR ratifies the shipped implementation so future
backup, restore, and cutover work can depend on one documented contract instead of inferring
behavior from code and tests.

Two follow-on beads rely on this contract:

- `open-brain-ccd`, whose canonical-entity contract stores
  `metadata.canonical_entity: true` on memories that must round-trip as stable entities.
- `open-brain-m9e`, whose agent-first cutover verification consumes the portable bundle and
  round-trip verification report as its safety gate.

The source of truth for this ADR is the shipped code in
`python/src/open_brain/portable_backup.py`,
`python/src/open_brain/data_layer/postgres.py`, `python/src/open_brain/cli/main.py`, and the
behavioral tests in `python/tests/test_portable_backup.py`.

## Decision

The portable bundle format version is `1.1.0`. Readers accept bundles whose major version
matches the current reader major version: `_is_compatible_bundle_version()` treats any `1.x.y`
bundle as compatible with this reader. A minor version bump is used for backward-compatible
file or field additions. A major version bump is reserved for changes that break reader
compatibility.

The legacy `1.0.0` format remains readable. It used the same bundle structure except that it
omitted `sessions.jsonl`. During restore, a legacy `1.0.0` bundle fails with
`IncompatibleBundleVersionError` if any memory references a `session_id`, because that legacy
format cannot carry the referenced session rows.

### File Layout

A `1.1.0` bundle contains exactly these files:

- `manifest.json`
- `indexes.jsonl`
- `sessions.jsonl`
- `memories.jsonl`
- `relationships.jsonl`

There is no optional markdown export file in the shipped v1.1 contract. Markdown export was
considered in earlier planning, but it is not part of the bundle format ratified here.

Each JSONL file contains canonicalized records: records are projected onto an allowlisted field
set, sorted deterministically, and written as deterministic JSON.

`indexes.jsonl` fields:

- `id`
- `name`

`sessions.jsonl` fields:

- `id`
- `session_id`
- `index_id`
- `project`
- `started_at`
- `ended_at`
- `metadata`
- `status`
- `prompt_counter`

`memories.jsonl` fields:

- `id`
- `index_id`
- `session_id`
- `type`
- `title`
- `subtitle`
- `narrative`
- `content`
- `metadata`
- `priority`
- `stability`
- `access_count`
- `last_accessed_at`
- `created_at`
- `updated_at`
- `user_id`
- `importance`
- `last_decay_at`
- `session_ref`

`relationships.jsonl` fields:

- `id`
- `source_id`
- `target_id`
- `relation_type`
- `link_type`
- `confidence`
- `metadata`

### Manifest Schema

`manifest.json` contains:

- `bundle_format_version`
- `open_brain_schema_version`
- `created_at`, as an ISO-8601 UTC timestamp
- `record_counts`, with counts for `indexes`, `sessions`, `memories`, and `relationships`
- `files`, keyed by JSONL filename, with each value containing `sha256`
- `embedding_model`, with `name` and `dim`
- `contains_binaries`
- `contains_credentials`
- optional `source_label`

`restore_bundle()` verifies the bundle version, per-file SHA-256 hashes, and per-file record
counts before any database write. Version, hash, or count failures are fail-closed.

`export_bundle()` also fails closed with `ForbiddenExportContentError` if any exported record
contains a credential-shaped metadata key. The manifest asserts `contains_binaries: false` and
`contains_credentials: false`, so export rejects contradictory content instead of silently
shipping it.

### Restore Identity and Idempotency

Restore is id-preserving. Rows are inserted with the original primary-key `id` values from the
bundle. Restore does not allocate new ids and does not remap references. This is why indexes,
sessions, memories, and relationships can keep referencing each other by the same ids after
restore as before export.

Idempotency keys are:

- indexes: primary key `id`, inserted with `ON CONFLICT (id) DO NOTHING`
- sessions: primary key `id`, inserted with `ON CONFLICT (id) DO NOTHING`
- memories: primary key `id`, inserted with `ON CONFLICT (id) DO NOTHING`
- relationships: natural key `UNIQUE(source_id, target_id, relation_type)`, inserted with
  `ON CONFLICT (source_id, target_id, relation_type) DO NOTHING`

`metadata.content_hash` on a memory is a verification-only diagnostic. It is never used as the
restore deduplication key.

Restore is fail-closed against accidental merges into populated targets. If the target closure is
non-empty and its canonicalized records equal the bundle's canonicalized records, restore is a
no-op and returns `already_restored: true` with zero rows written. If the target closure is
non-empty and does not match the bundle, `RestoreTargetNotEmptyError` is raised.

The PostgreSQL implementation makes the emptiness check and write race-free by taking an
`EXCLUSIVE` lock on `memory_indexes`, `sessions`, `memories`, and `memory_relationships`, then
performing the existing-closure check, id-preserving inserts, and sequence repair inside the same
transaction.

### Round-Trip Verification Report

`verify_round_trip()` returns a report with this top-level shape:

- `bundle_format_version`
- `ok`
- `memories`
- `sessions`
- `relationships`
- `indexes`
- `canonical_entities`

The `memories` section contains:

- `expected`
- `restored`
- `content_hash_matches`
- `content_hash_mismatches`
- `record_hash_mismatches`

The `sessions` section contains:

- `expected`
- `restored`
- `missing`
- `extra`
- `record_mismatches`

The `relationships` section contains:

- `expected`
- `restored`
- `missing`
- `extra`
- `record_mismatches`

The `indexes` section contains:

- `expected`
- `restored`
- `missing`
- `extra`

The `canonical_entities` section contains:

- `expected`
- `restored`
- `preserved_ids`

The `ok` verdict is judged solely by full-record hashes, entity counts, missing/extra entity
sets, relationship edge diffs, index identity diffs, and canonical-entity id preservation.
`content_hash_matches` and `content_hash_mismatches` are informational only and do not gate
`ok`. A byte-perfect restore may carry a stored `metadata.content_hash` that was computed over
differently normalized source text; that is a source-data-integrity signal, not a
restore-fidelity signal.

Canonical entities are round-tripped through the memory metadata flag
`metadata.canonical_entity: true` and verified by `canonical_entities.preserved_ids`.

### CLI Surface

The command-line surface is:

- `open-brain export <path> [--source-label ...]`
- `open-brain restore <path> [--skip-embeddings]`
- `open-brain verify <path>`

Restore regenerates embeddings by default. `--skip-embeddings` sets `regenerate_embeddings` to
`False`.

## Consequences

- The bundle has one authoritative, deterministic v1.1 layout: one manifest plus four JSONL
  files.
- Restore preserves identity by retaining primary keys instead of creating a parallel identity
  namespace.
- Same-bundle restore reruns are idempotent, while differing populated targets fail closed.
- `content_hash` remains useful as a diagnostic without weakening the restore idempotency
  contract.
- Future compatible additions use a minor version bump within the `1.x.y` reader-compatibility
  line. Breaking changes require a major version bump.
