# Token Budget: Embedding Cost Tracking & Ingestion Guard

Embedding API cost tracking and daily ingestion rate limiting to prevent burst embedding costs and runaway memory accumulation.

## Was

Token Budget provides three complementary capabilities for managing open-brain's embedding costs and ingestion velocity:

1. **Embedding token tracking** — All embedding operations (save_memory, search, batch operations) log Voyage API token consumption to an `embedding_token_log` table, enabling cost visibility and auditing
2. **Daily cost stats** — `stats()` MCP tool exposes embeddings_today, embedding_tokens_today, and estimated_embedding_cost_today so operators can monitor API spending and adjust ingestion patterns
3. **Ingestion guard & rate limit** — `save_memory()` enforces two limits:
   - **Daily guard** (MAX_MEMORIES_PER_DAY, default 500): Rejects `save_memory()` if daily memory count exceeds threshold — prevents "unlimited" memory accumulation in a single day
   - **Rate limiter** (10/60s): Sliding-window rate limit on save_memory calls — prevents burst ingestion that could spike embedding costs or overwhelm the database

## Für wen

Operations, billing, and system administrators who need to:

- **Monitor embedding costs** — Track Voyage API token consumption daily; estimate API spend in USD
- **Set ingestion guardrails** — Limit daily memories to a sensible baseline (e.g., 500/day) to prevent runaway growth
- **Prevent abuse** — Rate-limit rapid `save_memory()` calls to prevent burst load and cost spikes
- **Audit API usage** — Query `embedding_token_log` to review which operations consumed tokens and when
- **Plan capacity** — Use stats to forecast daily token spend and adjust Voyage API quota or switch models if needed

**Use cases:**

- **Billing integration** — Poll `stats()` daily to extract estimated_embedding_cost_today, multiply by 30 for monthly estimate
- **Abuse detection** — Monitor `stats().embeddings_today` and compare against MAX_MEMORIES_PER_DAY threshold; alert if approaching limit
- **Load testing** — Stress-test with rapid `save_memory()` calls; verify rate limiter rejects at 10th call within 60s
- **Capacity planning** — Use `stats().embedding_tokens_today` and `embeddings_today` to calculate average tokens per memory; project monthly spend

## Wie es funktioniert

### Embedding Token Tracking (AK1)

All embedding operations call one of three token-tracking functions:

**`embed_with_usage(text: str) -> (list[float], int)`**
- Embeds a single document-type text (used in save_memory)
- Returns tuple: (embedding vector, total_tokens)
- Logs tokens to `embedding_token_log` table via `log_embedding_tokens()`

**`embed_query_with_usage(text: str) -> (list[float], int)`**
- Embeds a query-type text (used in search operations)
- Returns tuple: (embedding vector, total_tokens)
- Logs tokens to `embedding_token_log` table

**`embed_batch_with_usage(texts: list[str]) -> (list[list[float]], int)`**
- Batch-embeds multiple documents with automatic 64-text chunking
- Returns tuple: (list of embedding vectors, total_tokens across all batches)
- Logs aggregate token count to `embedding_token_log`

### Token Logging Database

```sql
CREATE TABLE embedding_token_log (
  id BIGSERIAL PRIMARY KEY,
  operation TEXT NOT NULL,          -- 'save', 'search', 'batch', etc.
  token_count INTEGER NOT NULL,     -- Tokens from API response
  logged_at TIMESTAMP DEFAULT NOW() -- UTC timestamp for daily roll-ups
);
CREATE INDEX idx_embedding_token_log_logged_at ON embedding_token_log(logged_at);
```

Each embedding operation is logged with its operation name and token count. The table is append-only — tokens are never deleted, only aged out by date-based queries.

### Daily Cost Stats (AK2)

The `stats()` MCP tool returns:

```json
{
  "memories": 1234,
  "sessions": 56,
  "relationships": 789,
  "db_size_bytes": 52428800,
  "db_size_mb": 50.0,
  "types": { "note": 1000, "lesson": 234 },
  "by_user": { "user@example.com": 600, "bot@example.com": 634 },
  "embeddings_today": 48,
  "embedding_tokens_today": 12480,
  "estimated_embedding_cost_today": 0.001498
}
```

#### Cost Fields Explained

| Field | Type | Meaning |
|-------|------|---------|
| **embeddings_today** | integer | Count of embedding operations logged today (save, search, batch ops) |
| **embedding_tokens_today** | integer | Total tokens consumed by all embedding operations today |
| **estimated_embedding_cost_today** | float | Estimated USD cost at Voyage-4 pricing ($0.00000012/token = $0.12/million) |

