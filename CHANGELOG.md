# Changelog

All notable changes to this project will be documented in this file.
## [unreleased]

### Bug Fixes

- *(open-brain-l5l)* Fix 3 regressions from Codex review
- *(open-brain-l5l)* Update provenance_check.py docstring to safe printf pattern

### Refactoring

- *(open-brain-l5l)* Extract memory-heartbeat inline code to plugin/scripts

## [0.13.1] - 2026-04-22

### Bug Fixes

- *(open-brain-pi9)* Add PEP 723 inline metadata to plugin scripts for tree-sitter dep

### Miscellaneous

- *(open-brain-pi9)* Update changelog
- Bump version to 0.13.1

## [0.13.0] - 2026-04-22

### Miscellaneous

- Bump version to 0.13.0

## [0.12.3] - 2026-04-22

### Bug Fixes

- *(open-brain-rxz)* Normalize index_id in content-hash dedup to match insert path

### Miscellaneous

- *(open-brain-rxz)* Update changelog
- Bump version to 0.12.3

## [0.12.2] - 2026-04-22

### Bug Fixes

- *(open-brain-g3z)* Address review findings — Go/Rust spec, cleanup dead code, type safety
- *(open-brain-2nr)* Replace local _importance_rank with interface.rank_importance

### Features

- *(open-brain-g3z)* Green — tree-sitter library integration for smart_* tools

### Miscellaneous

- *(open-brain-g3z)* Add tree-sitter-language-pack>=1.6.2 dependency
- *(open-brain-g3z)* Update changelog
- *(open-brain-2nr)* Update changelog
- Bump version to 0.12.2

## [0.12.1] - 2026-04-21

### Bug Fixes

- *(open-brain-7n0)* Guard materialize_archive against critical/high importance memories

### Miscellaneous

- *(open-brain-7n0)* Update changelog
- Bump version to 0.12.1

## [0.12.0] - 2026-04-21

### Miscellaneous

- Bump version to 0.12.0

## [0.11.0] - 2026-04-21

### Bug Fixes

- *(open-brain-9md)* Address review findings — batch recall decay, behavioral tests, last_decay_at in selects

### Documentation

- *(open-brain-9md)* Update architecture.md with importance-based decay mechanics and last_decay_at field

### Features

- *(open-brain-9md)* Importance-based memory decay with 24h race guard

### Miscellaneous

- Pre-merge CHANGELOG sync for open-brain-9md
- *(open-brain-9md)* Update changelog
- Bump version to 0.11.0

## [0.10.0] - 2026-04-21

### Bug Fixes

- *(open-brain-qrw)* Address review findings iteration 1
- *(open-brain-qrw)* Address codex adversarial findings
- *(open-brain-hhw)* Address review findings iteration 1

### Documentation

- *(open-brain-qrw)* Document dedup_mode=merge semantic deduplication in architecture.md

### Features

- *(open-brain-qrw)* Add auto-deduplication at store time (dedup_mode=merge)
- *(open-brain-hhw)* Green — wake_up.py + Memory.project_name + get_wake_up_memories (AK7/AK8/AK9/AK2)
- *(open-brain-hhw)* Green — get_wake_up_pack MCP tool + /api/wake_up_pack REST endpoint (AK1/AK5)
- *(open-brain-hhw)* Green — update context_inject.py to call /api/wake_up_pack (AK6)

### Miscellaneous

- *(open-brain-qrw)* Update changelog
- Pre-merge CHANGELOG sync for open-brain-hhw
- *(open-brain-hhw)* Remove unused rank_importance import
- *(open-brain-hhw)* Update changelog
- Bump version to 0.10.0

## [0.9.0] - 2026-04-21

### Bug Fixes

- *(open-brain-jpz)* Address review findings iteration 1

### Documentation

- *(open-brain-jpz)* Add importance axis to architecture.md memory fields table

### Features

- *(open-brain-jpz)* Green — importance contract & schema migration

### Miscellaneous

- *(open-brain-jpz)* Update changelog
- Update changelog
- Bump version to 0.9.0

### Testing

- *(open-brain-jpz)* Red — importance contract test suite

## [0.8.4] - 2026-04-21

### Miscellaneous

- Update changelog
- Bump version to 0.8.4

## [0.8.3] - 2026-04-21

