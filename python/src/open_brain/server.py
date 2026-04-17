"""FastAPI + FastMCP server with OAuth 2.1 middleware."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import logging
import re
import secrets
import time
from collections import deque
from dataclasses import asdict
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import asyncpg.exceptions
import httpx

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

from open_brain.auth.provider import get_provider
from open_brain.auth.tokens import verify_token
from open_brain.config import get_config
from open_brain.data_layer.interface import (
    DecayParams,
    DeleteParams,
    MaterializeParams,
    RefineParams,
    SaveMemoryParams,
    SearchParams,
    TimelineParams,
    TriageParams,
    UpdateMemoryParams,
    validate_domain_metadata,
)
from open_brain.capture_router import classify_and_extract
from open_brain.data_layer.llm import LlmMessage, llm_complete
from open_brain.utils import parse_llm_json
from open_brain.data_layer.postgres import PostgresDataLayer, close_pool, get_pool
from open_brain.digest import generate_weekly_briefing
from open_brain.evolution import (
    analyze_engagement,
    generate_suggestion,
    log_evolution_approval as _log_evolution_approval,
    query_evolution_history,
)

logger = logging.getLogger(__name__)

# ContextVar to track the authenticated user ID for the current request
_current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)

# Rate limiter for save_memory: per-user sliding window of timestamps (max 10 per 60 seconds)
_save_timestamps: dict[str, deque[float]] = {}
_RATE_LIMIT_MAX = 10
_RATE_LIMIT_WINDOW = 60
_MAX_TURNS_TEXT = 8000

# ContextVar to track the OAuth scopes for the current request (Bearer token auth only)
_current_scopes: ContextVar[tuple[str, ...]] = ContextVar("current_scopes", default=())

# ContextVar set to True when the request was authenticated via x-api-key header.
# Token management endpoints accept either admin OAuth scope OR direct API key auth.
_is_api_key_auth: ContextVar[bool] = ContextVar("is_api_key_auth", default=False)

# Evolution tools require the `evolution` OAuth scope
_EVOLUTION_TOOLS: frozenset[str] = frozenset({
    "weekly_briefing",
    "analyze_briefing_engagement",
    "generate_evolution_suggestion",
    "log_evolution_approval",
    "query_evolution_history_tool",
})

# Admin tools require the `admin` OAuth scope (none defined yet)
_ADMIN_TOOLS: frozenset[str] = frozenset()

# Valid scopes that can be granted to URL tokens
_VALID_URL_TOKEN_SCOPES: frozenset[str] = frozenset({"memory", "evolution"})


_ENTITY_EXTRACTION_PROMPT = """\
Extract named entities from the following text and return ONLY valid JSON (no markdown fences):
{"people": [...], "orgs": [...], "tech": [...], "locations": [...], "dates": [...]}

Rules:
- people: named individuals only
- orgs: companies, organizations, institutions
- tech: programming languages, frameworks, tools, platforms, services
- locations: geographic places, cities, countries, regions
- dates: time references, periods, years
- Use empty arrays if no entities of that type found

Text: """

_ENTITY_KEYS = {"people", "orgs", "tech", "locations", "dates"}


async def _extract_entities(text: str) -> dict:
    """Extract named entities from text using Haiku.

    Args:
        text: The text to extract entities from.

    Returns:
        Dict with keys people, orgs, tech, locations, dates (lists of strings).
        Returns empty dict on failure or empty text.

    Trust boundary: text is assumed to be user-controlled input.
    Not suitable for multi-tenant use without content isolation.
    """
    if not text or not text.strip():
        return {}
    try:
        response = await llm_complete(
            messages=[LlmMessage(role="user", content=_ENTITY_EXTRACTION_PROMPT + text)],
            model="claude-haiku-4-5-20251001",
        )
        parsed = parse_llm_json(response)
        # Filter to expected keys only; strip non-string items from lists (prevents hallucinated keys/values)
        entities = {}
        for k, v in parsed.items():
            if k in _ENTITY_KEYS and isinstance(v, list):
                filtered = [item for item in v if isinstance(item, str)]
                if filtered:
                    entities[k] = filtered
        return entities
    except Exception:
        logger.warning("Entity extraction failed — continuing without entities", exc_info=True)
        return {}


class ScopeDeniedError(PermissionError):
    """Raised when the current OAuth caller lacks a required scope.

    Subclasses PermissionError for broad except compatibility, but carries
    a clearer semantic than the stdlib PermissionError (which is filesystem-oriented).
    """


def _require_scope(scope: str) -> None:
    """Raise ScopeDeniedError if the current caller lacks the required OAuth scope.

    Args:
        scope: The scope name that must be present in _current_scopes.

    Raises:
        ScopeDeniedError: If the required scope is not in the current request's scopes.
    """
    if scope not in _current_scopes.get():
        raise ScopeDeniedError(
            f"Scope '{scope}' required"
        )


class ScopedFastMCP(FastMCP):
    """FastMCP subclass that filters tool list based on OAuth scopes in current context.

    Tools in _EVOLUTION_TOOLS are only listed when the caller has the 'evolution' scope.
    Tools in _ADMIN_TOOLS are only listed when the caller has the 'admin' scope.
    All other tools are always listed (authentication is enforced by BearerAuthMiddleware).
    """

    async def list_tools(self):
        """Return tools filtered by the current request's OAuth scopes."""
        all_tools = await super().list_tools()
        scopes = _current_scopes.get()
        filtered = []
        for tool in all_tools:
            if tool.name in _EVOLUTION_TOOLS and "evolution" not in scopes:
                continue
            if tool.name in _ADMIN_TOOLS and "admin" not in scopes:
                continue
            filtered.append(tool)
        return filtered


# MCP server (ScopedFastMCP for scope-gated tool listing)
_config = get_config()
_host = _config.MCP_SERVER_URL.replace("https://", "").replace("http://", "").rstrip("/")
mcp = ScopedFastMCP(
    "open-brain",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[_host, f"{_host}:443"],
    ),
)

# Server start time — set in lifespan, used for uptime calculation
_server_start_time: datetime | None = None

# Shared data layer instance
_dl: PostgresDataLayer | None = None


def get_dl() -> PostgresDataLayer:
    """Return the shared PostgresDataLayer instance."""
    global _dl
    if _dl is None:
        _dl = PostgresDataLayer()
    return _dl


# ─── MCP Tools ───────────────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "3-LAYER WORKFLOW (ALWAYS FOLLOW):\n"
        "1. search(query) -> Get index with IDs (~50-100 tokens/result)\n"
        "2. timeline(anchor=ID) -> Get context around interesting results\n"
        "3. get_observations([IDs]) -> Fetch full details ONLY for filtered IDs\n"
        "NEVER fetch full details without filtering first. 10x token savings."
    )
)
async def __IMPORTANT() -> str:  # noqa: N802
    """Workflow reminder tool."""
    return "This is a workflow reminder tool. Use search -> timeline -> get_observations for efficient memory access."


