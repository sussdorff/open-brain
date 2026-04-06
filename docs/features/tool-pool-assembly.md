# Tool Pool Assembly: Scope-Gated MCP Tools

Conditionally exposes MCP tools based on OAuth client scopes, enabling fine-grained access control and progressive feature availability.

## Was

Tool Pool Assembly is a two-layer access control system for MCP tools:

1. **Tool List Filtering** — The MCP `/tools/list` endpoint returns only tools visible to the authenticated client based on its OAuth scopes
2. **Runtime Enforcement** — Individual tools perform scope checks at invocation time as a defense-in-depth measure

This decouples authentication (already enforced by Bearer middleware) from authorization (which tools a particular client can see or call).

## Für wen

**Server operators and multi-tenant MCP deployments:**
- **Restrict advanced features** — Gate experimental tools or org-specific functionality behind scopes
- **Progressive feature access** — Offer tiered functionality (basic memory vs. evolution suggestions)
- **Compliance and security** — Implement least-privilege access for integrations
- **Future extensibility** — Define new scopes and tool groupings as features evolve

**Current tools protected:**
- `evolution` scope required for: `weekly_briefing`, `analyze_briefing_engagement`, `generate_evolution_suggestion`, `log_evolution_approval`, `query_evolution_history_tool`

## Wie es funktioniert

### Scope Concept

OAuth clients are issued access tokens with a set of scopes (stored as JWT claims). Scopes define capability areas:

| Scope | Tools | Purpose |
|-------|-------|---------|
| `memory` | search, save_memory, timeline, get_observations, etc. | Core memory operations |
| `evolution` | weekly_briefing, analyze_briefing_engagement, generate_evolution_suggestion, log_evolution_approval, query_evolution_history_tool | Self-improvement loop and adaptation |
| `admin` | (reserved; no tools currently) | Future admin/maintenance tools |

### Architecture

**ContextVar for Request Context:**
```python
_current_scopes: ContextVar[tuple[str, ...]] = ContextVar("current_scopes", default=())
```

The `BearerAuthMiddleware` extracts scopes from the JWT token and sets `_current_scopes` for the duration of the request.

**ScopedFastMCP Subclass:**
```python
class ScopedFastMCP(FastMCP):
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
```

When a client calls `/tools/list`, only tools matching its scopes appear in the response. Clients can't discover or list tools they lack permission for.

**Runtime Enforcement:**
```python
def _require_scope(scope: str) -> None:
    """Raise PermissionError if the current caller lacks the required OAuth scope."""
    if scope not in _current_scopes.get():
        raise PermissionError(f"Scope '{scope}' required")

# In each evolution tool:
@mcp.tool()
async def generate_evolution_suggestion():
    _require_scope("evolution")  # Guard at entry point
    # ... rest of tool logic
```

Even if a client somehow calls an evolution tool directly (e.g., via stored reference or cached spec), the tool enforces scope at runtime.

### Workflow: Restricted Client

1. **Token Exchange** — Client authenticates (OAuth 2.1 PKCE flow)
2. **Scope Assignment** — Authorization server issues token with `scopes=["memory"]` (no evolution)
3. **MCP Connection** — Client connects to open-brain, sends Bearer token in MCP handshake
4. **Middleware Processing** — `BearerAuthMiddleware` extracts scopes, calls `_current_scopes.set(("memory",))`
5. **Tool Discovery** — Client calls `/tools/list` → returns only core memory tools
6. **Evolution Attempt** — Client attempts to call `generate_evolution_suggestion` → **403 PermissionError**

### Workflow: Privileged Client

1. **Token Exchange** — Client authenticates with elevated credentials
2. **Scope Assignment** — Token issued with `scopes=["memory", "evolution"]`
3. **MCP Connection** — Client connects with Bearer token
4. **Middleware Processing** — `_current_scopes.set(("memory", "evolution"))`
5. **Tool Discovery** — Client calls `/tools/list` → returns core tools + 5 evolution tools
6. **Evolution Call** — Client calls `generate_evolution_suggestion` → succeeds

## Zusammenspiel

**Integration Points:**

- **OAuth Provider** (`open_brain/auth/provider.py`) — Issues tokens with scope claims
- **BearerAuthMiddleware** — Extracts scopes from JWT and populates `_current_scopes` ContextVar
- **MCP Server** (`ScopedFastMCP`) — Uses `_current_scopes` to filter `/tools/list` response
- **Tool Functions** — Call `_require_scope("evolution")` as entry-point guards
- **Self-Improvement Loop** — All 5 evolution tools protected by `_require_scope("evolution")`

