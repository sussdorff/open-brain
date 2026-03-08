# Changelog

All notable changes to this project will be documented in this file.
## [unreleased]

### Features

- Add /health endpoint to Python MCP server

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


