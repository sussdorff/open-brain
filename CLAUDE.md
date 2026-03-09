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

- **Server**: LXC 116 (Elysium Proxmox), SSH host `services`, path `/opt/open-brain/`
- **Runtime**: Python 3.14 via uv, systemd unit `open-brain.service`
- **Secrets**: 1Password via `op run --env-file=.env.tpl` (never on disk)
- **Port**: 8091

### Deploy (after git push)

```bash
ssh services /opt/open-brain/deploy/deploy.sh
```

This runs: `git pull --ff-only && uv sync && systemctl restart open-brain`

### Verify

```bash
ssh services 'curl -s http://localhost:8091/health'
# Expected: {"status":"ok","service":"open-brain","runtime":"python"}
```

### Server-only files (not in git)

- `.env.tpl` -- 1Password secret references (MCP_SERVER_URL, AUTH_USER, etc.)
- `clients.json` -- OAuth client registrations
- `logs/` -- Application logs

### One-time setup (disaster recovery)

1. Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. Install Python: `uv python install 3.14`
3. Deploy key: `ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_github` + add to GitHub repo
4. Clone: `git clone git@github.com:sussdorff/open-brain.git /opt/open-brain`
5. Deps: `cd /opt/open-brain/python && uv sync --python 3.14`
6. Copy `.env.tpl` and `clients.json` from backup
7. Install systemd unit (see `deploy/` or git history for template)
8. `systemctl enable --now open-brain`

## Key Decisions

- Python (simpler deployment, UV for dependency management)
- Voyage-4 for embeddings (14% better retrieval vs. text-embedding-3-small)
- Claude Haiku 4.5 for metadata extraction (not gpt-4o-mini)
- Shared Postgres instance with Langfuse on LXC 116
- No Redis, no Web UI (keep it simple)
