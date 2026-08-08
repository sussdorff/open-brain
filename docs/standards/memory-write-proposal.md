# Memory Write Proposal

Contract URI: `standard://open-brain/proposals/memory-write-proposal.v1`

Schema version: `memory-write-proposal.v1`

Maturity: active.

Owned at `docs/standards/memory-write-proposal.md` (repository pre-migration
standards owner). Installer/catalog migration is out of scope for this bead.

A Memory Write Proposal is the sole structured claim surface consumed by the
Memory-Write Judge before a judged `save_memory` persistence operation. Free-form
actor explanation cannot substitute for missing evidence or authorization fields.

Python contract: `open_brain.memory_write_proposal`.

Related: `docs/standards/provenance-schema.md`, `docs/features/memory-write-judge.md`,
generic Action Proposal at `standard://judge-layer/proposals/action-proposal.v1`.

## Required / optional matrix

| Field | Required on wire | Null allowed | Meaning |
|-------|------------------|--------------|---------|
| `intended_memory_content` | yes | no | Exact memory text proposed for persistence. |
| `category` | yes | no | `preference`, `fact`, `policy`, `lesson`, or `observation`. |
| `source_citation` | yes | no | `{ref, label}` evidence for the claim. |
| `authorization_basis` | yes (key present) | yes (`null`) | `{ref, label, granted_by?}` or `null`. |
| `expected_use` | yes | no | `evidence` or `instruction`. |
| `retention_scope` | yes | no | `session`, `project`, `personal`, or `team`. |
| `risk_flags` | yes | no (use `[]`) | Zero or more unique risk flags. |

Optional nested field:

| Field | Required | Meaning |
|-------|----------|---------|
| `authorization_basis.granted_by` | no | Who granted the authorization when known. Omit when absent. |

`authorization_basis` may be JSON `null` for wire compatibility with the legacy
optional path and eval cases. Structured producers must treat null authorization
as a preflight failure (`missing_authorization`) before judge submission.

Provenance labels on `source_citation.label` and `authorization_basis.label` are
the six epistemic labels from `.1` (`EpistemicLabel` / `ProvenanceLabel` alias):
`observed`, `inferred`, `generated`, `confirmed`, `disputed`, `superseded`.

Risk flags: `pii`, `secret`, `credential`, `policy-sensitive`,
`external-confidential`.

## Source locator grammar

`source_citation.ref` and `authorization_basis.ref` must match the shared
`SOURCE_LOCATOR_PATTERN` used by Python runtime `fullmatch`, Python
`jsonschema`, and ECMA-262. The pattern ends with `(?![\s\S])` (true
end-of-input) rather than `$`, so a trailing newline cannot validate.

Path segments are ASCII-only (`[A-Za-z0-9._-]` plus single spaces between
tokens). Non-ASCII path characters (for example `docs/café/note.md`) are
rejected at this boundary; scheme-qualified refs may still carry non-space
Unicode after `://` via `\S+`.

Accepted forms:

1. **Scheme-qualified**
   - Examples: `conversation://current/preference`, `agent://summary-draft`,
     `file://.env`, `policy://session/evidence-write`
2. **Namespaced identifier**
   - Example: `agent-session:codex:session-123`
3. **Path-like** (slash-separated ASCII segments; multi-word segments allowed):
   - File with extension: `docs/adr/0002-credentials-and-privacy.md`
   - Deep path: `python/src/open_brain/session_summary.py`
   - Multi-word path form: `docs/meeting notes/decision.md`

Rejected examples (failure code `vague_source`):

- generic single token: `somewhere`, `unknown`
- non-locator slash token: `n/a`
- actor prose: `user said so earlier today`, `the conversation`
- leading/trailing whitespace or trailing newline on an otherwise valid locator
- non-ASCII path segments: `docs/café/note.md`

Actor prose is never treated as evidence or authorization.

## Validation versus judge policy

1. **Schema validation** (`parse_memory_write_proposal` and
   `memory_write_proposal_json_schema`) are kept acceptance-equivalent for the
   reviewed corpus. Validation is deterministic and runs before model-reasoned
   judgment or persistence. Issues are machine-readable
   (`code`, `field`, `message`).
