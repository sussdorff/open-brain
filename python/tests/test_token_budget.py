"""Tests for NBJ-5 Token Budget: embedding cost tracking + ingestion guard.

AK1: embed_with_usage() returns (embedding, token_count) tuple
AK2: stats() includes embeddings_today, embedding_tokens_today, estimated_embedding_cost_today
AK3: save_memory() enforces MAX_MEMORIES_PER_DAY guard
AK4: save_memory() enforces rate limit (10/60s)
"""

from __future__ import annotations

import json
import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_voyage_response_with_usage(embedding: list[float], token_count: int) -> httpx.Response:
    """Build a mock Voyage API response including usage.total_tokens."""
    body = json.dumps({
        "data": [{"embedding": embedding}],
        "usage": {"total_tokens": token_count},
    })
    return httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})


def _mock_httpx_client(response: httpx.Response):
    """Context manager helper that patches httpx.AsyncClient to return response."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=response)
    return mock_client


# ─── AK1: embed_with_usage returns (embedding, token_count) ────────────────────

class TestEmbedWithUsage:
    """embed_with_usage() and embed_query_with_usage() return (list[float], int)."""

    @pytest.mark.asyncio
    async def test_embed_with_usage_returns_tuple(self):
        """embed_with_usage() must return (list[float], int)."""
        from open_brain.data_layer.embedding import embed_with_usage

        expected_vec = [0.1] * 1024
        mock_response = _make_voyage_response_with_usage(expected_vec, token_count=42)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_httpx_client(mock_response)
            result = await embed_with_usage("hello world")

        assert isinstance(result, tuple), "embed_with_usage must return a tuple"
        embedding, token_count = result
        assert embedding == expected_vec
        assert token_count == 42

    @pytest.mark.asyncio
    async def test_embed_with_usage_token_count_from_api(self):
        """Token count is taken from API response usage.total_tokens."""
        from open_brain.data_layer.embedding import embed_with_usage

        mock_response = _make_voyage_response_with_usage([0.5] * 1024, token_count=128)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_httpx_client(mock_response)
            _, token_count = await embed_with_usage("some text")

        assert token_count == 128

    @pytest.mark.asyncio
    async def test_embed_query_with_usage_returns_tuple(self):
        """embed_query_with_usage() must return (list[float], int)."""
        from open_brain.data_layer.embedding import embed_query_with_usage

        expected_vec = [0.2] * 1024
        mock_response = _make_voyage_response_with_usage(expected_vec, token_count=15)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_httpx_client(mock_response)
            result = await embed_query_with_usage("search query")

        embedding, token_count = result
        assert embedding == expected_vec
        assert token_count == 15

    @pytest.mark.asyncio
    async def test_embed_batch_with_usage_returns_tuple(self):
        """embed_batch_with_usage() must return (list[list[float]], int)."""
        from open_brain.data_layer.embedding import embed_batch_with_usage

        embeddings = [[0.1] * 1024, [0.2] * 1024]
        body = json.dumps({
            "data": [{"embedding": e} for e in embeddings],
            "usage": {"total_tokens": 50},
        })
        mock_response = httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/json"}
        )

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_httpx_client(mock_response)
            result = await embed_batch_with_usage(["text1", "text2"])

        vecs, token_count = result
        assert len(vecs) == 2
        assert token_count == 50


# ─── AK2: stats() exposes embedding cost metrics ──────────────────────────────

class TestStatsEmbeddingMetrics:
    """stats() includes embeddings_today, embedding_tokens_today, estimated_embedding_cost_today."""

    @pytest.mark.asyncio
    async def test_stats_includes_embeddings_today(self):
        """stats() result includes 'embeddings_today' key."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        # Mock all fetchrow / fetch calls
        async def fetchrow_side_effect(query, *args):
            if "COUNT(*)::int AS count FROM memories" in query:
                return {"count": 100}
            if "COUNT(*)::int AS count FROM sessions" in query:
                return {"count": 10}
            if "COUNT(*)::int AS count FROM memory_relationships" in query:
                return {"count": 50}
            if "pg_database_size" in query:
                return {"size": 1048576}
            # embedding token log: count today's rows
            if "embedding_token_log" in query and "COUNT" in query:
                return {"count": 5, "total_tokens": 250}
            return None

        mock_conn.fetchrow = fetchrow_side_effect

        async def fetch_side_effect(query, *args):
            if "type" in query and "COUNT" in query:
                return []
            if "user_id" in query and "COUNT" in query:
                return []
            return []

        mock_conn.fetch = fetch_side_effect

        with patch("open_brain.data_layer.postgres.get_pool", return_value=mock_pool):
            from open_brain.data_layer.postgres import PostgresDataLayer
            dl = PostgresDataLayer()
            result = await dl.stats()

        assert "embeddings_today" in result, "stats() must include 'embeddings_today'"
        assert "embedding_tokens_today" in result, "stats() must include 'embedding_tokens_today'"
        assert "estimated_embedding_cost_today" in result, (
            "stats() must include 'estimated_embedding_cost_today'"
        )

    @pytest.mark.asyncio
    async def test_stats_embedding_cost_calculation(self):
        """estimated_embedding_cost_today = tokens * 0.00000012."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        token_count = 1_000_000  # 1M tokens → $0.12

        async def fetchrow_side_effect(query, *args):
            if "COUNT(*)::int AS count FROM memories" in query:
                return {"count": 0}
            if "COUNT(*)::int AS count FROM sessions" in query:
                return {"count": 0}
            if "COUNT(*)::int AS count FROM memory_relationships" in query:
                return {"count": 0}
            if "pg_database_size" in query:
                return {"size": 0}
            if "embedding_token_log" in query:
                return {"count": 10, "total_tokens": token_count}
            return None

        mock_conn.fetchrow = fetchrow_side_effect
        mock_conn.fetch = AsyncMock(return_value=[])

        with patch("open_brain.data_layer.postgres.get_pool", return_value=mock_pool):
            from open_brain.data_layer.postgres import PostgresDataLayer
            dl = PostgresDataLayer()
            result = await dl.stats()

        expected_cost = round(token_count * 0.00000012, 6)
        assert result["embedding_tokens_today"] == token_count
        assert abs(result["estimated_embedding_cost_today"] - expected_cost) < 1e-9


# ─── AK3: MAX_MEMORIES_PER_DAY guard ─────────────────────────────────────────

class TestDailyMemoryGuard:
    """save_memory() rejects saves beyond MAX_MEMORIES_PER_DAY."""

    @pytest.mark.asyncio
    async def test_save_memory_rejected_when_daily_limit_exceeded(self):
        """save_memory() returns error string when today's count >= MAX_MEMORIES_PER_DAY."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        # Simulate daily count AT the limit
        mock_conn.fetchval = AsyncMock(return_value=500)  # exactly at MAX_MEMORIES_PER_DAY default

        with patch("open_brain.server.get_pool", return_value=mock_pool):
            import open_brain.server as server_module
            # Reset rate limiter to avoid interference
            server_module._save_timestamps.clear()
            result = await server_module.save_memory(
                text="This should be rejected",
                is_test=False,
            )

        data = json.loads(result)
        assert "error" in data or "limit" in data.get("message", "").lower(), (
            f"Expected error/limit message, got: {result}"
        )
        assert "500" in result or "limit" in result.lower() or "exceeded" in result.lower()

    @pytest.mark.asyncio
    async def test_save_memory_allowed_below_daily_limit(self):
        """save_memory() proceeds normally when today's count < MAX_MEMORIES_PER_DAY."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        # Simulate daily count below limit
        mock_conn.fetchval = AsyncMock(return_value=10)

        mock_dl = AsyncMock()
        from open_brain.data_layer.interface import SaveMemoryResult
        mock_dl.save_memory.return_value = SaveMemoryResult(id=99, message="Memory saved")
        mock_dl.update_memory.return_value = None

        with (
            patch("open_brain.server.get_pool", return_value=mock_pool),
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", new_callable=AsyncMock, return_value={}),
            patch("open_brain.server._extract_entities", new_callable=AsyncMock, return_value={}),
        ):
            import open_brain.server as server_module
            server_module._save_timestamps.clear()
            result = await server_module.save_memory(
                text="This should be allowed",
                is_test=False,
            )

        data = json.loads(result)
        assert data.get("id") == 99

    @pytest.mark.asyncio
    async def test_save_memory_daily_guard_bypassed_for_is_test(self):
        """is_test=True bypasses daily guard without checking DB."""
        import open_brain.server as server_module
        server_module._save_timestamps.clear()
        result = await server_module.save_memory(
            text="Test artifact",
            is_test=True,
        )
        data = json.loads(result)
        # is_test returns early before any DB check
        assert data.get("id") == -1

    @pytest.mark.asyncio
    async def test_max_memories_per_day_config_default(self):
        """Config has MAX_MEMORIES_PER_DAY with default 500."""
        from open_brain.config import Config
        # Check the field exists with default 500
        import os
        os.environ.setdefault("MAX_MEMORIES_PER_DAY", "500")
        c = Config(
            MCP_SERVER_URL="http://localhost:8091",
            AUTH_USER="u",
            AUTH_PASSWORD="password123",
            JWT_SECRET="a" * 32,
            VOYAGE_API_KEY="key",
        )
        assert hasattr(c, "MAX_MEMORIES_PER_DAY"), "Config must have MAX_MEMORIES_PER_DAY field"
        assert c.MAX_MEMORIES_PER_DAY == 500


# ─── AK4: Rate limit (10/60s) ─────────────────────────────────────────────────

class TestSaveMemoryRateLimit:
    """save_memory() is rate-limited to 10 calls per 60 seconds."""

    @pytest.mark.asyncio
    async def test_rate_limit_rejected_after_10_calls(self):
        """11th save_memory call within 60s returns rate-limit error."""
        import open_brain.server as server_module

        # Pre-fill the deque with 10 recent timestamps (now - 1 second each)
        server_module._save_timestamps.clear()
        now = time.monotonic()
        for _ in range(10):
            server_module._save_timestamps.append(now - 1.0)  # 1s ago, within 60s window

        result = await server_module.save_memory(
            text="This should be rate-limited",
            is_test=False,
        )

        data = json.loads(result)
        # Should contain rate limit error
        result_str = result.lower()
        assert "rate" in result_str or "limit" in result_str, (
            f"Expected rate limit message, got: {result}"
        )

    @pytest.mark.asyncio
    async def test_rate_limit_not_triggered_below_threshold(self):
        """9 calls within 60s should not trigger rate limit."""
        import open_brain.server as server_module

        # Pre-fill with only 9 timestamps
        server_module._save_timestamps.clear()
        now = time.monotonic()
        for _ in range(9):
            server_module._save_timestamps.append(now - 1.0)

        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_conn.fetchval = AsyncMock(return_value=0)  # 0 memories today

        mock_dl = AsyncMock()
        from open_brain.data_layer.interface import SaveMemoryResult
        mock_dl.save_memory.return_value = SaveMemoryResult(id=77, message="Memory saved")
        mock_dl.update_memory.return_value = None

        with (
            patch("open_brain.server.get_pool", return_value=mock_pool),
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", new_callable=AsyncMock, return_value={}),
            patch("open_brain.server._extract_entities", new_callable=AsyncMock, return_value={}),
        ):
            result = await server_module.save_memory(
                text="This should go through",
                is_test=False,
            )

        data = json.loads(result)
        assert data.get("id") == 77

    @pytest.mark.asyncio
    async def test_rate_limit_window_expires(self):
        """Calls older than 60s are pruned and do not count toward the limit."""
        import open_brain.server as server_module

        # Fill with 10 OLD timestamps (61 seconds ago — outside window)
        server_module._save_timestamps.clear()
        old_time = time.monotonic() - 61.0
        for _ in range(10):
            server_module._save_timestamps.append(old_time)

        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_conn.fetchval = AsyncMock(return_value=0)  # 0 memories today

        mock_dl = AsyncMock()
        from open_brain.data_layer.interface import SaveMemoryResult
        mock_dl.save_memory.return_value = SaveMemoryResult(id=88, message="Memory saved")
        mock_dl.update_memory.return_value = None

        with (
            patch("open_brain.server.get_pool", return_value=mock_pool),
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", new_callable=AsyncMock, return_value={}),
            patch("open_brain.server._extract_entities", new_callable=AsyncMock, return_value={}),
        ):
            result = await server_module.save_memory(
                text="Old timestamps expired, should be allowed",
                is_test=False,
            )

        data = json.loads(result)
        assert data.get("id") == 88

    @pytest.mark.asyncio
    async def test_rate_limit_bypassed_for_is_test(self):
        """is_test=True bypasses rate limit check."""
        import open_brain.server as server_module

        # Even if 10 recent timestamps are present, is_test bypasses everything
        server_module._save_timestamps.clear()
        now = time.monotonic()
        for _ in range(10):
            server_module._save_timestamps.append(now - 1.0)

        result = await server_module.save_memory(
            text="Test artifact — rate limit bypassed",
            is_test=True,
        )
        data = json.loads(result)
        assert data.get("id") == -1

    @pytest.mark.asyncio
    async def test_rate_limit_error_includes_retry_hint(self):
        """Rate limit error message includes a retry-after hint."""
        import open_brain.server as server_module

        server_module._save_timestamps.clear()
        now = time.monotonic()
        for _ in range(10):
            server_module._save_timestamps.append(now - 5.0)  # 5s ago, oldest will expire in 55s

        result = await server_module.save_memory(
            text="Rate limited — check hint",
            is_test=False,
        )
        # Should mention seconds until retry
        result_lower = result.lower()
        assert "second" in result_lower or "retry" in result_lower or "try again" in result_lower, (
            f"Expected retry hint in: {result}"
        )

    def test_save_timestamps_deque_exists_at_module_level(self):
        """_save_timestamps deque must exist at module level in server.py."""
        import open_brain.server as server_module
        assert hasattr(server_module, "_save_timestamps"), (
            "server.py must have _save_timestamps deque at module level"
        )
        from collections import deque
        assert isinstance(server_module._save_timestamps, deque)
