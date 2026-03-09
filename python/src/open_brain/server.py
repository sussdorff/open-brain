"""FastAPI + FastMCP server with OAuth 2.1 middleware."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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
    RefineParams,
    SaveMemoryParams,
    SearchParams,
    TimelineParams,
    UpdateMemoryParams,
)
from open_brain.data_layer.postgres import PostgresDataLayer, close_pool

logger = logging.getLogger(__name__)

# MCP server (FastMCP high-level API)
_config = get_config()
_host = _config.MCP_SERVER_URL.replace("https://", "").replace("http://", "").rstrip("/")
mcp = FastMCP(
    "open-brain",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[_host, f"{_host}:443", "localhost:8091", "127.0.0.1:8091"],
    ),
)

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
    "Params: query, limit, project, type, obs_type, dateStart, dateEnd, offset, orderBy, filePath"
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
) -> str:
    """Step 1: Hybrid memory search."""
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
        )
    )
    return json.dumps(
        {"total": result.total, "results": [vars(m) for m in result.results]},
        default=str,
    )


@mcp.tool(
    description="Step 2: Get context around results. "
    "Params: anchor (observation ID) OR query (finds anchor automatically), depth_before, depth_after, project"
)
async def timeline(
    anchor: int | None = None,
    query: str | None = None,
    depth_before: int | None = None,
    depth_after: int | None = None,
    project: str | None = None,
) -> str:
    """Step 2: Anchor-based timeline context."""
    dl = get_dl()
    result = await dl.timeline(
        TimelineParams(
            anchor=anchor,
            query=query,
            depth_before=depth_before,
            depth_after=depth_after,
            project=project,
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
    "Params: text (required), type, project, title, subtitle, narrative"
)
async def save_memory(
    text: str,
    type: str | None = None,
    project: str | None = None,
    title: str | None = None,
    subtitle: str | None = None,
    narrative: str | None = None,
) -> str:
    """Save a new memory entry."""
    dl = get_dl()
    result = await dl.save_memory(
        SaveMemoryParams(
            text=text,
            type=type,
            project=project,
            title=title,
            subtitle=subtitle,
            narrative=narrative,
        )
    )
    return json.dumps({"id": result.id, "message": result.message})


@mcp.tool(
    description="Update an existing memory by ID. Only provided fields are changed. "
    "Re-embeds automatically if text/title/subtitle/narrative change. "
    "Params: id (required), text, type, project, title, subtitle, narrative"
)
async def update_memory(
    id: int,
    text: str | None = None,
    type: str | None = None,
    project: str | None = None,
    title: str | None = None,
    subtitle: str | None = None,
    narrative: str | None = None,
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


@mcp.tool(description="Get database statistics (memory count, sessions, DB size)")
async def stats() -> str:
    """Get DB statistics."""
    dl = get_dl()
    result = await dl.stats()
    return json.dumps(result, default=str)


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


# ─── FastAPI App ──────────────────────────────────────────────────────────────

# Build the MCP sub-app first so session_manager is available
_mcp_app = mcp.streamable_http_app()

# OAuth paths that must remain unauthenticated
_PUBLIC_PATHS = {
    "/authorize", "/authorize/submit", "/token", "/register", "/revoke",
    "/.well-known/oauth-authorization-server", "/health",
}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validate Bearer tokens on MCP endpoints (everything not in _PUBLIC_PATHS)."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"error": "unauthorized", "error_description": "Missing Bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = auth_header[7:]  # Strip "Bearer "
        try:
            provider = get_provider()
            provider.verify_access_token(token)
        except Exception:
            return JSONResponse(
                {"error": "unauthorized", "error_description": "Invalid or expired token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage MCP session manager and DB pool lifecycle."""
    config = get_config()
    logger.info("open-brain starting on port %d", config.PORT)
    async with mcp.session_manager.run():
        yield
    await close_pool()


app = FastAPI(title="open-brain MCP Server", version="0.1.0", lifespan=lifespan)
app.add_middleware(BearerAuthMiddleware)


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


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "service": "open-brain", "runtime": "python"}


# Mount MCP sub-app AFTER all routes (mount at "/" catches everything)
app.mount("/", _mcp_app)


def create_app() -> FastAPI:
    """Factory function for the FastAPI app."""
    return app


if __name__ == "__main__":
    import uvicorn

    config = get_config()
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
