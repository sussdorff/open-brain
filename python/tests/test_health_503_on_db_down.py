"""Unit tests: /health endpoint returns 503 when DB is down."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from .test_helpers import make_mock_pool as _make_mock_pool


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
        mock_pool = _make_mock_pool(memory_count=42)

        async def _get_pool():
            return mock_pool

        with patch("open_brain.server.get_pool", side_effect=_get_pool):
            from open_brain.server import health
            response = await health()
            assert response.status_code == 200
            body = json.loads(response.body)
            assert body["status"] == "ok"
            assert body["db"] == "ok"

    @pytest.mark.asyncio
    async def test_health_response_structure(self):
        """Response must contain required fields: status, service, runtime, db, memory_count."""
        with patch("open_brain.server.get_pool", side_effect=Exception("DB down")):
            from open_brain.server import health
            response = await health()
            body = json.loads(response.body)
            assert "status" in body
            assert "service" in body
            assert "runtime" in body
            assert body["runtime"] == "python"
            assert "db" in body
            assert "memory_count" in body
            assert "embedding_api" not in body


@pytest.mark.asyncio
async def test_health_concurrent_requests():
    """Scenario variant 6: concurrent requests return consistent results."""
    from open_brain.server import health

    async def _get_pool():
        return _make_mock_pool(memory_count=10)

    with patch("open_brain.server.get_pool", side_effect=_get_pool):
        responses = await asyncio.gather(*[health() for _ in range(5)])

    for response in responses:
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["status"] == "ok"
        assert body["db"] == "ok"
