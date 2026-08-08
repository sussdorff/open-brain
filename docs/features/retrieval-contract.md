# Feature: Retrieval Contract

Status: active

Schema: `retrieval-contract.v1`

Standard: `docs/standards/retrieval-contract.md`

## Summary

Open Brain retrieval consumers declare a versioned retrieval contract so memory
can influence evidence/context without silently becoming identity, constraint,
policy, or system-instruction authority.

High authority is intentionally disabled until `open-brain-ekn.5` lands a
server-issued promotion ledger. Actor-authored promotion metadata and Judge
`ALLOW` are never read-time promotion sources.

## Behavior

- MCP `search`, `get_context`, and `get_wake_up_pack` accept an optional
  `retrieval_contract` parameter (or profile shorthand).
- Legacy `search` / `get_context` JSON shapes are preserved unless
  `include_retrieval_units` / `include_retrieval_contract` is set or a contract
  is supplied. When units are requested without a contract, the compatibility
  profile returns searchable evidence only.
- MCP `get_wake_up_pack` with no arguments still returns markdown (compatible
  media/type), but the body is evidence-constraining (banner, influence tags,
  no Identity/Constraints authority sections).
- Optional `save_memory(retrieval_contract=...)` enforces the write-back
  dimension; omitted preserves current write behavior.
- `/api/wake_up_pack` defaults to `format=markdown` and `profile=compatibility`.
  The SessionStart hook opts into `format=envelope&profile=claude-wake-up`.
- Token budgets apply to the final SessionStart payload (server body plus hook
  preamble). Envelope contracts are referenced, not inlined, so the default
  budget of 500 remains useful for unit payload.
- Caller-authored authoritative `source` strings cannot use reserved
  `user-confirmed-` / `server-issued-` namespaces.
- `get_context` uses the same typed `{error, message}` invalid-contract
  failure contract and project/work-object binding as `search`.
- Embedding and ranking behavior are unchanged.

## Surfaces

| Surface | Role |
|---------|------|
| `open_brain.retrieval_contract` | Machine-readable contract, units, profiles |
| `open_brain.wake_up` | Contract-aware wake-up compilation |
| `hooks/scripts/context_inject.py` | Claude/Codex SessionStart envelope injection |
| MCP retrieval/write tools | Optional contract + provenance-preserving units / write-back gate |

## Non-goals

- Memory-write judging (owned by the Memory-Write Judge)
- Server-issued promotion ledger (owned by `open-brain-ekn.5`)
- Changing Voyage embeddings or RRF ranking
- Owning external iOS/mobile application code
- Stamping every existing ingestion path with trusted attestation in this bead