### Miscellaneous

- Update changelog
- Bump version to 0.8.3

## [0.8.2] - 2026-04-21

### Miscellaneous

- *(open-brain-d8x)* AST-scan test for session_summary source allowlist
- *(open-brain-058)* Add source='worktree-session-summary' marker to session_summary writer
- *(open-brain-2co)* Bump cryptography, pygments, pyjwt, python-multipart to CVE-patched versions
- Bump version to 0.8.2

## [0.8.1] - 2026-04-21

### Miscellaneous

- *(open-brain-pfn)* Fleet-wide compact_memories run + decision doc
- Update changelog
- Bump version to 0.8.1

## [0.8.0] - 2026-04-21

### Miscellaneous

- Bump version to 0.8.0

## [0.7.1] - 2026-04-21

### Bug Fixes

- *(open-brain-v76)* Address review findings iteration 1

### Features

- *(open-brain-v76)* Green — transcript-based session-summary regeneration

### Miscellaneous

- *(open-brain-9j3)* Remove summarize.py from Stop+SubagentStop hooks, delete /api/summarize endpoint
- Update changelog
- Bump version to 0.7.1

### Testing

- *(open-brain-v76)* Red — transcript-based session-summary regeneration

## [0.7.0] - 2026-04-21

### Miscellaneous

- Update changelog
- Bump version to 0.7.0

## [0.6.0] - 2026-04-21

### Bug Fixes

- *(open-brain-52g)* Address review findings iteration 1
- *(open-brain-52g)* Address codex adversarial findings
- *(open-brain-d4n)* Address review findings iteration 1

### Documentation

- *(open-brain-d4n)* Update changelog

### Features

- *(open-brain-52g)* Green — compact_memories MCP tool
- *(open-brain-d4n)* Green — SessionEnd hook safety-net writer

### Miscellaneous

- Bump version to 0.6.0

## [0.5.2] - 2026-04-17

### Documentation

- Document production host (LXC116 on elysium) in CLAUDE.md

### Miscellaneous

- Update changelog
- Bump version to 0.5.2

### Refactoring

- *(llm)* Enable LLM_MODEL env var and add LLM_MODEL_CAPTURE tier

## [0.5.1] - 2026-04-17

### Bug Fixes

- *(open-brain-e0k)* Use POST /v1/embeddings for Voyage health check
- *(open-brain-e0k)* Add TTL cache to Voyage health check to limit quota burn
- *(open-brain-e0k)* Remove Voyage check from /health, keep live POST in doctor() only

### Documentation

- Add canonical schema doc for /api/worktree-session-summary

### Miscellaneous

- Update changelog
- Bump version to 0.5.1

## [0.5.0] - 2026-04-17

### Miscellaneous

- Update changelog
- Bump version to 0.5.0

## [0.4.0] - 2026-04-17

### Bug Fixes

- *(open-brain-9tt)* Address review findings iteration 1
- *(open-brain-9tt)* Address review findings iteration 2
- *(open-brain-9tt)* Worktree field relative to main repo root
- *(open-brain-x7s)* Address review findings iteration 1
- *(open-brain-x7s)* Prompt truncation and valid_turns guard for robustness
- *(open-brain-x7s)* Use head+tail truncation to preserve session outcome in prompt

### Documentation

- Update feature documentation for open-brain-9tt

### Features

- *(open-brain-9tt)* Green — worktree_turn_log hook (19/19 tests passing)
- *(open-brain-x7s)* Green — implement POST /api/worktree-session-summary

### Miscellaneous

- Update changelog
- Update changelog
- Bump version to 0.4.0

## [0.3.0] - 2026-04-11

### Features

- *(plugin)* Add ingest-content, learnings-pipeline, memory-heartbeat skills
- *(plugin)* Add sync-claude-memories Stop hook and standalone migration script

### Miscellaneous

- Bump version to 2026.04.21
- Update changelog
- Bump version to 2026.04.22
- *(plugin)* Sync version to CalVer 2026.04.22
- Switch to SemVer, set version 0.3.0

## [2026.04.21] - 2026-04-06

### Miscellaneous

- Bump version to 2026.04.20
- Update changelog

### Refactoring

- *(triage)* Rewrite triage_ccmem.py v2 with two-phase LLM classification

