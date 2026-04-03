# open-brain

[![CI](https://github.com/sussdorff/open-brain/actions/workflows/ci.yml/badge.svg)](https://github.com/sussdorff/open-brain/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/protocol-MCP-green.svg)](https://modelcontextprotocol.io/)

A pluggable MCP memory server that gives AI assistants long-term, searchable memory across sessions and projects.

**The problem:** AI assistants forget everything between sessions. They can't retain learnings, recall past decisions, or build on previous work. When you're running multiple agents across multiple projects, context is constantly lost.

**open-brain solves this** by providing a shared memory layer that any MCP-compatible assistant can read from and write to — with hybrid search (vector + full-text), human-in-the-loop triage, and a memory lifecycle that promotes valuable learnings into persistent artifacts like coding standards, skills, or project documentation.

## How It Works

```
  AI Assistant (Claude Code, IDE, etc.)
       │
       │  MCP protocol
       ▼
  ┌─────────────────────────┐
  │     open-brain Server    │
  │                         │
  │  save ──► embed ──► search
  │                    ▲
  │  refine (auto)     │
  │  triage (human) ───┘
  │  materialize ──► files, issues, standards
  └──────────┬──────────────┘
             │
             ▼
  Postgres + pgvector + Voyage-4
```

1. **Save**: Observations, learnings, and session summaries are stored with embeddings
2. **Search**: Hybrid search combines keyword matching (FTS) and semantic similarity (pgvector) via Reciprocal Rank Fusion
3. **Refine**: Automatic consolidation — finds duplicates, merges similar memories, adjusts priority
4. **Triage**: Human-in-the-loop review — classify memories as keep, merge, promote, or archive
5. **Materialize**: Write approved learnings to their target — project docs, coding standards, work items

See [docs/architecture.md](docs/architecture.md) for detailed diagrams and technical deep-dives.

## Quick Start

### Docker Compose (recommended)

```bash
git clone https://github.com/sussdorff/open-brain.git
cd open-brain

# Create .env from template
cp .env.example .env
# Edit .env — fill in VOYAGE_API_KEY, AUTH_PASSWORD, JWT_SECRET, MCP_SERVER_URL

# Start the stack (Postgres + open-brain)
docker compose up -d

# Verify
curl http://localhost:8091/health
# {"status":"ok","service":"open-brain","runtime":"python"}
```

### Pre-built Image

```bash
OPEN_BRAIN_IMAGE=ghcr.io/sussdorff/open-brain:latest docker compose up -d
```

### Connect Claude Code

Add to your Claude Code MCP config:

```json
{
  "mcpServers": {
    "open-brain": {
      "type": "streamable-http",
      "url": "https://your-server.example.com/mcp"
    }
  }
}
```

## Configuration

All configuration is via environment variables (`.env` file or injected by your orchestrator).

| Variable | Required | Default | Description |
|---|---|---|---|
| `MCP_SERVER_URL` | Yes | — | Public HTTPS URL of this server |
| `DATABASE_URL` | Yes | `postgresql://...localhost` | Postgres connection string |
| `AUTH_USER` | Yes | — | Username for OAuth login |
| `AUTH_PASSWORD` | Yes | — | Password (min 8 chars) |
| `JWT_SECRET` | Yes | — | JWT signing secret (min 32 chars) |
| `VOYAGE_API_KEY` | Yes | — | [Voyage AI](https://www.voyageai.com/) API key |
| `VOYAGE_MODEL` | No | `voyage-4` | Embedding model |
| `LLM_PROVIDER` | No | `anthropic` | `anthropic` or `openrouter` |
| `LLM_MODEL` | No | `claude-haiku-4-5-20251001` | Model for refine/triage |
| `ANTHROPIC_API_KEY` | Cond. | — | Required when `LLM_PROVIDER=anthropic` |
| `OPENROUTER_API_KEY` | Cond. | — | Required when `LLM_PROVIDER=openrouter` |
| `PORT` | No | `8091` | Server port |
| `CLIENTS_FILE` | No | `/app/clients.json` | OAuth client registry path |

See `.env.example` for a complete template with comments.

## MCP Tools

AI assistants interact with memory through MCP tools. The recommended workflow is a **3-step funnel** that minimizes token usage:

```
search(query)          →  compact index with IDs (~50-100 tokens/result)
  timeline(anchor=ID)  →  context around interesting results
    get_observations([IDs])  →  full details ONLY for what you need
```

### Memory Access

| Tool | Description |
|---|---|
| `search` | Hybrid search (vector + FTS). Filter by `project`, `type`, date range, `file_path`. Omit query for browse mode. |
| `timeline` | Context around a result (anchor mode by ID) or date window. |
| `get_observations` | Fetch full details for a list of IDs. |
| `search_by_concept` | Pure vector search — good for "what did I learn about X?" |
| `get_context` | Recent session summaries — useful at conversation start. |
| `stats` | Database statistics: memory count, type taxonomy, DB size. |

### Memory Writing

| Tool | Description |
|---|---|
| `save_memory` | Store an observation. `text` + `project` required. Auto-embeds async. **Capture Router** applies domain templates and extracts structured fields concurrently. |
| `update_memory` | Update fields on an existing memory. Re-embeds if content changes. |
| `refine_memories` | Automatic consolidation: dedup, merge, priority adjustment. |
| `triage_memories` | Human-in-the-loop classification into lifecycle actions. |
| `materialize_memories` | Execute triage actions (promote to docs, create issues, archive). |

### Self-Improvement Loop

| Tool | Description |
|---|---|
| `analyze_briefing_engagement` | Compute response rates by briefing type over the last N days. Shows which briefing types users engage with most. |
| `generate_evolution_suggestion` | Propose ONE behavior change per 7 days: remove low-engagement briefing types or expand high-engagement ones. Rate-limited and respects 30-day rejection suppression. |
| `log_evolution_approval` | Record user approval or rejection of a suggestion. Logged rejections suppress re-proposals for 30 days. |
| `query_evolution_history` | Retrieve past evolution suggestions and approvals — track which briefing types have been adjusted over time. |

See [docs/features/self-improvement-loop.md](docs/features/self-improvement-loop.md) for the full workflow and examples.

### Memory Types

`discovery`, `change`, `feature`, `decision`, `bugfix`, `refactor`, `session_summary`, `learning`, `briefing`, `evolution`

New types are allowed when none fit. Check `stats()` to see existing vocabulary.

## Structured Memory: Capture Router

**Capture Router** automatically classifies and structures incoming memories into domain-specific templates. When you call `save_memory`, an LLM concurrently:

1. Classifies the text (decision, meeting, person context, etc.)
2. Extracts structured fields (attendees, action items, owner, rationale, etc.)
3. Merges fields into memory metadata

No changes to your code — it works transparently:

```python
# Caller: just save raw text
await save_memory(
    text="Decided to use async for better scalability",
    type="decision"
)

# Result in database:
# metadata = {
#   "capture_template": "decision",
#   "what": "Use async",
#   "context": "Scalability requirements",
#   "owner": "...",
#   "alternatives": ["...", "..."],
#   "rationale": "Better I/O throughput"
# }
```

This enables:
- **Automatic structure** without caller effort
- **Downstream processing** — triage, refine, and materialize can rely on structured data
- **Better retrieval** — action items, decisions, and learnings are queryable
- **Agent workflows** — One agent captures; another retrieves and acts on structured fields

See [docs/features/capture-router.md](docs/features/capture-router.md) for template reference and examples.

## Multi-User

open-brain currently supports a **single user** per instance (one `AUTH_USER` / `AUTH_PASSWORD` pair). Multiple MCP clients can connect simultaneously via OAuth or API keys, but all share the same memory pool.

Memory is segmented by `project`, not by user. This works well for individual use or small teams where shared context is the goal.

**Planned**: Shared memory with user attribution — memories tagged by author, visible to all authenticated users, filterable by contributor.

## Claude Code Plugin

The plugin provides **automatic memory capture** — no manual MCP calls needed:

- **Session start**: Injects recent memories and session summaries as narrative context
- **Session end**: Generates and saves a session summary from recent observations

Install:
```bash
claude plugin add /path/to/open-brain/plugin
```

See [plugin/](plugin/) for configuration details.

## Embedding into Existing Stacks

open-brain can be embedded into larger Docker Compose stacks via `include:`:

```yaml
include:
  - path: ../open-brain/docker-compose.service.yml

services:
  your-app:
    depends_on:
      - open-brain
```

The `docker-compose.service.yml` contains only the open-brain service (no Postgres) — bring your own database.

## Development

```bash
cd python
uv sync --dev

# Unit tests (no external services)
uv run pytest -m "not integration"

# All tests (needs VOYAGE_API_KEY)
uv run pytest

# Run locally
uv run python -m open_brain
```

## Deployment

```bash
# Standalone (includes Postgres)
docker compose up -d

# Service-only (bring your own Postgres)
docker compose -f docker-compose.service.yml up --build -d
```

Secrets can be injected via `.env`, environment variables, or a secrets manager:

```bash
# 1Password example
op run --env-file=.env.tpl -- docker compose up -d
```

### OAuth Client Registration

Clients register dynamically via `/register` (RFC 7591) or statically via `clients.json`:

```yaml
volumes:
  - ./clients.json:/app/clients.json:ro
```

## Documentation

- [Architecture & Diagrams](docs/architecture.md) — system design, hybrid search, memory lifecycle, auth flow
- [Contributing](CONTRIBUTING.md) — development setup, PR process, coding guidelines
- [Security](SECURITY.md) — vulnerability reporting, security considerations
- [Changelog](CHANGELOG.md) — version history

## License

[MIT](LICENSE)
