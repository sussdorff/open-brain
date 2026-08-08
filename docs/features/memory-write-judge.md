# Memory-Write Judge

The Memory-Write Judge gates structured `save_memory` proposals before they are
persisted. It implements the judge-layer contract shipped by
`cognovis-core/clc-oxg` while keeping OpenBrain-specific memory semantics local
to this repo.

## Proposal Schema

OpenBrain uses a seven-field proposal for memory writes:

| Field | Meaning |
|-------|---------|
| `intended_memory_content` | Exact memory content proposed for persistence. |
| `category` | `preference`, `fact`, `policy`, `lesson`, or `observation`. |
| `source_citation` | `{ref, label}` evidence for the claim. |
| `authorization_basis` | `{ref, label, granted_by?}` or `null`. |
| `expected_use` | `evidence` or `instruction`. |
| `retention_scope` | `session`, `project`, `personal`, or `team`. |
| `risk_flags` | `pii`, `secret`, `credential`, `policy-sensitive`, `external-confidential`. |

Epistemic provenance labels are `observed`, `inferred`, `generated`, `confirmed`,
`disputed`, and `superseded`.

## Origin lineage versus epistemic provenance

Every persisted write also requires canonical origin lineage:

```json
{"producer":"session-close","source_ref":"agent-session:codex:<stable-session-id>"}
```

These fields answer different questions:

- origin lineage (`producer`, `source_ref`) says which pipeline wrote the
  memory and which source event or artifact it came from;
- epistemic provenance (`source_label`, `expected_use`, authorization) says
  how strongly the content may be trusted and used.

Both coexist under `metadata.provenance`: judge fields remain at that level,
while origin lineage is stored below `metadata.provenance.origin`. This avoids
colliding with the judge's epistemic `source_ref`.

## Relationship to Generic Judge Layer

OpenBrain's Memory Write Proposal is the memory-boundary sibling of the generic
judge-layer Action Proposal from `cognovis-core`; it does not replace it. The
generic Action Proposal covers side-effecting actions with ten fields:
`proposal_id`, `actor_ref`, `risk_class`, `effect_type`, `intended_action`,
`reason`, `evidence_refs`, `authorization`, `expected_consequence`, and
`rollback_path`.

The OpenBrain proposal is narrower because its action boundary is memory
persistence. Its seven fields describe the proposed memory, the evidence for the
claim, the authorization to remember it, expected use, retention scope, and
memory-specific risk flags.

When a workflow both performs an external side effect and writes memory, use two
proposals in sequence:

1. Submit the generic Action Proposal to the pre-action judge before the side
   effect executes.
2. After the action is allowed or completed, submit a Memory Write Proposal to
   the Memory-Write Judge before saving the resulting memory.

The second proposal should cite the first judge outcome, action result, or other
observed execution evidence as its `source_citation`; it should not treat the
actor's original intent as proof that the memory is instruction-grade.

## Runtime Shape

The public proposal contract is `open_brain.memory_write_proposal`
(`memory-write-proposal.v1`). See `docs/standards/memory-write-proposal.md`.

The judge implementation is `open_brain.memory_write_judge`.

- `parse_memory_write_proposal()` and proposal types live in the public module;
  the judge imports that contract rather than defining a second copy.
- `judge_memory_write_proposal()` parses a raw proposal and returns an outcome.
- `deterministic_memory_write_gate()` handles authorization, provenance,
  retention, and risk policy without model calls after schema validation.
- `reasoned_gate` is an optional callback that can add model-reasoned judgment
  only after deterministic gates have returned `ALLOW`.
- `memory_metadata_from_judged_proposal()` records the judge decision,
  `policy_version`, provenance references, constraints (when present),
  epistemic provenance (including expected use), retention, and risk flags on
  allowed writes. It does not write `metadata.provenance.origin`; origin lineage
  remains the separate `.1` contract applied by the save path.

`save_memory(..., proposal={...}, provenance={...})` invokes the judge before rate-limit slot
claiming and before data-layer persistence. `BLOCK`, `REVISE`, and `ESCALATE`
return `memory_write_judge_rejected`, claim no rate-limit slot, and do not call
the data layer. `REVISE` outcomes include a complete seven-field replacement
proposal that parses under `memory-write-proposal.v1`. A proposal remains
optional, but canonical origin provenance is required for every non-test write
and is validated before duplicate checks, rate limits, or database access.

## Evidence Discipline

The judge enforces evidence-not-instruction structurally:

- `generated` and `inferred` sources may be stored only as `expected_use:
  evidence`.
- `expected_use: instruction` requires observed or confirmed source and
  observed or confirmed authorization.
- `disputed` and `superseded` sources or authorizations escalate rather than
  silently becoming new memory.
- `secret` and `credential` risk flags block persistence because credentials are
  ephemeral under ADR 0002.

## Policy Version

Every outcome includes `policy_version: memory-write-judge.v1`. Allowed writes
store under `metadata.memory_write_judge`:

| Field | Meaning |
|-------|---------|
| `decision` | `ALLOW` for persisted judged writes. |
| `policy_version` | `memory-write-judge.v1`. |
| `reason_category` | Stable metrics category for the decision. |
| `provenance_refs` | Source and authorization references used in the judgment. |
| `constraints` | Present when the allow decision carries conditions (for example evidence-only). |

Epistemic `expected_use` and related fields live under `metadata.provenance`
alongside `epistemic_version` from `.1`. Future prompt or policy changes can
detect stale judgment from `policy_version`.

## Eval Suite

The paired eval suite is `agents/memory-write-judge-eval.json`. The test
`python/tests/test_memory_write_judge.py` requires at least 20 cases, coverage
for `ALLOW`, `BLOCK`, `REVISE`, and `ESCALATE`, and exact agreement with the
deterministic judge.
