# Memory Promotion

Feature status: active (`memory-promotion.v1`).

Standard: `docs/standards/memory-promotion.md`.

Open Brain raises epistemic authority only through an allowlisted-admin, audited
promotion decision bound by a signed grant and origin attestation digest. Search
and wake-up attach ledger-backed promotion projections at read time; forged
metadata cannot elevate influence.

## Operator surfaces

- MCP `promote_memory_authority` (OAuth `admin` + `PROMOTION_ADMIN_USERS` + signed grant)
- MCP `get_memory_promotion_history` (ordinary `memory` scope)
- CLI `ob --json provenance history <memory-id>`

## Configuration

Set `PROMOTION_GRANT_SECRET` (32+ chars) to enable signed-grant verification.
Set `PROMOTION_ADMIN_USERS` to the comma-separated exact subjects allowed to
mutate. Leave either empty to fail closed. Do not reuse `JWT_SECRET`. Restart
after rotating the secret or allowlist (`get_config` is process-cached).
Automated rule promotion stays disabled unless
`PROMOTION_AUTOMATIC_RULE_VERSION` names an exact configured rule.

Out-of-band signers should import `compute_reason_digest`,
`compute_evidence_digest`, and `compute_origin_attestation_digest` from
`open_brain.memory_promotion` (see the standard for UTF-8/JSON rules). Do not
hand-roll ambiguous digests. Integration tests use per-run UUIDs and never
delete append-only ledger rows on disposable DBs.
