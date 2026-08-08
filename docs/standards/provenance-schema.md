# Provenance Schema

Contract URI: `standard://open-brain/provenance/epistemic-provenance.v1`

Maturity: active.

Every Open Brain memory carries two independent provenance dimensions:

1. **Origin lineage** — who produced the record and from which source event.
2. **Epistemic provenance** — how strongly the content may be trusted and used.

They must not be collapsed into one field.

## Origin lineage (canonical)

Stored at `metadata.provenance.origin`:

```json
{
  "producer": "session-close",
  "source_ref": "agent-session:codex:<stable-session-id>"
}
```

| Field | Meaning |
|-------|---------|
| `producer` | Writer or pipeline that persisted the memory. |
| `source_ref` | Stable namespaced reference to the source event or artifact. |

Origin answers lineage and attribution. It does not grant instruction-grade
authority. See `docs/standards/session-summary-writers.md` for writer catalog
rules and the existing `origin_provenance_report` coverage cohorts.

## Epistemic provenance (versioned)

Schema version: `epistemic-provenance.v1`

Epistemic fields live alongside origin under `metadata.provenance`:

```json
{
  "origin": {
    "producer": "agent",
    "source_ref": "agent-session:codex:session-123"
  },
  "epistemic_version": "epistemic-provenance.v1",
  "source_label": "inferred",
  "expected_use": "evidence",
  "source_ref": "conversation://optional-citation"
}
```

| Field | Meaning |
|-------|---------|
| `epistemic_version` | Contract version for epistemic fields. |
| `source_label` | One of the six epistemic labels below. |
| `expected_use` | `evidence` or `instruction`. |
| `source_ref` | Optional epistemic citation reference (distinct from origin `source_ref`). |
| `authorization_ref` / `authorization_label` | Optional judge authorization fields. |

### Six labels

| Label | Meaning | Typical use |
|-------|---------|-------------|
| `observed` | Directly read from a source, tool result, file, conversation, or API response. | User instruction, account record, current file state. |
| `inferred` | Derived from observed evidence through model reasoning. | Intent classification, likely preference. |
| `generated` | Created by the actor or model, not independently verified. | Draft summary, generated claim. |
| `confirmed` | Verified by an authority or second independent source. | User confirmation, policy lookup. |
| `disputed` | Conflicting evidence exists or the claim is contested. | Contradictory statements. |
| `superseded` | Previously valid evidence replaced by newer evidence. | Updated record, expired instruction. |

### Legal expected-use combinations

| `source_label` | Legal `expected_use` |
|----------------|----------------------|
| `observed` | `evidence`, `instruction` |
| `confirmed` | `evidence`, `instruction` |
| `inferred` | `evidence` only |
| `generated` | `evidence` only |
| `disputed` | `evidence` only |
| `superseded` | `evidence` only |

Instruction-grade use requires `observed` or `confirmed`. Invalid or
authority-raising combinations are rejected before persistence.

## Default-on-write semantics

Every new memory write receives an epistemic classification.

- When a Memory-Write Judge proposal is allowed, the judged `source_label` and
  `expected_use` are persisted with `epistemic_version`.
- When no proposal is supplied, Open Brain applies the conservative default:
  `source_label=inferred`, `expected_use=evidence`,
  `epistemic_version=epistemic-provenance.v1`.
- Absence of a proposal never implies `confirmed` and never implies
  instruction-grade use. Raw callers cannot set `expected_use=instruction`
  without an allowed Memory-Write Judge outcome on that write (or preservation
  of an already instruction-grade prior state). Promotion authority is owned by
  a later bead and is not implied here.

## Coverage and legacy backfill

Epistemic coverage is reported separately from `origin_provenance_report`.

| Cohort | Meaning |
|--------|---------|
| `labeled` | Valid `source_label`, `expected_use`, and `epistemic_version`. |
| `unlabeled` | No epistemic fields present. |
| `partial` | Valid `source_label` present without `expected_use` (completable). |
| `ambiguous` | Use-only, invalid-label/no-use, illegal, or conflicting epistemic fields. |

Cohort counters are mutually exclusive and must sum to `total`. Invalid labels
are never classified as `partial`. SQL coverage classification uses the same
valid-label set as the Python classifier.

Backfill is idempotent and dry-run by default:

- Unlabeled rows receive inferred + evidence.
- Valid label-only (`partial`) rows may be completed with `expected_use=evidence`.
- Use-only, invalid-label, or otherwise ambiguous rows are counted and must not
  be promoted. Dry-run/apply reports include a capped `ambiguous_ids` list
  (cap `100`, with `ambiguous_ids_truncated` when truncated) so operators can
  repair rows without unbounded output.
- Ambiguous legacy rows fail closed on ordinary append/update until manually
  repaired. Session-summary append uniquely allows `repair_orphaned_use` for
  use-only rows (fills a conservative label); update does not auto-repair
  use-only or invalid-label state.
- Apply mode is bounded by an explicit batch `limit` (default 500, max 1000)
  and optional keyset cursor `after_id`. Dry-run may inventory the full cohort.
- Metadata-only epistemic backfill must not rewrite `updated_at` or other
  content recency/decay signals.
- Canonical `metadata.provenance.origin` is never rewritten.
- Hard deletes are never performed.

## Instruction authorization boundary

Instruction-grade writes are authorized only at the MCP/server judge boundary.
The server passes an internal non-wire `instruction_authorized` signal into the
data layer / session-summary append path. Caller-authored
`metadata.memory_write_judge.decision` is evidence for audit only and must not
be treated as trusted authorization. Ordinary direct data-layer callers remain
evidence-only.

## Shared validation surface

Python contract: `open_brain.epistemic_provenance`.

Used by write paths, retrieval consumers (`.4`), promotion (`.5`), and migration
paths so label semantics stay one shared source of truth. Read-time influence
enforcement is owned by the retrieval contract, not this schema.
