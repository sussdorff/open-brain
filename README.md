# open-brain

A production-grade MCP (Model Context Protocol) memory server that gives AI assistants long-term, searchable memory across sessions. Built on Postgres + pgvector with hybrid search (vector + full-text), OAuth 2.1 authentication, and a configurable LLM backend.

## Architecture

open-brain is Layer 2 in a 4-layer knowledge system:

```
Layer 1: Raw data / documents
Layer 2: open-brain (this project) — semantic memory, observations, session summaries
Layer 3: Knowledge graph / structured reasoning
Layer 4: Agent orchestration
```

**Stack:**
- **Storage**: Postgres 17 + pgvector (1024-dim embeddings)
- **Embeddings**: Voyage AI (`voyage-4`) — 14% better retrieval than OpenAI text-embedding-3-small
- **Search**: Hybrid (cosine similarity + tsvector FTS via Reciprocal Rank Fusion)
- **Transport**: Streamable HTTP (MCP spec), OAuth 2.1
- **LLM**: Configurable — Anthropic Claude or OpenRouter (used for `refine_memories`)

## Quick Start (Standalone Docker Compose)

```bash
# 1. Clone the repo
git clone https://github.com/your-org/open-brain.git
cd open-brain

# 2. Create your .env from the example
cp .env.example .env
# Edit .env — fill in VOYAGE_API_KEY, AUTH_PASSWORD, JWT_SECRET, MCP_SERVER_URL

# 3. Start the stack
docker compose up -d

# 4. Verify
curl http://localhost:8091/health
# {"status":"ok","service":"open-brain","runtime":"python"}
```

**For 1Password users:**
```bash
op run --env-file=.env.tpl -- docker compose up -d
```

## Configuration

All configuration is via environment variables (loaded from `.env` or injected by your orchestrator).

| Variable | Required | Default | Description |
|---|---|---|---|
| `MCP_SERVER_URL` | Yes | — | Public HTTPS URL of this server (e.g. `https://brain.example.com`) |
| `DATABASE_URL` | Yes | `postgresql://open_brain:password@localhost:5432/open_brain` | Postgres connection string |
| `AUTH_USER` | Yes | — | Username for the OAuth login form |
| `AUTH_PASSWORD` | Yes | — | Password (min 8 chars) |
| `JWT_SECRET` | Yes | — | Secret for JWT signing (min 32 chars, use a random hex string) |
| `CLIENTS_FILE` | No | `/app/clients.json` (Docker) | Path to the OAuth clients JSON file |
| `PORT` | No | `8091` | Port to listen on |
| `VOYAGE_API_KEY` | Yes | — | Voyage AI API key for embeddings |
| `VOYAGE_MODEL` | No | `voyage-4` | Voyage embedding model |
| `LLM_PROVIDER` | No | `anthropic` | LLM for metadata extraction: `anthropic` or `openrouter` |
| `LLM_MODEL` | No | `claude-haiku-4-5-20251001` | Model name (must match the provider) |
| `ANTHROPIC_API_KEY` | Conditional | — | Required when `LLM_PROVIDER=anthropic` |
| `OPENROUTER_API_KEY` | Conditional | — | Required when `LLM_PROVIDER=openrouter` |

See `.env.example` for a ready-to-copy template with comments.

## MCP Tools Reference

AI assistants access memory through these MCP tools. The recommended workflow is a 3-step funnel that minimizes token usage:

```
search(query)          ->  index with IDs (~50-100 tokens/result)
  timeline(anchor=ID)  ->  context around interesting results
    get_observations([IDs])  ->  full details ONLY for filtered IDs
```

### Memory Access

| Tool | Description |
|---|---|
| `__IMPORTANT` | Workflow reminder — always call this first to load the 3-step pattern |
| `search` | Step 1: Hybrid search (vector + FTS). Supports filtering by `project`, `type`, `obs_type`, date range, `file_path`. Omit query for browse mode. |
| `timeline` | Step 2: Context around a result. Anchor mode (by ID) or date-window mode. Shows N memories before/after. |
| `get_observations` | Step 3: Fetch full details for a list of IDs. Only call after filtering via search/timeline. |
| `search_by_concept` | Pure vector search across memories (no FTS). Good for conceptual/"what did I learn about X?" queries. |
| `get_context` | Get recent session summaries. Useful at conversation start. |
| `stats` | DB statistics: memory count, sessions, DB size, type taxonomy with counts. |

### Memory Writing

| Tool | Description |
|---|---|
| `save_memory` | Save a new observation. `text` is the primary content (embedded + searched). `project` is required. `type` should reuse existing vocabulary (check `stats()` first). Optional: `title`, `subtitle`, `narrative`, `session_ref`. |
| `update_memory` | Update an existing memory by ID. Only provided fields are changed. Re-embeds automatically when content changes. |
| `refine_memories` | LLM-powered consolidation: deduplication, merging, cleanup. Supports `scope`, `limit`, `dry_run`. |

### Memory Types (conventional vocabulary)

`discovery`, `change`, `feature`, `decision`, `bugfix`, `refactor`, `session_summary`

New types are allowed when none of the above fit. Check `stats()` to see what exists before inventing new ones.

## Mira Integration

open-brain is designed to be embedded into larger Docker Compose stacks via the `include:` feature (Docker Compose v2.20+).

**In your project's `docker-compose.yml`:**
```yaml
include:
  - path: ../open-brain/docker-compose.service.yml

services:
  your-app:
    # ...
    depends_on:
      - open-brain
```

**Requirements:**
- Set `DATABASE_URL` in open-brain's `.env` to point at your existing Postgres instance
- open-brain's `.env` file must exist at the path relative to `docker-compose.service.yml`
- open-brain exposes port `8091`

The `docker-compose.service.yml` contains only the `open-brain` service (no Postgres), making it suitable for stacks that provide their own database.

## Development

```bash
# Install dependencies
cd python
uv sync --dev

# Run tests (no external services needed)
uv run pytest -m "not integration"

# Run all tests (requires VOYAGE_API_KEY env var)
uv run pytest

# Run the server locally
uv run python -m open_brain
```

**Health check:**
```bash
curl http://localhost:8091/health
```

## Deployment (Production)

The production server runs on LXC 116 via systemd + 1Password secret injection.

```bash
# After git push — SSH into the server and deploy
ssh services /opt/open-brain/deploy/deploy.sh

# Verify
ssh services 'curl -s http://localhost:8091/health'
```

### 1Password Integration

Secrets are injected at startup via `op run` and never written to disk:

```bash
# .env.tpl references 1Password items:
# DATABASE_URL=op://vault/open-brain/DATABASE_URL
# VOYAGE_API_KEY=op://vault/open-brain/VOYAGE_API_KEY
# ...

op run --env-file=.env.tpl -- docker compose up -d
```

### OAuth Client Registration

Clients are registered dynamically via the `/register` endpoint (RFC 7591) or statically via `clients.json`. In Docker, mount your clients file:

```yaml
volumes:
  - ./clients.json:/app/clients.json:ro
```

The `CLIENTS_FILE` env var controls where open-brain looks for static client registrations. Defaults to `/app/clients.json` in Docker.
