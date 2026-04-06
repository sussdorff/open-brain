"""Unit tests: /health endpoint returns 503 when DB is down."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestHealth503OnDbDown:
    @pytest.mark.asyncio
    async def test_health_returns_503_when_db_raises(self):
        """When get_pool() raises, health() must return HTTP 503."""
        with patch("open_brain.server.get_pool", side_effect=Exception("connection refused")):
            from open_brain.server import health
            response = await health()
            assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_health_body_contains_unreachable_when_db_down(self):
        """Response body must include db: unreachable and status: unhealthy when DB is down."""
        with patch("open_brain.server.get_pool", side_effect=Exception("connection refused")):
            from open_brain.server import health
            response = await health()
            body = json.loads(response.body)
            assert body["db"] == "unreachable"
            assert body["status"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_health_returns_200_when_db_ok(self):
        """When DB is reachable, health() returns 200 with status: ok and db: ok."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetchrow = AsyncMock(return_value={"count": 42})
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_http_response = MagicMock()
        mock_http_response.status_code = 200
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_http_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("open_brain.server.get_pool", return_value=mock_pool),
            patch("open_brain.server.httpx") as mock_httpx,
        ):
            mock_httpx.AsyncClient.return_value = mock_http_client

            from open_brain.server import health
            response = await health()
            assert response.status_code == 200
            body = json.loads(response.body)
            assert body["status"] == "ok"
            assert body["db"] == "ok"

    @pytest.mark.asyncio
    async def test_health_response_structure(self):
        """Response must contain required fields: status, service, db, embedding_api, memory_count."""
        with patch("open_brain.server.get_pool", side_effect=Exception("DB down")):
            from open_brain.server import health
            response = await health()
            body = json.loads(response.body)
            assert "status" in body
            assert "service" in body
            assert "db" in body
            assert "embedding_api" in body
            assert "memory_count" in body
