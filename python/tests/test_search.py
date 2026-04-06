"""AK 3: Unit tests for hybrid search logic (mocked DB)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.embedding import to_pg_vector
from open_brain.data_layer.interface import Memory, SearchParams, TimelineParams
from open_brain.data_layer.refine import find_obvious_duplicates


# ─── pgvector format tests ─────────────────────────────────────────────────────

class TestToPgVector:
    def test_basic_format(self):
        embedding = [0.1, 0.2, 0.3]
        result = to_pg_vector(embedding)
        assert result == "[0.1,0.2,0.3]"

    def test_empty_list(self):
        result = to_pg_vector([])
        assert result == "[]"

    def test_negative_values(self):
        embedding = [-0.5, 0.0, 0.5]
        result = to_pg_vector(embedding)
        assert result == "[-0.5,0.0,0.5]"

    def test_1024_dimension(self):
        embedding = [0.001] * 1024
        result = to_pg_vector(embedding)
        assert result.startswith("[")
        assert result.endswith("]")
        assert result.count(",") == 1023

    def test_no_spaces_in_output(self):
        embedding = [1.0, 2.0, 3.0]
        result = to_pg_vector(embedding)
        assert " " not in result


# ─── FindObviousDuplicates tests ───────────────────────────────────────────────

class TestFindObviousDuplicates:
    def test_finds_duplicates_by_title(self, sample_memories):
        # sample_memories[0] and [1] both have title "Python best practices"
        actions = find_obvious_duplicates(sample_memories)
        merge_actions = [a for a in actions if a.action == "merge"]
        assert len(merge_actions) == 1
        assert set(merge_actions[0].memory_ids) == {1, 2}

    def test_no_duplicates(self):
        from open_brain.data_layer.interface import Memory
        memories = [
            Memory(
                id=1, index_id=1, session_id=None, type="obs", title="Unique title A",
                subtitle=None, narrative=None, content="Content A",
                metadata={}, priority=0.5, stability="stable", access_count=0,
                last_accessed_at=None, created_at="", updated_at="",
            ),
            Memory(
                id=2, index_id=1, session_id=None, type="obs", title="Unique title B",
                subtitle=None, narrative=None, content="Content B",
                metadata={}, priority=0.5, stability="stable", access_count=0,
                last_accessed_at=None, created_at="", updated_at="",
            ),
        ]
        actions = find_obvious_duplicates(memories)
        assert actions == []

    def test_empty_title_uses_content_prefix(self):
        from open_brain.data_layer.interface import Memory
        memories = [
            Memory(
                id=1, index_id=1, session_id=None, type="obs", title=None,
                subtitle=None, narrative=None, content="Same content prefix here",
                metadata={}, priority=0.5, stability="stable", access_count=0,
                last_accessed_at=None, created_at="", updated_at="",
            ),
            Memory(
                id=2, index_id=1, session_id=None, type="obs", title=None,
                subtitle=None, narrative=None, content="Same content prefix here",
                metadata={}, priority=0.5, stability="stable", access_count=0,
                last_accessed_at=None, created_at="", updated_at="",
            ),
        ]
        actions = find_obvious_duplicates(memories)
        assert len(actions) == 1
        assert actions[0].action == "merge"
        assert set(actions[0].memory_ids) == {1, 2}

    def test_case_insensitive_matching(self):
        from open_brain.data_layer.interface import Memory
        memories = [
            Memory(
                id=1, index_id=1, session_id=None, type="obs", title="Python Tips",
                subtitle=None, narrative=None, content="content",
                metadata={}, priority=0.5, stability="stable", access_count=0,
                last_accessed_at=None, created_at="", updated_at="",
            ),
            Memory(
                id=2, index_id=1, session_id=None, type="obs", title="python tips",
                subtitle=None, narrative=None, content="content",
                metadata={}, priority=0.5, stability="stable", access_count=0,
                last_accessed_at=None, created_at="", updated_at="",
            ),
        ]
        actions = find_obvious_duplicates(memories)
        assert len(actions) == 1

    def test_three_duplicates_merge_all(self):
        from open_brain.data_layer.interface import Memory
        memories = [
            Memory(
                id=i, index_id=1, session_id=None, type="obs", title="Same",
                subtitle=None, narrative=None, content="content",
                metadata={}, priority=0.5, stability="stable", access_count=0,
                last_accessed_at=None, created_at="", updated_at="",
            )
            for i in [10, 20, 30]
        ]
        actions = find_obvious_duplicates(memories)
        assert len(actions) == 1
        assert set(actions[0].memory_ids) == {10, 20, 30}


# ─── PostgresDataLayer unit tests (mocked pool) ───────────────────────────────

def _make_mock_pool(mock_conn):
    """Build a properly structured asyncpg pool mock."""
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    @asynccontextmanager
    async def fake_acquire():
        yield mock_conn

    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


class TestPostgresDataLayerSearch:
    """Unit tests for PostgresDataLayer.search() with mocked asyncpg."""

    @pytest.fixture
    def dl(self):
        from open_brain.data_layer.postgres import PostgresDataLayer
        return PostgresDataLayer()

    @pytest.fixture
    def mock_conn(self):
        """Mock asyncpg connection."""
        conn = AsyncMock()
        return conn

    @pytest.fixture
    def mock_pool(self, mock_conn):
        """Mock asyncpg pool that yields the mock connection."""
        return _make_mock_pool(mock_conn)

    @pytest.mark.asyncio
    async def test_search_no_query_builds_basic_select(self, dl, mock_pool, mock_conn):
        """search() without query uses SELECT with WHERE conditions."""
        # project=None => _resolve_index_id returns None immediately (no DB call)
        # Then: conn.fetch for main SELECT, conn.fetchrow for COUNT
        mock_conn.fetch.return_value = []
        mock_conn.fetchrow.return_value = {"total": 0}

        with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            result = await dl.search(SearchParams())
            assert result.results == []
            assert result.total == 0

    @pytest.mark.asyncio
    async def test_search_with_query_calls_embed_query(self, dl, mock_pool, mock_conn):
        """search() with query calls embed_query and uses hybrid_search."""
        # project=None => no fetchrow for index; hybrid_search uses conn.fetch
        mock_conn.fetch.return_value = []

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=mock_pool),
            patch("open_brain.data_layer.postgres.embed_query_with_usage", new_callable=AsyncMock) as mock_embed,
            patch("asyncio.create_task"),
        ):
            mock_embed.return_value = ([0.1] * 1024, 10)
            await dl.search(SearchParams(query="test query"))
            mock_embed.assert_called_once_with("test query")

    @pytest.mark.asyncio
    async def test_search_fallback_on_embed_error(self, dl, mock_pool, mock_conn):
        """search() falls back to FTS if embedding fails."""
        # project=None => no index fetchrow; fallback FTS uses fetch + fetchrow
        mock_conn.fetch.return_value = []
        mock_conn.fetchrow.return_value = {"total": 0}

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=mock_pool),
            patch("open_brain.data_layer.postgres.embed_query_with_usage", side_effect=RuntimeError("API down")),
        ):
            # Should not raise; falls back to FTS
            result = await dl.search(SearchParams(query="test"))
            assert result.results == []


class TestPostgresDataLayerStats:
    @pytest.fixture
    def dl(self):
        from open_brain.data_layer.postgres import PostgresDataLayer
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_stats_returns_correct_keys(self, dl):
        mock_conn = AsyncMock()
        mock_conn.fetchrow.side_effect = [
            {"count": 42},    # memories
            {"count": 5},     # sessions
            {"count": 100},   # relationships
            {"size": 10 * 1024 * 1024},  # db size 10MB
            {"count": 0, "total_tokens": 0},  # embedding_token_log today
        ]
        mock_pool = _make_mock_pool(mock_conn)

        with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            result = await dl.stats()

        assert result["memories"] == 42
        assert result["sessions"] == 5
        assert result["relationships"] == 100
        assert result["db_size_bytes"] == 10 * 1024 * 1024
        assert result["db_size_mb"] == 10.0


class TestPostgresDataLayerSaveMemory:
    @pytest.fixture
    def dl(self):
        from open_brain.data_layer.postgres import PostgresDataLayer
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_save_memory_returns_id_and_message(self, dl):
        mock_conn = AsyncMock()
        # When project=None, _resolve_index_id returns early (no fetchrow call)
        # Dedup check (1st fetchrow) returns None (no duplicate found),
        # INSERT RETURNING id (2nd fetchrow) returns {"id": 99}
        mock_conn.fetchrow.side_effect = [None, {"id": 99}]
        mock_pool = _make_mock_pool(mock_conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=mock_pool),
            patch("asyncio.create_task"),  # prevent background task from running
        ):
            from open_brain.data_layer.interface import SaveMemoryParams
            result = await dl.save_memory(SaveMemoryParams(text="test memory"))

        assert result.id == 99
        assert result.message == "Memory saved"


class TestPostgresDataLayerGetObservations:
    @pytest.fixture
    def dl(self):
        from open_brain.data_layer.postgres import PostgresDataLayer
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_empty_ids_returns_empty_list(self, dl):
        result = await dl.get_observations([])
        assert result == []

    @pytest.mark.asyncio
    async def test_fetches_by_ids(self, dl):
        mock_data = {
            "id": 1, "index_id": 1, "session_id": None, "type": "observation",
            "title": "Test", "subtitle": None, "narrative": None,
            "content": "test content", "metadata": {}, "priority": 0.5,
            "stability": "stable", "access_count": 0,
            "last_accessed_at": None, "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
        }
        mock_row = MagicMock()
        mock_row.__getitem__ = lambda self, key: mock_data[key]
        mock_row.get = lambda key, default=None: mock_data.get(key, default)

        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = [mock_row]
        mock_pool = _make_mock_pool(mock_conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=mock_pool),
            patch("asyncio.create_task"),
        ):
            result = await dl.get_observations([1])

        assert len(result) == 1
        mock_conn.fetch.assert_called_once()


# ─── Search param mapping tests ────────────────────────────────────────────────

class TestSearchParams:
    def test_default_values(self):
        params = SearchParams()
        assert params.query is None
        assert params.limit is None
        assert params.offset is None

    def test_order_by_oldest(self):
        params = SearchParams(order_by="oldest")
        assert params.order_by == "oldest"

    def test_obs_type_alias(self):
        # obs_type is an alias for type in the TS code
        params = SearchParams(obs_type="decision")
        assert params.obs_type == "decision"


# ─── metadata_filter pre-condition tests ──────────────────────────────────────

class TestMetadataFilterPreCondition:
    """
    Verify that metadata_filter is passed as a pre-condition parameter to
    hybrid_search() and NOT applied as a post-WHERE clause after the function
    returns its top-60 candidates.
    """

    @pytest.fixture
    def dl(self):
        from open_brain.data_layer.postgres import PostgresDataLayer
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_metadata_filter_passed_to_hybrid_search_not_post_where(self, dl):
        """
        When metadata_filter is provided, the SQL call to hybrid_search() must
        include the filter as a parameter ($6), and the outer WHERE clause must
        NOT contain metadata conditions.
        """
        import json

        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = []
        mock_pool = _make_mock_pool(mock_conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=mock_pool),
            patch("open_brain.data_layer.postgres.embed_query_with_usage", new_callable=AsyncMock) as mock_embed,
            patch("asyncio.create_task"),
        ):
            mock_embed.return_value = ([0.1] * 1024, 10)
            await dl.search(SearchParams(
                query="test query",
                metadata_filter={"source": "claude"},
            ))

        # Verify conn.fetch was called (hybrid search path)
        mock_conn.fetch.assert_called_once()
        call_args = mock_conn.fetch.call_args

        # First argument is the SQL string
        sql: str = call_args[0][0]
        # Positional values passed after the SQL string
        values: tuple = call_args[0][1:]

        # The hybrid_search call must include a 6th argument ($6) for metadata filter
        assert "hybrid_search($1, $2::vector, $3, 60, $4, $5, $6)" in sql, (
            "metadata_filter must be passed as $6 to hybrid_search(), not applied as a post-filter"
        )

        # The metadata JSONB value must appear in the positional values (as the 6th value, index 5)
        expected_jsonb = json.dumps({"source": "claude"})
        assert values[5] == expected_jsonb, (
            f"Expected metadata JSONB '{expected_jsonb}' as the 6th positional value, got: {values[5]!r}"
        )

        # The outer WHERE clause must NOT contain metadata key/value conditions
        assert "m.metadata->>" not in sql, (
            "metadata_filter must not appear as a post-WHERE condition (m.metadata->>...)"
        )

    @pytest.mark.asyncio
    async def test_no_metadata_filter_uses_null_for_hybrid_search(self, dl):
        """
        When no metadata_filter is provided, hybrid_search() is still called with
        NULL as the 6th argument (always pass the parameter for consistency).
        """
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = []
        mock_pool = _make_mock_pool(mock_conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=mock_pool),
            patch("open_brain.data_layer.postgres.embed_query_with_usage", new_callable=AsyncMock) as mock_embed,
            patch("asyncio.create_task"),
        ):
            mock_embed.return_value = ([0.1] * 1024, 10)
            await dl.search(SearchParams(query="test query"))

        mock_conn.fetch.assert_called_once()
        call_args = mock_conn.fetch.call_args
        sql: str = call_args[0][0]
        values: tuple = call_args[0][1:]

        # Must still pass $6 (NULL) for consistency
        assert "hybrid_search($1, $2::vector, $3, 60, $4, $5, $6)" in sql, (
            "hybrid_search() must always receive $6 (NULL when no metadata_filter)"
        )
        # The 6th value (index 5) should be None
        assert values[5] is None, (
            f"Expected None as 6th positional value when no metadata_filter, got: {values[5]!r}"
        )