Scopes are transparent to the data layer and memory storage — they only affect tool availability and invocation.

## Besonderheiten

### Unauthenticated Requests

The existing `BearerAuthMiddleware` enforces authentication at the FastAPI route level:

- **No Bearer token** → HTTP 401 Unauthorized (MCP never reached)
- **Invalid token** → HTTP 401 Unauthorized
- **Expired token** → HTTP 401 Unauthorized

Thus, unauthenticated callers never reach `ScopedFastMCP.list_tools()`. The tool-level filtering is a **defense-in-depth layer**, not the primary auth boundary.

### Scope Assignment in Development

In development, the OAuth provider may not issue meaningful scopes. To test scope-gated tools:

```python
# Set scopes manually in tests
from open_brain.server import _current_scopes

token = _current_scopes.set(("memory", "evolution"))
try:
    # Call tools with elevated scope
    result = await generate_evolution_suggestion()
finally:
    _current_scopes.reset(token)
```

### Empty Scope Set

If a token somehow lacks all scopes (edge case), evolution tools are hidden:

```python
token = _current_scopes.set(())  # Empty tuple
tools = await mcp.list_tools()
# Evolution tools absent; core tools still listed
_current_scopes.reset(token)
```

This is safe because:
1. Such tokens should be rejected by middleware (401) anyway
2. Tool-level `_require_scope()` guards also fail
3. Test coverage validates the behavior

### Defining New Scopes

To add a new scope-gated tool group:

1. **Define the tool set:**
   ```python
   _NEWFEATURE_TOOLS: frozenset[str] = frozenset({
       "tool_a", "tool_b"
   })
   ```

2. **Update `ScopedFastMCP.list_tools()`:**
   ```python
   if tool.name in _NEWFEATURE_TOOLS and "newfeature" not in scopes:
       continue
   ```

3. **Add runtime guards to each tool:**
   ```python
   @mcp.tool()
   async def tool_a():
       _require_scope("newfeature")
       # ...
   ```

4. **Issue tokens with the scope** (configure in OAuth provider)

## Technische Details

### Route & Endpoints

- **MCP endpoint:** `/mcp` (FastAPI streaming transport)
- **Auth middleware:** Applied globally to all routes
- **Scope extraction:** From JWT `scope` claim (space-separated string, parsed into tuple)

### Token Spec

Bearer tokens are JWTs with:
```json
{
  "iss": "https://open-brain.example.com",
  "sub": "client_id_or_user_id",
  "scope": "memory evolution",  // Space-separated
  "exp": 1234567890
}
```

See `open_brain/auth/tokens.py` for token generation and validation.

### Testing

All scope-gating is tested in `python/tests/test_scope_gated_tools.py`:

| Test Class | Purpose |
|-----------|---------|
| `TestScopeGatedToolList` | Tool list visibility based on scopes |
| `TestUnauthenticatedToolList` | Empty scopes → evolution tools hidden |
| `TestScopeRuntimeEnforcement` | Tool invocation fails without required scope |

Run via:
```bash
cd python
uv run pytest tests/test_scope_gated_tools.py -v
```

### ContextVar Isolation

Each HTTP request gets its own `_current_scopes` context value (ContextVar isolation). Request A's scopes do not bleed into Request B's context.

### Performance

- **Tool list filtering:** O(n) scan of tool names against frozenset (fast)
- **Runtime scope check:** O(1) tuple membership test
- **No database overhead** — Scopes are JWT claims, not database records

## Grenzen

**Out of Scope (future work):**

- **Fine-grained field-level access control** — Scopes control tool visibility, not memory field access
- **Dynamic scope assignment** — Scopes are static at token issue time; runtime scope changes require new token
- **Scope revocation** — No token invalidation mechanism yet; relies on token TTL
- **Tool-specific parameters** — Scopes control whole tools, not parameter subsets (e.g., can't restrict `save_memory` to certain metadata fields)

**Known Limitations:**

- Tools in `_EVOLUTION_TOOLS` frozenset are hardcoded; adding/removing scopes requires code change + deploy
- Two distinct error surfaces: unauthenticated requests return HTTP 401 from `BearerAuthMiddleware` (before MCP is reached); insufficient-scope calls return a `ScopeDeniedError` within the MCP tool layer (surfaced as a tool error, not an HTTP 403)