## [2026.04.20] - 2026-04-06

### Bug Fixes

- *(open-brain-0rd)* Address review findings iteration 1
- *(open-brain-0rd)* Address cmux review findings — server.py scope doc + no second LLM call in execute mode
- *(open-brain-0rd)* Remove dead dry_run param, unused TriageParams import, move Memory import out of loop

### Features

- *(open-brain-0rd)* Green — add session_ref: scope to triage_memories
- *(open-brain-0rd)* Add triage_ccmem.py script for session_ref:ccmem: triage

### Miscellaneous

- Bump version to 2026.04.19
- Update changelog

## [2026.04.19] - 2026-04-06

### Bug Fixes

- *(scripts)* Address review findings in migrate_claude_memories.py

### Miscellaneous

- Update changelog

## [2026.04.18] - 2026-04-06

### Bug Fixes

- *(open-brain-2jg)* Metadata_filter als Pre-Condition in hybrid_search SQL-Funktion
- *(open-brain-2jg)* Tighten metadata_filter test assertion — remove tautological or-clause
- *(open-brain-2jg)* Browse path metadata_filter uses @> containment like hybrid path

### Miscellaneous

- Bump version to 2026.04.17
- Update changelog
- Bump version to 2026.04.18

## [2026.04.17] - 2026-04-06

### Documentation

- Add url-based token auth feature documentation

### Features

- *(scripts)* Add Claude Code memory migration script

### Miscellaneous

- Bump version to 2026.04.16
- Update changelog

## [2026.04.16] - 2026-04-06

### Documentation

- Rewrite installation guide for pre-built image + URL token auth
- *(deploy)* Update compose comment to reflect CI image workflow

### Miscellaneous

- Bump version to 2026.04.15
- *(deploy)* Switch from op run to plain .env file for secrets
- *(deploy)* Pull pre-built image from GHCR instead of building on server
- Update changelog

### Security

- Harden codebase for public GHCR image

## [2026.04.15] - 2026-04-06

### Bug Fixes

- *(open-brain-pg4)* Address review findings iteration 1
- *(open-brain-pg4)* Address review findings iteration 2
- *(open-brain-pg4)* Address cmux review panel findings
- *(open-brain-pg4)* Address cmux review panel findings iteration 2

### Features

- *(open-brain-pg4)* Green — URL-based token auth (AK1-AK7)

### Miscellaneous

- Update changelog

## [2026.04.14] - 2026-04-06

### Bug Fixes

- *(open-brain-vro)* Address review findings iteration 1
- *(open-brain-vro)* Address review findings iteration 2

### Features

- *(open-brain-vro)* Green — deploy.sh health check exits with code 1 on failure
- *(open-brain-vro)* Green — add scripts/install-hooks.sh to install pytest pre-commit hook
- *(open-brain-vro)* Green — add nightly test runner script and Claude Code schedule trigger
- *(open-brain-vro)* Green — add post-deploy smoke checks for OAuth endpoints to deploy.sh

### Miscellaneous

- Update changelog
- Bump version to 2026.04.14

### Testing

- *(open-brain-vro)* Red — deploy.sh health check must exit on failure
- *(open-brain-vro)* Red — install-hooks.sh must exist and install pytest pre-commit hook
- *(open-brain-vro)* Red — nightly test runner script must exist and run full suite
- *(open-brain-vro)* Red — deploy.sh must have post-deploy smoke checks on API endpoints

## [2026.04.13] - 2026-04-06

### Miscellaneous

- Bump version to 2026.04.12
- Update changelog
- Bump version to 2026.04.13

## [2026.04.12] - 2026-04-06

### Bug Fixes

- *(open-brain-9o6)* Address review findings iteration 1
- *(open-brain-9o6)* Address review findings iteration 2 — patch asyncio.create_task in test_search
- *(open-brain-9o6)* Address review panel findings iter 1 — BIGSERIAL, logged_at index, rate-limit after guard, MAX=0 disables, race comment
- *(open-brain-m4t)* Address review findings iteration 1
- *(open-brain-m4t)* Address review findings iteration 2 — tuple types and precise exception in tests
- *(open-brain-m4t)* Address review panel findings — ScopeDeniedError, API key comment, remove redundant test, scope check assertion, docs error surface
- *(open-brain-m4t)* Address review panel findings iter 2 — ScopeDeniedError in docs code example

