"""Unit tests for _check_voyage_api_status() — three outcome paths + TTL cache."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import open_brain.server as server_module
from open_brain.server import _check_voyage_api_status


class TestCheckVoyageApiStatus:
    @pytest.mark.asyncio
    async def test_returns_ok_on_http_200(self):
        """HTTP 200 from POST /v1/embeddings must return 'ok'."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("open_brain.server.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client

            result = await _check_voyage_api_status()

        assert result == "ok"
        mock_client.post.assert_awaited_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://api.voyageai.com/v1/embeddings"
        assert call_args[1]["json"]["input"] == ["healthcheck"]

    @pytest.mark.asyncio
    async def test_returns_degraded_on_non_200(self):
        """Non-200 HTTP response from POST /v1/embeddings must return 'degraded'."""
        for status_code in [404, 500, 503, 401, 429]:
            server_module._voyage_status_cache = None  # reset between iterations

            mock_response = MagicMock()
            mock_response.status_code = status_code

            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch("open_brain.server.httpx") as mock_httpx:
                mock_httpx.AsyncClient.return_value = mock_client

                result = await _check_voyage_api_status()

            assert result == "degraded", f"Expected 'degraded' for status {status_code}, got '{result}'"

    @pytest.mark.asyncio
    async def test_returns_unreachable_on_network_exception(self):
        """Network exception (timeout, DNS, etc.) must return 'unreachable'."""
        import httpx

        for exc in [httpx.TimeoutException("timed out", request=None), ConnectionError("DNS failure"), Exception("network error")]:
            server_module._voyage_status_cache = None  # reset between iterations

            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=exc)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch("open_brain.server.httpx") as mock_httpx:
                mock_httpx.AsyncClient.return_value = mock_client

                result = await _check_voyage_api_status()

            assert result == "unreachable", f"Expected 'unreachable' for {type(exc).__name__}, got '{result}'"

    @pytest.mark.asyncio
    async def test_cached_result_returned_within_ttl(self):
        """Within TTL, the cached status is returned without making an HTTP call."""
        import time

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("open_brain.server.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client

            # First call — hits the real (mocked) API
            result1 = await _check_voyage_api_status()
            assert result1 == "ok"
            assert mock_client.post.await_count == 1

            # Second call within TTL — must return cached value, no extra HTTP call
            result2 = await _check_voyage_api_status()
            assert result2 == "ok"
            assert mock_client.post.await_count == 1, "HTTP call must NOT fire again within TTL"

    @pytest.mark.asyncio
    async def test_cache_refreshed_after_ttl_expires(self):
        """After TTL expires, the next call fires a new HTTP request."""
        import time

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        # Seed the cache with an expired entry
        expired_at = time.monotonic() - server_module._VOYAGE_STATUS_TTL - 1.0
        server_module._voyage_status_cache = ("ok", expired_at)

        with patch("open_brain.server.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client

            result = await _check_voyage_api_status()

        assert result == "ok"
        assert mock_client.post.await_count == 1, "HTTP call must fire when cache has expired"
