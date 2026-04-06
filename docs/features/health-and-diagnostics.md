# Health Checks & Diagnostics

Deep operational health monitoring for open-brain via /health endpoint and doctor MCP tool, providing subsystem status visibility and diagnostic metrics.

## Was

Health Checks & Diagnostics provides two complementary capabilities for monitoring open-brain's operational state:

1. **GET /health** — HTTP endpoint that returns current status of critical subsystems (database, embedding API) and memory count
2. **doctor** MCP tool — Structured diagnostic report with latency metrics, subsystem states, and server metadata

Both leverage shared health-check helpers that efficiently probe database connectivity, Voyage API reachability, and memory statistics. The endpoint returns HTTP 503 if the database is down (critical) but 200 if only the embedding API is degraded (non-critical fallback).

## Für wen

Operations, automation, and monitoring systems that need to:

- **Monitor server readiness** — Health check endpoint for Kubernetes liveness/readiness probes, load balancers, or monitoring dashboards
- **Diagnose connection issues** — doctor tool returns db_latency_ms and subsystem status to troubleshoot slow queries or API failures
- **Track memory growth** — View memory_count and last_ingestion_at to understand dataset size and activity
- **Verify uptime and version** — Confirm server is running and at expected version after deployments

**Use cases:**

- **Kubernetes integration** — Configure liveness probe: `curl -f http://localhost:8000/health || exit 1`
- **Production alerting** — Monitor /health status code; alert on 503 (DB down) vs 200 (healthy)
- **Pre-flight checks** — Call doctor() after deploying to verify all subsystems online before returning to service
- **Performance troubleshooting** — doctor() returns db_latency_ms to detect slow database responses

## Wie es funktioniert

### GET /health Endpoint

The `/health` endpoint returns a JSON response with subsystem status:

```json
{
  "status": "ok",
  "service": "open-brain",
  "runtime": "python",
  "db": "ok",
  "embedding_api": "ok",
  "memory_count": 42
}
```

#### Status Codes

| Code | Meaning | When |
|------|---------|------|
| **200** | Healthy | Database is reachable (embedding API status irrelevant) |
| **503** | Unhealthy | Database is unreachable (critical subsystem down) |

#### Subsystem Status Values

- **db** — `"ok"` (connected, latency < threshold), `"unreachable"` (connection failed)
- **embedding_api** — `"ok"` (API responds 200), `"degraded"` (API responds non-200), `"unreachable"` (network error), `"unknown"` (skipped because DB is down)

#### Behavior

- **Database check is critical**: A single SELECT 1 query with round-trip time measurement. If DB is unreachable, short-circuits and returns 503 immediately without checking Voyage API (avoids cascading failures)
- **Embedding API check is non-critical**: Probes https://api.voyageai.com/v1/models with Authorization header. Failure is logged as warning but does not affect overall status code

### doctor() MCP Tool

The `doctor()` tool returns a comprehensive diagnostic JSON string:

```json
{
  "db_latency_ms": 2.43,
  "db_status": "ok",
  "voyage_api_status": "ok",
  "memory_count": 42,
  "last_ingestion_at": "2026-04-06T14:27:31.123456",
  "server_version": "2026.04.10",
  "uptime_seconds": 3621.456
}
```

#### Fields

| Field | Type | Meaning |
|-------|------|---------|
| **db_latency_ms** | float or null | Round-trip time for SELECT 1 query; null if unreachable |
| **db_status** | string | `"ok"` or `"unreachable"` |
| **voyage_api_status** | string | `"ok"`, `"degraded"`, or `"unreachable"` |
| **memory_count** | integer | Total memories stored in database (0 if DB unreachable) |
| **last_ingestion_at** | ISO8601 or null | Timestamp of most recently created memory; null if no memories exist |
| **server_version** | string | Package version (e.g., "2026.04.10"); "unknown" if not installed via setuptools |
| **uptime_seconds** | float | Seconds since server started (rounded to 3 decimal places) |