### Documentation

- Update feature documentation for open-brain-9o6
- Update feature documentation for open-brain-m4t — scope-gated tool pool

### Features

- *(open-brain-9o6)* Green — token budget: embed_with_usage, stats embedding metrics, daily guard, rate limit
- *(open-brain-m4t)* Green — scope-gated MCP tool list via ScopedFastMCP + ContextVar

### Miscellaneous

- Update changelog

### Testing

- *(open-brain-9o6)* Red — token budget: embed_with_usage, stats embedding metrics, daily guard, rate limit
- *(open-brain-m4t)* Red — scope-gated tool list and runtime enforcement

## [2026.04.11] - 2026-04-06

### Bug Fixes

- *(open-brain-qba)* Address review findings iteration 1
- *(open-brain-qba)* Address review findings iteration 2
- *(open-brain-qba)* Address review panel findings — fetchval side_effect, server_start_time None-safe, log level consistency, docs GET/POST
- *(open-brain-qba)* Address review panel findings iter 2 — docs log level, shared mock pool, rename test class, concurrency test

### Documentation

- Add health-and-diagnostics feature documentation for open-brain-qba

### Features

- *(open-brain-qba)* Green — enhanced /health + doctor MCP tool

### Miscellaneous

- Bump version to 2026.04.10
- Update changelog

### Testing

- *(open-brain-qba)* Red — health 503, doctor tool, version/uptime tests

## [2026.04.10] - 2026-04-03

### Bug Fixes

- *(open-brain-9h1)* Green — duplicate save skips enrichment + cross-project uses index_id
- *(open-brain-9h1)* Trust boundary docstring, parameterized dedup SQL, remove redundant import
- *(open-brain-9h1)* Address review findings iteration 1
- *(open-brain-9h1)* Save-first then LLM, None guard for index_id, assert no LLM on duplicate

### Miscellaneous

- Bump version to 2026.04.9
- Update changelog
- Update changelog

### Testing

- *(open-brain-9h1)* Red — duplicate save skips enrichment + cross-project uses index_id

## [2026.04.9] - 2026-04-03

### Miscellaneous

- Bump version to 2026.04.8
- Update changelog

## [2026.04.8] - 2026-04-03

### Bug Fixes

- *(open-brain-q9y)* Address review findings iteration 1
- *(open-brain-q9y)* Address review findings iteration 2
- *(open-brain-q9y)* Validation, dry_run comments, protected rename, test call_args
- *(open-brain-q9y)* Boost_threshold validation, recent_memories docstring, params validation test
- *(open-brain-dxd)* Address review findings iteration 1
- *(open-brain-dxd)* Address review findings iteration 2
- *(open-brain-dxd)* Address cmux review panel findings iteration 1

### Documentation

- Update feature documentation for open-brain-q9y
- Update feature documentation for open-brain-dxd

### Features

- *(open-brain-q9y)* Green — memory decay AK1-AK5 all passing
- *(open-brain-q9y)* Integrate decay_memories into run_lifecycle_pipeline
- *(open-brain-dxd)* Green — evolution engagement tracking + behavior proposals

### Miscellaneous

- Bump version to 2026.04.7
- Update changelog

### Testing

- *(open-brain-q9y)* Red — memory decay AK1-AK5 failing tests
- *(open-brain-dxd)* Red — evolution engagement tracking + behavior proposals

## [2026.04.7] - 2026-04-03

### Bug Fixes

- *(open-brain-jrq)* Address review findings iteration 1
- *(open-brain-jrq)* Address cmux review panel findings iteration 1

### Features

- *(open-brain-jrq)* Green — weekly briefing digest with all 6 AK

### Miscellaneous

- Bump version to 2026.04.6
- *(open-brain-jrq)* Remove unused field import
- Update changelog

### Refactoring

- Address 7 code review findings from wave 2-3 review

### Testing

- *(open-brain-jrq)* Red — weekly briefing all 6 acceptance criteria

## [2026.04.6] - 2026-04-03

### Miscellaneous

- Bump version to 2026.04.5
- Update changelog

## [2026.04.5] - 2026-04-03

### Bug Fixes

