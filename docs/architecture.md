# Architecture

open-brain is an MCP (Model Context Protocol) server that gives AI assistants persistent, searchable memory across sessions and projects.

## System Overview

```
┌─────────────────────────────────────────────────────────┐
│                    AI Assistant                          │
│              (Claude Code, IDE, etc.)                    │
└──────────────┬──────────────────────┬───────────────────┘
               │ MCP Tools            │ Plugin Hooks
               │ (search, save, ...)  │ (auto-capture)
               ▼                      ▼
┌─────────────────────────────────────────────────────────┐
│                   open-brain Server                      │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐             │
│  │ MCP API  │  │ REST API │  │   OAuth   │             │
│  │ (tools)  │  │ (plugin) │  │   2.1     │             │
│  └────┬─────┘  └────┬─────┘  └───────────┘             │
│       │              │                                   │
│  ┌────▼──────────────▼─────┐  ┌───────────────────┐    │
│  │     Data Layer          │  │   LLM Provider    │    │
│  │  (search, CRUD, embed)  │◄─┤ (Anthropic/       │    │
│  │                         │  │  OpenRouter)       │    │
│  └────────────┬────────────┘  └───────────────────┘    │
│               │                                         │
└───────────────┼─────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────┐
│              Postgres 17 + pgvector                      │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐     │
│  │ memories │  │ sessions │  │ memory_usage_log  │     │
│  │ (+ FTS)  │  │          │  │ (priority decay)  │     │
│  │ (+ vec)  │  │          │  │                   │     │
│  └──────────┘  └──────────┘  └───────────────────┘     │
│                                                         │
│  Voyage-4 embeddings (1024 dim) stored in pgvector      │
│  Full-text search via tsvector + GIN index              │
└─────────────────────────────────────────────────────────┘
```

## Hybrid Search

open-brain uses **Reciprocal Rank Fusion (RRF)** to combine two independent ranking systems into a single result set:

```
┌────────────┐     ┌──────────────┐
│  User      │     │  Voyage-4    │
│  Query     │────►│  Embedding   │
└─────┬──────┘     └──────┬───────┘
      │                   │
      ▼                   ▼
┌──────────────┐   ┌──────────────┐
│  Full-Text   │   │   Vector     │
│  Search      │   │   Search     │
│  (tsvector)  │   │  (pgvector)  │
│              │   │  cosine sim  │
└──────┬───────┘   └──────┬───────┘
       │ rank_fts         │ rank_vec
       ▼                  ▼
┌─────────────────────────────────┐
│    Reciprocal Rank Fusion       │
│                                 │
│  score = 1/(k + rank_fts)      │
│        + 1/(k + rank_vec)      │
│                                 │
│  k = 60 (default)              │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│   Optional: Voyage Rerank-2.5   │
│   (second-pass reranking)       │
└────────────┬────────────────────┘
             │
             ▼
         Results
```

**Why RRF?** It balances keyword precision (FTS catches exact terms) with semantic understanding (vectors catch meaning). A query like "how does billing work" finds both documents containing "billing" and documents about "claims processing" or "invoicing."

## Memory Lifecycle

Memories flow through a defined lifecycle from creation to long-term storage:

```
  Save                    Embed                    Search
  ─────►  ┌──────────┐  ─────►  ┌──────────┐  ◄─────  queries
          │  memory   │         │ embedding │
          │  + meta   │         │ + links   │
          │ +entities │         │ + entities│
          └──────────┘         └──────────┘
                                     │
                    ┌────────────────┘
                    ▼
              ┌──────────┐     Automatic consolidation
   Refine ──►│  merge    │     (find duplicates, merge
              │  promote  │      similar, adjust priority)
              │  demote   │
              └──────────┘
                    │
                    ▼
              ┌──────────┐     Human-in-the-loop review
  Triage ───►│ classify  │     (keep, merge, promote,
              │ recommend │      scaffold, archive)
              └──────────┘
                    │
                    ▼
              ┌──────────────┐  Write to persistent targets:
 Materialize─►│ MEMORY.md    │  - Project memory files
              │ bd create    │  - Work item (bead/issue)
              │ standards    │  - Coding standards
              │ skills       │  - Agent skills
              └──────────────┘
```

### Stage Details

| Stage | Tool | Mode | Description |
|-------|------|------|-------------|
| **Save** | `save_memory` | Auto/Manual | Store observation with metadata. Auto-extract entities (people, orgs, tech, locations, dates) and auto-embed async. |
| **Embed** | (internal) | Automatic | Voyage-4 embedding + auto-link to similar memories (cosine > 0.65). |
| **Search** | `search`, `timeline`, `get_observations` | On demand | 3-step funnel: search → context → details. Minimizes token usage. |
| **Refine** | `refine_memories` | Automatic | LLM finds duplicates, merges similar, adjusts priority. Rule-based. |
| **Triage** | `triage_memories` | Human-in-loop | LLM classifies memories; user approves each action. |
| **Materialize** | `materialize_memories` | Semi-auto | Writes approved triage actions to their targets (files, issues, etc.). |

