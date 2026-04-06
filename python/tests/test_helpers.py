"""Shared mock helpers for test modules.

These are plain functions (NOT pytest fixtures) so they can be called
directly in tests and used alongside unittest.mock.patch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def make_mock_pool(memory_count: int = 10, last_ingestion=None):
    """Create a properly structured mock asyncpg pool."""
    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=memory_count)
    mock_conn.fetchrow = AsyncMock(return_value={"max": last_ingestion})

    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return mock_pool


def make_mock_http_client(status_code: int = 200):
    """Create a properly structured mock httpx AsyncClient."""
    mock_http_response = MagicMock()
    mock_http_response.status_code = status_code
    mock_http_client = AsyncMock()
    mock_http_client.get = AsyncMock(return_value=mock_http_response)
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=None)
    return mock_http_client
