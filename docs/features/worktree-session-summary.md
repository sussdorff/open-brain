# Worktree Session Summary

POST /api/worktree-session-summary endpoint that ingests turn logs from a worktree coding session, synthesizes them into a structured summary via Haiku, and stores the result as a session_summary memory with rich metadata.

## Was

The worktree session summary feature provides a high-level synthesis endpoint for capturing the essence of a multi-turn coding session. Instead of storing individual turn logs, it accepts a batch of turns (with user input, assistant response, tools used, and timing), uses Claude Haiku to generate a concise summary covering what was worked on, key decisions, and outcomes, and persists the result as a `session_summary` memory with metadata linking back to the worktree, bead, and individual session IDs.

## Für wen

Worktree orchestration tools and plugin hooks that need to:

- **Capture session context at hand-off** — Save a summary when transitioning between worktrees or closing a bead, so that future sessions can quickly recall what was done
- **Track bead progress** — Store turn summaries with bead_id in metadata for project management and retrospectives
- **Enable cross-session learning** — Memories are searchable, so future work can query "how did we solve this before in bead-X?"
- **Audit and traceability** — Metadata captures worktree path, agent, turn count, and timestamp range for compliance and debugging

**Use cases:**

- **At end of worktree session** — Plugin hook calls POST /api/worktree-session-summary with accumulated turns before closing worktree
- **Subagent completion** — When a subagent finishes, capture its turns to preserve decision-making for the parent session
- **Bead closure** — Before marking a bead complete, synthesize final session turns into memory for retrospective analysis
- **Session reconstruction** — Search for past summaries by bead_id or worktree path to understand what was tried and why

## Wie es funktioniert

### Request

**POST /api/worktree-session-summary**

```json
{
  "worktree": ".claude/worktrees/bead-open-brain-x7s",
  "bead_id": "open-brain-x7s",
  "project": "open-brain",
  "turns": [
    {
      "ts": "2026-04-17T10:23:45Z",
      "agent": "claude-code",
      "session_id": "9f3a1b22-5c7e-4a10-8e44-2d1f6b0c9e01",
      "parent_session_id": null,
      "hook_type": "Stop",
      "user_input_excerpt": "Implement /api/worktree-session-summary endpoint",
      "assistant_summary_excerpt": "Scaffolded route with Pydantic validation",
      "tool_calls": [
        {"name": "Edit", "target": "python/src/open_brain/server.py"},
        {"name": "Write", "target": "python/tests/test_worktree_summary.py"}
      ]
    },
    {
      "ts": "2026-04-17T10:31:02Z",
      "agent": "claude-code",
      "session_id": "9f3a1b22-5c7e-4a10-8e44-2d1f6b0c9e01",
      "parent_session_id": null,
      "hook_type": "SubagentStop",
      "user_input_excerpt": "Run tests and check coverage",
      "assistant_summary_excerpt": "8 new tests passing, 630 total",
      "tool_calls": [{"name": "Bash", "target": "uv run pytest"}]
    }
  ]
}
```

#### Request Fields

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| **worktree** | string | optional | Path to worktree (e.g., `.claude/worktrees/bead-x7s`); stored in metadata for traceability |
| **bead_id** | string | optional | Bead identifier (e.g., `open-brain-x7s`); stored in metadata if provided |
| **project** | string | **required** | Project name (validates at request level; 400 if missing) |
| **turns** | array | **required, non-empty** | List of turn objects (400 if empty or missing) |

#### Turn Object Schema

Each object in `turns` array captures one interaction turn:

| Field | Type | Notes |
|-------|------|-------|
| **ts** | ISO8601 string | Timestamp of turn completion (e.g., `2026-04-17T10:23:45Z`) |
| **agent** | string | Agent name (e.g., `claude-code`, `mira-support`) |
| **session_id** | string | Session ID; multiple unique IDs are collected and joined in session_ref |
| **parent_session_id** | string or null | Parent session if this is a subagent; used for tracing call chains |
| **hook_type** | string | Turn type: `Stop`, `SubagentStop`, or user-defined hook identifier |
| **user_input_excerpt** | string | Brief excerpt of user input (max ~100 chars) |
| **assistant_summary_excerpt** | string | Brief excerpt of assistant response (max ~100 chars) |
| **tool_calls** | array | List of tool executions: `[{"name": "Edit", "target": "..."}]` |

### Response

**HTTP 202 Accepted** (immediately, async processing):

```json
{
  "status": "accepted"
}
```

The endpoint validates the request (project required, turns non-empty) and immediately returns 202. Background processing happens asynchronously via `asyncio.create_task()`.

#### Error Responses

| Code | Error | When |
|------|-------|------|
| **400** | `{"error": "project is required"}` | Missing `project` field |
| **400** | `{"error": "turns must be a non-empty array"}` | `turns` is empty or missing |
| **401** | `{"error": "unauthorized", "error_description": "Missing Bearer token or API key"}` | No X-API-Key or Bearer token |

### Background Processing

Once accepted, the endpoint:

1. **Builds turn summary prompt** — Formats all turns into a readable log with timestamps, agent name, hook type, user/assistant excerpts, and tools used
2. **Calls Haiku** — Sends prompt to Claude Haiku 4.5 (model: `claude-haiku-4-5-20251001`) with max_tokens=512
3. **Parses JSON response** — Expects Haiku to return:
   ```json
   {
     "title": "Implemented /api/worktree-session-summary endpoint",
     "content": "3-5 sentence summary of work done",
     "narrative": "Learned about tool logging patterns"
   }
   ```