@mcp.tool(
    description="Step 1: Search memory (hybrid: vector + FTS). Returns index with IDs. "
    "Browse mode: omit query (or use '*') to list memories with filters only. "
    "Params: query, limit, project, type, obs_type, dateStart, dateEnd, offset, orderBy, filePath, metadata_filter, author"
)
async def search(
    query: str | None = None,
    limit: int | None = None,
    project: str | None = None,
    type: str | None = None,
    obs_type: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    offset: int | None = None,
    order_by: str | None = None,
    file_path: str | None = None,
    metadata_filter: dict | None = None,
    author: str | None = None,
) -> str:
    """Step 1: Hybrid memory search or browse mode."""
    dl = get_dl()
    result = await dl.search(
        SearchParams(
            query=query,
            limit=limit,
            project=project,
            type=type,
            obs_type=obs_type,
            date_start=date_start,
            date_end=date_end,
            offset=offset,
            order_by=order_by,
            file_path=file_path,
            metadata_filter=metadata_filter,
            author=author,
        )
    )
    return json.dumps(
        {"total": result.total, "results": [vars(m) for m in result.results]},
        default=str,
    )


@mcp.tool(
    description="Step 2: Get context around results. Two modes: "
    "(a) Anchor mode: anchor (observation ID) OR query (finds anchor automatically), shows N memories before/after. "
    "(b) Date window mode: date_start and/or date_end (ISO format), browse memories in that time range. "
    "Params: anchor, query, depth_before, depth_after, project, date_start, date_end"
)
async def timeline(
    anchor: int | None = None,
    query: str | None = None,
    depth_before: int | None = None,
    depth_after: int | None = None,
    project: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
) -> str:
    """Step 2: Anchor-based or date-window timeline context."""
    dl = get_dl()
    result = await dl.timeline(
        TimelineParams(
            anchor=anchor,
            query=query,
            depth_before=depth_before,
            depth_after=depth_after,
            project=project,
            date_start=date_start,
            date_end=date_end,
        )
    )
    return json.dumps(
        {
            "anchor_id": result.anchor_id,
            "results": [vars(m) for m in result.results],
        },
        default=str,
    )


@mcp.tool(
    description="Step 3: Fetch full details for filtered IDs. "
    "Params: ids (array of observation IDs, required)"
)
async def get_observations(ids: list[int]) -> str:
    """Step 3: Bulk fetch memories by IDs."""
    dl = get_dl()
    memories = await dl.get_observations(ids)
    return json.dumps([vars(m) for m in memories], default=str)


@mcp.tool(
    description="Save a new observation to memory (auto-embeds via Voyage). "
    "project is REQUIRED — use git repo name, folder name, or Claude Desktop project name. If ambiguous, ask the user. "
    "type: check existing types via stats() before inventing new ones. Prefer existing vocabulary "
    "(discovery, change, feature, decision, bugfix, refactor, session_summary). New types are allowed when none fit. "
    "text: PRIMARY content — put the main substance here. Required. Gets embedded and full-text searched. "
    "Do NOT leave text minimal while putting all substance in narrative. "
    "title: short headline (1 line). subtitle: secondary label, tags, or category hint. "
    "narrative: optional prose context or reasoning that SUPPLEMENTS text (also embedded). Use for background story or 'why'. "
    "session_ref: optional stable identifier (e.g. bead ID or 'session-YYYY-MM-DD'). When type='session_summary' "
    "and session_ref is set, re-calling with the same session_ref updates the existing memory instead of inserting a duplicate. "
    "is_test: set to true to skip persistence (returns mock response, useful for integration tests). "
    "DOMAIN SCHEMAS — structured metadata by type: "
    "event: {when (ISO datetime, required), who: [str], where: str, recurrence: str}. "
    "person: {name: str, org: str, role: str, relationship: str, last_contact: ISO datetime}. "
    "meeting: {attendees: [str], topic: str, key_points: [str], action_items: [str], date: ISO datetime}. "
    "decision: {what: str, context: str, owner: str, alternatives: [str], rationale: str}. "
    "household: {category: str, item: str, location: str, details: str, warranty_expiry: ISO datetime}. "
    "ISO datetime format: 'YYYY-MM-DDTHH:MM:SS' (e.g. '2026-04-15T10:00:00'). "
    "Invalid or missing required datetime fields produce a warning in the response but still save the memory. "
    "Params: text (required), type, project, title, subtitle, narrative, session_ref, is_test, metadata"
)
async def save_memory(
    text: str,
    type: str | None = None,
    project: str | None = None,
    title: str | None = None,
    subtitle: str | None = None,
    narrative: str | None = None,
    session_ref: str | None = None,
    is_test: bool = False,
    metadata: dict | None = None,
) -> str:
    """Save a new memory entry."""
    if is_test:
        return json.dumps({"id": -1, "message": "Test artifact — not persisted"})

    # ── Rate limit check (per-user sliding window, 10/60s) ─────────────────────
    # Note: not atomic — concurrent coroutines may slightly exceed the limit. Acceptable for soft guardrail.
    now = time.monotonic()
    user_key = _current_user_id.get() or "__anonymous__"
    if user_key not in _save_timestamps:
        _save_timestamps[user_key] = deque()
    user_timestamps = _save_timestamps[user_key]
    # Prune timestamps older than the window
    while user_timestamps and now - user_timestamps[0] > _RATE_LIMIT_WINDOW:
        user_timestamps.popleft()
    if len(user_timestamps) >= _RATE_LIMIT_MAX:
        oldest = user_timestamps[0]
        retry_in = int(_RATE_LIMIT_WINDOW - (now - oldest)) + 1
        return json.dumps({
            "error": "rate_limit_exceeded",
            "message": f"Rate limit exceeded: {_RATE_LIMIT_MAX} saves/{_RATE_LIMIT_WINDOW}s. Try again in {retry_in} seconds.",
        })

    # ── Daily guard check ──────────────────────────────────────────────────────
    config = get_config()
    if config.MAX_MEMORIES_PER_DAY > 0:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # DB query for accuracy across restarts and multi-instance deployments
            today_count = await conn.fetchval(
                "SELECT COUNT(*)::int FROM memories WHERE created_at >= CURRENT_DATE"
            ) or 0
        if today_count >= config.MAX_MEMORIES_PER_DAY:
            return json.dumps({
                "error": "daily_limit_exceeded",
                "message": f"Daily memory limit of {config.MAX_MEMORIES_PER_DAY} exceeded. {today_count} memories saved today.",
            })

    # Rate-limit slot claimed only after all guards pass
    user_timestamps.append(now)

    dl = get_dl()
    user_id = _current_user_id.get()

    # classify_and_extract handles bypass conditions internally (returns existing_metadata
    # unchanged when capture_template already set or type=session_summary).
    # Entity extraction is skipped when "entities" key already present in metadata.
    has_entities = isinstance(metadata, dict) and "entities" in metadata

    save_params = SaveMemoryParams(
        text=text,
        type=type,
        project=project,
        title=title,
        subtitle=subtitle,
        narrative=narrative,
        session_ref=session_ref,
        metadata=metadata,
        user_id=user_id,
    )

    # Save first — must know if duplicate before firing expensive LLM calls.
    result = await dl.save_memory(save_params)

    # Skip LLM enrichment entirely for duplicates — no wasted API calls, no update_memory.
    if result.duplicate_of is None:
        # Run classification and entity extraction concurrently (non-duplicate path only).
        classify_coro = classify_and_extract(text, existing_metadata=metadata, memory_type=type)
        if not has_entities:
            entities_coro = _extract_entities(text)
            entities_result, classification = await asyncio.gather(entities_coro, classify_coro)
        else:
            classification = await classify_coro
            entities_result = {}

        post_save_metadata: dict[str, Any] = {}

        # Add classification metadata (guard: skip if bypass returned metadata unchanged,
        # and only send keys that differ from the original metadata to avoid redundant writes)
        if classification != (metadata or {}):
            post_save_metadata = {k: v for k, v in classification.items() if (metadata or {}).get(k) != v}

        # Add entity extraction results
        if entities_result:
            post_save_metadata["entities"] = entities_result

        if post_save_metadata:
            try:
                await dl.update_memory(
                    UpdateMemoryParams(id=result.id, metadata=post_save_metadata)
                )
            except Exception:
                logger.exception("save_memory: post-save metadata update failed (classification/entities)")

    payload: dict = {"id": result.id, "message": result.message}
    if result.duplicate_of is not None:
        payload["duplicate_of"] = result.duplicate_of

    # Domain metadata validation (warns but never blocks save)
    domain_warnings = validate_domain_metadata(type, metadata)
    if domain_warnings:
        payload["warning"] = "; ".join(domain_warnings)

    return json.dumps(payload)


