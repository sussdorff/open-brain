# open-brain Memory Status Conventions

Conventions for the `status` lifecycle metadata that controls when a memory
appears in retrieval, triage, and heartbeat operations.

Applies to: `ob-triage`, `memory-heartbeat`, any custom lifecycle worker.

## Status Lifecycle

Every memory has an implicit or explicit `metadata.status`:

| Status | Meaning | Set by |
|---|---|---|
| `open` (default) | Live memory, eligible for retrieval and triage | `save_memory` |
| `materialized` | Promoted to a Library primitive (skill / standard / agent / etc.) | `ob-triage` after forge writes the file |
| `discarded` | Decision recorded that this memory is no longer useful | `ob-triage` after merge/archive/delete |
| `archived` | Auto-archived by lifecycle (stale, low confidence, superseded) | `memory-heartbeat` provenance check |

`open` is the default; absence of a `status` key means `open`.

## Required Companion Fields

When transitioning out of `open`, write the reason at the same time:

| Status | Companion field | Example |
|---|---|---|
| `materialized` | `materialized_to` | `"cognovis-core/standards/python-uv-sync"` |
| `discarded` | `discard_reason` | `"merged into #1234"`, `"deleted — superseded"`, `"archived — transient session"` |
| `archived` | `stale_refs` + `confidence_score` | `confidence_score: low`, `stale_refs: ["/path/to/deleted-file.py"]` |

## Retrieval Filter

Triage and heartbeat operations filter out non-open memories:

```
# Pseudocode
where metadata.status is null OR metadata.status = 'open'
```

`materialized`, `discarded`, and `archived` memories remain in the database for
audit and history but do not surface in `/ob-triage` runs.

## Lifecycle Pipeline Contract

`mcp__open-brain__run_lifecycle_pipeline(scope=..., dry_run=...)`:

- `scope="recent"` — classify the newest eligible memories
- `scope=None` (omit) — classify eligible open memories
- `dry_run=true` — classify and return a read-only preview of the eligible
  scope, including memories that already have a proposal for the active policy;
  it never populates the review queue
- `dry_run=false` — persist proposals in `memory_lifecycle_actions`; never
  materialize, archive, write files, create beads, or delete memories; memories
  with an action for the active policy version are excluded

The ledger uses `(memory_id, policy_version)` as its idempotency key. Every
classification, including `keep`, creates at most one row for the active policy.
Non-dry runs reserve rows in the internal `classifying` state before invoking
the LLM, then move completed proposals to `staged`. If classification raises or
returns fewer proposals than were reserved, the unfinished owned rows move to
`failed` immediately. The one-hour reclaim remains a fallback for abrupt process
termination before cleanup can run. Review workflows may transition staged rows
to `resolved`, `needs_review`, or `failed`.
Failed classifications are eligible for reservation again on the next non-dry
run under the same policy; the existing ledger row is reused with a new ownership
token rather than duplicated.

`list_lifecycle_actions(state="staged")` is the read path for the review queue
across policy versions; callers may pass `policy_version` to restrict it.
After a human decision, `set_lifecycle_action_state` records the reviewed state
and a resolution note. `resolved` means the review workflow recorded an outcome;
it does not by itself prove or execute materialization. Neither tool executes the
proposed action.

### Scheduler Rollout Sequence

Use this order before enabling an unattended lifecycle trigger:

1. Run `run_lifecycle_pipeline(..., dry_run=true)` to inspect the preview. The
   queue is unchanged and `newly_staged_count` is zero.
2. Run `run_lifecycle_pipeline(..., dry_run=false)` to persist proposals.
3. Run `list_lifecycle_actions(state="staged")` and inspect the actual queue.
4. Review the staged records and record their outcomes as `resolved`,
   `needs_review`, or `failed`.
5. Enable the scheduled trigger only after the staged output is understood.

Heartbeat callers use `dry_run=false`. Triage callers always start with
`dry_run=true`, present results to the user, then execute approved actions
client-side (no second pipeline call needed for those).

## MCP Error Message Format

When any `mcp__open-brain__*` tool throws or returns unreachable, surface the
same message verbatim:

```
⚠️ open-brain MCP unreachable — heartbeat skipped. Reconnect with /mcp reconnect open-brain
```

Replace "heartbeat skipped" with the verb that fits ("triage skipped",
"migration aborted", etc.). The reconnect hint stays unchanged.

## update_memory Status-Write Patterns

After every action that ends a memory's open lifecycle, the executing skill
MUST call `update_memory`:

```
# After merging into another memory
update_memory(id=<merged-away>, metadata={
  'status': 'discarded',
  'discard_reason': 'merged into #<surviving-id>'
})

# After archiving
update_memory(id=<id>, metadata={
  'status': 'discarded',
  'discard_reason': 'archived — <reason>'
})

# After deletion (deletion happens via REST API; metadata write happens first)
update_memory(id=<id>, metadata={
  'status': 'discarded',
  'discard_reason': 'deleted — <reason>'
})

# After promotion to a Library primitive
update_memory(id=<id>, metadata={
  'status': 'materialized',
  'materialized_to': '<marketplace>/<primitive-type>/<name>'
})

# After heartbeat stale-detection
update_memory(id=<id>, type='archived', metadata={
  'confidence_score': 'low',
  'stale_refs': [...],
  'last_verified': '<iso-now>'
})
```

The status field excludes memories that have completed the knowledge lifecycle.
The lifecycle-action ledger independently prevents heartbeat and triage from
classifying the same memory twice under one policy version.

## Consumers

Skills that manage memory lifecycle declare:

```yaml
requires_standards:
  - open-brain/cli-routing
  - open-brain/memory-status-conventions
```
