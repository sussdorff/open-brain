"""AK 4: Embedding integration tests (mocked HTTP + real format tests)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from open_brain.data_layer.embedding import (
    EMBEDDING_DIM,
    embed,
    embed_batch,
    embed_query,
    to_pg_vector,
)


# ─── Format tests (no API calls) ──────────────────────────────────────────────

class TestToPgVectorFormat:
    def test_correct_bracket_format(self):
        assert to_pg_vector([1.0, 2.0, 3.0]) == "[1.0,2.0,3.0]"

    def test_preserves_precision(self):
        result = to_pg_vector([0.123456789])
        assert "0.123456789" in result

    def test_1024_dim_vector(self):
        vec = [0.001] * EMBEDDING_DIM
        result = to_pg_vector(vec)
        parts = result[1:-1].split(",")
        assert len(parts) == EMBEDDING_DIM

    def test_no_spaces(self):
        result = to_pg_vector([1.0, 2.0])
        assert " " not in result


# ─── Embed function tests (mocked HTTP) ────────────────────────────────────────

def _make_voyage_response(embedding: list[float]) -> httpx.Response:
    """Build a mock Voyage API response."""
    import json
    body = json.dumps({"data": [{"embedding": embedding}]})
    return httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})


class TestEmbed:
    @pytest.mark.asyncio
    async def test_embed_returns_list_of_floats(self):
        expected = [0.1] * 1024
        mock_response = _make_voyage_response(expected)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await embed("test text")

        assert result == expected
        assert len(result) == 1024

    @pytest.mark.asyncio
    async def test_embed_uses_document_input_type(self):
        expected = [0.5] * 1024
        mock_response = _make_voyage_response(expected)
        captured_body = {}

        async def mock_post(url, **kwargs):
            captured_body.update(kwargs.get("json", {}))
            return mock_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            await embed("test")

        assert captured_body.get("input_type") == "document"

    @pytest.mark.asyncio
    async def test_embed_raises_on_api_error(self):
        error_response = httpx.Response(401, content=b'{"error": "invalid key"}')

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=error_response)
            mock_client_cls.return_value = mock_client

            with pytest.raises(RuntimeError, match="Voyage API error 401"):
                await embed("test")


class TestEmbedQuery:
    @pytest.mark.asyncio
    async def test_embed_query_uses_query_input_type(self):
        expected = [0.3] * 1024
        mock_response = _make_voyage_response(expected)
        captured_body = {}

        async def mock_post(url, **kwargs):
            captured_body.update(kwargs.get("json", {}))
            return mock_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            result = await embed_query("search query")

        assert captured_body.get("input_type") == "query"
        assert result == expected

    @pytest.mark.asyncio
    async def test_embed_query_raises_on_error(self):
        error_response = httpx.Response(500, content=b"Server error")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=error_response)
            mock_client_cls.return_value = mock_client

            with pytest.raises(RuntimeError, match="Voyage API error 500"):
                await embed_query("test")


class TestEmbedBatch:
    @pytest.mark.asyncio
    async def test_embed_batch_returns_multiple_embeddings(self):
        embeddings = [[float(i)] * 1024 for i in range(3)]
        import json
        batch_response = httpx.Response(
            200,
            content=json.dumps({"data": [{"embedding": e} for e in embeddings]}).encode(),
            headers={"content-type": "application/json"},
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=batch_response)
            mock_client_cls.return_value = mock_client

            result = await embed_batch(["text1", "text2", "text3"])

        assert len(result) == 3
        assert result[0] == embeddings[0]

    @pytest.mark.asyncio
    async def test_embed_batch_empty_returns_empty(self):
        with patch("httpx.AsyncClient"):
            result = await embed_batch([])
        assert result == []


# ─── Integration test (skipped by default) ────────────────────────────────────

@pytest.mark.integration
class TestEmbedIntegration:
    """Real Voyage API integration tests. Run with INTEGRATION_TEST=1."""

    @pytest.mark.asyncio
    async def test_embed_real_text(self):
        """Actually call Voyage API and verify embedding shape."""
        result = await embed("Python is a programming language")
        assert isinstance(result, list)
        assert len(result) == EMBEDDING_DIM
        assert all(isinstance(x, float) for x in result)

    @pytest.mark.asyncio
    async def test_embed_query_real(self):
        """Actually call Voyage API with query type."""
        result = await embed_query("What is Python?")
        assert len(result) == EMBEDDING_DIM

    @pytest.mark.asyncio
    async def test_query_and_document_embeddings_differ(self):
        """Query and document embeddings should be different for same text."""
        text = "Python programming"
        doc_emb = await embed(text)
        query_emb = await embed_query(text)
        # They should differ due to different input_type
        assert doc_emb != query_emb
