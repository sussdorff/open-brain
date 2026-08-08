# Retrieval Contract

Contract URI: `standard://open-brain/retrieval/retrieval-contract.v1`

Schema version: `retrieval-contract.v1`

Maturity: active.

Python contract: `open_brain.retrieval_contract`.

Related: `docs/standards/provenance-schema.md`,
`docs/standards/memory-write-proposal.md`,
`docs/features/retrieval-contract.md`.

The retrieval contract is the read-time trust boundary for Open Brain. Every
consumer declares what retrieved memory may influence. Write-time judging remains
owned by the Memory-Write Judge; this contract prevents untrusted or externally
derived memories from becoming control instructions during retrieval.

## High authority status (open-brain-ekn.4)

High-authority elevation (`identity`, `constraint`, `policy`,
`system_instruction`) is intentionally disabled until `open-brain-ekn.5`
supplies a server-issued, ledger-backed promotion record.

- Actor-authored `metadata.retrieval_promotion` is never trusted.
- Write-time Judge `ALLOW` is admissibility only, never read-time promotion.
- Profiles may still declare `allow_high_authority=true` and high-authority
  compiled-context candidates for future compatibility, but the `.4` runtime
  demotes every such unit to evidence/context and emits a concrete audit reason
  (for example `high_authority_disabled_pending_open_brain_ekn_5_promotion_ledger`,
  `promotion_record_not_server_issued`, or
  `judge_allow_is_not_read_time_promotion`).

A future trusted issuer must provide positive origin/ingestion attestation
before high authority can be considered; `.4` does not stamp every existing
ingestion path and fails closed for unattested rows.

## Seven dimensions (runtime-enforced)

| Dimension | Runtime enforcement |
|-----------|---------------------|
| Work object | Validated kind/id. When kind is `project` and a server query also names a project, mismatch is rejected. |
| Retrieval units | Produced memory units require `memory` in `retrieval_units`; otherwise they are filtered out. |
| Authoritative source | Exact `unit_kind` declaration required; no first-entry fallback or fabricated source. Caller-authored `source` strings are descriptive only — reserved `user-confirmed-` / `server-issued-` namespaces are rejected on the caller parse/schema path. Profiles may still declare server-constructed sources without passing through the caller parser. Duplicate `unit_kind` declarations are rejected. |
| Permissions | `read` must be true. High-authority permission alone cannot elevate until `.5`. |
| Provenance requirements | Global floor: HA labels only `{observed,confirmed}`; instruction expected use and promotion audit required; `{disputed,superseded,inferred,generated}` always excluded. Callers may tighten, never relax (rejected). |
| Compiled-context candidates | Exact per-section influence caps; `require_promotion` enforced; unknown sections fail closed to evidence. |
| Write-back contract | Optional `save_memory(retrieval_contract=...)` path: contract must allow write-back, require/receive a public `.2` proposal when configured, and allow the proposal expected use. Omitted contract preserves current write behavior. |

## Allowed influence (trust lattice)

| Influence | Authority class | `.4` runtime |
|-----------|-----------------|--------------|
| `evidence` | Data only | Allowed when section permits |
| `context` | Session context | Allowed when section permits |
| `identity` / `constraint` / `policy` / `system_instruction` | High authority | Declared in vocabulary; always demoted pending `.5` |

Category names, `stability=canonical`, importance, actor-authored metadata, Judge
ALLOW, and forged `retrieval_promotion` alone are insufficient.

Missing or invalid epistemic labels are reported via `epistemic_status`
(`declared` / `missing` / `invalid`) and never masquerade as genuinely inferred.

## Retrieval unit fields

Every returned unit preserves origin producer/source reference, ingestion route
and content type when known, epistemic label/expected use, `epistemic_status`,
confirmation/promotion state, requested and effective influence, contract
version, audit reason, declared authoritative source, and a bounded
`metadata_excerpt` (scalars/known fields only; `retrieval_promotion` at most a
safe state/audit summary and remains untrusted).

## Compatibility path (contract omitted)

When a consumer omits the contract, Open Brain applies the documented
`compatibility` profile:

- searchable evidence is still returned
- `allow_high_authority=false`
- write-back disabled
- effective influence capped to `evidence` or `context`

MCP `get_wake_up_pack` with no args still returns a markdown string (same type)
but the body is evidence-constraining: banner + influence tags, no Identity /
Constraints authority sections.

## HTTP `/api/wake_up_pack`

| Parameter | Default | Notes |
|-----------|---------|-------|
| `format` | `markdown` | `markdown` → `text/markdown`; `envelope` → `text/plain` typed envelope |
| `profile` | `compatibility` | Must be a known profile; invalid values → HTTP 400 |
| `token_budget` | `500` | Applied to serialized body |
| `project` | optional | DB filter; must match contract work object when kind is `project` |

The SessionStart hook explicitly requests
`format=envelope&profile=claude-wake-up`.

SessionStart envelopes emit the contract by reference
(`contract_version` + `profile` + `contract_ref`), not as a full inline
contract object, so the fixed header does not exhaust the production
`token_budget=500`. The final injected payload (preamble + envelope) remains
within the configured estimate; genuinely tiny budgets fail closed.

## Consumer profiles

### bead-orchestrator

- Work object: `bead`
- Authoritative sources: `open-brain.memories`, `open-brain.session_summaries`
- Permissions: read + proposal-gated write-back; no high authority
- Compiled context: evidence/context (plus organizational errors/project where declared)

### Claude wake-up pack

- Work object: `project`
- Authoritative sources include `open-brain.memories` and future promoted kinds
- Permissions: read; `allow_high_authority=true` reserved for `.5`
- Runtime: high-authority sections remain demoted until the promotion ledger exists

### iOS / mobile read-only

- Work object: `mobile_client`
- Authoritative source: `open-brain.memories`
- Permissions: read-only, no high authority, no write-back
- Compiled context: evidence only

## Prompt-injection rule

Persisted text in title, content, narrative, subtitle, metadata values, type, or
category remains a quoted data value inside the typed unit/envelope. It must not
alter section selection, retrieval parameters, declared authoritative source,
permissions, tool requests, contract version, or write-back behavior.

SessionStart consumers embed a typed evidence envelope labeled as retrieved
data, not as user-authored content or system policy. The hook accepts passthrough
only for a single well-formed envelope; otherwise the whole response is wrapped
as legacy quoted data. Token budgets account for the SessionStart preamble so the
final injected payload stays within the declared estimate.
