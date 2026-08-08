# Test Fixture: session-knowledge-capture

## Test 1 — Session Close happy path

**Input:** Session Close for an authenticated coding session that finished a
focused repair: compact observed outcomes, one material decision with rationale,
one durable inferred learning with evidence, and unfinished producer handoff
items.

**Expected behavior:**
- Calls Open Brain `capture_session_knowledge` under a memory-scoped auth path
  (Bearer, configured API key, or named URL token)
- Sets producer `session-knowledge-capture` and
  `source_ref=agent-session:<harness>:<session-id>`
- Writes compact `what_happened` as observed execution evidence only (outcomes,
  verification, bounded handoff) without verbose implementation narration
- Persists decisions as context-specific observed evidence
- Persists learnings only as inferred evidence after Memory-Write Judge ALLOW
- Returns `unfinished_work` to the producer and does not persist it
- Does not create Beads or tracker state from unfinished work

**Pass criteria:**
- Observed (`what_happened`, decisions) stay separate from inferred learnings
- No fabricated learning is added when a durable lesson is present
- Unfinished work is absent from persisted memory rows
- Capture crosses the judge/auth boundary (memory scope + ALLOW before write)
- `what_happened` remains compact and justified by provenance/handoff value

## Test 2 — Explicit save with no durable learning

**Input:** User asks "what did you learn and save it" after a session that only
verified existing behavior and produced no reusable mechanism or constraint.

**Expected behavior:**
- Still may record compact `what_happened` when useful for provenance
- Leaves `what_was_learned` empty rather than inventing a lesson
- May return unfinished follow-ups to the producer without persisting them
- Does not treat completed-work narration as durable learning
- Still requires authenticated memory-scope access and judge ALLOW for any
  persisted rows

**Pass criteria:**
- Observed evidence is not relabeled as inferred learning
- No fabricated learning is persisted
- Unfinished work is not persisted
- Judge/auth boundary still applies to any write
- Any `what_happened` stays compact and provenance-oriented
