"""Tests for API key auth in the BearerAuthMiddleware."""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from open_brain.auth.tokens import TokenClaims, issue_access_token


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


def _make_bearer() -> str:
    claims = TokenClaims(sub="testuser", client_id="test-client", scopes=[])
    return f"Bearer {issue_access_token(claims)}"


# ─── Public path bypass ───────────────────────────────────────────────────────

class TestPublicPaths:
    @pytest.mark.asyncio
    async def test_health_requires_no_auth(self, auth_client):
        resp = await auth_client.get("/health")
        # 200 (DB up) or 503 (DB down in test env) — both mean auth was not required
        assert resp.status_code in (200, 503)

    @pytest.mark.asyncio
    async def test_health_with_no_headers_returns_200(self, auth_client):
        resp = await auth_client.get("/health", headers={})
        # 200 (DB up) or 503 (DB down in test env) — both mean auth was not required
        assert resp.status_code in (200, 503)

    @pytest.mark.asyncio
    async def test_oauth_metadata_is_public(self, auth_client):
        resp = await auth_client.get("/.well-known/oauth-authorization-server")
        assert resp.status_code == 200


# ─── API key auth ─────────────────────────────────────────────────────────────

class TestApiKeyAuth:
    @pytest.mark.asyncio
    async def test_valid_api_key_passes_through(self, auth_client):
        """A request with a valid X-API-Key header is not rejected by the middleware."""
        resp = await auth_client.get(
            "/api/context",
            headers={"X-API-Key": "valid-key-abc"},
        )
        # The middleware lets it through; whatever the handler returns is fine
        assert resp.status_code != 401

    @pytest.mark.asyncio
    async def test_second_valid_key_passes_through(self, auth_client):
        """Comma-separated list: second key is also accepted."""
        resp = await auth_client.get(
            "/api/context",
            headers={"X-API-Key": "another-key-xyz"},
        )
        assert resp.status_code != 401

    @pytest.mark.asyncio
    async def test_invalid_api_key_returns_401(self, auth_client):
        resp = await auth_client.post(
            "/api/ingest",
            json={"tool_name": "Edit", "tool_input": "x", "tool_response": "y"},
            headers={"X-API-Key": "bad-key"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"] == "unauthorized"

    @pytest.mark.asyncio
    async def test_empty_api_key_header_falls_through_to_bearer_check(self, auth_client):
        """An empty X-API-Key header is ignored and auth falls back to Bearer."""
        resp = await auth_client.post(
            "/api/ingest",
            json={"tool_name": "Edit"},
            headers={"X-API-Key": ""},
        )
        # Empty key is not in the valid set → falls through to Bearer path → 401
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_no_auth_at_all_returns_401(self, auth_client):
        resp = await auth_client.post("/api/ingest", json={"tool_name": "Edit"})
        assert resp.status_code == 401
        assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_no_auth_returns_www_authenticate_header(self, auth_client):
        resp = await auth_client.post("/api/ingest", json={})
        assert resp.status_code == 401
        assert "WWW-Authenticate" in resp.headers


# ─── Bearer token auth still works ───────────────────────────────────────────

class TestBearerTokenStillWorks:
    @pytest.mark.asyncio
    async def test_valid_bearer_token_is_accepted(self, auth_client):
        resp = await auth_client.post(
            "/api/ingest",
            json={"tool_name": "Edit", "tool_input": "x", "tool_response": "y"},
            headers={"Authorization": _make_bearer()},
        )
        assert resp.status_code != 401

    @pytest.mark.asyncio
    async def test_invalid_bearer_token_returns_401(self, auth_client):
        resp = await auth_client.post(
            "/api/ingest",
            json={},
            headers={"Authorization": "Bearer totally-invalid-token"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_bearer_keyword_returns_401(self, auth_client):
        claims = TokenClaims(sub="u", client_id="c", scopes=[])
        token = issue_access_token(claims)
        resp = await auth_client.post(
            "/api/ingest",
            json={},
            headers={"Authorization": token},  # no "Bearer " prefix
        )
        assert resp.status_code == 401
