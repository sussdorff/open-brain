"""Unit tests for PostgresDataLayer methods (mocked asyncpg)."""

from __future__ import annotations

import hashlib
import inspect
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from open_brain.data_layer.interface import (
    Memory,
    RefineParams,
    SaveMemoryParams,
    SearchParams,
    TimelineParams,
    UpdateMemoryParams,
)
from open_brain.data_layer.postgres import PostgresDataLayer, _execute_refine_action, _row_to_memory
from open_brain.data_layer.interface import RefineAction


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Build a properly structured asyncpg pool mock."""
    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


def _make_row(overrides: dict | None = None) -> MagicMock:
    """Create a mock asyncpg Record."""
    data = {
        "id": 1, "index_id": 1, "session_id": None, "type": "observation",
        "title": "Test", "subtitle": None, "narrative": None,
        "content": "test content", "metadata": {}, "priority": 0.5,
        "stability": "stable", "access_count": 0,
        "last_accessed_at": None, "created_at": "2026-01-01",
        "updated_at": "2026-01-01", "importance": "medium",
    }
    if overrides:
        data.update(overrides)
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    row.get = lambda key, default=None: data.get(key, default)
    return row


class TestPostgresPoolMigrations:
    @pytest.mark.asyncio
    async def test_get_pool_skip_migrations_no_writes(self):
        """One-shot callers can acquire a pool without running data mutations."""
        from open_brain.data_layer import postgres as pg_module

        original_pool = pg_module._pool
        pg_module._pool = None
        try:
            conn = AsyncMock()
            conn.execute = AsyncMock(return_value=None)
            pool = _make_pool(conn)

            with patch(
                "open_brain.data_layer.postgres.asyncpg.create_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ):
                result = await pg_module.get_pool(run_migrations=False)

            assert result is pool
            conn.execute.assert_not_called()
        finally:
            pg_module._pool = original_pool

    @pytest.mark.asyncio
    async def test_get_pool_default_runs_migrations_once(self):
        """Default server path applies migrations on first need and skips the second call."""
        from open_brain.data_layer import postgres as pg_module

        original_pool = pg_module._pool
        pg_module._pool = None
        try:
            conn = AsyncMock()
            pool = _make_pool(conn)
            run_migrations = AsyncMock()

            with (
                patch(
                    "open_brain.data_layer.postgres.asyncpg.create_pool",
                    new_callable=AsyncMock,
                    return_value=pool,
                ),
                patch(
                    "open_brain.data_layer.postgres._run_migrations",
                    new=run_migrations,
                ),
            ):
                first = await pg_module.get_pool()
                second = await pg_module.get_pool()

            assert first is pool
            assert second is pool
            run_migrations.assert_awaited_once_with(conn)
        finally:
            pg_module._pool = original_pool

    @pytest.mark.asyncio
    async def test_suppress_migrations_skips_default_get_pool_migrations(self):
        """Process-level suppression lets PostgresDataLayer callers use default get_pool."""
        from open_brain.data_layer import postgres as pg_module

        original_pool = pg_module._pool
        original_migrations_ensured = pg_module._migrations_ensured
        original_migrations_suppressed = pg_module._migrations_suppressed
        pg_module._pool = None
        pg_module._migrations_ensured = False
        pg_module._migrations_suppressed = False
        try:
            conn = AsyncMock()
            conn.execute = AsyncMock(return_value=None)
            pool = _make_pool(conn)

            with patch(
                "open_brain.data_layer.postgres.asyncpg.create_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ):
                pg_module.suppress_migrations()
                result = await pg_module.get_pool()

            assert result is pool
            assert pg_module._migrations_ensured is False
            conn.execute.assert_not_called()
        finally:
            pg_module._pool = original_pool
            pg_module._migrations_ensured = original_migrations_ensured
            pg_module._migrations_suppressed = original_migrations_suppressed

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_pool_default_runs_migrations_once_real_database(
        self, bootstrapped_database_url: str
    ):
        """Real-DB proxy for server semantics: first get_pool migrates, second skips.

        The ``bootstrapped_database_url`` fixture applies
        ``scripts/bootstrap_test_schema.sql`` against the caller-provided
        ``DATABASE_URL`` (and skips cleanly when only the test placeholder URL is
        set), so the migration battery runs against a real, pre-seeded schema.
        """
        from open_brain.config import get_config
        from open_brain.data_layer import postgres as pg_module

        # Wire get_pool()'s connection string to the bootstrapped database.
        get_config().DATABASE_URL = bootstrapped_database_url

        await pg_module.close_pool()
        run_migrations = AsyncMock(wraps=pg_module._run_migrations)
        try:
            with patch(
                "open_brain.data_layer.postgres._run_migrations",
                new=run_migrations,
            ):
                first = await pg_module.get_pool()
                second = await pg_module.get_pool()

            assert first is second
            assert pg_module._migrations_ensured is True
            run_migrations.assert_awaited_once()
        finally:
            await pg_module.close_pool()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_pool_drops_legacy_function_overloads(
        self, bootstrapped_database_url: str
    ):
        """Runtime migrations remove legacy function overloads before recreating."""
        from open_brain.config import get_config
        from open_brain.data_layer import postgres as pg_module

        async def count_public_overloads(
            conn: asyncpg.Connection, function_name: str
        ) -> int:
            return await conn.fetchval(
                """
                SELECT COUNT(*)
                  FROM pg_catalog.pg_proc p
                  JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname = 'public'
                   AND p.proname = $1;
                """,
                function_name,
            )

        conn = await asyncpg.connect(bootstrapped_database_url)
        try:
            await conn.execute("""
                DROP FUNCTION IF EXISTS public.hybrid_search(
                    TEXT, vector, INTEGER, INTEGER, INTEGER
                );
                CREATE OR REPLACE FUNCTION public.hybrid_search(
                    query_text TEXT,
                    query_embedding vector,
                    match_limit INTEGER,
                    rrf_k INTEGER,
                    p_index_id INTEGER
                )
                RETURNS TABLE(
                    id INTEGER,
                    title TEXT,
                    subtitle TEXT,
                    type TEXT,
                    score REAL,
                    created_at TIMESTAMP WITH TIME ZONE
                )
                LANGUAGE sql
                STABLE
                AS $fn$
                    SELECT
                        NULL::INTEGER,
                        NULL::TEXT,
                        NULL::TEXT,
                        NULL::TEXT,
                        NULL::REAL,
                        NULL::TIMESTAMP WITH TIME ZONE
                    WHERE FALSE;
                $fn$;
            """)
            await conn.execute("""
                DROP FUNCTION IF EXISTS public.decay_unused_priorities(INTEGER, REAL);
                CREATE OR REPLACE FUNCTION public.decay_unused_priorities(
                    p_stale_days INTEGER,
                    p_decay_factor REAL
                ) RETURNS INTEGER
                LANGUAGE plpgsql
                AS $fn$
                BEGIN
                    RETURN 0;
                END;
                $fn$;
            """)
        finally:
            await conn.close()

        get_config().DATABASE_URL = bootstrapped_database_url
        await pg_module.close_pool()

        try:
            await pg_module.get_pool()

            conn = await asyncpg.connect(bootstrapped_database_url)
            try:
                zero_vector = f"[{','.join(['0'] * 1024)}]"
                await conn.fetchval(
                    """
                    SELECT COUNT(*)
                      FROM public.hybrid_search($1, $2::vector, $3, $4, $5::integer);
                    """,
                    "test",
                    zero_vector,
                    20,
                    60,
                    None,
                )

                assert await count_public_overloads(conn, "hybrid_search") == 1
                assert await count_public_overloads(conn, "decay_unused_priorities") == 1
            finally:
                await conn.close()
        finally:
            await pg_module.close_pool()


class TestRowToMemory:
    def test_converts_record_to_memory(self):
        row = _make_row()
        memory = _row_to_memory(row)
        assert isinstance(memory, Memory)
        assert memory.id == 1
        assert memory.type == "observation"
        assert memory.content == "test content"
        assert memory.priority == 0.5

    def test_handles_none_optional_fields(self):
        row = _make_row({"title": None, "subtitle": None, "narrative": None})
        memory = _row_to_memory(row)
        assert memory.title is None
        assert memory.subtitle is None
        assert memory.narrative is None

    def test_metadata_defaults_to_empty_dict(self):
        row = _make_row({"metadata": None})
        memory = _row_to_memory(row)
        assert memory.metadata == {}

    def test_metadata_from_json_string(self):
        """Test that JSON string metadata (as returned by asyncpg without codec) is parsed."""
        row = _make_row({"metadata": '{"agent_type": "foo", "status": "open"}'})
        memory = _row_to_memory(row)
        assert memory.metadata == {"agent_type": "foo", "status": "open"}

    def test_metadata_from_dict(self):
        """Test that dict metadata (as returned by asyncpg with JSONB codec) is preserved."""
        row = _make_row({"metadata": {"agent_type": "foo", "status": "open"}})
        memory = _row_to_memory(row)
        assert memory.metadata == {"agent_type": "foo", "status": "open"}


class TestPostgresSaveMemory:
    @pytest.fixture
    def dl(self):
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_save_memory_inserts_with_session_ref(self, dl):
        """Normal insert stores session_ref in the new column."""
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 99 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,          # _resolve_index_id: no existing index
            {"id": 1},     # _resolve_index_id: INSERT new index
            None,          # upsert check: no existing session_summary with this session_ref
            None,          # dedup check: no duplicate content
            inserted_row,  # INSERT INTO memories ... RETURNING id
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(
                    text="Session content",
                    type="session_summary",
                    project="myproj",
                    title="Summary Title",
                    session_ref="open-brain-193",
                )
            )

        assert result.id == 99
        assert result.message == "Memory saved"
        # Verify session_ref was passed in the INSERT call
        insert_call = conn.fetchrow.call_args_list[-1]
        insert_sql = insert_call[0][0]
        assert "session_ref" in insert_sql
        insert_args = insert_call[0]
        assert "open-brain-193" in insert_args

    @pytest.mark.asyncio
    async def test_session_summary_upsert_updates_existing(self, dl):
        """Same-project session_summary rows are updated instead of duplicated."""
        existing_row = MagicMock()
        existing_row.__getitem__ = lambda self, key: {
            "id": 55, "content": "Original content"
        }[key]

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            {"id": 7},     # _resolve_index_id: existing index found
            existing_row,  # scoped session_summary upsert check
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(
                    text="New summary text",
                    type="session_summary",
                    project="myproj",
                    title="Updated Title",
                    session_ref="open-brain-193",
                )
            )

        assert result.id == 55
        assert result.message == "Memory updated (upsert)"
        upsert_sql, session_ref, search_index_id = conn.fetchrow.call_args_list[1][0]
        assert "session_ref = $1" in upsert_sql
        assert "type = 'session_summary'" in upsert_sql
        assert "index_id IS NOT DISTINCT FROM $2" in upsert_sql
        assert session_ref == "open-brain-193"
        assert search_index_id == 7
        # Verify an UPDATE was executed (not an INSERT)
        conn.execute.assert_called_once()
        update_sql = conn.execute.call_args[0][0]
        assert "UPDATE memories" in update_sql
        # Verify merged content contains both old and new text
        update_args = conn.execute.call_args[0]
        merged = update_args[1]  # first positional value after the SQL
        assert "Original content" in merged
        assert "New summary text" in merged

    @pytest.mark.asyncio
    async def test_session_summary_upsert_preserves_non_summary_records_with_colliding_session_ref(
        self, dl
    ):
        """A session_summary save must not mutate non-summary rows sharing session_ref."""
        non_summary_records = [
            {
                "id": 201,
                "type": "learning",
                "title": "Learning title",
                "content": "Learning content",
                "narrative": "Learning narrative",
                "index_id": 7,
            },
            {
                "id": 202,
                "type": "debrief",
                "title": "Debrief title",
                "content": "Debrief content",
                "narrative": "Debrief narrative",
                "index_id": 7,
            },
        ]
        before = [record.copy() for record in non_summary_records]
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 900 if key == "id" else None

        def row_from_record(record: dict[str, object]) -> MagicMock:
            row = MagicMock()
            row.__getitem__ = lambda self, key: record[key]
            return row

        async def fetchrow_side_effect(sql: str, *args: object):
            if "SELECT id FROM memory_indexes" in sql:
                return {"id": 7}
            if "SELECT id, content FROM memories WHERE session_ref" in sql:
                if "type = 'session_summary'" in sql and "index_id IS NOT DISTINCT FROM $2" in sql:
                    return None
                return row_from_record(non_summary_records[0])
            if "metadata->>'content_hash'" in sql:
                return None
            if "INSERT INTO memories" in sql:
                return inserted_row
            raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

        async def execute_side_effect(sql: str, *args: object):
            if not sql.startswith("UPDATE memories SET"):
                return None
            updated_id = args[-1]
            for record in non_summary_records:
                if record["id"] == updated_id:
                    record["content"] = args[0]
                    if "title =" in sql:
                        record["title"] = args[1]
                    if "narrative =" in sql:
                        record["narrative"] = args[-2]

        conn = AsyncMock()
        conn.fetchrow.side_effect = fetchrow_side_effect
        conn.execute.side_effect = execute_side_effect
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(
                    text="New summary text",
                    type="session_summary",
                    project="myproj",
                    title="Summary title",
                    narrative="Summary narrative",
                    session_ref="shared-session",
                )
            )

        assert non_summary_records == before
        assert result.id == 900
        assert result.message == "Memory saved"
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_summary_replace_scopes_lookup_and_delete_by_project(self, dl):
        """Replace-mode session_summary lookup and delete stay inside the project scope."""
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 901 if key == "id" else None

        @asynccontextmanager
        async def fake_transaction():
            yield

        conn = AsyncMock()
        conn.transaction = fake_transaction
        conn.fetchrow.side_effect = [
            {"id": 7},     # _resolve_index_id: existing index found
            inserted_row,  # INSERT INTO memories
        ]
        conn.fetch.return_value = [{"id": 55}]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(
                    text="Replacement summary",
                    type="session_summary",
                    project="myproj",
                    title="Replacement title",
                    session_ref="open-brain-193",
                    upsert_mode="replace",
                )
            )

        assert result.id == 901
        existing_sql, session_ref, search_index_id = conn.fetch.call_args[0]
        assert "session_ref = $1" in existing_sql
        assert "type = 'session_summary'" in existing_sql
        assert "index_id IS NOT DISTINCT FROM $2" in existing_sql
        assert session_ref == "open-brain-193"
        assert search_index_id == 7

        delete_memories_call = [
            call
            for call in conn.execute.call_args_list
            if "DELETE FROM memories WHERE" in call[0][0]
        ][0]
        delete_sql, delete_session_ref, delete_index_id = delete_memories_call[0]
        assert "session_ref = $1" in delete_sql
        assert "type = 'session_summary'" in delete_sql
        assert "index_id IS NOT DISTINCT FROM $2" in delete_sql
        assert delete_session_ref == "open-brain-193"
        assert delete_index_id == 7

    @pytest.mark.asyncio
    async def test_non_session_summary_skips_upsert(self, dl):
        """For types other than session_summary, no upsert check is made."""
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 77 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,          # _resolve_index_id: no existing index
            {"id": 1},     # _resolve_index_id: INSERT
            None,          # dedup check: no duplicate content
            inserted_row,  # INSERT INTO memories
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(
                    text="Regular memory",
                    type="discovery",
                    project="myproj",
                    session_ref="open-brain-193",  # session_ref provided but type != session_summary
                )
            )

        assert result.message == "Memory saved"
        # No UPDATE should have been called
        conn.execute.assert_not_called()


class TestPostgresTimeline:
    @pytest.fixture
    def dl(self):
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_timeline_no_anchor_no_query_returns_empty(self, dl):
        conn = AsyncMock()
        conn.fetchrow.return_value = None  # no anchor found
        pool = _make_pool(conn)

        with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await dl.timeline(TimelineParams())

        assert result.results == []
        assert result.anchor_id is None

    @pytest.mark.asyncio
    async def test_timeline_with_anchor_id_fetches_context(self, dl):
        conn = AsyncMock()
        conn.fetchrow.return_value = {"created_at": "2026-01-01T12:00:00", "session_id": 1}
        conn.fetch.return_value = [_make_row()]
        pool = _make_pool(conn)

        with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await dl.timeline(TimelineParams(anchor=42))

        assert result.anchor_id == 42
        assert len(result.results) == 1

    @pytest.mark.asyncio
    async def test_timeline_with_query_finds_anchor(self, dl):
        conn = AsyncMock()
        # First fetchrow: resolve_index_id (project=None, skip)
        # Second fetchrow: FTS anchor search -> returns anchor row
        # Third fetchrow: anchor created_at
        conn.fetchrow.side_effect = [
            {"id": 5},  # FTS result -> anchor_id
            {"created_at": "2026-01-02", "session_id": None},  # anchor data
        ]
        conn.fetch.return_value = []
        pool = _make_pool(conn)

        with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await dl.timeline(TimelineParams(query="search query"))

        assert result.anchor_id == 5

    @pytest.mark.asyncio
    async def test_timeline_anchor_not_found_returns_empty(self, dl):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            {"id": 99},  # FTS finds anchor
            None,  # but anchor row doesn't exist
        ]
        conn.fetch.return_value = []
        pool = _make_pool(conn)

        with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await dl.timeline(TimelineParams(query="test"))

        assert result.results == []
        assert result.anchor_id is None


class TestPostgresSearchByConcept:
    @pytest.fixture
    def dl(self):
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_search_by_concept_calls_embed_query(self, dl):
        conn = AsyncMock()
        conn.fetchrow.return_value = None  # no index
        conn.fetch.return_value = [_make_row()]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.embed_query_with_usage", new_callable=AsyncMock) as mock_embed,
            patch("open_brain.data_layer.postgres.rerank", new_callable=AsyncMock, return_value=[0]),
            patch("asyncio.create_task"),
        ):
            mock_embed.return_value = ([0.1] * 1024, 10)
            result = await dl.search_by_concept("test concept")

        mock_embed.assert_called_once_with("test concept")
        assert "results" in result
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_search_by_concept_with_limit(self, dl):
        conn = AsyncMock()
        conn.fetchrow.return_value = None
        conn.fetch.return_value = []
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.embed_query_with_usage", new_callable=AsyncMock, return_value=([0.1] * 1024, 10)),
            patch("asyncio.create_task"),
        ):
            result = await dl.search_by_concept("query", limit=5)

        # Verify limit=5 was used in the query (via fetch args)
        assert "results" in result


class TestPostgresIngestStatus:
    @pytest.fixture
    def dl(self):
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_ingest_status_by_source_refs_returns_ordered_statuses(self, dl):
        row = MagicMock()
        row.__getitem__ = lambda self, key: {
            "source_ref": "macwhisper:session:abc123",
            "memory_id": 42,
            "run_id": "run-123",
            "ingested_at": "2026-04-30T12:00:00",
            "title": "Meeting: macwhisper:session:abc123",
        }[key]

        conn = AsyncMock()
        conn.fetch.return_value = [row]
        pool = _make_pool(conn)

        with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await dl.ingest_status_by_source_refs([
                "macwhisper:session:abc123",
                "macwhisper:session:new",
                "macwhisper:session:abc123",
            ])

        conn.fetch.assert_awaited_once()
        assert conn.fetch.call_args[0][1] == [
            "macwhisper:session:abc123",
            "macwhisper:session:new",
        ]
        assert result["macwhisper:session:abc123"]["ingested"] is True
        assert result["macwhisper:session:abc123"]["memory_id"] == 42
        assert result["macwhisper:session:new"]["ingested"] is False
        assert result["macwhisper:session:new"]["memory_id"] is None

    @pytest.mark.asyncio
    async def test_ingest_status_by_source_refs_empty_skips_db(self, dl):
        result = await dl.ingest_status_by_source_refs(["", "   "])

        assert result == {}

    @pytest.mark.asyncio
    async def test_memory_ids_by_content_hashes_matches_save_dedup_scope(self, dl):
        row = MagicMock()
        row.__getitem__ = lambda self, key: {
            "content_hash": "a" * 64,
            "memory_id": 42,
        }[key]
        conn = AsyncMock()
        conn.fetch.return_value = [row]
        pool = _make_pool(conn)

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            result = await dl.memory_ids_by_content_hashes([
                "a" * 64,
                "b" * 64,
                "a" * 64,
                "",
            ])

        assert result == {"a" * 64: 42}
        query, hashes, index_id, window_days = conn.fetch.call_args[0]
        assert "metadata->>'content_hash' = ANY($1::text[])" in query
        assert "created_at > NOW() - ($3 * INTERVAL '1 day')" in query
        assert hashes == ["a" * 64, "b" * 64]
        assert index_id == 1
        assert window_days == 30

    @pytest.mark.asyncio
    async def test_memory_ids_by_content_hashes_empty_skips_db(self, dl):
        result = await dl.memory_ids_by_content_hashes(["", "   "])

        assert result == {}


class TestPostgresGetContext:
    @pytest.fixture
    def dl(self):
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_get_context_returns_sessions(self, dl):
        session_row = MagicMock()
        session_row_data = {
            "id": 1, "session_id": "abc", "project": "myproject",
            "started_at": "2026-01-01", "ended_at": None,
            "metadata": {}, "summaries": None,
        }
        session_row.__iter__ = lambda self: iter(session_row_data.items())
        session_row.keys = lambda: session_row_data.keys()

        conn = AsyncMock()
        conn.fetchrow.return_value = None  # no index
        conn.fetch.return_value = [session_row]
        pool = _make_pool(conn)

        with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
            # Mock the dict() call on the row
            with patch("open_brain.data_layer.postgres.dict", side_effect=lambda r: dict(r)):
                result = await dl.get_context(limit=5)

        assert "sessions" in result


class TestPostgresRefineMemories:
    @pytest.fixture
    def dl(self):
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_refine_recent_scope(self, dl):
        memory_row = _make_row()
        conn = AsyncMock()
        conn.fetch.return_value = [memory_row]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.analyze_with_llm", new_callable=AsyncMock) as mock_llm,
        ):
            mock_llm.return_value = []
            result = await dl.refine_memories(RefineParams(scope="recent"))

        assert result.analyzed == 1
        assert result.actions == []

    @pytest.mark.asyncio
    async def test_refine_empty_candidates(self, dl):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)

        with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await dl.refine_memories(RefineParams())

        assert result.analyzed == 0
        assert result.summary == "No candidates found"

    @pytest.mark.asyncio
    async def test_refine_dry_run_does_not_execute(self, dl):
        memory_row = _make_row()
        conn = AsyncMock()
        conn.fetch.return_value = [memory_row]
        pool = _make_pool(conn)

        merge_action = RefineAction(action="merge", memory_ids=[1, 2], reason="dup", executed=False)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.analyze_with_llm", new_callable=AsyncMock, return_value=[merge_action]),
        ):
            result = await dl.refine_memories(RefineParams(dry_run=True))

        assert result.actions[0].executed is False
        assert "dry run" in result.summary

    @pytest.mark.asyncio
    async def test_refine_low_priority_scope(self, dl):
        conn = AsyncMock()
        conn.fetch.return_value = [_make_row()]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.analyze_with_llm", new_callable=AsyncMock, return_value=[]),
        ):
            result = await dl.refine_memories(RefineParams(scope="low-priority"))

        assert result.analyzed == 1

    @pytest.mark.asyncio
    async def test_refine_duplicates_scope(self, dl):
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)

        with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await dl.refine_memories(RefineParams(scope="duplicates"))

        assert result.analyzed == 0

    @pytest.mark.asyncio
    async def test_refine_project_scope(self, dl):
        conn = AsyncMock()
        # resolve_index_id call + fetch
        conn.fetchrow.return_value = {"id": 1}
        conn.fetch.return_value = []
        pool = _make_pool(conn)

        with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await dl.refine_memories(RefineParams(scope="project:myproject"))

        assert result.analyzed == 0


class TestExecuteRefineAction:
    @pytest.mark.asyncio
    async def test_merge_deletes_all_but_first(self):
        conn = AsyncMock()
        # 1st fetch: mutation-site protected check -> none protected.
        # 2nd fetch: pre-repoint re-check -> ids 2 and 3 are still safe.
        conn.fetch = AsyncMock(
            side_effect=[[], [_make_row({"id": 2}), _make_row({"id": 3})]]
        )
        action = RefineAction(
            action="merge",
            memory_ids=[1, 2, 3],
            reason="dup",
            executed=False,
            skip_llm_merge=True,
        )
        skipped = await _execute_refine_action(conn, action)

        assert skipped == 0
        sql_calls = [call.args[0] for call in conn.execute.call_args_list]
        assert any("UPDATE memory_relationships" in sql for sql in sql_calls)
        assert "DELETE FROM memories" in sql_calls[-1]
        assert conn.execute.call_args_list[-1].args[1] == [2, 3]

    @pytest.mark.asyncio
    async def test_merge_single_id_no_delete(self):
        conn = AsyncMock()
        action = RefineAction(action="merge", memory_ids=[1], reason="dup", executed=False)
        await _execute_refine_action(conn, action)
        # No delete needed
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_promote_updates_stability(self):
        conn = AsyncMock()
        action = RefineAction(action="promote", memory_ids=[5, 6], reason="high quality", executed=False)
        await _execute_refine_action(conn, action)
        # Called once per ID
        assert conn.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_demote_updates_priority(self):
        conn = AsyncMock()
        action = RefineAction(action="demote", memory_ids=[3, 4], reason="low quality", executed=False)
        await _execute_refine_action(conn, action)
        conn.execute.assert_called_once()
        call_args = conn.execute.call_args[0]
        assert "priority" in call_args[0]

    @pytest.mark.asyncio
    async def test_delete_removes_memories(self):
        conn = AsyncMock()
        action = RefineAction(action="delete", memory_ids=[7, 8], reason="obsolete", executed=False)
        await _execute_refine_action(conn, action)
        conn.execute.assert_called_once()
        call_args = conn.execute.call_args[0]
        assert "DELETE" in call_args[0]


class TestSaveMemoryWithMetadata:
    @pytest.fixture
    def dl(self):
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_save_memory_with_metadata(self, dl):
        """AK1: save_memory(metadata={...}) persists JSON in DB."""
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 42 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,          # _resolve_index_id: no existing index
            {"id": 1},     # _resolve_index_id: INSERT new index
            None,          # dedup check: no duplicate content
            inserted_row,  # INSERT INTO memories ... RETURNING id
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(
                    text="Memory with metadata",
                    type="discovery",
                    project="test-project",
                    metadata={"status": "open", "source": "bot"},
                )
            )

        assert result.id == 42
        assert result.message == "Memory saved"
        # Verify metadata was passed in the INSERT call
        insert_call = conn.fetchrow.call_args_list[-1]
        insert_sql = insert_call[0][0]
        assert "metadata" in insert_sql
        # Check the metadata dict was passed as an argument (asyncpg handles JSONB encoding)
        insert_args = insert_call[0]
        metadata_arg = next((a for a in insert_args if isinstance(a, dict) and "status" in a), None)
        assert metadata_arg is not None

        assert metadata_arg["status"] == "open"
        assert metadata_arg["source"] == "bot"
        assert "content_hash" in metadata_arg

    @pytest.mark.asyncio
    async def test_save_memory_without_metadata_defaults_to_empty(self, dl):
        """save_memory without metadata sends '{}' for the metadata column."""
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 10 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,
            {"id": 1},
            None,          # dedup check: no duplicate content
            inserted_row,
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(SaveMemoryParams(text="No metadata", project="proj"))

        assert result.id == 10
        insert_call = conn.fetchrow.call_args_list[-1]
        insert_args = insert_call[0]
        # Metadata should now always contain content_hash (never bare '{}')

        metadata_arg = next(
            (a for a in insert_args if isinstance(a, dict) and "content_hash" in a), None
        )
        assert metadata_arg is not None
        assert "content_hash" in metadata_arg


class TestUpdateMemoryMetadataMerge:
    @pytest.fixture
    def dl(self):
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_update_memory_metadata_merge(self, dl):
        """AK2: update_memory(metadata={...}) merges JSONB (uses metadata || $n::jsonb)."""
        existing_row = MagicMock()
        existing_row_data = {
            "id": 5,
            "content": "existing content",
            "title": "existing title",
            "subtitle": None,
            "narrative": None,
        }
        existing_row.__getitem__ = lambda self, key: existing_row_data[key]

        conn = AsyncMock()
        conn.fetchrow.return_value = existing_row
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.update_memory(
                UpdateMemoryParams(
                    id=5,
                    metadata={"status": "closed", "reviewer": "alice"},
                )
            )

        assert result.id == 5
        assert result.message == "Memory updated"
        # Verify UPDATE was called with JSONB merge syntax
        conn.execute.assert_called_once()
        update_sql = conn.execute.call_args[0][0]
        assert "metadata || " in update_sql
        assert "::jsonb" in update_sql
        # Verify the metadata JSON was passed
        update_args = conn.execute.call_args[0]

        metadata_arg = next(
            (a for a in update_args if isinstance(a, dict) and "status" in a), None
        )
        assert metadata_arg is not None
        assert metadata_arg["status"] == "closed"
        assert metadata_arg["reviewer"] == "alice"

    @pytest.mark.asyncio
    async def test_update_memory_metadata_only_no_other_updates(self, dl):
        """update_memory with only metadata (no text/title/etc.) still triggers an UPDATE."""
        existing_row = MagicMock()
        existing_row_data = {
            "id": 7, "content": "c", "title": None, "subtitle": None, "narrative": None,
        }
        existing_row.__getitem__ = lambda self, key: existing_row_data[key]

        conn = AsyncMock()
        conn.fetchrow.return_value = existing_row
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.update_memory(UpdateMemoryParams(id=7, metadata={"key": "value"}))

        # Should NOT return "No fields to update"
        assert result.message == "Memory updated"
        conn.execute.assert_called_once()


class TestSearchMetadataFilter:
    @pytest.fixture
    def dl(self):
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_search_metadata_filter_in_browse_mode(self, dl):
        """AK3: search(metadata_filter={'status': 'open'}) adds JSONB condition to WHERE clause."""
        count_row = MagicMock()
        count_row.__getitem__ = lambda self, key: 1 if key == "total" else None

        conn = AsyncMock()
        conn.fetchrow.return_value = count_row  # COUNT query (no project, no _resolve_index_id call)
        conn.fetch.return_value = [_make_row()]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.search(
                SearchParams(metadata_filter={"status": "open"})
            )

        assert len(result.results) == 1
        # Verify the fetch call used @> containment (not per-key ->> text equality)
        fetch_call = conn.fetch.call_args
        fetch_sql = fetch_call[0][0]
        assert "metadata @>" in fetch_sql
        assert "metadata->>" not in fetch_sql
        # The JSONB value should appear as a single dict arg (asyncpg handles encoding)
        fetch_args = fetch_call[0]
        assert any(isinstance(a, dict) and a.get("status") == "open" for a in fetch_args)

    @pytest.mark.asyncio
    async def test_search_metadata_filter_multiple_keys(self, dl):
        """search with multiple metadata_filter keys generates one condition per key."""
        count_row = MagicMock()
        count_row.__getitem__ = lambda self, key: 0 if key == "total" else None

        conn = AsyncMock()
        conn.fetchrow.return_value = count_row
        conn.fetch.return_value = []
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.search(
                SearchParams(metadata_filter={"status": "open", "source": "bot"})
            )

        fetch_call = conn.fetch.call_args
        fetch_sql = fetch_call[0][0]
        # Single @> containment condition for all keys (not one ->> per key)
        assert fetch_sql.count("metadata @>") == 1
        assert "metadata->>" not in fetch_sql
        # Both keys and values must appear in one dict arg (asyncpg handles encoding)
        fetch_args = fetch_call[0]
        assert any(
            isinstance(a, dict) and "status" in a and "source" in a for a in fetch_args
        )


HASH_A = hashlib.sha256("Python prefers explicit over implicit".encode()).hexdigest()


class TestContentHashDedup:
    """Content-hash dedup tests for save_memory."""

    @pytest.fixture
    def dl(self):
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_dedup_identical_returns_duplicate_of(self, dl):
        """Saving identical text returns duplicate_of with original ID; no INSERT called."""
        dup_row = MagicMock()
        dup_row.__getitem__ = lambda self, key: 42 if key == "id" else None

        conn = AsyncMock()
        # No project → _resolve_index_id skipped; next call is dedup check → dup found
        conn.fetchrow.side_effect = [
            dup_row,  # dedup check: existing row found
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(text="Python prefers explicit over implicit", type="observation")
            )

        assert result.id == 42
        assert result.duplicate_of == 42
        assert "Duplicate" in result.message
        # INSERT should NOT have been called
        assert conn.fetchrow.call_count == 1  # only dedup check

    @pytest.mark.asyncio
    async def test_dedup_different_text_inserts(self, dl):
        """When dedup check returns None, INSERT proceeds normally."""
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 99 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,          # dedup check: no dup
            inserted_row,  # INSERT INTO memories ... RETURNING id
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(text="Python prefers simple over complex", type="observation")
            )

        assert result.id == 99
        assert result.duplicate_of is None
        assert result.message == "Memory saved"

    @pytest.mark.asyncio
    async def test_dedup_hash_stored_in_metadata(self, dl):
        """INSERT receives metadata JSON containing 'content_hash' key with correct SHA-256."""
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 7 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,          # dedup check: no dup
            inserted_row,  # INSERT
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            await dl.save_memory(
                SaveMemoryParams(text="Python prefers explicit over implicit", type="observation")
            )


        insert_call = conn.fetchrow.call_args_list[-1]
        insert_args = insert_call[0]
        metadata_arg = next(
            (a for a in insert_args if isinstance(a, dict) and "content_hash" in a), None
        )
        assert metadata_arg is not None
        assert metadata_arg["content_hash"] == HASH_A

    @pytest.mark.asyncio
    async def test_dedup_metadata_merged_not_replaced(self, dl):
        """Save with metadata={'source': 'test'} — INSERT gets metadata with both keys."""
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 8 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,          # dedup check: no dup
            inserted_row,  # INSERT
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            await dl.save_memory(
                SaveMemoryParams(
                    text="Python prefers explicit over implicit",
                    type="observation",
                    metadata={"source": "test"},
                )
            )


        insert_call = conn.fetchrow.call_args_list[-1]
        insert_args = insert_call[0]
        metadata_arg = next(
            (a for a in insert_args if isinstance(a, dict) and "content_hash" in a), None
        )
        assert metadata_arg is not None
        assert metadata_arg["source"] == "test"
        assert metadata_arg["content_hash"] == HASH_A

    @pytest.mark.asyncio
    async def test_dedup_metadata_none_gets_hash_and_default_capture_status(self, dl):
        """Save with metadata=None inserts content_hash and default capture_status."""
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 9 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,          # dedup check: no dup
            inserted_row,  # INSERT
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            await dl.save_memory(
                SaveMemoryParams(text="Python prefers explicit over implicit", type="observation")
            )


        insert_call = conn.fetchrow.call_args_list[-1]
        insert_args = insert_call[0]
        metadata_arg = next(
            (a for a in insert_args if isinstance(a, dict) and "content_hash" in a), None
        )
        assert metadata_arg is not None
        assert metadata_arg["content_hash"] == HASH_A
        assert metadata_arg["capture_status"] == "inbox"

    @pytest.mark.asyncio
    async def test_dedup_session_summary_upsert_bypasses_dedup(self, dl):
        """session_summary + existing session_ref returns upsert result; dedup never queried."""
        existing_row = MagicMock()
        existing_row.__getitem__ = lambda self, key: {"id": 55, "content": "Original"}[key]

        conn = AsyncMock()
        # No project → _resolve_index_id skipped; then session_summary upsert check
        conn.fetchrow.side_effect = [
            existing_row,  # upsert check: existing row found → upsert, early return
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(
                    text="New content",
                    type="session_summary",
                    session_ref="open-brain-42",
                )
            )

        assert result.id == 55
        assert result.message == "Memory updated (upsert)"
        assert result.duplicate_of is None
        # Only 1 fetchrow call: the session_summary check (dedup never reached)
        assert conn.fetchrow.call_count == 1

    @pytest.mark.asyncio
    async def test_dedup_scoped_to_index_id(self, dl):
        """Same content under different index_ids both insert (dedup uses index_id scoping)."""
        inserted_row_1 = MagicMock()
        inserted_row_1.__getitem__ = lambda self, key: 10 if key == "id" else None
        inserted_row_2 = MagicMock()
        inserted_row_2.__getitem__ = lambda self, key: 11 if key == "id" else None

        # First save: project "proj-a" → _resolve_index_id → returns index 1
        conn1 = AsyncMock()
        conn1.fetchrow.side_effect = [
            {"id": 1},     # _resolve_index_id: existing index found
            None,          # dedup check: no dup for index_id=1
            inserted_row_1,  # INSERT
        ]
        pool1 = _make_pool(conn1)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool1),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result1 = await dl.save_memory(
                SaveMemoryParams(
                    text="Python prefers explicit over implicit",
                    type="observation",
                    project="proj-a",
                )
            )

        # Second save: project "proj-b" → _resolve_index_id → returns index 2
        conn2 = AsyncMock()
        conn2.fetchrow.side_effect = [
            {"id": 2},     # _resolve_index_id: different index
            None,          # dedup check: no dup for index_id=2
            inserted_row_2,  # INSERT
        ]
        pool2 = _make_pool(conn2)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool2),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result2 = await dl.save_memory(
                SaveMemoryParams(
                    text="Python prefers explicit over implicit",
                    type="observation",
                    project="proj-b",
                )
            )

        assert result1.id == 10
        assert result1.duplicate_of is None
        assert result2.id == 11
        assert result2.duplicate_of is None

    @pytest.mark.asyncio
    async def test_dedup_whitespace_is_significant(self, dl):
        """Scenario v3: Trailing whitespace creates a different hash — no dedup."""
        text_a = "Python prefers explicit over implicit"
        text_b = "Python prefers explicit over implicit "  # trailing space
        # These should have different hashes (whitespace is significant, no normalization applied)
        assert hashlib.sha256(text_a.encode()).hexdigest() != hashlib.sha256(text_b.encode()).hexdigest()

        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 21 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,         # dedup check: no duplicate found (different hash from text_a)
            inserted_row, # INSERT INTO memories
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(text=text_b)  # text with trailing space
            )

        assert result.duplicate_of is None
        assert result.message == "Memory saved"

    @pytest.mark.asyncio
    async def test_dedup_aged_out_duplicate_inserts_new(self, dl):
        """Scenario v7: When dedup query returns None (content older than 30 days), INSERT proceeds."""
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 22 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,         # dedup check returns None — simulates 30-day window expired
            inserted_row, # INSERT INTO memories
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(text="Python prefers explicit over implicit")
            )

        assert result.duplicate_of is None
        assert result.id == 22
        assert result.message == "Memory saved"

    @pytest.mark.asyncio
    async def test_dedup_identical_different_metadata(self, dl):
        """Scenario 2: Duplicate detected even when metadata differs — hash is content-only."""
        dup_row = MagicMock()
        dup_row.__getitem__ = lambda self, key: 100 if key == "id" else None

        conn = AsyncMock()
        # No project → _resolve_index_id skipped; dedup check finds existing row
        conn.fetchrow.side_effect = [
            dup_row,  # dedup: content hash matches regardless of metadata
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(
                    text="Python prefers explicit over implicit",
                    type="observation",
                    metadata={"source": "mcp"},  # different metadata from original save
                )
            )

        assert result.duplicate_of == 100
        assert "Duplicate" in result.message
        # No INSERT — dedup fired
        assert conn.fetchrow.call_count == 1

    @pytest.mark.asyncio
    async def test_dedup_session_ref_observation_still_deduped(self, dl):
        """Scenario 6: Non-session_summary with session_ref is still subject to content dedup.

        First save: type=observation, session_ref=Y, text A → inserts (dedup returns None).
        Second save: type=observation, no session_ref, text A → returns duplicate_of.
        session_ref bypass only applies to type=session_summary.
        """
        dup_row = MagicMock()
        dup_row.__getitem__ = lambda self, key: 50 if key == "id" else None

        conn = AsyncMock()
        # Simulates the second save: dedup query finds the first save's row
        conn.fetchrow.side_effect = [
            dup_row,  # dedup check: existing row found (from first save with session_ref)
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(
                    text="Python prefers explicit over implicit",
                    type="observation",
                    # No session_ref — still deduped by content hash
                )
            )

        assert result.duplicate_of == 50
        assert "Duplicate" in result.message


class TestContentHashDedupIndex:
    """Verify the migration SQL includes the content_hash index (AK4 MoC: integ)."""

    @pytest.fixture
    def dl(self):
        return PostgresDataLayer()

    def test_dedup_index_migration_sql_present(self, dl):
        """AK4: Verify migrations include the expression index for dedup performance.

        Inspects the migration helper to confirm the CREATE INDEX statement is present.
        This is a static code check (no DB needed) — actual latency is only measurable
        against a live DB with real data volumes.
        """
        from open_brain.data_layer import postgres as pg_module
        source = inspect.getsource(pg_module._run_migrations)
        assert "idx_memories_content_hash" in source, (
            "_run_migrations must create idx_memories_content_hash index for dedup performance"
        )
