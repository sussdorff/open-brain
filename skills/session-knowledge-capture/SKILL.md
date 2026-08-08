---
name: session-knowledge-capture
description: >-
  use when: Session Close or an explicit "what did you learn and save it" request
  must persist structured session evidence and inferred learnings to Open Brain.
  NOT for: offline session-learning analysis, Bead creation, full transcripts, or
  replacing legacy session_summary writers during adapter rollout.
  boundary: Open Brain capture_session_knowledge API with memory-write judge;
  not generic ob save, transcript backfill, or ccore session-close itself.
action_boundary:
  risk_class: external-side-effect
  effect_type: network
  proposal_schema: standard://judge-layer/proposals/action-proposal.v1
  judge: agent://judge-default
  requires_mandate: true
compatibility: {}
metadata: {}
---

# Session Knowledge Capture

Persist compact observed execution evidence separately from durable inferred
learnings through Open Brain `capture_session_knowledge`.

## Why keep compact `what_happened`

It anchors provenance, verification, decision context, timeline queries, and
next-session handoff. Value comes from observable outcomes and evidence, not
verbose implementation narration.

## Fields

- `what_happened`: outcomes, material decisions, verification, bounded handoff
- `decisions[]`: context-specific evidence (not automatic promotion)
- `what_was_learned[]`: reusable mechanisms/constraints/patterns; may be empty
- `unfinished_work[]`: returned only; never persisted; never creates Beads

## Examples

Session Close: call `capture_session_knowledge` with producer
`session-knowledge-capture`, `source_ref=agent-session:<harness>:<session-id>`,
compact `what_happened`, optional decisions/learnings, and unfinished work for
the producer handoff.

Explicit save: when asked "what did you learn and save it", capture only durable
inferred learnings with evidence; omit fabricated lessons; keep `what_happened`
compact when useful for provenance.

## Auth and scope

`capture_session_knowledge` requires the `memory` scope and a stable authenticated
actor on every supported path:

- Bearer OAuth: actor = token subject (`sub`)
- `x-api-key` (hooks/CLI): actor = `api-key:configured` (logical identity; never the raw key or a digest)
- URL token (`?token=`): actor = `url-token:<token-name>` (never the raw secret)

Direct Python callers must pass an explicit auditable `actor=`. Promotion and
other admin mutations intentionally reject API-key auth; do not weaken that.

## `ccore session-close` producer handoff

Do not edit the external `ccore session-close` repository here. During rollout it
may still write `type=session_summary` via existing writers. The adapter switch
is: keep that writer working, then point Session Close at
`capture_session_knowledge` with the same `agent-session:<harness>:<session-id>`
identity under a memory-scoped authenticated path (API key, URL token, or
Bearer). Content persistence still requires a Memory-Write Judge ALLOW.