- *(open-brain-3u2)* Address review findings iteration 1
- *(open-brain-3u2)* Address review findings iteration 2
- *(open-brain-3u2)* Address cmux review findings — rename TestCaptureRouterSkillFormat, error-branch test, remove dead blank-line check, document Markdown parsing approach
- *(open-brain-17x)* Address review findings iteration 1
- *(open-brain-17x)* Rename type→memory_type in validate_domain_metadata signature
- *(open-brain-17x)* Update docs to reflect memory_type rename in validate_domain_metadata

### Documentation

- Add periodic learnings extraction feature documentation
- Update feature documentation for open-brain-3u2
- Update feature documentation for open-brain-17x

### Features

- *(open-brain-3u2)* Green — ob-migrate skill with interactive+batch+idempotency
- *(open-brain-17x)* Green — domain TypedDicts, validate_domain_metadata, save_memory warnings

### Miscellaneous

- Bump version to 2026.04.4
- Update changelog

### Testing

- *(open-brain-3u2)* Red — ob-migrate skill format, batch mode parsing, and idempotency
- *(open-brain-17x)* Red — domain schema validation and TypedDict tests

## [2026.04.4] - 2026-04-03

### Miscellaneous

- Bump version to 2026.04.3
- Update changelog

## [2026.04.2] - 2026-04-03

### Bug Fixes

- *(open-brain-dg9)* Address review findings iteration 1
- *(open-brain-dg9)* Address cmux review findings — remove redundant guard, add cross-ref comments, document deferred integ tests
- *(open-brain-dg9)* Remove unused imports (logging, timedelta, logger)
- *(open-brain-qt9)* Address review findings iteration 1
- *(open-brain-qt9)* Address review findings iteration 2 — explicit None check + remove duplicate bypass logic
- *(open-brain-qt9)* Address review findings iteration 3 — bypass guard, text truncation, return type, realistic test mocks, new edge case tests
- *(open-brain-90p)* Address review findings iteration 1
- *(open-brain-90p)* Address review findings iteration 2
- *(open-brain-90p)* Address review panel findings iteration 1
- *(open-brain-90p)* Address review panel findings iteration 2

### Documentation

- Update feature documentation for open-brain-qt9 capture router
- Update feature documentation for open-brain-90p entity extraction

### Features

- *(open-brain-dg9)* Green — learnings state helper and periodic dedup tests
- *(open-brain-qt9)* Green — capture router with LLM classification + concurrent save integration
- *(open-brain-90p)* Green — entity extraction on save_memory via Haiku

### Miscellaneous

- Bump version to 2026.04.1
- Update changelog
- Update changelog
- Bump version to 2026.04.2

### Testing

- *(open-brain-dg9)* Red — periodic learnings state and dedup tests
- *(open-brain-qt9)* Red — capture router classification + server integration tests
- *(open-brain-90p)* Red — entity extraction tests for all 6 acceptance criteria

## [2026.04.1] - 2026-04-03

### Bug Fixes

- *(open-brain-z8p)* Update test mock for dedup fetchrow sequence

### Miscellaneous

- Update changelog

## [2026.04.0] - 2026-04-03

### Bug Fixes

- *(open-brain-z8p)* Address review findings iteration 1
- *(open-brain-z8p)* Add @pytest.mark.asyncio to integration test
- *(open-brain-z8p)* Address review findings iteration 1
- *(open-brain-z8p)* Remove unused HASH_B constant
- *(open-brain-3qa)* Register JSONB codec + robust metadata parsing in _row_to_memory
- *(open-brain-3qa)* Register JSONB codec + robust metadata parsing in _row_to_memory
- *(open-brain-3qa)* Remove unused import json in test_metadata_from_json_string

### Features

- *(api)* /api/context since parameter für Session-Delta Filtering
- *(open-brain-z8p)* Green — content-hash dedup on save_memory

### Miscellaneous

- *(changelog)* Update for v2026.03.26
- *(changelog)* Update for v2026.04.03
- Bump version to 2026.04.0

### Testing

- *(open-brain-z8p)* Red — TestContentHashDedup (7 tests for content hash dedup)
- *(open-brain-3qa)* Red — metadata_from_json_string fails for str input
- *(open-brain-3qa)* Red — metadata_from_json_string fails for str input

