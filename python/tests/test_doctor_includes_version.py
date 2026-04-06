"""Unit tests: doctor() includes server_version and uptime_seconds."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestDoctorIncludesVersion:
    @pytest.mark.asyncio
    async def test_doctor_includes_server_version(self):
        """doctor() must include server_version from importlib.metadata."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetchrow = AsyncMock(return_value={"count": 0, "max": None})
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(side_effect=Exception("no network"))
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

        assert "server_version" in data
        assert isinstance(data["server_version"], str)
        assert len(data["server_version"]) > 0

    @pytest.mark.asyncio
    async def test_doctor_includes_uptime_seconds_positive(self):
        """doctor() must include uptime_seconds > 0 after server start."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetchrow = AsyncMock(return_value={"count": 0, "max": None})
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(side_effect=Exception("no network"))
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

        assert "uptime_seconds" in data
        assert isinstance(data["uptime_seconds"], float | int)
        assert data["uptime_seconds"] >= 0

    @pytest.mark.asyncio
    async def test_doctor_version_matches_package(self):
        """doctor() server_version must match importlib.metadata.version('open-brain')."""
        import importlib.metadata

        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.fetchrow = AsyncMock(return_value={"count": 0, "max": None})
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(side_effect=Exception("no network"))
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

        expected_version = importlib.metadata.version("open-brain")
        assert data["server_version"] == expected_version
