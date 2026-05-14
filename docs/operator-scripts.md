# Operator Script Promotion Policy

Reviewed for bead `open-brain-b6a` on 2026-04-30.

The `ob` CLI is for recurring human/operator workflows that can run against a
remote open-brain server. Scripts remain the right home for one-shot migrations,
developer checks, local batch jobs, and Claude Code hook internals.

## Promoted

| Source | Stable surface | Rationale |
|---|---|---|
| `scripts/merge_persons.py` | MCP tools `people_list` and `people_merge`; CLI commands `ob people list` and `ob people merge` | Person deduplication is a recurring operator workflow, but remote CLI installs do not have `DATABASE_URL`. The merge must therefore execute server-side. The root script now remains only as a checkout-local compatibility wrapper. |

## Kept As Scripts

| Area | Files | Decision |
|---|---|---|
| One-shot data migrations | `scripts/migrate_*.py`, `scripts/migrate-*.ts`, `scripts/delta-migrate.py`, `scripts/migrate-from-sqlite.ts` | Keep as scripts. They encode historical schema/data corrections and should not become long-lived user commands. |
| Local maintenance batches | `scripts/embed-missing.ts`, `scripts/prune-discoveries.ts`, `scripts/decay-priorities.ts`, `scripts/fleet-compact.py`, `scripts/triage_ccmem.py` | Keep as scripts until a specific workflow is reused enough to deserve MCP auth, rate limiting, and documentation. |
| Hook installation and test helpers | `scripts/install-hooks.sh`, `scripts/test_install_hooks.sh`, `scripts/run-nightly-tests.sh`, `scripts/test_nightly_schedule.sh` | Keep as repo/developer scripts. They operate on checkout state rather than a remote server API. |
| Server bootstrap | `python/scripts/create_user.py` | Keep as a server-local bootstrap helper. Promote later only if an authenticated admin API is introduced. |
| Hook runtime (Claude Code) | `hooks/scripts/*` | Keep as hook internals. Human-facing behavior belongs in skills or `ob`; hook plumbing should not be exposed as stable CLI. |

## Promotion Criteria

Promote a script into `ob` when all are true:

- A human/operator repeats the workflow across sessions.
- The workflow can be expressed through server-side MCP tools or existing HTTP APIs.
- Remote CLI users should not need filesystem paths, checkout state, or direct database credentials.
- The command has stable arguments, JSON output, and tests.

Keep a script when any are true:

- It is a one-shot migration or historical repair.
- It is coupled to Claude Code hook execution.
- It needs unchecked direct database access and has no authenticated server API yet.
- It is a developer-only test helper.