**Cost calculation:**
```
estimated_cost = embedding_tokens_today * 0.00000012
```

Example: 12,480 tokens × $0.00000012 = $0.001498 USD.

### Daily Memory Ingestion Guard (AK3)

When `save_memory()` is called:

1. Acquires a database connection
2. Executes: `SELECT COUNT(*) FROM memories WHERE created_at >= CURRENT_DATE`
3. Compares today_count against `MAX_MEMORIES_PER_DAY` config (default: 500)
4. If today_count >= limit, rejects the save with error response:

```json
{
  "error": "daily_limit_exceeded",
  "message": "Daily memory limit of 500 exceeded. 427 memories saved today."
}
```

**Key characteristics:**

- **DB-backed enforcement** — Uses database query for accuracy across restarts and multi-instance deployments (not in-memory counter)
- **Per-calendar-day** — Resets at UTC midnight (uses `CURRENT_DATE` which is in UTC)
- **Configurable** — Set `MAX_MEMORIES_PER_DAY` via environment variable (default 500)
- **Idempotent** — Query returns 0 if no memories saved today; no side effects

### Rate Limiter for save_memory (AK4)

Before any save_memory processing (database check, embedding, enrichment), a sliding-window rate limiter is evaluated:

**Mechanism:**
- Global module-level deque (`_save_timestamps`) tracks timestamps of the last ~10 save attempts
- Window size: 60 seconds
- Limit: 10 saves per 60 seconds
- Algorithm: On each call, prune timestamps older than 60s; if remaining count >= 10, reject

**Rate-limited response:**

```json
{
  "error": "rate_limit_exceeded",
  "message": "Rate limit exceeded: 10 saves/60s. Try again in 42 seconds."
}
```

**Key characteristics:**

- **Sliding window** — Not fixed 1-minute buckets; evaluates continuously as requests arrive
- **Global (not per-user)** — Single rate limit for all users combined (simplified implementation)
- **Fast rejection** — Checked before DB queries or embedding, saving resources
- **Retry-after estimate** — Returns number of seconds to wait before retry
- **Module-level state** — Resets on server restart (acceptable for rate-limit use case)

## Zusammenspiel

### Embedding Workflow Integration

1. **save_memory()** calls `embed_with_usage()` → logs tokens to DB
2. **search()** calls `embed_query_with_usage()` → logs tokens to DB
3. **Batch operations** (e.g., memory_migration) call `embed_batch_with_usage()` → logs aggregate tokens
4. **stats()** queries `embedding_token_log` WHERE logged_at >= CURRENT_DATE to compute daily totals

### Rate Limit & Guard Interaction

```
save_memory() request
  │
  ├─ Rate limit check (in-memory, <1ms)
  │  ├─ If exceeded → return error, abort
  │  └─ Otherwise → append timestamp, continue
  │
  ├─ Daily guard check (DB query, ~10ms)
  │  ├─ If exceeded → return error, abort
  │  └─ Otherwise → continue
  │
  └─ Full save workflow (embed, extract, persist)
```

### Operational Monitoring Loop

**Daily monitoring script:**
```
1. Call stats() to get embeddings_today, embedding_tokens_today, estimated_embedding_cost_today
2. Alert if embeddings_today is close to MAX_MEMORIES_PER_DAY (e.g., >450 when limit is 500)
3. Alert if estimated_embedding_cost_today exceeds budget (e.g., >$0.01/day)
4. Log stats to cost tracking system
```

## Besonderheiten

### Edge Cases & Behavior

1. **Rate limiter reset on restart** — In-memory deque is lost when server restarts. Acceptable because:
   - Rate limiting is a soft guardrail, not a hard security boundary
   - Daily memory guard (DB-backed) is the production safety net
   - Can be replaced with Redis-backed distributed rate limiter in future if needed

2. **Daily guard uses UTC midnight** — `CURRENT_DATE` in PostgreSQL is always UTC. If users are in different time zones, the calendar day is still UTC-based. Workaround: Set MAX_MEMORIES_PER_DAY conservatively, or migrate to per-user daily limits in a future enhancement.

3. **Voyage API pricing constant** — Hardcoded as `$0.00000012/token` in the cost calculation. If Voyage changes pricing, update:
   - `python/src/open_brain/data_layer/postgres.py:950` (hardcoded constant in cost formula)
   - This must be updated manually; there's no environment variable override yet