#### Behavior

- Returns valid JSON even when subsystems are down (no exception thrown)
- Does **not** short-circuit embedding API check when DB is down (unlike /health) — always attempts both checks for complete diagnostics
- Memory count and last_ingestion_at are 0/null when DB is unreachable

### Shared Implementation Details

Both endpoints use a pair of helper functions:

**`_check_db_status() -> dict`**
- Acquires a connection from the pool and runs SELECT 1
- Measures round-trip latency in milliseconds
- Counts total memories: `SELECT COUNT(*) FROM memories`
- Finds most recent ingestion: `SELECT MAX(created_at) FROM memories`
- Returns dict with db_status, db_latency_ms, memory_count, last_ingestion_at
- Catches all exceptions and returns status="unreachable" without propagating

**`_check_voyage_api_status() -> str`**
- Makes async GET request to https://api.voyageai.com/v1/models
- Uses config.VOYAGE_API_KEY in Authorization header
- Returns "ok" if status 200, "degraded" if other status, "unreachable" if exception
- 3-second timeout to avoid blocking health checks on slow network

## Zusammenspiel

- **Lifespan hook** — `_server_start_time` is set when the server starts (in lifespan context manager), used for uptime calculation
- **Monitoring dashboards** — Poll /health every 10s; alert if status_code != 200 or db != "ok"
- **Pre-flight automation** — Call doctor() after deploy; check all statuses are "ok" or uptime < 30s (still warming up)
- **Debug workflows** — Run doctor() when search/embed operations are slow to isolate root cause (DB latency vs API latency)

## Besonderheiten

### Edge Cases

1. **Empty database** — memory_count=0, last_ingestion_at=null (not an error)
2. **DB down but API up** — /health returns 503; doctor() returns db_status="unreachable" but still probes Voyage API
3. **Multiple pool.acquire() failures** — All caught; db_status stays "unreachable", no exception propagated
4. **Voyage API timeout** — Treated as "unreachable" (same as 5xx response), logged at warning level
5. **Package installed but version missing** — server_version="unknown" (rare edge case; shouldn't happen in production)
6. **Uptime precision** — Rounded to 3 decimal places (millisecond precision)

### Timeout & Performance

- **DB check timeout**: Depends on pool connection timeout (default 5s in asyncpg)
- **Voyage API check timeout**: Fixed 3-second timeout (any network error → "unreachable")
- **/health latency**: Typically <10ms when DB is ok; <3s when DB is down (short-circuits after DB timeout)
- **doctor() latency**: Always probes both subsystems (~5-10ms when all ok, up to ~6s when DB slow and API times out)

## Technische Details

### Routes & MCP Tool

- **HTTP endpoint**: GET /health (no authentication required; health checks bypass auth)
- **MCP tool**: `doctor()` with no parameters; exported as `@mcp.tool()` to all MCP clients

### Relevant Code Locations

- `/health` endpoint — `python/src/open_brain/server.py:904` (async def health)
- `doctor()` tool — `python/src/open_brain/server.py:468` (async def doctor)
- DB health helper — `python/src/open_brain/server.py:417` (async def _check_db_status)
- Voyage API health helper — `python/src/open_brain/server.py:448` (async def _check_voyage_api_status)

### Dependencies

- **asyncpg** — Database connection pool (already required)
- **httpx** — Async HTTP client for Voyage API probe (already required)
- **importlib.metadata** — Standard library; reads package version at runtime

### OpenAPI / API Documentation

/health endpoint is intentionally not auto-documented in OpenAPI specs because:
- Health checks bypass authentication (should be accessible without tokens)
- Low-overhead monitoring endpoint that doesn't fit the MCP tool pattern
- Typically accessed by infrastructure (load balancers, Kubernetes) not API clients

If you need OpenAPI documentation for /health, manually add it as a schema override in your API docs (not generated from Zod).
