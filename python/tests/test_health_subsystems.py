"""Integration tests: /health endpoint returns subsystem status."""

from __future__ import annotations

import pytest


@pytest.mark.integration
class TestHealthSubsystems:
    @pytest.mark.asyncio
    async def test_health_returns_subsystem_fields(self):
        """GET /health must return status, db, embedding_api, memory_count."""
        import httpx
        from open_brain.server import app

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")
            # Health endpoint should not require auth
            assert response.status_code in (200, 503)
            data = response.json()
            assert "status" in data
            assert "service" in data
            assert "db" in data
            assert "embedding_api" in data
            assert "memory_count" in data

    @pytest.mark.asyncio
    async def test_health_service_name(self):
        """GET /health must identify service as open-brain."""
        import httpx
        from open_brain.server import app

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")
            data = response.json()
            assert data["service"] == "open-brain"

    @pytest.mark.asyncio
    async def test_health_db_field_valid_values(self):
        """db field must be either 'ok' or 'unreachable'."""
        import httpx
        from open_brain.server import app

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")
            data = response.json()
            assert data["db"] in ("ok", "unreachable")

    @pytest.mark.asyncio
    async def test_health_embedding_api_field_valid_values(self):
        """embedding_api field must be 'ok', 'degraded', or 'unreachable'."""
        import httpx
        from open_brain.server import app

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")
            data = response.json()
            assert data["embedding_api"] in ("ok", "degraded", "unreachable", "unknown")
