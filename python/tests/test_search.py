"""AK 3: Unit tests for hybrid search logic (mocked DB)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from open_brain.data_layer.embedding import to_pg_vector
from open_brain.data_layer.interface import Memory, SearchParams
from open_brain.data_layer.refine import find_obvious_duplicates


def _memory(memory_id: int, priority: float, content: str = "content") -> Memory:
    return Memory(
        id=memory_id,
        index_id=1,
        session_id=None,
        type="observation",
        title=f"Memory {memory_id}",
        subtitle=None,
        narrative=None,
        content=content,
        metadata={},
        priority=priority,
        stability="stable",
        access_count=0,
        last_accessed_at=None,
        created_at="2026-01-01",
        updated_at="2026-01-01",
    )


def _memory_row() -> MagicMock:
    data = {
        "id": 1,
        "index_id": 1,
        "session_id": None,
        "type": "observation",
        "title": "Test",
        "subtitle": None,
        "narrative": None,
        "content": "test content",
        "metadata": {},
        "priority": 0.5,
        "stability": "stable",
        "access_count": 0,
        "last_accessed_at": None,
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
    }
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    row.get = lambda key, default=None: data.get(key, default)
    return row


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

    def test_priority_score_orders_equal_relevance_without_filtering_low_priority(self):
        from open_brain.data_layer.postgres import _order_by_priority_score

        low_priority = _memory(1, 0.1)
        high_priority = _memory(2, 1.0)

        ordered = _order_by_priority_score(
            [(low_priority, 1.0), (high_priority, 1.0)],
            limit=2,
        )

        assert [memory.id for memory in ordered] == [2, 1]

    def test_priority_score_preserves_stronger_relevance_tradeoff(self):
        from open_brain.data_layer.postgres import _order_by_priority_score

        low_priority = _memory(1, 0.1)
        high_priority = _memory(2, 1.0)

        ordered = _order_by_priority_score(
            [(low_priority, 1.0), (high_priority, 0.3)],
            limit=2,
        )

        assert [memory.id for memory in ordered] == [1, 2]

    def test_priority_score_clamps_priority_defensively(self):
        from open_brain.data_layer.postgres import _priority_factor

        assert _priority_factor(-5.0) == 0.35
        assert _priority_factor(5.0) == 1.0


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
            result = await dl.save_memory(SaveMemoryParams(text="test memory", provenance={"producer": "test-suite", "source_ref": "test-suite:test_search"}))

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

        def close_scheduled_coroutine(coroutine):
            coroutine.close()

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=mock_pool),
            patch("asyncio.create_task", side_effect=close_scheduled_coroutine),
        ):
            result = await dl.get_observations([1])

        assert len(result) == 1
        mock_conn.fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_default_retrieval_schedules_usage_tracking(self, dl):
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = [_memory_row()]
        mock_pool = _make_mock_pool(mock_conn)
        scheduled = []

        def capture_task(coroutine):
            scheduled.append(coroutine)
            coroutine.close()

        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=mock_pool,
            ),
            patch("asyncio.create_task", side_effect=capture_task),
        ):
            await dl.get_observations([1])

        assert len(scheduled) == 1

    @pytest.mark.asyncio
    async def test_regression_inspection_does_not_schedule_retrieval_tracking(self, dl):
        """Inspection must not enqueue a late priority or usage-log mutation."""
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = [_memory_row()]
        mock_pool = _make_mock_pool(mock_conn)

        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=mock_pool,
            ),
            patch("asyncio.create_task") as create_task,
        ):
            result = await dl.get_observations([1], track_retrieval=False)

        assert len(result) == 1
        create_task.assert_not_called()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_regression_inspection_preserves_real_database_state(
        self,
        dl,
        bootstrapped_database_url: str,
    ):
        """Inspection must leave the complete memory and usage rows unchanged."""
        from open_brain.config import get_config
        from open_brain.data_layer import postgres as pg_module

        config = get_config()
        original_url = config.DATABASE_URL
        config.DATABASE_URL = bootstrapped_database_url
        await pg_module.close_pool()
        await pg_module.get_pool()
        conn = await asyncpg.connect(bootstrapped_database_url)
        memory_id = await conn.fetchval(
            """
            INSERT INTO memories (
                type, title, content, metadata, priority, stability,
                access_count, last_accessed_at, last_decay_at, updated_at
            )
            VALUES (
                'observation', 'Inspection regression', 'unchanged',
                '{"capture_status":"inbox"}'::jsonb, 0.42, 'stable',
                3, NOW() - INTERVAL '2 days', NOW() - INTERVAL '1 day',
                NOW() - INTERVAL '1 day'
            )
            RETURNING id
            """
        )
        try:
            before_memory = await conn.fetchval(
                "SELECT to_jsonb(m)::text FROM memories m WHERE id = $1",
                memory_id,
            )
            before_usage = await conn.fetchval(
                """
                SELECT COALESCE(
                    jsonb_agg(to_jsonb(u) ORDER BY u.id), '[]'::jsonb
                )::text
                FROM memory_usage_log u
                WHERE u.memory_id = $1
                """,
                memory_id,
            )

            tasks_before = asyncio.all_tasks()
            memories = await dl.get_observations(
                [memory_id], track_retrieval=False
            )
            request_tasks = asyncio.all_tasks() - tasks_before
            if request_tasks:
                await asyncio.gather(*request_tasks)

            after_memory = await conn.fetchval(
                "SELECT to_jsonb(m)::text FROM memories m WHERE id = $1",
                memory_id,
            )
            after_usage = await conn.fetchval(
                """
                SELECT COALESCE(
                    jsonb_agg(to_jsonb(u) ORDER BY u.id), '[]'::jsonb
                )::text
                FROM memory_usage_log u
                WHERE u.memory_id = $1
                """,
                memory_id,
            )

            assert [memory.id for memory in memories] == [memory_id]
            assert after_memory == before_memory
            assert after_usage == before_usage
        finally:
            await conn.execute("DELETE FROM memories WHERE id = $1", memory_id)
            await conn.close()
            await pg_module.close_pool()
            config.DATABASE_URL = original_url


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
        # (and a trailing $7 for capture_status, always passed for consistency).
        assert "hybrid_search($1, $2::vector, $3, 60, $4, $5, $6, $7)" in sql, (
            "metadata_filter must be passed as $6 to hybrid_search(), not applied as a post-filter"
        )

        # The metadata JSONB value must appear as a dict in the positional values (index 5)
        # asyncpg handles JSONB encoding — dicts are passed directly, not pre-serialized
        assert values[5] == {"source": "claude"}, (
            f"Expected metadata dict as the 6th positional value, got: {values[5]!r}"
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

        # Must still pass $6 (NULL) for consistency, plus the trailing $7 for capture_status
        assert "hybrid_search($1, $2::vector, $3, 60, $4, $5, $6, $7)" in sql, (
            "hybrid_search() must always receive $6 (NULL when no metadata_filter)"
        )
        # The 6th value (index 5) should be None
        assert values[5] is None, (
            f"Expected None as 6th positional value when no metadata_filter, got: {values[5]!r}"
        )
        # The 7th value (index 6) should be None when no capture_status is requested
        assert values[6] is None, (
            f"Expected None as 7th positional value when no capture_status, got: {values[6]!r}"
        )


# ─── Retrieval contract compatibility (ranking unchanged) ─────────────────────

class TestSearchRetrievalContractCompatibility:
    """AC2 compatibility: retrieval contract is a server overlay, not a ranker input."""

    @pytest.mark.asyncio
    async def test_search_params_have_no_retrieval_contract_field(self):
        params = SearchParams(query="hello")
        assert not hasattr(params, "retrieval_contract")
