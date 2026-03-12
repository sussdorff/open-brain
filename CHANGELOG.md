# Changelog

All notable changes to this project will be documented in this file.
## [unreleased]

### Bug Fixes

- *(plugin)* Remove duplicate hooks reference from plugin.json

### Documentation

- *(ob-triage)* Document learning lifecycle in SKILL.md

### Features

- *(data-layer)* Add metadata parameter to save_memory, update_memory, and search
- *(ob-triage)* Add status tracking after promote/discard actions
- *(scripts)* Add migrate_learnings.py with complete JSONL→open-brain field mapping
- *(plugin)* Add ob-smart-explore skills (search/outline/unfold)

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


