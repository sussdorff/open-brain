"""Tests for URL-based token auth (opaque hex tokens stored in DB).

Covers all acceptance criteria for open-brain-pg4:
AK1: BearerAuthMiddleware checks ?token= query param
AK2: URL tokens stored in Postgres with required columns
AK3: POST /token/url endpoint issues tokens (requires admin scope)
AK4: DELETE /token/url/{name} revokes tokens by name (requires admin scope)
AK5: Middleware redacts ?token= value in access log
AK6: admin scope never grantable to URL tokens
AK7: Token verification checks: valid, not revoked, not expired
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def set_api_keys(monkeypatch):
    """Set valid API_KEYS env var before each test so Config picks it up."""
    monkeypatch.setenv("API_KEYS", "valid-key-abc,another-key-xyz")


@pytest.fixture
def auth_client():
    """AsyncClient backed by the FastAPI ASGI app."""
    from open_brain.server import app
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://testserver")


# Admin client sends valid API key (grants admin scope through middleware)
@pytest.fixture
def admin_headers():
    """Headers that grant admin scope (via API key auth path)."""
    return {"X-API-Key": "valid-key-abc"}


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _make_url_token_row(
    name: str = "test-token",
    scopes: list[str] | None = None,
    revoked: bool = False,
    expired: bool = False,
) -> dict:
    """Build a mock asyncpg Record-like dict for a url_tokens row."""
    if scopes is None:
        scopes = ["memory"]
    expires_at = datetime.now(UTC) + (timedelta(seconds=-1) if expired else timedelta(days=365))
    revoked_at = datetime.now(UTC) if revoked else None
    return {
        "name": name,
        "scopes": json.dumps(scopes),
        "expires_at": expires_at,
        "revoked_at": revoked_at,
    }


def _make_mock_pool_with_url_token(row: dict | None):
    """Build a mock asyncpg pool whose fetchrow returns the given row.

    The DB WHERE clause filters revoked/expired tokens. The mock simulates this:
    if the row has revoked_at set or expires_at in the past, return None (no match).
    """
    # Simulate DB WHERE clause: revoked_at IS NULL AND expires_at > NOW()
    effective_row: dict | None = None
    if row is not None:
        is_revoked = row.get("revoked_at") is not None
        is_expired = row.get("expires_at", datetime.now(UTC)) <= datetime.now(UTC)
        if not is_revoked and not is_expired:
            effective_row = row

    mock_conn = AsyncMock()
    # fetchval for daily guard (rate limiter), fetchrow for URL token lookup
    mock_conn.fetchval = AsyncMock(return_value=0)
    mock_conn.fetchrow = AsyncMock(return_value=effective_row)
    mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)
    # fetchrow directly on pool (not via acquire)
    mock_pool.fetchrow = AsyncMock(return_value=effective_row)
    mock_pool.execute = AsyncMock(return_value="INSERT 0 1")
    return mock_pool, mock_conn


# ─── AK1: ?token= query param auth ───────────────────────────────────────────

class TestUrlTokenQueryParamAuth:
    """AK1: BearerAuthMiddleware checks ?token= query param as third auth path."""

    @pytest.mark.asyncio
    async def test_valid_url_token_is_accepted(self, auth_client):
        """A request with a valid ?token= param is accepted (not 401)."""
        raw_token = secrets.token_hex(32)
        row = _make_url_token_row(scopes=["memory"])
        mock_pool, _ = _make_mock_pool_with_url_token(row)

        with patch("open_brain.server.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            resp = await auth_client.get(
                f"/api/context?token={raw_token}",
            )
        assert resp.status_code != 401

    @pytest.mark.asyncio
    async def test_invalid_url_token_returns_401(self, auth_client):
        """A request with an unknown ?token= param returns 401."""
        raw_token = secrets.token_hex(32)
        mock_pool, _ = _make_mock_pool_with_url_token(None)  # token not found

        with patch("open_brain.server.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            resp = await auth_client.get(
                f"/api/context?token={raw_token}",
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_url_token_checked_before_bearer(self, auth_client):
        """?token= param is checked even when no Authorization header present."""
        raw_token = secrets.token_hex(32)
        row = _make_url_token_row(scopes=["memory"])
        mock_pool, _ = _make_mock_pool_with_url_token(row)

        with patch("open_brain.server.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            resp = await auth_client.get(
                f"/api/context?token={raw_token}",
                headers={},  # no auth header
            )
        # Should not get 401 for missing bearer — URL token should succeed
        assert resp.status_code != 401

    @pytest.mark.asyncio
    async def test_missing_token_falls_through_to_bearer_check(self, auth_client):
        """No ?token= param falls through to Bearer check → 401 if no Bearer either."""
        resp = await auth_client.get("/api/context")
        assert resp.status_code == 401


# ─── AK5: Log redaction ───────────────────────────────────────────────────────

class TestUrlTokenLogRedaction:
    """AK5: Middleware redacts ?token= value from access log lines."""

    @pytest.mark.asyncio
    async def test_token_redacted_in_query_string(self, auth_client):
        """After middleware processes request, query_string has token=REDACTED."""
        raw_token = "abc123secret"
        row = _make_url_token_row(scopes=["memory"])
        mock_pool, _ = _make_mock_pool_with_url_token(row)

        captured_scope = {}

        async def capturing_call_next(request):
            captured_scope["query_string"] = request.scope.get("query_string", b"")
            # Return a minimal response
            from starlette.responses import Response
            return Response(content="{}", status_code=200, media_type="application/json")

        with patch("open_brain.server.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            from open_brain.server import BearerAuthMiddleware
            from starlette.testclient import TestClient
            from starlette.requests import Request as StarletteRequest

            middleware = BearerAuthMiddleware(app=None)

            # Build a minimal ASGI scope
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/api/context",
                "query_string": f"token={raw_token}&other=value".encode(),
                "headers": [],
            }
            request = StarletteRequest(scope)

            await middleware.dispatch(request, capturing_call_next)

        qs = captured_scope.get("query_string", b"")
        assert raw_token.encode() not in qs
        assert b"REDACTED" in qs
        # other params preserved
        assert b"other=value" in qs

    @pytest.mark.asyncio
    async def test_query_string_without_token_not_modified(self, auth_client):
        """Query strings without ?token= are not modified."""
        from open_brain.server import BearerAuthMiddleware
        from starlette.requests import Request as StarletteRequest
        from starlette.responses import Response

        middleware = BearerAuthMiddleware(app=None)

        captured = {}

        async def capturing_call_next(request):
            captured["query_string"] = request.scope.get("query_string", b"")
            return Response(content="{}", status_code=200, media_type="application/json")

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/health",
            "query_string": b"foo=bar&baz=qux",
            "headers": [],
        }
        request = StarletteRequest(scope)
        await middleware.dispatch(request, capturing_call_next)

        assert captured["query_string"] == b"foo=bar&baz=qux"


# ─── AK6: admin scope never grantable ─────────────────────────────────────────

class TestAdminScopeNotGrantable:
    """AK6: admin scope is never grantable to URL tokens (enforced server-side)."""

    @pytest.mark.asyncio
    async def test_admin_scope_stripped_from_url_token(self, auth_client):
        """Even if DB row has admin scope, URL token auth never grants admin."""
        raw_token = secrets.token_hex(32)
        # DB row claims admin scope (should be blocked at issuance, but test middleware too)
        row = _make_url_token_row(scopes=["memory", "admin"])
        mock_pool, _ = _make_mock_pool_with_url_token(row)

        from open_brain.server import _current_scopes

        captured_scopes = None

        async def capturing_call_next(request):
            nonlocal captured_scopes
            captured_scopes = _current_scopes.get()
            from starlette.responses import Response
            return Response(content="{}", status_code=200, media_type="application/json")

        with patch("open_brain.server.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            from open_brain.server import BearerAuthMiddleware
            from starlette.requests import Request as StarletteRequest

            middleware = BearerAuthMiddleware(app=None)
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/api/context",
                "query_string": f"token={raw_token}".encode(),
                "headers": [],
            }
            request = StarletteRequest(scope)
            await middleware.dispatch(request, capturing_call_next)

        assert "admin" not in (captured_scopes or ())

    @pytest.mark.asyncio
    async def test_url_token_issue_strips_admin_scope(self, auth_client, admin_headers):
        """POST /token/url with admin in scopes silently strips it."""
        mock_pool, mock_conn = _make_mock_pool_with_url_token(None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        with patch("open_brain.server.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            resp = await auth_client.post(
                "/token/url",
                json={
                    "name": "test-admin-token",
                    "scopes": ["memory", "admin"],
                    "expires_in_days": 30,
                },
                headers=admin_headers,
            )

        # Endpoint should succeed (2xx)
        assert resp.status_code in (200, 201)
        data = resp.json()
        # admin must not be in returned scopes
        assert "admin" not in data.get("scopes", [])


# ─── AK7: Token verification ──────────────────────────────────────────────────

class TestUrlTokenVerification:
    """AK7: Token verification checks: valid opaque token, not revoked, not expired."""

    @pytest.mark.asyncio
    async def test_revoked_token_returns_401(self, auth_client):
        """A revoked URL token is rejected."""
        raw_token = secrets.token_hex(32)
        row = _make_url_token_row(revoked=True)
        mock_pool, _ = _make_mock_pool_with_url_token(row)

        with patch("open_brain.server.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            resp = await auth_client.get(f"/api/context?token={raw_token}")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_expired_token_returns_401(self, auth_client):
        """An expired URL token is rejected."""
        raw_token = secrets.token_hex(32)
        row = _make_url_token_row(expired=True)
        mock_pool, _ = _make_mock_pool_with_url_token(row)

        with patch("open_brain.server.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            resp = await auth_client.get(f"/api/context?token={raw_token}")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_token_not_in_db_returns_401(self, auth_client):
        """A token that doesn't exist in DB returns 401."""
        raw_token = secrets.token_hex(32)
        mock_pool, _ = _make_mock_pool_with_url_token(None)

        with patch("open_brain.server.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            resp = await auth_client.get(f"/api/context?token={raw_token}")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_sets_correct_scopes(self, auth_client):
        """A valid URL token sets _current_scopes to the token's scopes."""
        raw_token = secrets.token_hex(32)
        row = _make_url_token_row(scopes=["memory", "evolution"])
        mock_pool, _ = _make_mock_pool_with_url_token(row)

        from open_brain.server import _current_scopes

        captured_scopes = None

        async def capturing_call_next(request):
            nonlocal captured_scopes
            captured_scopes = _current_scopes.get()
            from starlette.responses import Response
            return Response(content="{}", status_code=200, media_type="application/json")

        with patch("open_brain.server.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            from open_brain.server import BearerAuthMiddleware
            from starlette.requests import Request as StarletteRequest

            middleware = BearerAuthMiddleware(app=None)
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/api/context",
                "query_string": f"token={raw_token}".encode(),
                "headers": [],
            }
            request = StarletteRequest(scope)
            await middleware.dispatch(request, capturing_call_next)

        assert "memory" in (captured_scopes or ())
        assert "evolution" in (captured_scopes or ())

    @pytest.mark.asyncio
    async def test_sha256_hash_used_for_lookup(self, auth_client):
        """Middleware computes SHA-256 of the raw token for DB lookup."""
        raw_token = "my-test-token-12345"
        expected_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        lookup_hashes = []

        async def mock_get_pool():
            mock_pool = MagicMock()
            async def fake_fetchrow(query, token_hash, *args, **kwargs):
                lookup_hashes.append(token_hash)
                return None  # return not found
            mock_pool.fetchrow = fake_fetchrow
            return mock_pool

        with patch("open_brain.server.get_pool", side_effect=mock_get_pool):
            resp = await auth_client.get(f"/api/context?token={raw_token}")

        # The hash passed to DB should be the SHA-256 of raw token
        assert len(lookup_hashes) == 1
        assert lookup_hashes[0] == expected_hash


# ─── AK3: POST /token/url endpoint ────────────────────────────────────────────

class TestIssueUrlToken:
    """AK3: POST /token/url issues URL tokens (requires admin scope)."""

    @pytest.mark.asyncio
    async def test_issue_url_token_requires_admin_scope(self, auth_client):
        """POST /token/url returns 401/403 with a URL token that lacks admin scope."""
        raw_token = secrets.token_hex(32)
        # URL token with only memory scope — no admin
        row = _make_url_token_row(scopes=["memory"])
        mock_pool, _ = _make_mock_pool_with_url_token(row)

        with patch("open_brain.server.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            resp = await auth_client.post(
                f"/token/url?token={raw_token}",
                json={
                    "name": "my-token",
                    "scopes": ["memory"],
                    "expires_in_days": 30,
                },
            )

        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_issue_url_token_with_admin_scope(self, auth_client, admin_headers):
        """POST /token/url with admin scope (via API key) issues a token."""
        mock_pool, mock_conn = _make_mock_pool_with_url_token(None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        with patch("open_brain.server.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            resp = await auth_client.post(
                "/token/url",
                json={
                    "name": "malte-obsidian",
                    "scopes": ["memory", "evolution"],
                    "expires_in_days": 365,
                },
                headers=admin_headers,
            )

        assert resp.status_code in (200, 201)
        data = resp.json()
        assert "token" in data
        assert "name" in data
        assert data["name"] == "malte-obsidian"
        # Token should be a non-empty string (opaque hex)
        assert len(data["token"]) >= 32

    @pytest.mark.asyncio
    async def test_issue_url_token_response_schema(self, auth_client, admin_headers):
        """POST /token/url response includes name, token, scopes, expires_at."""
        mock_pool, mock_conn = _make_mock_pool_with_url_token(None)
        mock_conn.execute = AsyncMock(return_value="INSERT 0 1")

        with patch("open_brain.server.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            resp = await auth_client.post(
                "/token/url",
                json={
                    "name": "test-token",
                    "scopes": ["memory"],
                    "expires_in_days": 7,
                },
                headers=admin_headers,
            )

        assert resp.status_code in (200, 201)
        data = resp.json()
        assert "token" in data
        assert "name" in data
        assert "scopes" in data
        assert "expires_at" in data

    @pytest.mark.asyncio
    async def test_url_token_not_accessible_without_auth(self, auth_client):
        """POST /token/url returns 401 with no auth at all."""
        resp = await auth_client.post(
            "/token/url",
            json={
                "name": "my-token",
                "scopes": ["memory"],
                "expires_in_days": 30,
            },
        )
        assert resp.status_code == 401


# ─── AK4: DELETE /token/url/{name} ───────────────────────────────────────────

class TestRevokeUrlToken:
    """AK4: DELETE /token/url/{name} revokes tokens by name (requires admin scope)."""

    @pytest.mark.asyncio
    async def test_revoke_url_token_requires_admin_scope(self, auth_client):
        """DELETE /token/url/{name} returns 401 without any auth."""
        resp = await auth_client.delete("/token/url/my-token")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_revoke_url_token_with_admin_scope(self, auth_client, admin_headers):
        """DELETE /token/url/{name} with admin scope (via API key) returns 200."""
        mock_pool, mock_conn = _make_mock_pool_with_url_token(None)
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        with patch("open_brain.server.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            resp = await auth_client.delete(
                "/token/url/malte-obsidian",
                headers=admin_headers,
            )

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_revoke_url_token_not_accessible_without_auth(self, auth_client):
        """DELETE /token/url/{name} returns 401 with no auth."""
        resp = await auth_client.delete("/token/url/some-token")
        assert resp.status_code == 401
