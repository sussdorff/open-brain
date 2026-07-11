# open-brain

[![CI](https://github.com/sussdorff/open-brain/actions/workflows/ci.yml/badge.svg)](https://github.com/sussdorff/open-brain/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue.svg)](https://www.python.org/)
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

## Installation

open-brain has two installable components:

- **Server**: the long-running FastAPI/MCP service backed by Postgres + pgvector. Install this on the machine or container host where open-brain runs.
- **CLI**: the `ob` command for humans and operator scripts. Install this on your workstation, laptop, jump host, or the server itself.

The server and CLI do not need to be on the same machine. Docker Compose is the recommended server install. `uv tool install` is the recommended CLI install.

### 1. Start the server

You need a Postgres instance with the pgvector extension, and a [Voyage AI](https://www.voyageai.com/) API key.

**Standalone (includes Postgres):**

```bash
# Download the compose file and example config
curl -O https://raw.githubusercontent.com/sussdorff/open-brain/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/sussdorff/open-brain/main/.env.example
cp .env.example .env
```

Edit `.env` — the required fields are:

| Variable | Description |
|---|---|
| `MCP_SERVER_URL` | Public HTTPS URL of this server (e.g. `https://brain.example.com`) |
| `DATABASE_URL` | Postgres connection string |
| `AUTH_USER` | Username for the OAuth login form |
| `AUTH_PASSWORD` | Password (min 8 chars) |
| `JWT_SECRET` | Random secret for signing JWTs — `openssl rand -hex 32` |
| `VOYAGE_API_KEY` | [Voyage AI](https://www.voyageai.com/) API key |
| `ANTHROPIC_API_KEY` | Anthropic API key (for memory refinement) |

```bash
# Pull the image and start
docker compose pull
docker compose up -d

# Verify
curl http://localhost:8091/health
# {"status":"ok","service":"open-brain","runtime":"python"}
```

**Service-only (bring your own Postgres):**

```bash
curl -O https://raw.githubusercontent.com/sussdorff/open-brain/main/docker-compose.service.yml
curl -O https://raw.githubusercontent.com/sussdorff/open-brain/main/.env.example
cp .env.example .env
# Edit .env, then:
docker compose -f docker-compose.service.yml pull
docker compose -f docker-compose.service.yml up -d
```

**Python package server (local/bare-metal):**

Use this on the server host when you already have Postgres + pgvector running and want the same installed `ob` command to launch the server.

```bash
uv tool install --python 3.14 "git+https://github.com/sussdorff/open-brain.git#subdirectory=python"

# Set the same variables shown in .env.example, then:
ob server

# Optional bind overrides
ob server --host 127.0.0.1 --port 8091
```

For a local checkout, install from the Python package directory:

```bash
uv tool install --python 3.14 ./python
```

### 2. Issue an access token

Use your API key (from `API_KEYS` in `.env`) to issue a URL token for each client:

```bash
curl -X POST https://your-server.example.com/token/url \
  -H "x-api-key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-client", "scopes": ["memory", "evolution"], "expires_in_days": 365}'
# {"token": "abc123...", "name": "my-client", "scopes": [...], "expires_at": "..."}
```

Save the raw token — it is shown exactly once.

### 3. Install and configure the CLI

Run this on every machine where you want to use the CLI. If you did not already install `ob` for the Python package server path:

```bash
uv tool install --python 3.14 "git+https://github.com/sussdorff/open-brain.git#subdirectory=python"
```

Point the CLI at your MCP endpoint and provide the URL token from step 2:

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/open-brain"
cat > "${XDG_CONFIG_HOME:-$HOME/.config}/open-brain/config.json" <<'JSON'
{
  "server_url": "https://your-server.example.com",
  "url_token": "TOKEN_FROM_STEP_2"
}
JSON
chmod 600 "${XDG_CONFIG_HOME:-$HOME/.config}/open-brain/config.json"

ob --json doctor
ob search "what did I decide about X?"
```

The CLI follows XDG config conventions. The primary config file is:

- `$XDG_CONFIG_HOME/open-brain/config.json`
- `~/.config/open-brain/config.json` when `XDG_CONFIG_HOME` is unset

Use `server_url` for the public base URL (`/mcp` is appended automatically) or `mcp_url` for an explicit MCP endpoint.

If you previously used the hook setup script, your existing config may contain an `api_key` instead of a URL token. The CLI supports that too:

```json
{
  "server_url": "https://your-server.example.com",
  "api_key": "YOUR_API_KEY"
}
```

Environment variables override config:

```bash
export OB_URL="https://your-server.example.com/mcp"
export OB_URL_TOKEN="TOKEN_FROM_STEP_2"
# or:
export OB_API_KEY="YOUR_API_KEY"
```

OAuth bearer tokens are also supported via `OB_TOKEN` or `"token"` in the config file. URL tokens use `OB_URL_TOKEN`, `"url_token"` in the config file, or an explicit `?token=...` in `OB_URL`. API keys use `OB_API_KEY` or `"api_key"` in the config file. Legacy `~/.open-brain/config.json`, `~/.open-brain/token`, and `~/.open-brain/url-token` files still work as fallback.

### 4. Connect coding harnesses with OAuth

```bash
claude mcp add open-brain \
  --transport http \
  "https://your-server.example.com/mcp"
claude mcp login open-brain
```

Or manually in `~/.claude.json`:

```json
{
  "mcpServers": {
    "open-brain": {
      "type": "http",
      "url": "https://your-server.example.com/mcp"
    }
  }
}
```

Codex uses the same OAuth-protected HTTP endpoint:

```bash
codex mcp add open-brain --url "https://your-server.example.com/mcp"
codex mcp login open-brain --scopes memory
```

The harness configuration contains no access token. Claude Code and Codex own
their OAuth session state and refresh flow. Do not add a URL query token, static
authorization header, or bearer-token environment variable to these MCP
registrations.

URL tokens remain available for clients that cannot complete OAuth. Revoke such
a legacy token by its client name:

```bash
curl -X DELETE https://your-server.example.com/token/url/my-client \
  -H "x-api-key: YOUR_API_KEY"
```

## Configuration

All configuration is via environment variables (`.env` file or injected by your orchestrator).

| Variable | Required | Default | Description |
|---|---|---|---|
| `MCP_SERVER_URL` | Yes | — | Public HTTPS URL of this server |
| `DATABASE_URL` | Yes | — | Postgres connection string (pgvector required) |
| `AUTH_USER` | Yes | — | Username for OAuth login |
| `AUTH_PASSWORD` | Yes | — | Password (min 8 chars) |
| `JWT_SECRET` | Yes | — | JWT signing secret (min 32 chars) |
| `VOYAGE_API_KEY` | Yes | — | [Voyage AI](https://www.voyageai.com/) API key |
| `VOYAGE_MODEL` | No | `voyage-4` | Embedding model |
| `LLM_PROVIDER` | No | `anthropic` | `anthropic` or `openrouter` |
| `LLM_MODEL` | No | `claude-haiku-4-5-20251001` | Default model for small calls (entity/classification/tool-use/summaries) |
| `LLM_MODEL_CAPTURE` | No | — | Optional override for `/api/session-capture` (falls back to `LLM_MODEL`) |
| `ANTHROPIC_API_KEY` | Cond. | — | Required when `LLM_PROVIDER=anthropic` |
| `OPENROUTER_API_KEY` | Cond. | — | Required when `LLM_PROVIDER=openrouter` |
| `API_KEYS` | No | — | Comma-separated API keys for hook/CLI/script access |
| `PORT` | No | `8091` | Server port |
| `CLIENTS_FILE` | No | `/app/clients.json` | OAuth client registry path |
| `MAX_MEMORIES_PER_DAY` | No | `500` | Daily ingestion limit (0 = disabled) |

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
| `stats` | Database statistics: memory count, type taxonomy, DB size, embedding token usage, estimated API cost. |

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

## Library Marketplace

open-brain is registered as a marketplace in [`the-library`](https://github.com/disler/the-library) (`cognovis/library` fork). The repo's top-level `skills/` and `hooks/` directories are the harness-neutral source primitives; the meta library installs them into any harness (Claude Code, Codex, …) via `/library use`.

Register the marketplace once in your `library.yaml`:

```yaml
marketplaces:
  - name: open-brain
    source: https://github.com/sussdorff/open-brain
    description: open-brain memory store with skills+hooks for memory capture
    type: git
```

Then install primitives on demand:

```bash
/library use ob-search             # MCP-backed memory search skill
/library use ob-triage             # Human-in-the-loop memory triage
/library use ob-migrate            # Memory migration (JSONL/Markdown/interactive)
/library use ingest-content        # URL ingestion to curated_content
/library use memory-heartbeat      # Periodic memory lifecycle pipeline
/library use open-brain-hooks      # Claude/Codex memory hooks bundle
```

For bulk Obsidian/Markdown vault migration (historical Second Brain notes), use the standalone
importer: `python -m open_brain.second_brain_import <vault_path> [--apply]`. See
`docs/features/second-brain-import.md` for the full reference. For interactive single-note
capture, `ob-migrate` is the right tool.

Memory CLI routing rules and `save_memory` conventions live in the standards
`open-brain/cli-routing`, `open-brain/memory-write-patterns`, and
`open-brain/memory-status-conventions` (auto-loaded via `requires_standards`).

Code-intelligence tooling (`smart_search` / `smart_outline` / `smart_unfold`)
moved to the `cognovis-core` marketplace as `/library use code-navigator` —
the scripts have no functional relationship to open-brain memory.

The `open-brain-hooks` guardrail installs the harness-specific manifest from `hooks/`: Claude Code uses `hooks/hooks.json`, while Codex uses `hooks/hooks.codex.json`. Source paths are resolved to the library cache, so there is no symlinking into `plugin/`, no `claude plugin add`, and no marketplace.json manifest.

After installing the hooks, configure the server connection:

```bash
python3 ~/.local/share/library/guardrails/open-brain-hooks/checkout/hooks/scripts/setup.py
```

This prompts for server URL + API key and writes `~/.open-brain/config.json`.

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
uv run ob server
```

## Deployment

The Docker image is built in CI and published to `ghcr.io/sussdorff/open-brain:latest` on every push to `main`. No build step is required on the server.

```bash
# Pull latest image and restart
docker compose pull && docker compose up -d

# Or service-only
docker compose -f docker-compose.service.yml pull
docker compose -f docker-compose.service.yml up -d
```

Secrets are loaded from `.env` by docker compose. Generate strong values for `JWT_SECRET` and `AUTH_PASSWORD`:

```bash
openssl rand -hex 32   # JWT_SECRET
openssl rand -base64 16 | tr -d '=' # AUTH_PASSWORD
```

### OAuth Client Registration

Clients register dynamically via `/register` (RFC 7591) or statically via `clients.json`:

```yaml
volumes:
  - ./clients.json:/app/clients.json:ro
```

## CLI Usage

The `ob` command is the main human-facing CLI. It intentionally covers normal server and memory workflows; one-off migrations, dev checks, and Claude Code hook internals remain in `scripts/`, `python/scripts/`, and `hooks/scripts/`.

```bash
ob server
ob --json doctor
ob search "what did I decide about X?"
ob save "Decided to use asyncpg for lower overhead" --type decision
ob ingest transcript --source-ref meeting-2026-04-29 --file transcript.txt
ob ingest macwhisper list --limit 5
ob ingest macwhisper entry <entry-id>
ob people list --collisions
```

Current `ob` commands:

| Command | Purpose |
|---|---|
| `ob server` | Start the FastAPI/MCP server from the installed Python package |
| `ob doctor` | Run server diagnostics through the MCP `doctor` tool |
| `ob stats` | Show memory/database statistics |
| `ob search` | Hybrid vector + full-text search |
| `ob concept` | Semantic-only vector search |
| `ob timeline` | Show context around an anchor memory or query |
| `ob get` | Fetch full observations by ID |
| `ob context` | Fetch recent session context |
| `ob save` | Save a memory |
| `ob update` | Update an existing memory |
| `ob ingest email` | Ingest an IMAP inbox through the MCP server |
| `ob ingest transcript` | Ingest a transcript file or stdin |
| `ob ingest macwhisper` | List and ingest transcripts from local MacWhisper history |
| `ob people list` | List person memories and merge candidates through the MCP server |
| `ob people merge` | Merge duplicate person memories server-side |

### `ob people`

Person deduplication runs on the server because remote CLI installs should not
need `DATABASE_URL`.

```bash
ob people list --collisions
ob people merge --source 17692 --target 17700 --dry-run
ob people merge --source 17692 --target 17700
```

Use `--absorb-text` when the source content should be appended to the target as
provenance. The merge repoints mention/interaction references, repoints typed
relationships, updates target aliases, and marks the source with
`metadata.merged_into`.

### `ob ingest transcript`

Ingests a transcript file (or stdin) into open-brain memory.

```bash
# Via MCP server (default — suitable for agents and multi-user setups)
ob ingest transcript --source-ref meeting-2026-04-29 --file transcript.txt

# Direct mode — bypass MCP, call PostgresDataLayer in-process
ob ingest transcript --source-ref meeting-2026-04-29 --file transcript.txt --direct
```

**When to use `--direct`:**
- Local operator scripts with direct database access (`DATABASE_URL` available)
- Batch ingestion pipelines where MCP transport latency adds up
- Dev/test environments without a running open-brain server

**When NOT to use `--direct`:**
- Multi-user setups (bypasses all MCP-layer auth and rate limiting)
- Sandboxed agents that should not have direct DB access
- When you do not control `DATABASE_URL` (rely on MCP auth instead)

`--direct` requires `DATABASE_URL` to be set (env var or `DATABASE_URL=...` line in a `.env` file in the current directory). `VOYAGE_API_KEY` must also be set for embedding calls.

You can also set `OB_DIRECT=1` as an environment variable instead of passing `--direct` on every invocation.

### `ob ingest macwhisper`

Lists and ingests transcript sessions from the local MacWhisper history
directory. For modern MacWhisper SQLite history, `list` shows transcript
sessions/meetings by default, including source app, duration, and detected
participants from MacWhisper speaker data. Dictations remain readable by ID but
are not part of the default meeting/session list. This command intentionally
reads MacWhisper files on the CLI machine, then submits the transcript text to
the configured open-brain server. That means it still works when the server runs
somewhere else.

```bash
# Show recent local MacWhisper transcript sessions/meetings
ob ingest macwhisper list --limit 10

# Check the configured open-brain server and show ingest status
ob ingest macwhisper list --limit 10 --status

# Show only sessions that have no matching macwhisper:<entry-id> source_ref yet
ob ingest macwhisper list --not-ingested

# Machine-readable output
ob ingest macwhisper list --limit 10 --json

# Ingest one entry by ID; defaults to source_ref macwhisper:<entry-id>
ob ingest macwhisper entry <entry-id>

# Use a non-standard history directory
ob ingest macwhisper list --history-path ~/Exports/MacWhisper
ob ingest macwhisper entry <entry-id> --history-path ~/Exports/MacWhisper
```

The history path is auto-discovered from `MACWHISPER_HISTORY_PATH`, the standard
MacWhisper sandbox/application-support directories, or path hints from the `mw`
CLI. `macwhisper entry` defaults the medium hint to `macwhisper` unless the
MacWhisper entry metadata or `--medium-hint` provides a more specific value.
`macwhisper ingest` is accepted as a compatibility alias.

`--status` and `--not-ingested` call the configured open-brain server to compare
each local entry against ingested meeting memories by `metadata.source_ref`.
The default source reference is `macwhisper:<entry-id>`. `--not-ingested` scans
more local entries than it displays by default (`max(limit*5, 50)`); use
`--scan-limit N` to adjust that window.

## Documentation

- [Architecture & Diagrams](docs/architecture.md) — system design, hybrid search, memory lifecycle, auth flow
- [Operator Script Promotion Policy](docs/operator-scripts.md) — which scripts become stable `ob` commands and which stay as scripts
- [Contributing](CONTRIBUTING.md) — development setup, PR process, coding guidelines
- [Security](SECURITY.md) — vulnerability reporting, security considerations
- [Changelog](CHANGELOG.md) — version history

## License

[MIT](LICENSE)