## [2026.03.26] - 2026-03-12

### Features

- *(cli)* Add ob CLI wrapper for open-brain MCP tools

## [2026.03.25] - 2026-03-12

### Miscellaneous

- *(deps)* Bun update — fix express-rate-limit high vuln, hono moderate vuln
- *(changelog)* Update for v2026.03.25

## [2026.03.24] - 2026-03-12

### Bug Fixes

- *(deploy)* Mount users.json into container for multi-user auth

### Miscellaneous

- *(changelog)* Update for v2026.03.24

## [2026.03.23] - 2026-03-12

### Bug Fixes

- *(auth)* Use hmac.compare_digest for constant-time password comparison, eliminate redundant verify_token call
- *(search)* Pre-constrain author filter in hybrid_search DB function

### Features

- *(auth)* Multi-user support with shared memory and user attribution
- *(auth)* Replace USERS env var with users.json file for multi-user auth
- *(auth)* Bcrypt hashing for users.json passwords

### Miscellaneous

- *(changelog)* Update for v2026.03.22
- *(changelog)* Update for v2026.03.23

## [2026.03.22] - 2026-03-12

### Bug Fixes

- *(triage)* Nuanced learning classification prompt
- *(triage)* Exclude materialized/discarded memories from triage queries
- *(tests)* Update context endpoint tests to match search-based implementation

### Features

- *(context)* Narrative summary instead of raw table for session startup

### Miscellaneous

- *(oss)* Prepare repository for public release

## [2026.03.21] - 2026-03-12

### Bug Fixes

- *(tests)* Update minimal_entry test to expect content-hash session_ref
- *(triage)* Fix LLM classification — increase max_tokens and handle string IDs

### Miscellaneous

- *(changelog)* Update for v2026.03.21

## [2026.03.20] - 2026-03-12

### Bug Fixes

- *(scripts)* Improve migrate_learnings.py idempotency for non-lrn prefixed IDs

### Features

- *(plugin)* Add ob-smart-explore skills (search/outline/unfold)

### Miscellaneous

- *(changelog)* Update for v2026.03.19
- *(changelog)* Update for v2026.03.19

## [2026.03.19] - 2026-03-12

### Bug Fixes

- *(plugin)* Remove duplicate hooks reference from plugin.json

### Documentation

- *(ob-triage)* Document learning lifecycle in SKILL.md

### Features

- *(data-layer)* Add metadata parameter to save_memory, update_memory, and search
- *(ob-triage)* Add status tracking after promote/discard actions
- *(scripts)* Add migrate_learnings.py with complete JSONL→open-brain field mapping

### Miscellaneous

- *(changelog)* Update for v2026.03.18

## [2026.03.18] - 2026-03-12

### Features

- *(api)* Add /api/session-capture endpoint for auto-capture hooks

### Miscellaneous

- Add .gitleaksignore for test JWT secret false positive

## [2026.03.17] - 2026-03-12

### Miscellaneous

- *(beads)* Research yju — AST feature spec for ob-smart-explore

## [2026.03.16] - 2026-03-10

### Features

- *(plugin)* Add ob-triage skill with HITL convention
- *(plugin)* Ob-triage uses AskUserQuestion for HITL decisions

### Miscellaneous

- *(changelog)* Update for v2026.03.16

## [2026.03.15] - 2026-03-10

### Features

- *(triage)* Add memory lifecycle triage and materialization pipeline

### Miscellaneous

- *(changelog)* Update for v2026.03.15

## [2026.03.14] - 2026-03-10

### Bug Fixes

- *(api)* Move delete_memories into PostgresDataLayer class

### Features

- *(refine)* Drop merge actions with similarity < 0.4 as false positives
- *(ci)* Publish Docker image to ghcr.io/sussdorff/open-brain

### Miscellaneous

- *(changelog)* Update for v2026.03.13
- *(deps)* Update legacy TS deps to latest (audit fix attempt)
- *(changelog)* Update for next release

## [2026.03.13] - 2026-03-10

### Bug Fixes

- *(plugin)* Remove PostToolUse hook — only keep session/agent summaries
- *(refine)* Propagate similarity + skip_llm_merge to output actions
- *(refine)* Compute pairwise similarity on-demand for LLM merge groups

### Features

