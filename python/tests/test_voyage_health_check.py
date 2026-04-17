"""Unit tests for _check_voyage_api_status() — three outcome paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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
            from open_brain.server import _check_voyage_api_status

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
            mock_response = MagicMock()
            mock_response.status_code = status_code

            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch("open_brain.server.httpx") as mock_httpx:
                mock_httpx.AsyncClient.return_value = mock_client
                from open_brain.server import _check_voyage_api_status

                result = await _check_voyage_api_status()

            assert result == "degraded", f"Expected 'degraded' for status {status_code}, got '{result}'"

    @pytest.mark.asyncio
    async def test_returns_unreachable_on_network_exception(self):
        """Network exception (timeout, DNS, etc.) must return 'unreachable'."""
        import httpx

        for exc in [httpx.TimeoutException("timed out", request=None), ConnectionError("DNS failure"), Exception("network error")]:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=exc)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch("open_brain.server.httpx") as mock_httpx:
                mock_httpx.AsyncClient.return_value = mock_client
                # Re-raise the same exception type from the mock
                mock_httpx.AsyncClient.return_value = mock_client
                from open_brain.server import _check_voyage_api_status

                result = await _check_voyage_api_status()

            assert result == "unreachable", f"Expected 'unreachable' for {type(exc).__name__}, got '{result}'"
