# Project: open-brain

**Python** MCP server — Postgres+pgvector memory system for AI assistants.

**IMPORTANT: This is a Python project. The TypeScript code in `src-ts-DEPRECATED/` is legacy and must not be used or referenced.**

## Context

- MCP Server with OAuth 2.1 (Streamable HTTP transport)
- Embeddings via Voyage-4 API (1024 dim)
- Hybrid Search: pgvector cosine + tsvector FTS via Reciprocal Rank Fusion

## Structure

- `python/src/open_brain/` -- Main Python package
- `python/src/open_brain/server.py` -- MCP server setup + all MCP tool definitions
- `python/src/open_brain/auth/` -- OAuth 2.1 provider
- `python/src/open_brain/data_layer/` -- Postgres data layer (interface.py = Protocol, postgres.py = impl)
- `python/tests/` -- Test suite
- `deploy/` -- Deployment scripts (start.sh, deploy.sh)
- `scripts/` -- Migration scripts (one-time use)

## Commands

```bash
cd python
uv run python -m open_brain        # Run server locally
uv run pytest                      # Run tests (1 integration test needs VOYAGE_API_KEY)
uv run pytest -m "not integration" # Run without external deps
```

## Deployment

See `deploy/deploy.sh` and `docker-compose.service.yml` for production deployment patterns.

## Key Decisions

- Python (simpler deployment, UV for dependency management)
- Voyage-4 for embeddings (14% better retrieval vs. text-embedding-3-small)
- Claude Haiku 4.5 for metadata extraction (not gpt-4o-mini)
- No Redis, no Web UI (keep it simple)
