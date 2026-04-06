"""Unit tests: doctor() MCP tool returns structured diagnostic report."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestDoctorTool:
    @pytest.mark.asyncio
    async def test_doctor_returns_json_string(self):
        """doctor() must return a JSON string."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetchrow = AsyncMock(return_value={"count": 10, "max": None})
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
            from open_brain.server import doctor
            result = await doctor()
            data = json.loads(result)
            assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_doctor_contains_required_fields(self):
        """doctor() must include all required diagnostic fields."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetchrow = AsyncMock(return_value={"count": 10, "max": None})
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
            from open_brain.server import doctor
            result = await doctor()
            data = json.loads(result)

        assert "db_latency_ms" in data
        assert "db_status" in data
        assert "voyage_api_status" in data
        assert "memory_count" in data
        assert "last_ingestion_at" in data
        assert "server_version" in data
        assert "uptime_seconds" in data

    @pytest.mark.asyncio
    async def test_doctor_db_unreachable_when_pool_raises(self):
        """doctor() must report db_status: unreachable when DB raises."""
        mock_http_response = MagicMock()
        mock_http_response.status_code = 200
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_http_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("open_brain.server.get_pool", side_effect=Exception("DB down")),
            patch("open_brain.server.httpx") as mock_httpx,
        ):
            mock_httpx.AsyncClient.return_value = mock_http_client
            from open_brain.server import doctor
            result = await doctor()
            data = json.loads(result)

        assert data["db_status"] == "unreachable"
        assert data["db_latency_ms"] is None

    @pytest.mark.asyncio
    async def test_doctor_voyage_degraded_when_api_returns_non_200(self):
        """doctor() must report voyage_api_status: degraded when API returns non-200."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetchrow = AsyncMock(return_value={"count": 5, "max": None})
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_http_response = MagicMock()
        mock_http_response.status_code = 503
        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_http_response)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("open_brain.server.get_pool", return_value=mock_pool),
            patch("open_brain.server.httpx") as mock_httpx,
        ):
            mock_httpx.AsyncClient.return_value = mock_http_client
            from open_brain.server import doctor
            result = await doctor()
            data = json.loads(result)

        assert data["voyage_api_status"] == "degraded"

    @pytest.mark.asyncio
    async def test_doctor_voyage_unreachable_when_http_raises(self):
        """doctor() must report voyage_api_status: unreachable when httpx raises."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetchrow = AsyncMock(return_value={"count": 5, "max": None})
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(side_effect=Exception("network error"))
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("open_brain.server.get_pool", return_value=mock_pool),
            patch("open_brain.server.httpx") as mock_httpx,
        ):
            mock_httpx.AsyncClient.return_value = mock_http_client
            from open_brain.server import doctor
            result = await doctor()
            data = json.loads(result)

        assert data["voyage_api_status"] == "unreachable"
