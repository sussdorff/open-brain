# Project: open-brain

**Python** MCP server — Postgres+pgvector memory system for AI assistants.

**IMPORTANT: This is a Python project. The TypeScript code in `src-ts-DEPRECATED/` is legacy and must not be used or referenced.**

## Context

- Replaces claude-mem Worker (SQLite + ChromaDB) with Postgres 18 + pgvector
- MCP Server with OAuth 2.1 (Streamable HTTP transport)
- Embeddings via Voyage-4 API (1024 dim)
- Hybrid Search: pgvector cosine + tsvector FTS via Reciprocal Rank Fusion

## Structure

- `python/src/open_brain/` -- Main Python package
- `python/src/open_brain/server.py` -- MCP server setup
- `python/src/open_brain/auth/` -- OAuth 2.1 provider
- `python/src/open_brain/tools/` -- MCP tools (search, timeline, save_memory, etc.)
- `python/tests/` -- Test suite
- `scripts/` -- Migration scripts (TypeScript, for one-time use)
- `src-ts-DEPRECATED/` -- **DEPRECATED** old TypeScript server, do not use

## Commands

```bash
cd python
uv run python -m open_brain        # Run server
uv run pytest                      # Run tests
```

## Deployment

- **Server**: LXC 116 (Elysium Proxmox) at `/opt/open-brain/`
- **Old TS server**: `/opt/mcp-server-ts-DEPRECATED/` (do not use)
- Deploy scripts live in `elysium-proxmox` repo under `lxc/services/`

## Key Decisions

- Python (simpler deployment, UV for dependency management)
- Voyage-4 for embeddings (14% better retrieval vs. text-embedding-3-small)
- Claude Haiku 4.5 for metadata extraction (not gpt-4o-mini)
- Shared Postgres instance with Langfuse on LXC 116
- No Redis, no Web UI (keep it simple)