2. **`proposal_preflight_issues`** returns schema issues when present; when the
   proposal is schema-valid it also reports producer advisories
   (`missing_authorization`, `illegal_epistemic_combination`). It does not apply
   judge ALLOW/BLOCK/REVISE/ESCALATE outcome policy.
3. **Judge outcome policy** remains in `open_brain.memory_write_judge` and must
   not be duplicated here. Schema failures attach structured `issues` on
   `JudgeOutcome` and on the `save_memory` rejection payload.

## Failure catalog

| Code | Layer | Typical trigger |
|------|-------|-----------------|
| `missing_required_field` | schema | One issue per missing required key. |
| `unexpected_field` | schema | Unknown top-level or nested key. |
| `invalid_intended_memory_content` | schema | Empty or whitespace-only content. |
| `invalid_category` | schema | Category not in the enum. |
| `invalid_expected_use` | schema | Use not `evidence` or `instruction`. |
| `unclear_retention` | schema | Retention scope not in the enum. |
| `invalid_source_citation` | schema | Missing/invalid source object or label. |
| `vague_source` | schema | Ref fails the source-locator grammar. |
| `invalid_authorization_basis` | schema | Auth object malformed when not null. |
| `invalid_risk_flags` | schema | Non-array, unknown, or duplicate flag. |
| `invalid_proposal_type` | schema | Top-level value is not an object. |
| `missing_authorization` | preflight / judge | `authorization_basis` is `null`. |
| `illegal_epistemic_combination` | preflight | Source label cannot legally request the expected use. |
| `secret` / `credential` risk | judge | Risk-sensitive persistence blocked. |
| `policy-sensitive` | judge | Requires confirmed source and authorization for instruction/policy. |
| `pii` | judge | Team retention revised toward personal by default. |

## Compatibility

- `save_memory(proposal=...)` keeps the existing seven-field wire shape.
- Canonical `raw_proposal_payload()` omits `granted_by` when absent and validates
  against the public JSON Schema.
- Agent-owned and new structured producers construct proposals from
  `open_brain.memory_write_proposal`.
- Legacy no-proposal writes remain supported and stay evidence-only under the
  epistemic defaults from `open-brain-ekn.1`.
- Existing imports from `open_brain.memory_write_judge` continue to work without
  a restrictive `__all__`.

## Validated examples by category

### preference

Outcome: valid

```json
{
  "intended_memory_content": "User prefers concise status updates for open-brain sessions.",
  "category": "preference",
  "source_citation": {"ref": "conversation://current/user-preference", "label": "observed"},
  "authorization_basis": {"ref": "conversation://current/user-preference", "label": "observed", "granted_by": "user"},
  "expected_use": "instruction",
  "retention_scope": "personal",
  "risk_flags": []
}
```

### fact

Outcome: valid

```json
{
  "intended_memory_content": "OpenBrain stores session summary writer markers in ALLOWED_SESSION_SUMMARY_SOURCES.",
  "category": "fact",
  "source_citation": {"ref": "python/src/open_brain/session_summary.py", "label": "confirmed"},
  "authorization_basis": {"ref": "policy://project-memory/code-facts", "label": "confirmed", "granted_by": "repo-policy"},
  "expected_use": "instruction",
  "retention_scope": "project",
  "risk_flags": []
}
```

### policy

Outcome: valid

```json
{
  "intended_memory_content": "Credentials must never be persisted to OpenBrain memory.",
  "category": "policy",
  "source_citation": {"ref": "docs/adr/0002-credentials-and-privacy.md", "label": "confirmed"},
  "authorization_basis": {"ref": "docs/adr/0002-credentials-and-privacy.md", "label": "confirmed", "granted_by": "adr"},
  "expected_use": "instruction",
  "retention_scope": "project",
  "risk_flags": ["policy-sensitive"]
}
```

### lesson

Outcome: valid

```json
{
  "intended_memory_content": "Inferred lesson: run focused tests before full non-integration tests.",
  "category": "lesson",
  "source_citation": {"ref": "agent://test-plan-inference", "label": "inferred"},
  "authorization_basis": {"ref": "policy://project-memory/lessons", "label": "observed", "granted_by": "repo-policy"},
  "expected_use": "evidence",
  "retention_scope": "project",
  "risk_flags": []
}
```

### observation

Outcome: valid

