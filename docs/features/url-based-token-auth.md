# URL-Based Token Auth: Opaque Long-Lived Tokens

A lightweight authentication method for personal tooling (Obsidian, Raycast, custom scripts) where long-lived opaque tokens can be passed as a URL query parameter instead of managing full OAuth 2.1 flows.

## Was

URL-based token auth adds a **third authentication path** to the open-brain MCP server, complementing the existing two:

1. **API Key Auth** (`X-API-Key` header) — Simple, suitable for servers and trusted clients
2. **Bearer Token Auth** (OAuth 2.1) — Full OAuth flow for user-facing applications
3. **URL Token Auth** (new) — Opaque hex tokens passed as `?token=xxx` query param

URL tokens are:
- **Stored in Postgres** — Hashed with SHA-256 for security
- **Long-lived and scoped** — Expires at a configurable date; can grant `memory` or `evolution` scopes (but never `admin`)
- **Issued via API** — POST `/token/url` endpoint (requires `admin` scope)
- **Revocable** — DELETE `/token/url/{name}` endpoint (requires `admin` scope)
- **Redacted in logs** — Query string parameter `token=xxx` replaced with `token=REDACTED` before access logging

## Für wen

**Personal tooling integrations:**
- **Obsidian plugin** — Store a URL token in Obsidian's config; fetch memories without OAuth setup
- **Raycast extension** — Query open-brain from a Raycast command using a URL token
- **Custom scripts** — Shell, Python, or Bash scripts that need persistent memory without server complexity
- **Mobile access** — Apps (iOS shortcuts, Android) where Bearer token management is cumbersome

**Use case workflow:**
1. Admin/user logs in with OAuth to open-brain or uses API key
2. Admin calls `POST /token/url` with name, scopes, expiration date
3. Receives opaque token (printed once, never stored on server in plaintext)
4. Embeds token in Obsidian config, Raycast env var, or script parameter
5. Tools call open-brain with `?token=<token>` — no login needed

## Wie es funktioniert

### Token Storage & Hashing

Tokens are **never stored in plaintext**. On issuance:

1. Server generates a random 32-byte hex token (e.g., `a1b2c3d4...`)
2. SHA-256 hash is computed: `token_hash = SHA256(token)`
3. Hash is stored in Postgres; raw token returned **once** to the user (printed, not returned again)
4. User must save the raw token immediately — re-requesting issuance requires a new token

**Database Schema:**

