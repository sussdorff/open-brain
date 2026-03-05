# Project: open-brain

Postgres+pgvector memory system with MCP server for AI assistants.

## Context

- Replaces claude-mem Worker (SQLite + ChromaDB) with Postgres 18 + pgvector
- MCP Server with OAuth 2.1 (Streamable HTTP transport)
- Embeddings via Voyage-4 API (1024 dim)
- Hybrid Search: pgvector cosine + tsvector FTS via Reciprocal Rank Fusion

## Structure

- `src/server.ts` -- Express + MCP server setup
- `src/auth/` -- OAuth 2.1 provider (JWT, PKCE)
- `src/tools/` -- MCP tools (search, timeline, save_memory, etc.)
- `src/data-layer/` -- DataLayer interface + Postgres implementation
- `src/db/` -- Connection pool + migration runner
- `src/db/migrations/` -- SQL migration files
- `scripts/` -- Migration + pruning scripts

## Commands

```bash
npm run dev          # Dev server with watch
npm run start        # Production start
npm run migrate      # Run DB migrations
npm run migrate:verify  # Verify migration state
```

## Deployment

Deployed on LXC 116 (Elysium Proxmox). Deploy scripts live in
`elysium-proxmox` repo under `lxc/services/`.

## Key Decisions

- TypeScript (matches MCP SDK ecosystem)
- Voyage-4 for embeddings (14% better retrieval vs. text-embedding-3-small)
- Claude Haiku 4.5 for metadata extraction (not gpt-4o-mini)
- Shared Postgres instance with Langfuse on LXC 116
- No Redis, no Web UI (keep it simple)