- *(api)* Add DELETE /api/memories endpoint for bulk memory cleanup

### Performance

- *(refine)* Parallelize merge actions + skip LLM merge for high-similarity duplicates

## [2026.03.12] - 2026-03-10

### Features

- *(plugin)* Add marketplace.json for Claude Code plugin distribution

### Miscellaneous

- *(changelog)* Update for v2026.03.11

## [2026.03.11] - 2026-03-10

### Features

- *(reranker)* Add Voyage Rerank-2.5 second-pass reranker

### Miscellaneous

- *(changelog)* Update for Voyage Rerank-2.5 feature

## [2026.03.10] - 2026-03-10

### Bug Fixes

- *(deploy)* Load OP service account token from /etc/op-service-account-token

### Features

- *(deploy)* Migrate to Docker Compose on services (LXC 116)

### Miscellaneous

- *(changelog)* Update for Docker Compose deployment

## [2026.03.9] - 2026-03-10

### Features

- *(plugin)* Add Claude Code plugin for automatic memory capture

### Miscellaneous

- Add __pycache__ and *.pyc to .gitignore
- *(changelog)* Update for next release

## [2026.03.8] - 2026-03-09

### Features

- Add Docker setup, CI, and README for public release

### Miscellaneous

- *(changelog)* Update for v2026.03.8

## [2026.03.7] - 2026-03-09

### Bug Fixes

- Add missing columns to duplicates query in refine_memories
- Four refine_memories improvements
- Instruct LLM to use each memory ID in at most one action
- Batch large memory sets for LLM analysis + robust JSON parsing
- Filter empty-ID actions from refine_memories response
- Raise priority floor from 0.01 to 0.05 to prevent infinite demotion

### Documentation

- *(memory)* Clarify content vs narrative field semantics

### Features

- LLM-powered merge in refine_memories combines content before deleting
- *(memory)* Add session_ref field + upsert for session_summary type
- *(memory)* Add is_test flag to save_memory to prevent test artifact persistence

### Miscellaneous

- Add MCP reconnect reminder to deploy script output
- *(changelog)* Update for v2026.03.7

## [2026.03.6] - 2026-03-09

### Bug Fixes

- Timeline anchor mode passed orphaned param to asyncpg query

### Features

- Add OAuth protected resource metadata (RFC 9728) for Claude.ai MCP discovery

### Miscellaneous

- Bump version to v2026.03.5
- Sync uv.lock
- Update changelog for v2026.03.6

## [2026.03.5] - 2026-03-09

### Bug Fixes

- Add uv to PATH in deploy script
- Parse ISO date strings to datetime for asyncpg compatibility
- Handle metadata as string in _row_to_memory

### Features

- Search browse mode, timeline date window, type taxonomy, tool guidance

## [2026.03.4] - 2026-03-09

### Documentation

- Update CLAUDE.md with deployment runbook

### Features

- Add update_memory MCP tool and deploy scripts

### Miscellaneous

- Bump version to v2026.03.4

## [2026.03.3] - 2026-03-08

### Features

- Add /health endpoint to Python MCP server

### Miscellaneous

- Bump version to v2026.03.3

### Refactoring

- Remove legacy TypeScript server, add delta-migration script

## [2026.03.2] - 2026-03-05

### Features

- *(migration)* SQLite→Postgres migration for real claude-mem schema
- Python MCP server port with TDD

### Miscellaneous

- Add .gitignore for python directory
- Remove cached pycache files
- Update CHANGELOG.md
- Bump version to v2026.03.2

## [2026.03.1] - 2026-03-05

### Features

- Configurable embedding + LLM provider (Anthropic/OpenRouter)
- Align schema + queries with plan

### Miscellaneous

- Update CHANGELOG.md

## [2026.03.0] - 2026-03-05

### Features

- Copy MCP server code and create schema migrations (Phase 1)
- Voyage-4 embeddings + hybrid search integration (Phase 3)
- SQLite migration + prune scripts (Phase 3b)
- Auto-linking + priority decay + usage logging (Phase 4a)
- Refine_memories MCP tool with Haiku analysis (Phase 4b)

### Miscellaneous

- Add cliff.toml + generate initial CHANGELOG
- Tag v2026.03.0

### Init

- Scaffold open-brain repo with TypeScript + MCP server structure