```sql
CREATE TABLE url_tokens (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,              -- Human-readable name (e.g., "malte-obsidian")
    token_hash TEXT NOT NULL UNIQUE,        -- SHA-256(token)
    scopes JSONB NOT NULL DEFAULT '[]',     -- ["memory"] or ["memory", "evolution"]
    expires_at TIMESTAMPTZ NOT NULL,        -- Token becomes invalid after this time
    revoked_at TIMESTAMPTZ,                 -- NULL = active; set to NOW() on deletion
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Authentication Flow

**Request with URL token:**

```
GET /api/context?token=a1b2c3d4e5f6...
```

**BearerAuthMiddleware processing:**

1. Extract `?token` query param from request
2. Compute SHA-256 hash of the raw token
3. Query database: `SELECT * FROM url_tokens WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > NOW()`
4. If found: set `_current_scopes` to token's scopes, **filter out `admin` scope even if present**
5. If not found: return HTTP 401 Unauthorized
6. Redact token value in `request.scope["query_string"]` before logging (replace with `token=REDACTED`)
7. Call next middleware/route

**Log output (after redaction):**
```
GET /api/context?token=REDACTED 200 OK
```

### Scope Semantics

URL tokens can grant two scopes:

| Scope | Purpose | Restrictions |
|-------|---------|--------------|
| `memory` | Core memory tools: `search`, `save_memory`, `timeline`, `get_observations`, etc. | Always allowed |
| `evolution` | Self-improvement tools: `weekly_briefing`, `analyze_briefing_engagement`, `generate_evolution_suggestion`, etc. | Always allowed |
| `admin` | (Reserved) | **Never granted to URL tokens** — stripped at issuance and at verification |

When a URL token is used, the `admin` scope is **always removed** from the effective scopes, even if:
- Database row has `admin` in scopes (anomaly, should not happen)
- Caller claims `admin` in request

This is enforced **twice**:
1. At issuance: `POST /token/url` strips `admin` before inserting into DB
2. At verification: Middleware removes `admin` from `_current_scopes` when loading URL token

### Token Issuance Endpoint

**Endpoint:** `POST /token/url`

**Authentication:** Requires `admin` scope (via Bearer token or API key)

**Request body:**
```json
{
  "name": "malte-obsidian",
  "scopes": ["memory", "evolution"],
  "expires_in_days": 365
}
```

**Validation rules:**
- `name`: 1-255 characters, must be unique (no two tokens with same name)
- `scopes`: Array of valid scopes (must not contain `admin`)
- `expires_in_days`: 1–3650 days (max 10 years); values outside range return HTTP 400

**Response (201 Created):**
```json
{
  "token": "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6",
  "name": "malte-obsidian",
  "scopes": ["memory", "evolution"],
  "expires_at": "2027-04-06T10:30:00Z",
  "created_at": "2026-04-06T10:30:00Z"
}
```

**Token is printed once.** The server does not store or return it again. If the user loses it, they must delete the old token and issue a new one.

### Token Revocation Endpoint

**Endpoint:** `DELETE /token/url/{name}`

**Authentication:** Requires `admin` scope

**Response (200 OK):**
```json
{
  "success": true,
  "name": "malte-obsidian"
}
```

Internally, this sets `revoked_at = NOW()` on the matching row. The token remains in the database for audit purposes but is treated as invalid (middleware's WHERE clause filters `revoked_at IS NOT NULL`).

### Token Verification in Middleware

On each request with `?token=xxx`:

1. **Exists in DB?** Query by SHA-256 hash
   - Not found → HTTP 401
   - Found → Continue

2. **Not revoked?** Check `revoked_at IS NULL`
   - Revoked → HTTP 401
   - Active → Continue

3. **Not expired?** Check `expires_at > NOW()`
   - Expired → HTTP 401
   - Valid → Continue

4. **Remove admin scope?** Filter scopes
   - Strip `admin` from scopes tuple
   - Set `_current_scopes` to filtered tuple

5. **Redact in logs** — Replace `token=<value>` with `token=REDACTED` in query string before calling next middleware

## Zusammenspiel

**Integration with existing auth:**

- **BearerAuthMiddleware** (`server.py`) — Extended to check `?token=` as a third path (after API key, before Bearer)
- **Scope-gated tools** — URL tokens are subject to the same scope filtering as Bearer tokens
- **Tool runtime guards** — `_require_scope("evolution")` works identically for URL tokens and Bearer tokens
- **Access logs** — Token value redacted automatically; only administrators see the mechanism in code (log redaction config)
- **Database** — Uses existing Postgres connection pool; no separate OAuth provider integration

**Comparison with Bearer tokens:**

| Aspect | Bearer Token | URL Token |
|--------|--------------|-----------|
| Format | JWT | Opaque hex |
| Passed via | `Authorization: Bearer <token>` | `?token=<token>` query param |
| Scopes | Dynamic (JWT claim) | Static (DB row) |
| Revocation | TTL-based only | Explicit revocation endpoint |
| Storage | None (self-contained JWT) | SHA-256 hash in Postgres |
| Suitable for | Web apps, MCP clients | Scripts, personal tools |
| Admin scope | Can be granted | Never granted |

## Besonderheiten

### Log Redaction Mechanism

The redaction happens **before** any application code sees the request:

```python
# In BearerAuthMiddleware.dispatch()
raw_qs = request.scope.get("query_string", b"")
request.scope["query_string"] = re.sub(
    rb"token=[^&]*",  # Match token=<anything> before & or end
    b"token=REDACTED",
    raw_qs
)
```

Example transformations:

| Original | Redacted |
|----------|----------|
| `token=a1b2c3d4&other=value` | `token=REDACTED&other=value` |
| `token=a1b2c3d4` | `token=REDACTED` |
| `?foo=bar&token=a1b2c3d4&baz=qux` | `?foo=bar&token=REDACTED&baz=qux` |
| `?other=value` | (unchanged) |

This ensures that even if a request is logged to a file, stdout, or a logging service, the sensitive token value is never exposed.

### Token Loss & Reissuance

If a user loses a token:

1. They cannot re-fetch it from the server (not stored in plaintext)
2. They must either:
   - Use an admin API key to call `DELETE /token/url/name` then `POST /token/url` to get a new token
   - Contact an administrator with `admin` scope to reissue

**Recommendation:** Users should store URL tokens in a secure manner:
- **Obsidian:** Store in `.obsidian/OBSIDIAN_TOKEN` (gitignored, not in vault)
- **Raycast:** Store in environment variable or Raycast's secure storage
- **Scripts:** Store in `~/.config/myapp/token` with mode `0600`

### Scope Restrictions at Creation Time

`POST /token/url` endpoint performs **request validation**:

```python
# Validate scopes
valid_scopes = {"memory", "evolution", "admin"}
request_scopes = set(req_body["scopes"])
invalid = request_scopes - valid_scopes
if invalid:
    return 422 {"error": "invalid_request", "error_description": f"Invalid scopes: {invalid}"}

