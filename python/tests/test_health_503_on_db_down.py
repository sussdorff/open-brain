"""Unit tests: /health endpoint returns 503 when DB is down."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mock_http_client(status_code: int = 200):
    """Create a properly structured mock httpx AsyncClient."""
    mock_http_response = MagicMock()
    mock_http_response.status_code = status_code
    mock_http_client = AsyncMock()
    mock_http_client.get = AsyncMock(return_value=mock_http_response)
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=None)
    return mock_http_client


class TestHealth503OnDbDown:
    @pytest.mark.asyncio
    async def test_health_returns_503_when_db_raises(self):
        """When get_pool() raises, health() must return HTTP 503."""
        mock_http_client = _make_mock_http_client()
        with (
            patch("open_brain.server.get_pool", side_effect=Exception("connection refused")),
            patch("open_brain.server.httpx") as mock_httpx,
        ):
            mock_httpx.AsyncClient.return_value = mock_http_client
            from open_brain.server import health
            response = await health()
            assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_health_body_contains_unreachable_when_db_down(self):
        """Response body must include db: unreachable and status: unhealthy when DB is down."""
        mock_http_client = _make_mock_http_client()
        with (
            patch("open_brain.server.get_pool", side_effect=Exception("connection refused")),
            patch("open_brain.server.httpx") as mock_httpx,
        ):
            mock_httpx.AsyncClient.return_value = mock_http_client
            from open_brain.server import health
            response = await health()
            body = json.loads(response.body)
            assert body["db"] == "unreachable"
            assert body["status"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_health_returns_200_when_db_ok(self):
        """When DB is reachable, health() returns 200 with status: ok and db: ok."""
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=42)
        mock_conn.fetchrow = AsyncMock(return_value={"max": None})

        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_http_client = _make_mock_http_client(status_code=200)

        async def _get_pool():
            return mock_pool

        with (
            patch("open_brain.server.get_pool", side_effect=_get_pool),
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
        """Response must contain required fields: status, service, runtime, db, embedding_api, memory_count."""
        mock_http_client = _make_mock_http_client()
        with (
            patch("open_brain.server.get_pool", side_effect=Exception("DB down")),
            patch("open_brain.server.httpx") as mock_httpx,
        ):
            mock_httpx.AsyncClient.return_value = mock_http_client
            from open_brain.server import health
            response = await health()
            body = json.loads(response.body)
            assert "status" in body
            assert "service" in body
            assert "runtime" in body
            assert body["runtime"] == "python"
            assert "db" in body
            assert "embedding_api" in body
            assert "memory_count" in body

    @pytest.mark.asyncio
    async def test_health_skips_voyage_check_when_db_down(self):
        """When DB is down, health() must short-circuit and not call Voyage API."""
        mock_http_client = _make_mock_http_client()
        with (
            patch("open_brain.server.get_pool", side_effect=Exception("connection refused")),
            patch("open_brain.server.httpx") as mock_httpx,
        ):
            mock_httpx.AsyncClient.return_value = mock_http_client
            from open_brain.server import health
            response = await health()
            # Voyage API should not have been called
            mock_httpx.AsyncClient.assert_not_called()
            body = json.loads(response.body)
            assert body["status"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_health_returns_200_with_degraded_embedding_api(self):
        """When DB is up but Voyage API is degraded, health() returns 200 with embedding_api: degraded."""
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=5)
        mock_conn.fetchrow = AsyncMock(return_value={"max": None})

        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        # Voyage API returns non-200
        mock_http_client = _make_mock_http_client(status_code=503)

        async def _get_pool():
            return mock_pool

        with (
            patch("open_brain.server.get_pool", side_effect=_get_pool),
            patch("open_brain.server.httpx") as mock_httpx,
        ):
            mock_httpx.AsyncClient.return_value = mock_http_client
            from open_brain.server import health
            response = await health()
            assert response.status_code == 200
            body = json.loads(response.body)
            assert body["status"] == "ok"
            assert body["db"] == "ok"
            assert body["embedding_api"] == "degraded"

    @pytest.mark.asyncio
    async def test_health_returns_200_with_unreachable_embedding_api(self):
        """When DB is up but Voyage API is unreachable, health() returns 200 with embedding_api: unreachable."""
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=3)
        mock_conn.fetchrow = AsyncMock(return_value={"max": None})

        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        # Voyage API raises network error
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(side_effect=Exception("network error"))
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)

        async def _get_pool():
            return mock_pool

        with (
            patch("open_brain.server.get_pool", side_effect=_get_pool),
            patch("open_brain.server.httpx") as mock_httpx,
        ):
            mock_httpx.AsyncClient.return_value = mock_http_client
            from open_brain.server import health
            response = await health()
            assert response.status_code == 200
            body = json.loads(response.body)
            assert body["status"] == "ok"
            assert body["db"] == "ok"
            assert body["embedding_api"] == "unreachable"
