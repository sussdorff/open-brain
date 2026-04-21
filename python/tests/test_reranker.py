"""Tests for the Voyage Rerank-2.5 second-pass reranker."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── Unit tests: reranker module ──────────────────────────────────────────────


class TestRerank:
    """Unit tests for reranker.rerank() with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_rerank_returns_indices_in_relevance_order(self):
        """Successful rerank returns original indices sorted by relevance descending."""
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = {
            "object": "list",
            "data": [
                {"relevance_score": 0.95, "index": 2, "document": "doc C"},
                {"relevance_score": 0.80, "index": 0, "document": "doc A"},
                {"relevance_score": 0.55, "index": 1, "document": "doc B"},
            ],
            "model": "rerank-2.5",
            "usage": {"total_tokens": 100},
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("open_brain.data_layer.reranker.httpx.AsyncClient", return_value=mock_client):
            from open_brain.data_layer.reranker import rerank

            result = await rerank(
                query="database async",
                documents=["doc A", "doc B", "doc C"],
                model="rerank-2.5",
            )

        assert result == [2, 0, 1]

    @pytest.mark.asyncio
    async def test_rerank_with_top_k(self):
        """top_k parameter is forwarded to the API and only that many indices returned."""
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = {
            "object": "list",
            "data": [
                {"relevance_score": 0.9, "index": 1, "document": "doc B"},
                {"relevance_score": 0.7, "index": 0, "document": "doc A"},
            ],
            "model": "rerank-2.5",
            "usage": {"total_tokens": 50},
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("open_brain.data_layer.reranker.httpx.AsyncClient", return_value=mock_client):
            from open_brain.data_layer.reranker import rerank

            result = await rerank(
                query="test",
                documents=["doc A", "doc B", "doc C"],
                model="rerank-2.5",
                top_k=2,
            )

        # Verify top_k was included in the request payload
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs["json"]["top_k"] == 2
        assert result == [1, 0]

    @pytest.mark.asyncio
    async def test_rerank_api_failure_raises_runtime_error(self):
        """Non-success response raises RuntimeError."""
        mock_response = MagicMock()
        mock_response.is_success = False
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("open_brain.data_layer.reranker.httpx.AsyncClient", return_value=mock_client):
            from open_brain.data_layer.reranker import rerank

            with pytest.raises(RuntimeError, match="Voyage Rerank API error 401"):
                await rerank(
                    query="test",
                    documents=["doc A"],
                    model="rerank-2.5",
                )

    @pytest.mark.asyncio
    async def test_rerank_sends_correct_payload(self):
        """Correct query, documents, and model are sent to the API."""
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = {
            "object": "list",
            "data": [{"relevance_score": 0.9, "index": 0, "document": "doc A"}],
            "model": "rerank-2.5",
            "usage": {"total_tokens": 20},
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("open_brain.data_layer.reranker.httpx.AsyncClient", return_value=mock_client):
            from open_brain.data_layer.reranker import rerank

            await rerank(
                query="my search query",
                documents=["doc A", "doc B"],
                model="rerank-2.5",
            )

        call_kwargs = mock_client.post.call_args
        payload = call_kwargs.kwargs["json"]
        assert payload["query"] == "my search query"
        assert payload["documents"] == ["doc A", "doc B"]
        assert payload["model"] == "rerank-2.5"
        assert "top_k" not in payload  # not set when top_k is None


# ─── Unit tests: search integration with reranking ────────────────────────────


class TestSearchWithReranking:
    """Test that search() calls reranker when RERANK_ENABLED=true and skips it when false."""

    def _make_mock_pool(self, mock_conn: AsyncMock) -> MagicMock:
        """Create a properly configured mock pool that works as async context manager."""
        mock_pool = MagicMock()
        # pool.acquire() must return an async context manager
        acquire_cm = MagicMock()
        acquire_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        acquire_cm.__aexit__ = AsyncMock(return_value=False)
        mock_pool.acquire.return_value = acquire_cm
        return mock_pool

    @pytest.mark.asyncio
    async def test_search_with_reranking_disabled_does_not_call_reranker(self):
        """When RERANK_ENABLED=false, reranker.rerank is never called."""
        os.environ["RERANK_ENABLED"] = "false"

        import open_brain.config as config_module
        config_module._config = None  # force reload with new env var

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"id": 1})
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool = self._make_mock_pool(mock_conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=mock_pool),
            patch("open_brain.data_layer.postgres.embed_query_with_usage", new_callable=AsyncMock, return_value=([0.1] * 1024, 10)),
            patch("open_brain.data_layer.postgres.rerank", new_callable=AsyncMock) as mock_rerank,
            patch("asyncio.create_task"),
        ):
            from open_brain.data_layer.postgres import PostgresDataLayer
            from open_brain.data_layer.interface import SearchParams

            dl = PostgresDataLayer()
            await dl.search(SearchParams(query="python async", project="test", limit=5))

        mock_rerank.assert_not_called()

        # cleanup
        del os.environ["RERANK_ENABLED"]

    @pytest.mark.asyncio
    async def test_search_with_reranking_enabled_calls_reranker(self):
        """When RERANK_ENABLED=true (default), reranker.rerank IS called for queries."""
        os.environ["RERANK_ENABLED"] = "true"

        import open_brain.config as config_module
        config_module._config = None

        mock_row = MagicMock()
        mock_row.__getitem__ = MagicMock(side_effect=lambda k: {
            "id": 1, "index_id": 1, "session_id": None, "type": "observation",
            "title": "Test memory", "subtitle": None, "narrative": None,
            "content": "asyncpg is fast", "metadata": {}, "priority": 0.8,
            "stability": "stable", "access_count": 0, "last_accessed_at": None,
            "created_at": None, "updated_at": None, "user_id": None,
            "importance": "medium", "last_decay_at": None,
        }[k])
        mock_row.get = MagicMock(side_effect=lambda k, default=None: {
            "id": 1, "index_id": 1, "session_id": None, "type": "observation",
            "title": "Test memory", "subtitle": None, "narrative": None,
            "content": "asyncpg is fast", "metadata": {}, "priority": 0.8,
            "stability": "stable", "access_count": 0, "last_accessed_at": None,
            "created_at": None, "updated_at": None, "user_id": None,
            "importance": "medium", "last_decay_at": None,
        }.get(k, default))

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"id": 1})
        mock_conn.fetch = AsyncMock(return_value=[mock_row])
        mock_pool = self._make_mock_pool(mock_conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=mock_pool),
            patch("open_brain.data_layer.postgres.embed_query_with_usage", new_callable=AsyncMock, return_value=([0.1] * 1024, 10)),
            patch(
                "open_brain.data_layer.postgres.rerank",
                new_callable=AsyncMock,
                return_value=[0],
            ) as mock_rerank,
            patch("asyncio.create_task"),
        ):
            from open_brain.data_layer.postgres import PostgresDataLayer
            from open_brain.data_layer.interface import SearchParams

            dl = PostgresDataLayer()
            result = await dl.search(SearchParams(query="async database", project="test", limit=5))

        mock_rerank.assert_called_once()
        assert len(result.results) == 1

        # cleanup
        del os.environ["RERANK_ENABLED"]


# ─── Integration test ─────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reranked_results_differ_from_raw_order():
    """Real Voyage API call — verifies reranking produces a ranked order.

    Requires VOYAGE_API_KEY env var to be set.
    """
    from open_brain.data_layer.reranker import rerank

    # Create documents where the "correct" answer is not first
    documents = [
        "The Eiffel Tower is located in Paris, France and was built in 1889.",
        "Python is a programming language known for its simplicity.",
        "asyncpg is a high-performance async PostgreSQL driver for Python.",
    ]
    query = "async Python database driver"

    indices = await rerank(query=query, documents=documents, model="rerank-2.5")

    # We expect index 2 (asyncpg) to rank highest for this query
    assert len(indices) == len(documents)
    assert indices[0] == 2, (
        f"Expected asyncpg doc (index 2) to rank first, got order: {indices}"
    )