4. **Embeddings_today count vs memory count** — `embeddings_today` counts individual embedding API calls (one per save, one per search). It does NOT equal the number of memories saved, because:
   - Duplicate detections skip embedding (no API call, no token log)
   - Search operations add to embeddings_today without affecting memory count
   - Batch operations may embed many memories at once (high embeddings_today)

5. **Empty daily stats** — If no embeddings today, `embeddings_today=0`, `embedding_tokens_today=0`, `estimated_embedding_cost_today=0.0` (no error)

6. **Concurrent save_memory calls** — Rate limiter is global (non-distributed) so concurrent calls on the same server are limited. Calls across multiple open-brain instances are NOT rate-limited. For distributed rate limiting, use Redis or DynamoDB in a future version.

### Performance Implications

- **Rate limiter latency** — <1ms (simple deque operations, O(1) amortized)
- **Daily guard latency** — ~10-50ms (single SELECT COUNT query, depends on memory table size)
- **Token logging latency** — ~5-20ms per embedding (single INSERT to embedding_token_log, batched if possible)

### Constraints & Known Limitations

1. **Fixed daily guard threshold** — All users share the same MAX_MEMORIES_PER_DAY limit. Multi-tenant deployments should use per-user or per-organization limits (not yet implemented).

2. **Pricing constant is static** — No support for Voyage pricing model changes without code update.

3. **Rate limiter is per-instance** — Does not coordinate across multiple server instances. If you run 2+ open-brain servers, rate limiting is per-server (10 saves per 60s per server, not aggregate). Use a shared Redis limiter for global rate limiting (future enhancement).

4. **No differentiation by operation type** — Rate limiter treats all save_memory calls the same. Large memories don't cost more in rate limit (though they do cost more in tokens). Future enhancement: weight rate limits by memory size or embedding tokens.

## Technische Details

### Routes & MCP Tools

- **MCP tool**: `stats()` with no parameters; exported as `@mcp.tool()` to all MCP clients
- **No REST endpoint** — Token budget is accessible only via MCP stats() tool

### Relevant Code Locations

- **Token tracking functions** — `python/src/open_brain/data_layer/embedding.py:14-148`
  - `embed_with_usage()` — Line 14
  - `embed_query_with_usage()` — Line 60
  - `embed_batch_with_usage()` — Line 106
- **Token logging** — `python/src/open_brain/data_layer/postgres.py:216` (log_embedding_tokens method)
- **Daily stats** — `python/src/open_brain/data_layer/postgres.py:930-962` (stats method)
- **Rate limiter** — `python/src/open_brain/server.py:60-64` (module state), `server.py:290-302` (rate limit check)
- **Daily guard** — `python/src/open_brain/server.py:304-316` (daily memory limit check)
- **Config** — `python/src/open_brain/config.py:33` (MAX_MEMORIES_PER_DAY)

### Configuration

**Environment variable:**
```bash
MAX_MEMORIES_PER_DAY=500  # Default; 0 disables the guard
```

**Note:** Rate limiter constants are hardcoded in server.py:
- `_RATE_LIMIT_MAX = 10` (saves per window)
- `_RATE_LIMIT_WINDOW = 60` (seconds)

To change, edit server.py and redeploy.

### Database Schema

The `embedding_token_log` table is created automatically on server startup (idempotent CREATE TABLE IF NOT EXISTS):

```sql
CREATE TABLE IF NOT EXISTS embedding_token_log (
  id BIGSERIAL PRIMARY KEY,
  operation TEXT NOT NULL,
  token_count INTEGER NOT NULL,
  logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_embedding_token_log_logged_at ON embedding_token_log(logged_at);
```

### Dependencies

- **asyncpg** — Database connection pool (already required)
- **httpx** — Async HTTP client for Voyage API (already required)
- **PostgreSQL 13+** — For CURRENT_DATE temporal function

### OpenAPI / API Documentation

Token budget is exposed via `stats()` MCP tool, not a REST endpoint. The tool is auto-generated in MCP specs (no manual documentation needed).

### Testing

See `python/tests/test_token_budget.py` and `python/tests/test_token_budget_integ.py` for comprehensive test coverage:

- **AK1**: embed_with_usage returns (embedding, token_count) tuple
- **AK2**: stats includes embeddings_today, embedding_tokens_today, estimated_cost
- **AK3**: save_memory enforces MAX_MEMORIES_PER_DAY guard
- **AK4**: save_memory enforces 10/60s rate limit with accurate retry estimates

Run tests:
```bash
cd python
uv run pytest test_token_budget.py -v
uv run pytest test_token_budget_integ.py -v  # Requires VOYAGE_API_KEY
```
