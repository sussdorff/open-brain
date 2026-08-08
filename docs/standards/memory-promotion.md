# Memory Promotion

Contract URI: `standard://open-brain/promotion/memory-promotion.v1`

Schema version: `memory-promotion.v1`

Maturity: active.

Python contract: `open_brain.memory_promotion`.

Related: `docs/standards/provenance-schema.md`,
`docs/standards/retrieval-contract.md`,
`docs/features/typed-relationships.md`.

Epistemic authority may move only through an explicit, auditable promotion
decision. The append-only `memory_promotion_events` ledger is the sole read-time
promotion authority. Actor-authored `metadata.retrieval_promotion`, Judge
`ALLOW`, learning-review acceptance, repetition counts, migration origin, model
output, and memory type/category never raise authority.

## Signing and verification ownership

| Surface | Ownership |
|---------|-----------|
| Grant minting | Out-of-band operator process using `PROMOTION_GRANT_SECRET`. Open Brain does not expose a signing endpoint or private-key surface. |
| Grant verification | Server-side `verify_promotion_grant` during allowlisted-admin promotion attempts. |
| Origin attestation | Server-computed digest of memory id + origin producer/source_ref + ingestion route + domain version. Bound into the grant; stored as digest-only on the ledger; recomputed at retrieval. |
| Retrieval elevation | Server seams load ledger projections and pass them into the retrieval contract. Direct library calls with no projection fail closed. |

`PROMOTION_GRANT_SECRET` is a separate optional secret (minimum 32 characters).
Verification fails closed when the secret is absent or too short. There is no
fallback to `JWT_SECRET`.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `PROMOTION_GRANT_SECRET` | empty | HS256 secret for promotion grants. Empty/short => fail closed. |
| `PROMOTION_ADMIN_USERS` | empty | Comma-separated exact authenticated subjects allowed to mutate. Empty => fail closed. |
| `PROMOTION_AUTOMATIC_RULE_VERSION` | empty | Exact configured automatic rule version. Empty disables automated promotion (`automatic_rule_disabled`). |

`get_config()` caches settings for the process lifetime. After rotating
`PROMOTION_GRANT_SECRET` or changing `PROMOTION_ADMIN_USERS`, restart the
server so the new values are loaded.

Key rotation: mint new grants only with the current secret. Previously accepted
ledger events remain authoritative via stored `grant_jti` / `grant_digest` /
`origin_attestation_digest`; rotation does not rewrite history. Rejected grants
signed with a retired secret fail verification and are audited as
`grant_invalid`.

## Allowed transitions

| From | To | Authorization |
|------|----|---------------|
| inferred / generated / observed / disputed | confirmed | Allowlisted OAuth admin + signed grant |
| inferred / generated / observed / confirmed | disputed | Allowlisted OAuth admin + signed grant |
| inferred / generated / observed / confirmed / disputed | superseded | Allowlisted OAuth admin + signed grant bound to `successor_memory_id` |

All other edges are rejected as `invalid_transition`. Every allowed transition
requires a signed grant. Dispute is bound exactly like any other transition.
Automatic rule remains disabled in v1.

## Signed grant claims

Grants are short-lived HS256 JWTs with:

- `iss` = `open-brain.promotion-grant.v1`
- `aud` = `open-brain.promotion-grant`
- `sub` / `actor` bound to the authenticated allowlisted admin subject
- `memory_id`, `from_label`, `to_label`
- `reason_digest`, `evidence_refs`, `evidence_digest`
- `policy_version` = `memory-promotion.v1`
- `successor_memory_id` exact bind (null/absent for non-supersession)
- `origin_attestation_digest` (64-char hex of server-issued attestation)
- unique `jti` (max 128 chars), `iat`, `exp`

### TTL invariants (manual ordered checks)

`authenticate_promotion_grant` applies these checks in order (PyJWT signature /
iss / aud verification only; expiry is **not** left to PyJWT auto-exp):

1. typed finite `iat`/`exp` — boolean, NaN, Infinity, overflow →
   `grant_time_invalid`
2. `exp > iat` — equality/negative windows → `grant_time_invalid`
3. `exp - iat <= MAX_PROMOTION_GRANT_TTL` (10 minutes) → `grant_ttl_exceeded`
4. future `iat` within `FUTURE_IAT_SKEW_SECONDS` (30s) → `grant_future_iat`
5. current-time `exp <= now` → `grant_expired`

### Out-of-band signer helpers (prefer importing these)

Signers should import the shared Python helpers rather than re-implementing
recipes:

| Helper | Input | Serialization |
|--------|-------|---------------|
| `compute_reason_digest(reason)` | UTF-8 reason string | SHA-256 hex of the raw UTF-8 bytes (no JSON wrap) |
| `compute_evidence_digest(evidence_refs)` | list of strings | SHA-256 hex of `json.dumps(refs, sort_keys=True, separators=(",", ":"), ensure_ascii=True)` — **array order preserved** |
| `compute_origin_attestation_digest(...)` | memory_id, producer, source_ref, ingestion_route | SHA-256 hex of canonical JSON object below |

Canonical JSON rules for attestation (and evidence list):

- UTF-8 string produced by `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=True)`
- dict keys sorted; list/array element order preserved (not sorted)
- non-ASCII escaped via `ensure_ascii=True`
- domain = `open-brain.promotion-origin-attestation.v1`
- `policy_version` = `memory-promotion.v1`

```python
from open_brain.memory_promotion import (
    compute_evidence_digest,
    compute_origin_attestation_digest,
    compute_reason_digest,
)

reason_digest = compute_reason_digest(reason)
evidence_digest = compute_evidence_digest(evidence_refs)
origin_attestation_digest = compute_origin_attestation_digest(
    memory_id=memory_id,
    producer=producer,          # provenance.origin.producer
    source_ref=source_ref,      # provenance.origin.source_ref
    ingestion_route=route,      # metadata.ingestion_route
)
```

Missing or `unknown` producer/source_ref/ingestion_route fail closed before
signing and before acceptance. Caller-provided boolean/string attestation is
never trusted. Metadata changes after the grant was minted fail closed at
acceptance and at retrieval (exact digest recompute).

Rejected when signature/algorithm/issuer/audience/claims mismatch, expired,
future-issued, subject/actor/transition/successor/attestation mismatch,
malformed evidence, overlong/malformed `jti`, or replayed `jti`. Raw grant
tokens and secrets are never logged or stored; only `grant_jti`,
`grant_digest`, and `origin_attestation_digest` appear in the ledger. Replay
rejection events store the verified `jti` when known (including the concurrent
accepted-jti unique-index race). Only the
`memory_promotion_events_accepted_grant_jti_uidx` unique constraint maps to
`grant_replay`; unrelated unique violations re-raise.

## Automatic rule path

Disabled by default. When `authorization_mode=automatic_rule`, v1 returns
`automatic_rule_disabled` unless a real configured exact rule version exists and
evidence references are stored. No repetition-count or learning-acceptance rule
is invented.

## Audit ledger

Every accepted and rejected attempt appends one immutable row with memory id,
actor, source/target state, reason, bounded evidence refs, policy/rule version,
grant jti/digest, origin attestation digest, decision/outcome, rejection code,
timestamp, and relationship id when applicable. Database triggers reject
`UPDATE`/`DELETE`. Accepted mutation, ledger event, and supersession relationship
are one transaction.

## Dispute and supersession

- `disputed` immediately removes instruction-grade influence at read time and
  still requires a signed grant.
- `superseded` preserves the old memory, requires grant-bound
  `successor_memory_id`, creates a `supersedes` edge (newer -> older), rejects
  self edges, cycles, successors that are already disputed/superseded, and
  ambiguous multiple active successors, and keeps a reconstructable chain.
- Rejection attempts remain visible in history. Evidence is never deleted.

## MCP and CLI

| Surface | Scope | Role |
|---------|-------|------|
| `promote_memory_authority` | OAuth `admin` + `PROMOTION_ADMIN_USERS` | Mutation (promote/dispute/supersede) |
| `get_memory_promotion_history` | ordinary `memory` scope | Read-only history |
| `ob --json provenance history <memory-id>` | CLI via public MCP tool | Stable JSON history |

API keys and URL tokens never receive promotion authority. Self-requested admin
scope without allowlist membership does not list or invoke the mutation tool.

## Retrieval linkage

Search and wake-up server seams fetch ledger-backed `PromotionProjection`
values keyed by memory id and pass them into `apply_retrieval_contract`.
Elevation requires:

1. current epistemic state + expected use satisfy the global floor
2. contract/profile allows high authority
3. the exact compiled section requires promotion and caps influence
4. current accepted ledger event is promoted, not disputed/superseded
5. exact recomputation match of `origin_attestation_digest`
6. audit reason/source emitted

Dispute/supersession and metadata/ledger/attestation mismatch win over earlier
grants. Identity and constraint may elevate under `claude-wake-up` only through
the ledger. Policy/system-instruction remain bounded by declared sections.
