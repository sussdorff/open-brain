# Session Summary Writers

Canonical catalog of writers that produce `type='session_summary'` memories,
and the `metadata.source` marker each one must set.

## Why this exists

Session summaries flow in from multiple paths (agent, hook, API, backfill).
Without a disciplined `metadata.source` marker, we cannot:

- Dedup when two writers race on the same `session_ref`.
- Attribute provenance when auditing or reconstructing sessions.
- Catch regressions where a new writer is added without following the
  dedup / upsert contract.

A drifting set of source markers would re-open bug 2 (projekt-weiter
Observations-Fetch) from bead `open-brain-d4n` under a new name.

## Canonical allowlist

| `metadata.source`         | Writer                                        | Code location                                              |
|---------------------------|-----------------------------------------------|------------------------------------------------------------|
| `"session-close"`         | `core:session-close` agent (MCP save_memory)  | External — invoked via Agent tool at end of bead           |
| `"session-end-hook"`      | `POST /api/session-end`                       | `python/src/open_brain/server.py` → `summarize_transcript_turns(source="session-end-hook")` |
| `"transcript-backfill"`   | `regenerate_summaries` backfill utility       | `python/src/open_brain/regenerate.py` → `summarize_transcript_turns(source="transcript-backfill")` |
| `"worktree-session-summary"` | `POST /api/worktree-session-summary`       | `python/src/open_brain/server.py` (reserved marker; endpoint writes `type='session_summary'` without a source today — the marker is allowlisted so the endpoint may be upgraded without also updating the enforcer) |
| `None`                    | Legacy writers (e.g. `/api/session-capture`)  | `python/src/open_brain/server.py` (`_process_session_capture`) — no source key set → resolves to `None` |

The set is also codified at
[`ALLOWED_SESSION_SUMMARY_SOURCES`](../../python/src/open_brain/session_summary.py)
so Python code has a single source of truth.

## Adding a new writer

To introduce a new writer for `session_summary` memories:

1. Pick a short, hyphenated, present-tense marker (e.g. `"scheduled-rollup"`).
2. Add it to `ALLOWED_SESSION_SUMMARY_SOURCES` in
   `python/src/open_brain/session_summary.py`.
3. Add a row to the table above.
4. Write the memory through `summarize_transcript_turns(source=...)` when
   possible — it handles dedup and metadata consistently. If a direct
   `SaveMemoryParams(type="session_summary", metadata={"source": "...", ...})`
   call is unavoidable, make sure the marker and the allowlist match.

## Enforcement

Two tests protect the allowlist:

- **Behavioral** — `python/tests/test_session_summary_sources.py::test_session_summary_source_allowlist`
  exercises `summarize_transcript_turns` for every allowed source and
  verifies the resulting `metadata.source` round-trips through save.
- **AST scan** — `python/tests/test_session_summary_ast_allowlist.py`
  walks the AST of every module under `python/src/open_brain/` and fails
  the build if any literal `source` value at a session_summary write site
  is not in the allowlist. This is the exhaustive enforcer required by
  bead `open-brain-d8x`.

Both tests read from the `ALLOWED_SESSION_SUMMARY_SOURCES` frozenset, so
adding or renaming a marker needs one code change plus this doc.

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