4. **Builds session_ref** — Collects all unique `session_id` values from turns, sorts them, and joins with commas. If no session_ids present, session_ref is null.
5. **Saves to memory** — Calls `dl.save_memory()` with:
   - **type**: `session_summary`
   - **text**: Haiku-generated content
   - **title**: Haiku-generated headline
   - **narrative**: Haiku-generated narrative (optional)
   - **project**: From request body
   - **session_ref**: Comma-joined unique session_ids (or null)
   - **metadata**: Rich metadata object (see below)

#### Metadata Fields

The saved memory includes structured metadata:

| Key | Type | Notes |
|-----|------|-------|
| **worktree** | string | Path from request (e.g., `.claude/worktrees/bead-x7s`) |
| **agent** | string | Agent name from last turn |
| **bead_id** | string | From request (only if provided) |
| **turn_count** | integer | Total number of turns in batch |
| **last_ts** | ISO8601 | Timestamp of final turn |

#### Logging

On success: `"Worktree session summary saved for {worktree} [{turn_count} turns]"`

On failure: Exception logged with full traceback; memory is not saved.

## Zusammenspiel

- **Plugin hooks** — Orchestration layers call this endpoint when a worktree session ends or a bead completes
- **Memory search** — Future sessions can query by bead_id or project to find past summaries
- **Metadata filtering** — Search API supports filtering by metadata, allowing queries like "worktree contains bead-x7s"
- **Session context** — session_ref allows multiple turns from different sub-sessions to be linked back to a single logical work unit
- **LLM enrichment** — Haiku generates title and narrative; entity extraction and classification (if enabled) happen post-save like other memory types

## Besonderheiten

### Edge Cases

1. **Empty turns array** — Returns 400 (validated at request level, not in background task)
2. **Missing project** — Returns 400 (validated at request level, not in background task)
3. **No unique session_ids** — session_ref is set to null (memory saved normally)
4. **Haiku timeout or error** — Exception caught in background task; logged but no memory saved (silent failure to avoid blocking caller)
5. **Optional bead_id** — Metadata only includes bead_id if provided (not stored as null)
6. **Multiple agents in turns** — Last turn's agent is used for metadata.agent field

### Performance & Timeouts

- **Request validation**: <5ms (schema checks only)
- **Response latency**: <50ms (immediate 202 return, no LLM call in critical path)
- **Background Haiku call**: ~500ms–2s (Haiku latency + JSON parsing)
- **Database write**: ~10ms (save_memory via data layer)
- **Total end-to-end**: ~1-3s (async, doesn't block caller)

### Rate Limiting

The endpoint respects the same rate limits as `save_memory()`:
- **Per-user sliding window**: 10 saves per 60 seconds
- **Daily guard**: MAX_MEMORIES_PER_DAY config limit (if set > 0)

However, since requests are validated before rate limit checks in the background task, invalid requests (missing project, empty turns) are rejected before consuming any quota.

### Authentication

Requires **X-API-Key** header (handled by BearerAuthMiddleware). Bearer token auth also accepted:
- X-API-Key grants "memory" + "evolution" scopes automatically
- Bearer token scopes depend on OAuth client configuration

## Technische Details

### Routes & API

- **HTTP endpoint**: POST /api/worktree-session-summary
- **Authentication**: X-API-Key header (or Bearer token)
- **Returns**: 202 Accepted (or 400/401 on validation error)
- **Background task**: `_process_worktree_session_summary(body: dict)`

### Relevant Code Locations

- Main endpoint — `python/src/open_brain/server.py:1430` (async def api_worktree_session_summary)
- Background processor — `python/src/open_brain/server.py:1459` (async def _process_worktree_session_summary)
- Integration tests — `python/tests/test_worktree_summary.py` (8 tests covering validation, auth, metadata, session_ref logic)

### Dependencies

- **asyncio.create_task** — Spawn background task (standard library)
- **llm_complete** — Call Haiku for summary generation (internal; uses OpenRouter or Anthropic API)
- **parse_llm_json** — Parse JSON from LLM response (internal; handles graceful degradation)
- **SaveMemoryParams** — Data layer contract (already defined)
- **PostgresDataLayer.save_memory** — Persist memory (already defined)

### OpenAPI Compliance

This endpoint does **not** use the `createRoute` + Zod pattern and therefore is **not** included in the auto-generated OpenAPI spec. Callers must document the endpoint manually (as you're reading now).

If you need OpenAPI inclusion, convert the endpoint to use the standard FastAPI + Zod route pattern (currently uses direct `@app.post()` for flexibility with turn log validation).

### Error Handling

- **Request validation**: Synchronous checks (project, turns) → 400 immediately
- **Background errors**: All exceptions caught and logged; no callback to caller (async fire-and-forget pattern)
- **Partial failures**: If Haiku times out or returns invalid JSON, entire save is skipped (no incomplete memory)

## Related Features

- **Memory lifecycle** — Saved summaries are subject to decay and triage like any other memory (can be promoted, archived, merged)
- **Session search** — Summaries are searchable by project, session_ref, bead_id (via metadata filtering)
- **Entity extraction** — If entity extraction is enabled post-save, summaries may be enriched with extracted entities
- **Metadata filtering** — search() and triage_memories() support metadata_filter to find summaries by worktree, bead_id, or agent
