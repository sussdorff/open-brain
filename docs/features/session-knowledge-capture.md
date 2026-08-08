# Session Knowledge Capture

Open Brain owns a versioned live capture boundary that stores compact observed
execution evidence separately from durable inferred learnings.

Contract: `session-knowledge-capture.v1`
(`standard://open-brain/contracts/session-knowledge-capture.v1`).

## Why this exists

Legacy Session Close producers write one `session_summary` prose record that can
mix completed work, decisions, unfinished work, and possible durable learning.
That lets producer-specific formats dictate memory semantics and makes "what
happened" compete with "what was learned" during retrieval.

Compact `what_happened` still matters: it anchors provenance, verification,
decision context, timeline queries, and the next-session handoff. That value
comes from observable outcomes and evidence, not full implementation narration.

## Capture shape

| Field | Persisted? | Epistemic default | Role |
|---|---|---|---|
| `what_happened` | Yes, as `session_event` | observed / evidence | `session_event` |
| `decisions[]` | Yes, as `decision` | observed / evidence | `session_decision` |
| `what_was_learned[]` | Yes, as `learning` when judged ALLOW | inferred / evidence | `session_learning` |
| `unfinished_work[]` | No | n/a | returned to producer only |

Every derived decision/learning links to its session event with typed
relationship `derived_from`.

### Compactness and empty capture

`what_happened` is a compact observed summary with a character bound of
**2000** characters (`MAX_WHAT_HAPPENED_CHARS`). Item texts are bounded to
1000 characters; decisions/learnings/unfinished lists are each capped at 20.

A wholly empty capture (no `what_happened`, decisions, or learnings) persists
nothing and returns `status=captured` with empty ids. When decisions or
learnings arrive without `what_happened`, a lineage anchor session_event is
stored (prefix `Session-knowledge lineage anchor for session `) so
`derived_from` links have a parent — without inventing a substantive
execution claim.

## API

MCP tool: `capture_session_knowledge(capture=...)`

Python entry point: `open_brain.session_knowledge.capture_session_knowledge`

Idempotency identity: `actor|producer|source_ref|schema_version`. The MCP tool
binds `actor` from the authenticated principal on Bearer (`sub`), API-key
(`api-key:configured`), and URL-token (`url-token:<name>`) paths. Direct
Python callers must pass an explicit auditable `actor`. Replay of the same
normalized payload returns the prior ids (including stored redacted
`judge_outcomes` and classification `issues`). A different payload under the
same identity — including a different rejected learning hash — returns
`session_knowledge_capture_conflict` and does not overwrite.

Capacity: one accepted new capture consumes one rate-limit operation; the daily
guard reserves the estimated row count. Replay/conflict/rejected/empty paths
reserve nothing.

## Judge and authority

Each persisted record is constructed as a Memory Write Proposal and judged
before write. Decision rationale is included in judged content (not a
searchable narrative bypass). Secrets/credentials are detected conservatively
on all persisted text and block. Session learnings are always
`source_label=inferred` / `expected_use=evidence` with
`instruction_authorized=False`; caller authority-raising fields are rejected.

## Retrieval

Filter by `metadata.session_knowledge.role` (`session_event`,
`session_decision`, `session_learning`). Observed/inferred session material
defaults to evidence influence and cannot enter policy, identity, constraint, or
system-instruction sections without an audited promotion.

## Compatibility

Existing `session_summary` writers remain supported during adapter rollout. See
`docs/standards/session-summary-writers.md` and the repo-local skill
`skills/session-knowledge-capture/SKILL.md` for the `ccore session-close`
producer handoff (documented here; that external repository is not modified).