@mcp.tool(
    description="Update an existing memory by ID. Only provided fields are changed. "
    "Re-embeds automatically if text/title/subtitle/narrative change. "
    "Use to correct, consolidate, or enrich existing memories instead of creating duplicates. "
    "text: PRIMARY content (gets embedded+searched). narrative: supplementary prose context/reasoning. "
    "metadata: JSONB-merged into existing metadata (use to add/update keys without overwriting others). "
    "Params: id (required), text, type, project, title, subtitle, narrative, metadata"
)
async def update_memory(
    id: int,
    text: str | None = None,
    type: str | None = None,
    project: str | None = None,
    title: str | None = None,
    subtitle: str | None = None,
    narrative: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Update an existing memory entry."""
    dl = get_dl()
    result = await dl.update_memory(
        UpdateMemoryParams(
            id=id,
            text=text,
            type=type,
            project=project,
            title=title,
            subtitle=subtitle,
            narrative=narrative,
            metadata=metadata,
        )
    )
    return json.dumps({"id": result.id, "message": result.message})


@mcp.tool(
    description="Semantic search across memories using vector embeddings. "
    "Params: query (required), limit, project"
)
async def search_by_concept(
    query: str,
    limit: int | None = None,
    project: str | None = None,
) -> str:
    """Pure vector search by concept."""
    dl = get_dl()
    result = await dl.search_by_concept(query, limit, project)
    return json.dumps(
        {"results": [vars(m) for m in result["results"]]}, default=str
    )


@mcp.tool(description="Get recent session context. Params: limit, project")
async def get_context(limit: int | None = None, project: str | None = None) -> str:
    """Get recent sessions."""
    dl = get_dl()
    result = await dl.get_context(limit, project)
    return json.dumps(result, default=str)


@mcp.tool(description="Get database statistics (memory count, sessions, DB size, type taxonomy with counts)")
async def stats() -> str:
    """Get DB statistics."""
    dl = get_dl()
    result = await dl.stats()
    return json.dumps(result, default=str)


# ── Shared health-check helpers ───────────────────────────────────────────────

async def _check_db_status() -> dict:
    """Check DB connectivity and return status dict.

    Returns:
        Dict with keys: db_status, db_latency_ms, memory_count, last_ingestion_at.
    """
    db_status = "unreachable"
    db_latency_ms: float | None = None
    memory_count = 0
    last_ingestion_at: str | None = None
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            t0 = time.monotonic()
            await conn.fetchval("SELECT 1")
            db_latency_ms = round((time.monotonic() - t0) * 1000, 2)
            db_status = "ok"
            memory_count = await conn.fetchval("SELECT COUNT(*)::int FROM memories") or 0
            last_row = await conn.fetchrow("SELECT MAX(created_at) AS max FROM memories")
            if last_row and last_row["max"] is not None:
                last_ingestion_at = last_row["max"].isoformat()
    except Exception:
        logger.warning("Health check: DB unreachable", exc_info=True)
    return {
        "db_status": db_status,
        "db_latency_ms": db_latency_ms,
        "memory_count": memory_count,
        "last_ingestion_at": last_ingestion_at,
    }


async def _check_voyage_api_status() -> str:
    """Check Voyage API reachability.

    Returns:
        One of "ok", "degraded", "unreachable".
    """
    config = get_config()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                "https://api.voyageai.com/v1/models",
                headers={"Authorization": f"Bearer {config.VOYAGE_API_KEY}"},
            )
        return "ok" if resp.status_code == 200 else "degraded"
    except Exception:
        logger.warning("Health check: Voyage API unreachable", exc_info=True)
        return "unreachable"


@mcp.tool(description="Run diagnostic health check: DB latency, Voyage API status, memory stats, uptime")
async def doctor() -> str:
    """Return a structured diagnostic report for operational health monitoring.

    Returns:
        JSON string with db_latency_ms, db_status, voyage_api_status,
        memory_count, last_ingestion_at, server_version, uptime_seconds.
    """
    db_info = await _check_db_status()
    voyage_api_status = await _check_voyage_api_status()

    # ── Server metadata ───────────────────────────────────────────────────────
    try:
        server_version = importlib.metadata.version("open-brain")
    except importlib.metadata.PackageNotFoundError:
        server_version = "unknown"

    uptime_seconds = (datetime.now(UTC) - _server_start_time).total_seconds() if _server_start_time else None

    return json.dumps(
        {
            "db_latency_ms": db_info["db_latency_ms"],
            "db_status": db_info["db_status"],
            "voyage_api_status": voyage_api_status,
            "memory_count": db_info["memory_count"],
            "last_ingestion_at": db_info["last_ingestion_at"],
            "server_version": server_version,
            "uptime_seconds": round(uptime_seconds, 3) if uptime_seconds is not None else None,
        }
    )


@mcp.tool(
    description="Consolidate, deduplicate, and refine memories. "
    "Uses configured LLM for intelligent analysis. Params: scope, limit, dry_run"
)
async def refine_memories(
    scope: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> str:
    """LLM-powered memory refinement."""
    dl = get_dl()
    result = await dl.refine_memories(
        RefineParams(scope=scope, limit=limit, dry_run=dry_run)
    )
    return json.dumps(
        {
            "analyzed": result.analyzed,
            "summary": result.summary,
            "actions": [vars(a) for a in result.actions],
        }
    )


@mcp.tool(
    description="Classify memories into lifecycle actions: keep | merge | promote | scaffold | archive. "
    "Uses LLM with type-aware logic: learning→promote, session_summary→archive, observation→keep/merge. "
    "Params: scope (recent|project:<name>|type:<name>|low-priority|session_ref:<prefix>), limit, dry_run"
)
async def triage_memories(
    scope: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> str:
    """LLM-powered memory triage."""
    dl = get_dl()
    result = await dl.triage_memories(
        TriageParams(scope=scope, limit=limit, dry_run=dry_run)
    )
    return json.dumps(
        {
            "analyzed": result.analyzed,
            "summary": result.summary,
            "actions": [vars(a) for a in result.actions],
        }
    )


@mcp.tool(
    description="Execute materialization for triage actions. "
    "promote→writes to ~/.claude/projects/<project>/MEMORY.md. "
    "scaffold→runs bd create to make a task/bead. "
    "archive→sets memory priority to 0.1. "
    "merge→delegates to refine_memories. "
    "keep→no-op. "
    "Params: triage_actions (list of {action, memory_id, reason, memory_type, memory_title}), dry_run"
)
async def materialize_memories(
    triage_actions: list[dict],
    dry_run: bool = False,
) -> str:
    """Materialize triage actions (promote, scaffold, archive, merge, keep)."""
    from open_brain.data_layer.interface import TriageAction

    actions = [
        TriageAction(
            action=a["action"],
            memory_id=a["memory_id"],
            reason=a.get("reason", ""),
            memory_type=a.get("memory_type", "observation"),
            memory_title=a.get("memory_title"),
            executed=False,
        )
        for a in triage_actions
    ]

    dl = get_dl()
    result = await dl.materialize_memories(
        MaterializeParams(triage_actions=actions, dry_run=dry_run)
    )
    return json.dumps(
        {
            "processed": result.processed,
            "summary": result.summary,
            "results": [vars(r) for r in result.results],
        }
    )


@mcp.tool(
    description="Run the full memory lifecycle pipeline: triage → materialize. "
    "Returns a structured report with triage summary, actions taken, and materialization results. "
    "Params: scope, dry_run"
)
async def run_lifecycle_pipeline(
    scope: str | None = None,
    dry_run: bool = False,
) -> str:
    """Chain decay_memories → triage_memories → materialize_memories into one pipeline run."""
    dl = get_dl()

    # Step 0: Decay — reduce priority of stale memories, boost frequently accessed ones
    decay_result = await dl.decay_memories(DecayParams(dry_run=dry_run))

    # Step 1: Triage
    triage_result = await dl.triage_memories(
        TriageParams(scope=scope, dry_run=dry_run)
    )

    if not triage_result.actions:
        return json.dumps(
            {
                "decay_summary": decay_result.summary,
                "triage_summary": triage_result.summary,
                "materialization_summary": "No actions to materialize",
                "actions_taken": [],
            }
        )

    # Step 2: Materialize non-keep actions (keep is a no-op, skip for brevity unless dry_run)
    non_keep = [a for a in triage_result.actions if a.action != "keep"]
    keep_count = len(triage_result.actions) - len(non_keep)

    mat_result = await dl.materialize_memories(
        MaterializeParams(triage_actions=non_keep, dry_run=dry_run)
    )

    # Step 3: Build pipeline report
    action_counts: dict[str, int] = {}
    for a in triage_result.actions:
        action_counts[a.action] = action_counts.get(a.action, 0) + 1

    return json.dumps(
        {
            "decay_summary": decay_result.summary,
            "triage_summary": triage_result.summary,
            "triage_action_counts": action_counts,
            "materialization_summary": mat_result.summary,
            "actions_taken": [vars(r) for r in mat_result.results],
            "kept_count": keep_count,
            "dry_run": dry_run,
        }
    )


# ─── FastAPI App ──────────────────────────────────────────────────────────────

# Build the MCP sub-app first so session_manager is available
_mcp_app = mcp.streamable_http_app()

# OAuth paths that must remain unauthenticated
_PUBLIC_PATHS = {
    "/authorize", "/authorize/submit", "/token", "/register", "/revoke",
    "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource", "/health",
}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validate Bearer tokens on MCP endpoints (everything not in _PUBLIC_PATHS)."""

    def _get_api_keys(self) -> frozenset[str]:
        """Return valid API keys from current config (config is cached by get_config())."""
        config = get_config()
        return frozenset(k.strip() for k in config.API_KEYS.split(",") if k.strip())

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        # Check API key first (for plugin hooks)
        api_key = request.headers.get("x-api-key", "")
        if api_key:
            if api_key in self._get_api_keys():
                # API keys grant memory + evolution scopes for MCP tool access.
                # Admin scope is intentionally excluded — admin is reserved for
                # OAuth sessions. Token management endpoints accept _is_api_key_auth
                # as an alternative to admin scope.
                scopes_token = _current_scopes.set(("memory", "evolution"))
                api_key_token = _is_api_key_auth.set(True)
                try:
                    return await call_next(request)
                finally:
                    _current_scopes.reset(scopes_token)
                    _is_api_key_auth.reset(api_key_token)
            return JSONResponse(
                {"error": "unauthorized", "error_description": "Invalid API key"},
                status_code=401,
            )

        # Check URL token (?token= query param) — third auth path
        url_token_raw = request.query_params.get("token", "")
        if url_token_raw:
            # Redact token value in query_string BEFORE calling call_next (for access logs)
            raw_qs = request.scope.get("query_string", b"")
            request.scope["query_string"] = re.sub(
                rb"(token=)[^&]+", rb"\1REDACTED", raw_qs
            )

            token_hash = hashlib.sha256(url_token_raw.encode()).hexdigest()
            try:
                pool = await get_pool()
                row = await pool.fetchrow(
                    """
                    SELECT name, scopes, expires_at
                    FROM url_tokens
                    WHERE token_hash = $1
                      AND revoked_at IS NULL
                      AND expires_at > NOW()
                    """,
                    token_hash,
                )
            except Exception:
                logger.exception("URL token DB lookup failed")
                return JSONResponse(
                    {"error": "service_unavailable", "error_description": "Token verification temporarily unavailable"},
                    status_code=503,
                )

            if row is None:
                return JSONResponse(
                    {"error": "unauthorized", "error_description": "Invalid, expired, or revoked URL token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Parse scopes from DB row; strip admin scope (never grantable)
            raw_scopes = row["scopes"]
            if isinstance(raw_scopes, str):
                scopes_list = json.loads(raw_scopes)
            else:
                scopes_list = list(raw_scopes)
            safe_scopes = tuple(s for s in scopes_list if s != "admin")

            scopes_token = _current_scopes.set(safe_scopes)
            try:
                return await call_next(request)
            finally:
                _current_scopes.reset(scopes_token)

        # Fall back to Bearer token
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"error": "unauthorized", "error_description": "Missing Bearer token or API key"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = auth_header[7:]  # Strip "Bearer "
        try:
            # verify_token validates signature + expiry; verify_access_token checks revocation
            verified = verify_token(token)
            provider = get_provider()
            provider.verify_access_token(token)
            user_token = _current_user_id.set(verified.sub)
            scopes_token = _current_scopes.set(tuple(verified.scopes))
        except Exception:
            return JSONResponse(
                {"error": "unauthorized", "error_description": "Invalid or expired token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            return await call_next(request)
        finally:
            _current_user_id.reset(user_token)
            _current_scopes.reset(scopes_token)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage MCP session manager and DB pool lifecycle.

    Server-side files expected at their configured paths (NOT in git):
    - config.CLIENTS_FILE (default: /opt/open-brain/clients.json) — OAuth client registrations
    - config.USERS_FILE   (default: /opt/open-brain/users.json)   — multi-user credentials
      Format: [{"username": "alice", "password": "secret"}, ...]
      If absent, falls back to single-user AUTH_USER/AUTH_PASSWORD env vars.
    """
    global _server_start_time
    config = get_config()
    _server_start_time = datetime.now(UTC)
    logger.info("open-brain starting on port %d", config.PORT)
    async with mcp.session_manager.run():
        yield
    await close_pool()


app = FastAPI(title="open-brain MCP Server", version="0.1.0", lifespan=lifespan)
app.add_middleware(BearerAuthMiddleware)


@app.exception_handler(ScopeDeniedError)
async def scope_denied_handler(request: Request, exc: ScopeDeniedError) -> JSONResponse:
    """Return 403 Forbidden when a caller lacks the required OAuth scope."""
    return JSONResponse(
        {"error": "forbidden", "error_description": str(exc)},
        status_code=403,
    )


# ─── OAuth 2.1 Routes ─────────────────────────────────────────────────────────

@app.get("/authorize")
async def authorize(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    response_type: str = "code",
    state: str = "",
    scope: str = "",
) -> HTMLResponse:
    """Render the OAuth login form."""
    provider = get_provider()
    # Load client name from clients file if available
    client_name = _load_client_name(client_id)
    html = provider.build_login_form(
        client_name=client_name,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        state=state,
        scopes=scope.split() if scope else [],
    )
    return HTMLResponse(content=html)


@app.post("/authorize/submit")
async def authorize_submit(
    username: str = Form(...),
    password: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    code_challenge: str = Form(...),
    state: str = Form(default=""),
    scopes: str = Form(default=""),
) -> RedirectResponse:
    """Handle login form submission."""
    provider = get_provider()
    _success, redirect_url = provider.handle_login_submit(
        username=username,
        password=password,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        state=state,
        scopes_str=scopes,
    )
    return RedirectResponse(url=redirect_url, status_code=302)


@app.post("/token")
async def token_endpoint(request: Request) -> JSONResponse:
    """OAuth token endpoint: code exchange and refresh."""
    body = await request.form()
    grant_type = body.get("grant_type")
    provider = get_provider()

    if grant_type == "authorization_code":
        code = body.get("code", "")
        client_id = body.get("client_id", "")
        try:
            tokens = provider.exchange_authorization_code(str(client_id), str(code))
            return JSONResponse({
                "access_token": tokens.access_token,
                "token_type": tokens.token_type,
                "expires_in": tokens.expires_in,
                "refresh_token": tokens.refresh_token,
            })
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    elif grant_type == "refresh_token":
        refresh_token = body.get("refresh_token", "")
        client_id = body.get("client_id", "")
        try:
            tokens = provider.exchange_refresh_token(str(client_id), str(refresh_token))
            return JSONResponse({
                "access_token": tokens.access_token,
                "token_type": tokens.token_type,
                "expires_in": tokens.expires_in,
                "refresh_token": tokens.refresh_token,
            })
        except (ValueError, Exception) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    raise HTTPException(status_code=400, detail="Unsupported grant_type")


@app.post("/revoke")
async def revoke_endpoint(request: Request) -> JSONResponse:
    """OAuth token revocation endpoint."""
    body = await request.form()
    token = body.get("token", "")
    provider = get_provider()
    provider.revoke_token(str(token))
    return JSONResponse({})


@app.post("/register")
async def register_client(request: Request) -> JSONResponse:
    """Dynamic Client Registration (RFC 7591)."""
    body = await request.json()
    provider = get_provider()
    client = provider.register_client(
        client_name=body.get("client_name", "MCP Client"),
        redirect_uris=body.get("redirect_uris", []),
        grant_types=body.get("grant_types", ["authorization_code", "refresh_token"]),
        response_types=body.get("response_types", ["code"]),
        scope=body.get("scope", ""),
    )
    return JSONResponse(
        {
            "client_id": client.client_id,
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "grant_types": client.grant_types,
            "response_types": client.response_types,
            "scope": client.scope,
            "client_id_issued_at": client.client_id_issued_at,
        },
        status_code=201,
    )


# ─── URL Token Endpoints ──────────────────────────────────────────────────────

@app.post("/token/url")
async def issue_url_token(request: Request) -> JSONResponse:
    """Issue a URL-based opaque token (requires admin scope).

    Body: {"name": str, "scopes": [str], "expires_in_days": int}
    Returns: {"token": str, "name": str, "scopes": [str], "expires_at": str}

    The raw token is returned exactly once — it is never stored.
    Requesting admin scope returns 422 — admin is never grantable to URL tokens.
    """
    if not _is_api_key_auth.get():
        _require_scope("admin")

    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "name is required"},
            status_code=400,
        )

    requested_scopes: list[str] = [s for s in body.get("scopes", []) if isinstance(s, str)]
    # Reject if admin scope is explicitly requested — never grantable to URL tokens (AK6)
    if "admin" in requested_scopes:
        return JSONResponse(
            {"error": "invalid_scope", "error_description": "admin scope cannot be granted to URL tokens"},
            status_code=422,
        )
    # no-op after 422 guard above; retained as belt-and-suspenders
    safe_scopes = [s for s in requested_scopes if s != "admin"]

    # Validate scope names against known valid scopes
    for scope in safe_scopes:
        if scope not in _VALID_URL_TOKEN_SCOPES:
            return JSONResponse(
                {"error": "invalid_request", "error_description": f"Unknown scope: {scope}"},
                status_code=422,
            )

    try:
        expires_in_days = int(body.get("expires_in_days", 365))
    except (ValueError, TypeError):
        return JSONResponse(
            {"error": "invalid_request", "error_description": "expires_in_days must be a positive integer"},
            status_code=400,
        )
    if expires_in_days <= 0:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "expires_in_days must be positive"},
            status_code=400,
        )
    if expires_in_days > 3650:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "expires_in_days cannot exceed 3650 (10 years)"},
            status_code=400,
        )

    raw_token = secrets.token_hex(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(days=expires_in_days)

    pool = await get_pool()
    try:
        await pool.execute(
            """
            INSERT INTO url_tokens (name, token_hash, scopes, expires_at)
            VALUES ($1, $2, $3::jsonb, $4)
            """,
            name,
            token_hash,
            json.dumps(safe_scopes),
            expires_at,
        )
    except asyncpg.exceptions.UniqueViolationError:
        return JSONResponse(
            {"error": "conflict", "error_description": "A URL token with this name already exists"},
            status_code=409,
        )

    return JSONResponse(
        {
            "token": raw_token,
            "name": name,
            "scopes": safe_scopes,
            "expires_at": expires_at.isoformat(),
        },
        status_code=201,
    )


@app.delete("/token/url/{name}")
async def revoke_url_token(name: str) -> JSONResponse:
    """Revoke a URL token by name (requires admin scope).

    Sets revoked_at to NOW() for the named token.
    Returns 200 even if the token doesn't exist (idempotent).
    """
    if not _is_api_key_auth.get():
        _require_scope("admin")

    pool = await get_pool()
    await pool.execute(
        """
        UPDATE url_tokens
        SET revoked_at = NOW()
        WHERE name = $1 AND revoked_at IS NULL
        """,
        name,
    )

    return JSONResponse({"revoked": name})


@app.get("/.well-known/oauth-authorization-server")
async def oauth_metadata() -> JSONResponse:
    """OAuth 2.1 server metadata."""
    config = get_config()
    base = config.MCP_SERVER_URL.rstrip("/")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "revocation_endpoint": f"{base}/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
    })


def _load_client_name(client_id: str) -> str:
    """Load client name from dynamic registry or clients JSON file."""
    # Check dynamically registered clients first
    provider = get_provider()
    registered = provider.get_client(client_id)
    if registered:
        return registered.client_name
    # Fall back to static clients file
    try:
        config = get_config()
        clients_path = Path(config.CLIENTS_FILE)
        if clients_path.exists():
            clients = json.loads(clients_path.read_text())
            for client in clients:
                if client.get("client_id") == client_id:
                    return client.get("client_name", "MCP Client")
    except Exception:
        pass
    return "MCP Client"


@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource() -> JSONResponse:
    """OAuth 2.0 Protected Resource Metadata (RFC 9728)."""
    config = get_config()
    base = config.MCP_SERVER_URL.rstrip("/")
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
    })


@app.get("/health")
async def health() -> JSONResponse:
    """Health check endpoint with subsystem status.

    Returns HTTP 200 if DB is reachable, HTTP 503 if DB is down.
    Voyage API failure is non-critical (degraded, not unhealthy).
    Short-circuits Voyage API check when DB is down to avoid unnecessary delay.
    """
    db_info = await _check_db_status()
    db_status = db_info["db_status"]
    memory_count = db_info["memory_count"]

    # ── Voyage API check (non-critical) — skipped when DB is down ─────────────
    if db_status == "unreachable":
        embedding_api_status = "unknown"
    else:
        embedding_api_status = await _check_voyage_api_status()

    status = "ok" if db_status == "ok" else "unhealthy"
    http_status = 200 if db_status == "ok" else 503
    return JSONResponse(
        {
            "status": status,
            "service": "open-brain",
            "runtime": "python",
            "db": db_status,
            "embedding_api": embedding_api_status,
            "memory_count": memory_count,
        },
        status_code=http_status,
    )


# ─── REST API Endpoints (Plugin Hooks) ────────────────────────────────────────
#
# Endpoints (all require X-API-Key or Bearer token auth unless noted):
#   POST /api/ingest                    — ingest single tool observation (plugin hook)
#   POST /api/summarize                 — generate session summary from recent observations
#   POST /api/session-capture           — extract observations from conversation transcript
#   POST /api/worktree-session-summary  — generate summary from worktree session turn batch
#   DELETE /api/memories                — delete memories by IDs or filter
#   GET /api/context                    — get recent memory context (markdown or JSON)

# Skip list for observation capture — tools with no learning value
_INGEST_SKIP_TOOLS = {
    "Read", "Glob", "Grep", "Skill", "ToolSearch", "AskUserQuestion",
    "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop",
    "CronCreate", "CronDelete", "CronList", "SendMessage",
    "ListMcpResourcesTool", "ReadMcpResourceTool",
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree",
    "NotebookEdit",
}


@app.post("/api/ingest")
async def api_ingest(request: Request) -> JSONResponse:
    """Ingest tool observation from plugin hook. Returns 202, processes async."""
    body = await request.json()
    tool_name = body.get("tool_name", "")

    # Server-side skip list check
    if tool_name in _INGEST_SKIP_TOOLS:
        return JSONResponse({"status": "skipped"}, status_code=202)

    asyncio.create_task(_process_ingest(body))

    return JSONResponse({"status": "accepted"}, status_code=202)


async def _process_ingest(body: dict) -> None:
    """Background task: extract observation from tool data and save."""
    try:
        tool_name = body.get("tool_name", "")
        tool_input = body.get("tool_input", "")
        tool_response = body.get("tool_response", "")
        project = body.get("project", "unknown")
        session_id = body.get("session_id", "")
        cwd = body.get("cwd", "")

        extraction = await _extract_observation(tool_name, tool_input, tool_response, cwd)
        if not extraction:
            return

        dl = get_dl()
        await dl.save_memory(
            SaveMemoryParams(
                text=extraction["content"],
                type=extraction.get("type", "observation"),
                project=project,
                title=extraction.get("title"),
                subtitle=extraction.get("subtitle"),
                narrative=extraction.get("narrative"),
                session_ref=session_id if session_id else None,
            )
        )
        logger.info("Ingested observation: %s [%s]", extraction.get("title", ""), tool_name)
    except Exception:
        logger.exception("Failed to process ingest")


async def _extract_observation(
    tool_name: str, tool_input: str, tool_response: str, cwd: str
) -> dict | None:
    """Use LLM to extract a structured observation from tool data."""
    prompt = f"""Extract a concise, meaningful observation from this tool execution.

Tool: {tool_name}
Working Directory: {cwd}
Input: {tool_input[:2000]}
Output: {tool_response[:2000]}

Respond in JSON with these fields:
- "title": short headline (max 80 chars) capturing the core action
- "type": one of: bugfix, feature, refactor, discovery, decision, change
- "content": 2-4 sentences describing WHAT happened and WHY it matters. This is the primary searchable text.
- "subtitle": one-line secondary label or tags (max 100 chars)
- "narrative": optional broader context or reasoning (1-2 sentences, or null if not needed)
- "skip": true if this tool execution has no learning value (routine operations, failed reads, etc.)

Rules:
- Focus on the LEARNING or CHANGE, not the mechanics of the tool
- If the tool execution is routine (e.g., running tests with no failures, simple file reads), set skip=true
- Content should be self-contained and understandable without seeing the raw tool data
- Use the working directory to infer project context

Respond with ONLY valid JSON, no markdown fences."""

    try:
        response = await llm_complete(
            [LlmMessage(role="user", content=prompt)],
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
        )
        result = parse_llm_json(response)

        if result.get("skip"):
            return None

        return result
    except Exception:
        logger.exception("Extraction failed")
        return None


@app.post("/api/summarize")
async def api_summarize(request: Request) -> JSONResponse:
    """Generate session summary from recent observations. Returns 202, processes async."""
    body = await request.json()

    asyncio.create_task(_process_summarize(body))

    return JSONResponse({"status": "accepted"}, status_code=202)


async def _process_summarize(body: dict) -> None:
    """Background task: generate and save session summary."""
    try:
        session_id = body.get("session_id", "")
        project = body.get("project", "unknown")
        hook_type = body.get("hook_type", "Stop")

        if not session_id:
            logger.warning("Summarize called without session_id")
            return

        dl = get_dl()

        # Fetch recent observations for this session
        result = await dl.search(
            SearchParams(project=project, limit=30, order_by="oldest")
        )

        if not result.results:
            # No observations yet — save minimal summary
            await dl.save_memory(
                SaveMemoryParams(
                    text=f"Session {session_id} ended ({hook_type}). No observations captured.",
                    type="session_summary",
                    project=project,
                    title=f"Session summary ({hook_type})",
                    session_ref=session_id,
                )
            )
            return

        # Build context from observations
        obs_text = "\n".join(
            f"- [{m.type}] {m.title}: {m.content[:200]}"
            for m in result.results[-20:]  # Last 20 observations
        )

        summary_prompt = f"""Summarize this coding session based on the observations below.

Session Type: {hook_type} ({"main session ended" if hook_type == "Stop" else "subagent completed"})
Project: {project}

Observations:
{obs_text}

Respond in JSON:
- "title": short session headline (max 80 chars)
- "content": 3-5 sentence summary covering: what was worked on, key decisions made, outcome
- "narrative": what was learned or discovered (1-2 sentences, or null)

Respond with ONLY valid JSON, no markdown fences."""

        response = await llm_complete(
            [LlmMessage(role="user", content=summary_prompt)],
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
        )
        summary = parse_llm_json(response)

        await dl.save_memory(
            SaveMemoryParams(
                text=summary.get("content", f"Session {session_id} ended."),
                type="session_summary",
                project=project,
                title=summary.get("title", f"Session summary ({hook_type})"),
                narrative=summary.get("narrative"),
                session_ref=session_id,
            )
        )
        logger.info("Session summary saved for %s [%s]", session_id, hook_type)
    except Exception:
        logger.exception("Failed to process summarize")


@app.post("/api/worktree-session-summary")
async def api_worktree_session_summary(request: Request) -> JSONResponse:
    """Ingest a batch of worktree session turns and generate a session summary.

    Body schema:
        worktree (str): path to the worktree (e.g. '.claude/worktrees/bead-x7s')
        bead_id (str, optional): bead identifier
        project (str, required): project name
        turns (list, required, non-empty): list of turn dicts with ts, agent, session_id,
            user_input_excerpt, assistant_summary_excerpt, tool_calls fields

    Returns 202 Accepted immediately; summary is generated and stored asynchronously.
    Returns 400 if project is missing or turns is empty.
    Auth: X-API-Key header (handled by BearerAuthMiddleware).
    """
    body = await request.json()

    if not body.get("project"):
        return JSONResponse({"error": "project is required"}, status_code=400)

    turns = body.get("turns")
    if not isinstance(turns, list) or not turns:
        return JSONResponse({"error": "turns must be a non-empty array"}, status_code=400)

    asyncio.create_task(_process_worktree_session_summary(body))

    return JSONResponse({"status": "accepted"}, status_code=202)


async def _process_worktree_session_summary(body: dict) -> None:
    """Background task: build Haiku summary from worktree session turns and save to memory."""
    try:
        project = body.get("project", "unknown")
        worktree = body.get("worktree", "")
        bead_id = body.get("bead_id")
        turns = body.get("turns", [])

        # Filter to valid dicts only — skip any malformed entries
        valid_turns = [t for t in turns if isinstance(t, dict)]
        if not valid_turns:
            logger.warning("worktree_session_summary: no valid turn dicts in payload, skipping")
            return

        # Build prompt from valid_turns
        turn_lines = []
        for t in valid_turns:
            ts = t.get("ts", "")
            agent = t.get("agent", "")
            hook = t.get("hook_type", "")
            user_exc = t.get("user_input_excerpt", "")
            asst_exc = t.get("assistant_summary_excerpt", "")
            tool_calls = t.get("tool_calls", [])
            if isinstance(tool_calls, list):
                tools_str = ", ".join(
                    f"{tc.get('name', '')}({tc.get('target', '')})"
                    for tc in tool_calls
                    if isinstance(tc, dict)
                )
            else:
                tools_str = ""
            turn_lines.append(
                f"[{ts}] {agent} ({hook})\n"
                f"  User: {user_exc}\n"
                f"  Assistant: {asst_exc}\n"
                f"  Tools: {tools_str}"
            )

        turns_text = "\n\n".join(turn_lines)

        # Truncate to prevent context overflow for long-lived worktrees.
        # Keep head + tail so the model sees both setup context and the session outcome,
        # matching the /api/session-capture truncation idiom.
        if len(turns_text) > _MAX_TURNS_TEXT:
            half = _MAX_TURNS_TEXT // 2
            turns_text = turns_text[:half] + "\n\n[...truncated...]\n\n" + turns_text[-half:]

        summary_prompt = f"""Summarize this worktree coding session based on the turn log below.

Project: {project}
Worktree: {worktree}

Turn log:
{turns_text}

Respond in JSON:
- "title": short session headline (max 80 chars)
- "content": 3-5 sentence summary covering what was worked on, key actions, and outcome
- "narrative": what was learned or discovered (1-2 sentences, or null)

Respond with ONLY valid JSON, no markdown fences."""

        response = await llm_complete(
            [LlmMessage(role="user", content=summary_prompt)],
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
        )
        summary = parse_llm_json(response)

        # Build session_ref from sorted unique session_ids
        session_ids = sorted({t["session_id"] for t in valid_turns if t.get("session_id")})
        session_ref = ",".join(session_ids)

        # Metadata fields
        last_ts = valid_turns[-1].get("ts")
        agent = valid_turns[-1].get("agent", "")
        metadata: dict = {
            "worktree": worktree,
            "agent": agent,
            "turn_count": len(valid_turns),
            "last_ts": last_ts,
        }
        if bead_id is not None:
            metadata["bead_id"] = bead_id

        dl = get_dl()
        await dl.save_memory(
            SaveMemoryParams(
                text=summary.get("content", f"Worktree session summary for {worktree}."),
                type="session_summary",
                project=project,
                title=summary.get("title", f"Worktree session: {worktree}"),
                narrative=summary.get("narrative"),
                session_ref=session_ref if session_ref else None,
                metadata=metadata,
            )
        )
        logger.info(
            "Worktree session summary saved for %s [%s turns]", worktree, len(valid_turns)
        )
    except Exception:
        logger.exception("Failed to process worktree session summary")


@app.post("/api/session-capture")
async def api_session_capture(request: Request) -> JSONResponse:
    """Extract observations from a conversation transcript and save them.

    Body: {
        "session_id": "abc123",
        "project": "open-brain",
        "conversation": "User: ... \nAssistant: ...",
        "message_count": 15
    }

    Skips sessions with fewer than 3 user messages (rate limiting).
    Returns 202, processes async.
    """
    body = await request.json()
    message_count = body.get("message_count", 0)

    if message_count < 3:
        return JSONResponse({"status": "skipped", "reason": "too_few_messages"}, status_code=202)

    asyncio.create_task(_process_session_capture(body))
    return JSONResponse({"status": "accepted"}, status_code=202)


async def _process_session_capture(body: dict) -> None:
    """Background task: extract observations from conversation and save."""
    try:
        session_id = body.get("session_id", "")
        project = body.get("project", "unknown")
        conversation = body.get("conversation", "")

        if not conversation.strip():
            logger.warning("Session capture called with empty conversation")
            return

        # Truncate to ~8k chars to stay within Haiku context
        if len(conversation) > 8000:
            conversation = conversation[:4000] + "\n\n[...truncated...]\n\n" + conversation[-4000:]

        extraction_prompt = f"""Analyze this coding conversation and extract the most important observations.

Project: {project}
Session ID: {session_id}

Conversation:
{conversation}

Extract 1-5 observations (only things worth remembering for future sessions).
Focus on: decisions made, problems solved, new knowledge gained, patterns discovered.
Skip: routine operations, greetings, simple file reads, test runs with no issues.

Respond with ONLY valid JSON (no markdown fences):
{{
  "observations": [
    {{
      "title": "short headline (max 80 chars)",
      "type": "decision|discovery|bugfix|feature|refactor|change|learning",
      "content": "2-4 sentences describing WHAT and WHY",
      "narrative": "optional broader context (1-2 sentences, or null)"
    }}
  ],
  "session_summary": {{
    "title": "short session headline (max 80 chars)",
    "content": "3-5 sentence summary of the session",
    "narrative": "what was learned or discovered (1-2 sentences, or null)"
  }}
}}

If nothing worth remembering happened, return: {{"observations": [], "session_summary": null}}"""

        response = await llm_complete(
            [LlmMessage(role="user", content=extraction_prompt)],
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
        )
        result = parse_llm_json(response)

        dl = get_dl()

        # Save individual observations
        for obs in result.get("observations", []):
            await dl.save_memory(
                SaveMemoryParams(
                    text=obs["content"],
                    type=obs.get("type", "discovery"),
                    project=project,
                    title=obs.get("title"),
                    narrative=obs.get("narrative"),
                    session_ref=session_id,
                )
            )

        # Save session summary
        summary = result.get("session_summary")
        if summary and summary.get("content"):
            await dl.save_memory(
                SaveMemoryParams(
                    text=summary["content"],
                    type="session_summary",
                    project=project,
                    title=summary.get("title", "Session summary"),
                    narrative=summary.get("narrative"),
                    session_ref=session_id,
                )
            )

        obs_count = len(result.get("observations", []))
        logger.info(
            "Session capture: %d observations + summary for %s [%s]",
            obs_count, session_id, project,
        )
    except Exception:
        logger.exception("Failed to process session capture")


@mcp.tool(
    description="Generate weekly briefing: memory counts, top entities, theme trends (emerging/declining), "
    "open loops (unresolved action items), cross-project connections, and decay warnings (stale memories). "
    "Params: weeks_back (default 1), project (optional filter)"
)
async def weekly_briefing(
    weeks_back: int = 1,
    project: str | None = None,
) -> str:
    """Generate a structured weekly briefing with cross-type time-bridged insights."""
    _require_scope("evolution")
    try:
        dl = get_dl()
        result = await generate_weekly_briefing(dl, weeks_back=weeks_back, project=project)
        return json.dumps(asdict(result), default=str)
    except Exception:
        logger.exception("weekly_briefing failed")
        raise


@mcp.tool(description="Analyze briefing engagement: response rates by type over last N days. Returns EngagementReport.")
async def analyze_briefing_engagement(
    days_back: int = 7,
    project: str | None = None,
) -> str:
    """Analyze briefing engagement: response rates by type over last N days."""
    _require_scope("evolution")
    dl = get_dl()
    try:
        report = await analyze_engagement(dl, days_back=days_back, project=project)
        return json.dumps({
            "period_days": report.period_days,
            "total_briefings": report.total_briefings,
            "has_sufficient_data": report.has_sufficient_data,
            "by_type": [
                {
                    "briefing_type": e.briefing_type,
                    "total_count": e.total_count,
                    "responded_count": e.responded_count,
                    "response_rate": e.response_rate,
                }
                for e in report.by_type
            ],
        })
    except Exception:
        logger.exception("analyze_briefing_engagement failed")
        raise


@mcp.tool(description="Generate ONE self-improvement suggestion based on engagement (rate-limited to 1 per 7 days). Returns suggestion or null.")
async def generate_evolution_suggestion(
    days_back: int = 7,
    project: str | None = None,
) -> str:
    """Generate one self-improvement suggestion based on briefing engagement."""
    _require_scope("evolution")
    dl = get_dl()
    try:
        report = await analyze_engagement(dl, days_back=days_back, project=project)
        suggestion = await generate_suggestion(report, dl, project=project)
        if suggestion is None:
            return json.dumps({"suggestion": None})
        return json.dumps({
            "suggestion": {
                "id": suggestion.id,
                "action": suggestion.action,
                "briefing_type": suggestion.briefing_type,
                "reason": suggestion.reason,
                "response_rate": suggestion.response_rate,
            }
        })
    except Exception:
        logger.exception("generate_evolution_suggestion failed")
        raise


@mcp.tool(description="Log approval/rejection of an evolution suggestion. Approved changes saved as type=evolution. briefing_type, if provided, enables 30-day rejection suppression so the same briefing type is not re-proposed within 30 days.")
async def log_evolution_approval(
    suggestion_id: int,
    approved: bool,
    project: str | None = None,
    briefing_type: str | None = None,
) -> str:
    """Log approval or rejection of an evolution suggestion."""
    _require_scope("evolution")
    dl = get_dl()
    try:
        await _log_evolution_approval(dl, suggestion_id=suggestion_id, approved=approved, project=project, briefing_type=briefing_type)
        action_label = "approved" if approved else "rejected"
        return json.dumps({"status": "logged", "suggestion_id": suggestion_id, "action": action_label})
    except Exception:
        logger.exception("log_evolution_approval failed")
        raise


@mcp.tool(description="Query evolution history: past suggestions and approvals.")
async def query_evolution_history_tool(
    limit: int = 20,
    project: str | None = None,
) -> str:
    """Query evolution history: past suggestions and approvals."""
    _require_scope("evolution")
    dl = get_dl()
    try:
        memories = await query_evolution_history(dl, limit=limit, project=project)
        return json.dumps({
            "count": len(memories),
            "history": [
                {
                    "id": m.id,
                    "title": m.title,
                    "metadata": m.metadata,
                    "created_at": m.created_at,
                }
                for m in memories
            ],
        })
    except Exception:
        logger.exception("query_evolution_history_tool failed")
        raise


@app.delete("/api/memories")
async def api_delete_memories(request: Request) -> JSONResponse:
    """Delete memories by IDs or by filter (project + type + before).

    Body: {"ids": [1,2,3]} or {"project": "x", "type": "discovery", "before": "2026-03-01"}
    At least one filter is required.
    """
    body = await request.json()

    params = DeleteParams(
        ids=body.get("ids"),
        project=body.get("project"),
        type=body.get("type"),
        before=body.get("before"),
    )

    if params.ids is None and not params.project and not params.type and not params.before:
        return JSONResponse(
            {"error": "At least one filter (ids, project, type, before) is required"},
            status_code=400,
        )

    dl = get_dl()
    try:
        result = await dl.delete_memories(params)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"deleted": result.deleted})


@app.get("/api/context")
async def api_context(
    project: str | None = None,
    limit: int = 50,
    since: str | None = None,
    format: str = "markdown",
) -> Response:
    """Get recent memory context.

    since: ISO date/datetime or Unix epoch — only return memories created after this point.
    format=json returns structured data, format=markdown returns legacy table.
    """
    dl = get_dl()

    # Normalize since parameter (accept epoch or ISO)
    date_start = None
    if since:
        try:
            # If it looks like a Unix epoch (all digits), convert to ISO
            if since.strip().isdigit():
                from datetime import datetime, timezone
                date_start = datetime.fromtimestamp(int(since), tz=timezone.utc).isoformat()
            else:
                date_start = since
        except Exception:
            date_start = since  # Pass through, let DB handle validation

    # Fetch session summaries (stored as memories with obs_type=session_summary)
    sessions_result = await dl.search(
        SearchParams(obs_type="session_summary", project=project, limit=3, order_by=None, date_start=date_start)
    )

    # Fetch recent non-session observations
    result = await dl.search(
        SearchParams(project=project, limit=limit, order_by=None, date_start=date_start)
    )
    non_session = [m for m in result.results if m.type != "session_summary"]

    if format == "json":
        return JSONResponse({
            "sessions": [
                {
                    "title": m.title,
                    "narrative": m.narrative,
                    "content": m.content,
                    "created_at": str(m.created_at) if m.created_at else None,
                }
                for m in sessions_result.results
            ],
            "observations": [
                {
                    "type": m.type,
                    "title": m.title,
                    "narrative": m.narrative,
                    "created_at": str(m.created_at) if m.created_at else None,
                }
                for m in non_session[:20]
            ],
        })

    # Legacy markdown format
    lines = []
    if sessions_result.results:
        lines.append("## Recent Sessions\n")
        for s in sessions_result.results[:3]:
            title = s.title or "Session"
            body = s.narrative or (s.content or "")[:200]
            lines.append(f"- **{title}**: {body}")
        lines.append("")

    if non_session:
        lines.append("## Recent Observations\n")
        lines.append("| Type | Title | When |")
        lines.append("|------|-------|------|")
        for m in non_session[:30]:
            type_label = m.type or "observation"
            title = (m.title or m.content[:60])[:60]
            when = m.created_at[:10] if m.created_at else "?"
            lines.append(f"| {type_label} | {title} | {when} |")
        lines.append("")

    if not lines:
        return Response(content="", media_type="text/markdown")

    md = "\n".join(lines)
    return Response(content=md, media_type="text/markdown")


# Mount MCP sub-app AFTER all routes (mount at "/" catches everything)
app.mount("/", _mcp_app)


def create_app() -> FastAPI:
    """Factory function for the FastAPI app."""
    return app


if __name__ == "__main__":
    import uvicorn

    config = get_config()
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