# Strip admin (even if user requested it)
cleaned_scopes = request_scopes - {"admin"}
```

If a user requests `scopes=["memory", "admin"]`, the server returns:

```json
{
  "scopes": ["memory"],  // admin stripped
  "token": "...",
  // ...
}
```

This prevents accidentally creating tokens with overprivileged scopes.

### Scope Stripping at Verification

Even if a database row somehow contains `admin` in scopes (e.g., due to manual SQL injection or a bug), URL token authentication **always removes** the `admin` scope:

```python
# In BearerAuthMiddleware, when processing ?token=
scopes_list = json.loads(token_row["scopes"])
cleaned_scopes = tuple(s for s in scopes_list if s != "admin")
_current_scopes.set(cleaned_scopes)
```

This is a **defense-in-depth** measure to prevent privilege escalation via database tampering.

### Query Parameter Parsing Edge Cases

The middleware extracts the token using Starlette's `request.query_params.get("token", "")`:

- Empty value: `?token=` → Treated as missing token → 401
- Multiple tokens: `?token=a&token=b` → First value taken (standard query param behavior)
- URL encoding: `?token=a%2Bb%3Dc` → Decoded by Starlette before middleware sees it

### Performance Considerations

- **Token lookup:** Single indexed database query by `token_hash`
- **Scopes filtering:** O(n) tuple creation where n ≤ 2 (memory, evolution)
- **Log redaction:** Regex substitution on query string (typically <100 bytes)
- **No rate limiting on URL tokens** — Existing middleware may have a token-agnostic rate limit (e.g., per-IP)

## Technische Details

### Routes

| Method | Endpoint | Auth Required | Purpose |
|--------|----------|---------------|---------|
| `POST` | `/token/url` | `admin` scope | Issue new URL token |
| `DELETE` | `/token/url/{name}` | `admin` scope | Revoke URL token by name |
| (any) | `/api/*` | Bearer OR URL token | Use token to call MCP tools |

### Database Indexes

The `url_tokens` table uses:

- **Primary key:** `id` (auto-increment)
- **Unique constraint:** `name` (to prevent duplicate token names)
- **Unique constraint:** `token_hash` (to prevent token collisions; would be 2^-256 probability)

For high-traffic deployments, consider adding:
```sql
CREATE INDEX idx_url_tokens_expires ON url_tokens(expires_at)
WHERE revoked_at IS NULL;
```

This speeds up cleanup queries if implementing a background task to delete very old tokens.

### Token Scheme

URL tokens are **opaque 32-byte hex strings**:
- Generated: `secrets.token_hex(32)` → 64 hex characters
- Format: `a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2` (example)
- No structure; not JWTs or base64 — purely random bytes rendered as hex
- Strength: 2^256 entropy (matches SHA-256 output length)

### ContextVar Semantics

The `_current_scopes` context variable is set by middleware:

```python
_current_scopes: ContextVar[tuple[str, ...]] = ContextVar("current_scopes", default=())

# When a URL token is verified:
scopes_from_db = json.loads(token_row["scopes"])
cleaned = tuple(s for s in scopes_from_db if s != "admin")
_current_scopes.set(cleaned)
```

Scopes persist for the duration of the request and are automatically cleaned up when the request ends (ContextVar isolation per async task).

### Comparison with Bearer Token Flow

**Bearer Token (OAuth 2.1):**
```
Client → /authorize (with PKCE) → User login
      ← Authorization code
      → /token (exchange code for JWT + refresh token)
      ← JWT (contains scopes, exp, iss, sub)
      → /api/context + Authorization: Bearer <JWT>
      ← Protected resource (if signature & exp valid)
```

**URL Token:**
```
Admin → POST /token/url + admin scope
      ← Token hash, stored in DB; raw token returned
Admin → Shares raw token with user (out-of-band)
User  → GET /api/context?token=<token>
      ← SHA-256 hash computed; DB lookup; scopes set
      ← Protected resource (if token valid & not revoked)
```

### Testing

All URL token auth features are covered in `python/tests/test_url_token_auth.py`:

| Test Class | Coverage |
|-----------|----------|
| `TestUrlTokenQueryParamAuth` | AK1 — `?token=` query param acceptance & rejection |
| `TestUrlTokenLogRedaction` | AK5 — Token value redaction in logs |
| `TestAdminScopeNotGrantable` | AK6 — `admin` scope always stripped |
| `TestUrlTokenVerification` | AK7 — Revoked/expired/missing token handling |
| `TestIssueUrlToken` | AK3 — `POST /token/url` endpoint |
| `TestRevokeUrlToken` | AK4 — `DELETE /token/url/{name}` endpoint |

Run all tests:
```bash
cd python
uv run pytest tests/test_url_token_auth.py -v
```

### Error Codes

| Scenario | HTTP Status | Error Response |
|----------|-------------|---|
| No token + no Bearer | 401 Unauthorized | `{"error": "unauthorized", "error_description": "Missing Bearer token or API key"}` |
| Invalid/unknown token | 401 Unauthorized | `{"error": "unauthorized", "error_description": "Invalid or expired token"}` |
| Token expired | 401 Unauthorized | `{"error": "unauthorized", "error_description": "Invalid or expired token"}` |
| Token revoked | 401 Unauthorized | `{"error": "unauthorized", "error_description": "Invalid or expired token"}` |
| POST /token/url without admin | 401/403 | (Depends on other auth method; if URL token without admin, then 401) |
| POST /token/url, expires_in_days > 3650 | 400 Bad Request | `{"error": "invalid_request", "error_description": "expires_in_days must be <= 3650"}` |
| POST /token/url, unknown scope | 422 Unprocessable Entity | `{"error": "invalid_request", "error_description": "Invalid scopes: ..."}` |
| DB error during token lookup | 503 Service Unavailable | `{"error": "service_unavailable", "error_description": "..."}` |

## Grenzen

**Out of Scope:**

- **Token refresh** — URL tokens do not have a refresh mechanism; they are valid until expiry or explicit revocation
- **Fine-grained token management** — No per-tool or per-endpoint restrictions (all or nothing scopes)
- **Token rotation policies** — No automatic rotation; users are responsible for re-issuing if suspicious activity occurs
- **Token usage analytics** — No tracking of which integrations accessed memory via URL tokens
- **Scope upgrading** — Once issued, a URL token's scopes are fixed; cannot be upgraded without revocation & reissuance

**Known Limitations:**

- **Single token per integration** — The `name` field must be unique; users should use distinct names for each tool (e.g., "malte-obsidian", "malte-raycast")
- **No token list endpoint** — No API to list issued tokens (for security/privacy); users must track issuance externally
- **Query string encoding** — Token value must not contain `&` or `=` unencoded; the generated hex is safe, but user-supplied tokens should be validated
- **Log redaction is application-level** — If logs are forwarded to external systems before redaction, they may see the raw token; ensure the middleware runs before any log writing
- **No token metrics** — Server does not track token usage (hits, last-seen) for audit purposes (future work)
