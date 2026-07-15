# Session Summary Writers

Canonical catalog of writers that produce `type='session_summary'` memories,
their legacy `metadata.source` marker, and their required origin provenance.

## Why this exists

Session summaries flow in from multiple paths (agent, hook, API, backfill).
Without disciplined origin metadata, we cannot:

- Dedup when two writers race on the same `session_ref`.
- Attribute provenance when auditing or reconstructing sessions.
- Catch regressions where a new writer is added without following the
  dedup / upsert contract.

A drifting set of source markers would re-open bug 2 (project-wide
observations fetch) from bead `open-brain-d4n` under a new name.

## Canonical origin contract

Every new persisted memory, including a session summary, carries:

```yaml
provenance:
  producer: session-close
  source_ref: agent-session:codex:<stable-session-id>
```

The data layer stores this under `metadata.provenance.origin`. `producer`
identifies the writer; `source_ref` identifies the source event or artifact and
must be a stable namespaced string. The parent `metadata.provenance` object also
retains epistemic citation fields written by the Memory-Write Judge. A bead ID
may be useful metadata, but it is not a substitute for the agent-session
reference and is not required.

## Session-summary writer catalog

| `metadata.source` | Canonical `producer` | Canonical `source_ref` | Writer |
|---|---|---|---|
| `session-close` | `session-close` | `agent-session:<harness>:<session-id>` | Session Close agent |
| `session-end-hook` | `session-end-hook` | `agent-session:claude:<session-id>` | `POST /api/session-end` |
| `transcript-backfill` | `transcript-backfill` | `agent-session:claude:<session-id>` | `regenerate_summaries` |
| `worktree-session-summary` | `worktree-session-summary` | caller session reference, or `worktree-session:<project>:<worktree>:<timestamp>` | `POST /api/worktree-session-summary` |
| legacy/absent | `session-capture` | `agent-session:claude:<session-id>` | `POST /api/session-capture` |

The set is also codified at
[`ALLOWED_SESSION_SUMMARY_SOURCES`](../../python/src/open_brain/session_summary.py)
so Python code has a single source of truth.

## Adding a new writer

To introduce a new writer for `session_summary` memories:

1. Pick a short, hyphenated, present-tense marker (e.g. `"scheduled-rollup"`).
2. Add it to `ALLOWED_SESSION_SUMMARY_SOURCES` in
   `python/src/open_brain/session_summary.py`.
3. Add a row to the table above.
4. Supply stable canonical provenance at the write site. Write through
   `summarize_transcript_turns(source=...)` when
   possible — it handles dedup and metadata consistently. If a direct
   `SaveMemoryParams(type="session_summary", metadata={"source": "...", ...})`
   call is unavoidable, make sure the marker, provenance, and allowlist match.

Session-summary append adopts incoming provenance when an existing row is
legacy, preserves an exact match, and fails with
`origin_provenance_conflict` when two different origins target the same
logical `session_ref`.

## Enforcement

Two tests protect the contract:

- **Behavioral** — `python/tests/test_session_summary_sources.py::test_session_summary_source_allowlist`
  exercises `summarize_transcript_turns` for every allowed source and
  verifies the resulting `metadata.source` round-trips through save.
- **AST scan** — `python/tests/test_session_summary_ast_allowlist.py` walks
  every module under `python/src/open_brain/`. It rejects unknown literal
  session-summary sources and any repository-owned `SaveMemoryParams` call
  without an explicit `provenance=` argument.

Both tests read from the `ALLOWED_SESSION_SUMMARY_SOURCES` frozenset, so
adding or renaming a marker needs one code change plus this doc.

The direct, id-preserving portable restore transaction is intentionally exempt:
it restores historical rows byte-for-byte instead of creating new knowledge.

## Manual rollout

Keep scheduled and lifecycle processing disabled. Roll out in this order:

1. Deploy the new write contract.
2. Smoke-test one beadless Session Close, one direct `ob save` with
   `--source-ref`, and one external ingestion path.
3. Run `ob provenance report` and inspect the explicit,
   deterministic-backfill, inferred, and unresolved counts.
4. Plan any historical cleanup separately; this report never backfills or
   mutates memories.

## Non-literal sources

The AST scan ignores non-literal arguments (e.g. variables, function
calls). That is intentional: the scanner cannot statically evaluate
them, and they are covered by the behavioral test. If a new writer must
pass `source` as a variable (e.g. chosen at runtime), constrain that
variable's possible values to `ALLOWED_SESSION_SUMMARY_SOURCES` inside
the writer itself — do not rely on the AST scan to catch drift.

## Related beads

- `open-brain-d4n` — introduced `summarize_transcript_turns` and the
  original behavioral allowlist test.
- `open-brain-d8x` — this hardening pass: AST scan + standards doc +
  single-source-of-truth constant.