```json
{
  "intended_memory_content": "Generated summary: the save path should preserve provenance metadata.",
  "category": "observation",
  "source_citation": {"ref": "agent://summary-draft", "label": "generated"},
  "authorization_basis": {"ref": "policy://session/evidence-write", "label": "observed", "granted_by": "system"},
  "expected_use": "evidence",
  "retention_scope": "session",
  "risk_flags": []
}
```

## Boundary failure examples

### missing authorization

Outcome: preflight:missing_authorization

```json
{
  "intended_memory_content": "User likely prefers detailed writeups.",
  "category": "preference",
  "source_citation": {"ref": "agent://style-inference", "label": "inferred"},
  "authorization_basis": null,
  "expected_use": "evidence",
  "retention_scope": "personal",
  "risk_flags": []
}
```

### vague source

Outcome: parse:vague_source

```json
{
  "intended_memory_content": "Something important happened in the meeting.",
  "category": "observation",
  "source_citation": {"ref": "somewhere", "label": "observed"},
  "authorization_basis": {"ref": "conversation://current", "label": "observed", "granted_by": "user"},
  "expected_use": "evidence",
  "retention_scope": "session",
  "risk_flags": []
}
```

### unclear retention

Outcome: parse:unclear_retention

```json
{
  "intended_memory_content": "Keep this fact indefinitely without a retention class.",
  "category": "fact",
  "source_citation": {"ref": "conversation://current/fact", "label": "observed"},
  "authorization_basis": {"ref": "conversation://current/fact", "label": "observed", "granted_by": "user"},
  "expected_use": "evidence",
  "retention_scope": "forever",
  "risk_flags": []
}
```

### illegal epistemic combination

Outcome: preflight:illegal_epistemic_combination

```json
{
  "intended_memory_content": "Generated draft treated as standing instruction.",
  "category": "preference",
  "source_citation": {"ref": "agent://draft", "label": "generated"},
  "authorization_basis": {"ref": "conversation://current", "label": "observed", "granted_by": "user"},
  "expected_use": "instruction",
  "retention_scope": "personal",
  "risk_flags": []
}
```

### privacy (pii)

Outcome: judge:REVISE/risk

```json
{
  "intended_memory_content": "User's private phone number is available in the CRM.",
  "category": "fact",
  "source_citation": {"ref": "crm://person/phone", "label": "observed"},
  "authorization_basis": {"ref": "policy://personal-memory/pii", "label": "confirmed", "granted_by": "privacy-policy"},
  "expected_use": "evidence",
  "retention_scope": "team",
  "risk_flags": ["pii"]
}
```

### secrets

Outcome: judge:BLOCK/risk

```json
{
  "intended_memory_content": "API token begins with sk-live-redacted.",
  "category": "fact",
  "source_citation": {"ref": "terminal://env-output", "label": "observed"},
  "authorization_basis": {"ref": "conversation://current/save-request", "label": "observed", "granted_by": "user"},
  "expected_use": "evidence",
  "retention_scope": "personal",
  "risk_flags": ["secret"]
}
```

### policy-sensitive without confirmation

Outcome: judge:ESCALATE/policy

```json
{
  "intended_memory_content": "Agents may promote inferred memories to confirmed after one observation.",
  "category": "policy",
  "source_citation": {"ref": "conversation://current/policy-idea", "label": "observed"},
  "authorization_basis": {"ref": "conversation://current/policy-idea", "label": "observed", "granted_by": "user"},
  "expected_use": "instruction",
  "retention_scope": "project",
  "risk_flags": ["policy-sensitive"]
}
```

## Producer guidance

```python
from open_brain.memory_write_proposal import (
    build_memory_write_proposal,
    proposal_preflight_issues,
    raw_proposal_payload,
)

proposal = build_memory_write_proposal(
    intended_memory_content="...",
    category="observation",
    source_citation={"ref": "agent-session:codex:session-123", "label": "observed"},
    authorization_basis={
        "ref": "policy://session/evidence-write",
        "label": "observed",
        "granted_by": "system",
    },
    expected_use="evidence",
    retention_scope="session",
    risk_flags=[],
)
payload = raw_proposal_payload(proposal)
assert proposal_preflight_issues(payload) == []
# then: save_memory(text=payload["intended_memory_content"], proposal=payload, ...)
```

Do not import `open_brain.memory_write_judge` solely to build proposals. Use the
public module above; let the server/judge apply outcome policy.