## Authentication Flow

open-brain implements OAuth 2.1 with PKCE for secure client authentication:

```
┌──────────┐                           ┌──────────────┐
│  Client   │  1. GET /authorize        │  open-brain  │
│  (Claude  │ ────────────────────────► │  Server      │
│   Code)   │                           │              │
│           │  2. Login form            │              │
│           │ ◄──────────────────────── │              │
│           │                           │              │
│           │  3. POST /authorize       │              │
│           │     (user + pass + PKCE)  │              │
│           │ ────────────────────────► │              │
│           │                           │              │
│           │  4. Authorization code    │              │
│           │ ◄──────────────────────── │              │
│           │                           │              │
│           │  5. POST /token           │              │
│           │     (code + verifier)     │              │
│           │ ────────────────────────► │              │
│           │                           │              │
│           │  6. Access + Refresh      │              │
│           │     tokens (JWT)          │              │
│           │ ◄──────────────────────── │              │
│           │                           │              │
│           │  7. MCP calls with        │              │
│           │     Bearer token          │              │
│           │ ────────────────────────► │              │
└──────────┘                           └──────────────┘
```

Clients can also authenticate via API key (`x-api-key` header) for plugin/automation use cases.

Dynamic client registration is supported via the `/register` endpoint (RFC 7591).

## Plugin Architecture

The Claude Code plugin provides automatic memory capture without manual MCP tool calls:

```
┌─────────────────────────────────────────┐
│            Claude Code Session          │
│                                         │
│  SessionStart ──► context_inject.py     │
│    Injects recent memories + session    │
│    summaries into conversation start    │
│                                         │
│  Stop / SubagentStop ──► summarize.py   │
│    Generates session summary from       │
│    recent observations, saves to        │
│    open-brain                           │
└─────────────────────────────────────────┘
```

### Plugin Hooks

| Hook | Script | What it does |
|------|--------|-------------|
| `SessionStart` | `context_inject.py` | Fetches recent memories and injects a narrative summary into the session |
| `Stop` | `summarize.py` | Generates and saves a session summary from recent observations |
| `SubagentStop` | `summarize.py` | Same as Stop, for subagent sessions |

### Plugin REST API

The server exposes additional REST endpoints for the plugin:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/context` | GET | Fetch recent memories for context injection |
| `/api/summarize` | POST | Generate and save session summary |
| `/api/session-capture` | POST | Batch extract observations from conversation |
| `/api/memories` | DELETE | Bulk delete memories by IDs or filter |

## Deployment Options

### 1. Standalone Docker Compose

Includes Postgres + open-brain. Best for getting started:

```bash
docker compose up -d
```

### 2. Service-only (Bring Your Own Postgres)

For embedding into existing stacks:

```bash
docker compose -f docker-compose.service.yml up --build -d
```

### 3. Bare Metal

For direct installation without Docker:

```bash
cd python
uv sync
uv run python -m open_brain
```

Requires a running Postgres instance with pgvector.

## Database Schema

### Core Tables

| Table | Purpose |
|-------|---------|
| `memories` | Core observation store — content, embedding, metadata, priority, stability |
| `sessions` | Session isolation — groups memories by conversation session |
| `session_summaries` | Session context — summaries generated at session close |
| `memory_relationships` | Auto-linked similar memories (cosine > 0.65) |
| `memory_usage_log` | Usage tracking for priority decay (search_hit, retrieved, cited) |
| `memory_indexes` | Namespace isolation for multi-tenant setups |

### Key Indexes

- **HNSW vector index** on `memories.embedding` (cosine similarity, m=16, ef_construction=64)
- **GIN full-text index** on `to_tsvector('english', title || ' ' || content)`
- **GIN trigram index** on `content` for fuzzy matching
- Standard B-tree indexes on `type`, `created_at`, `priority`

### Memory Fields

| Field | Type | Purpose |
|-------|------|---------|
| `title` | text | Short headline |
| `subtitle` | text | Secondary label / tags |
| `content` | text | Primary searchable body (embedded + searched) |
| `narrative` | text | Supplementary reasoning / context |
| `embedding` | vector(1024) | Voyage-4 embedding |
| `metadata` | jsonb | Arbitrary structured data — includes auto-extracted `entities` (people, orgs, tech, locations, dates), capture templates, custom fields |
| `priority` | real | Decays based on usage; affects ranking |
| `stability` | text | `tentative` → `stable` → `canonical` |
| `type` | text | Memory type vocabulary (discovery, learning, session_summary, ...) |
