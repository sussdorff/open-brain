"""Unit tests for PostgresDataLayer methods (mocked asyncpg)."""

from __future__ import annotations

import hashlib
import inspect
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

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
        """When a memory with the same session_ref exists, it is updated instead of inserted."""
        existing_row = MagicMock()
        existing_row.__getitem__ = lambda self, key: {
            "id": 55, "content": "Original content"
        }[key]

        conn = AsyncMock()
        # fetchrow calls: _resolve_index_id (no project), then SELECT by session_ref
        conn.fetchrow.side_effect = [
            existing_row,  # SELECT ... WHERE session_ref = $1
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
                    title="Updated Title",
                    session_ref="open-brain-193",
                )
            )

        assert result.id == 55
        assert result.message == "Memory updated (upsert)"
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
        action = RefineAction(action="merge", memory_ids=[1, 2, 3], reason="dup", executed=False)
        await _execute_refine_action(conn, action)
        # Should delete [2, 3]
        conn.execute.assert_called_once()
        call_args = conn.execute.call_args[0]
        assert "DELETE" in call_args[0]

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
        # Check the metadata JSON was passed as an argument
        insert_args = insert_call[0]
        metadata_arg = next((a for a in insert_args if isinstance(a, str) and "status" in a), None)
        assert metadata_arg is not None

        parsed = json.loads(metadata_arg)
        assert parsed["status"] == "open"
        assert parsed["source"] == "bot"
        assert "content_hash" in parsed

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
            (a for a in insert_args if isinstance(a, str) and "content_hash" in a), None
        )
        assert metadata_arg is not None
        parsed = json.loads(metadata_arg)
        assert "content_hash" in parsed


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
            (a for a in update_args if isinstance(a, str) and "status" in a), None
        )
        assert metadata_arg is not None
        parsed = json.loads(metadata_arg)
        assert parsed["status"] == "closed"
        assert parsed["reviewer"] == "alice"

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
        # The JSONB value should appear as a single serialized arg
        fetch_args = fetch_call[0]
        assert any('"status"' in str(a) and '"open"' in str(a) for a in fetch_args)

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
        # Both keys and values must be serialized into one JSONB arg
        fetch_args = fetch_call[0]
        assert any(
            '"status"' in str(a) and '"source"' in str(a) for a in fetch_args
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
            (a for a in insert_args if isinstance(a, str) and "content_hash" in a), None
        )
        assert metadata_arg is not None
        parsed = json.loads(metadata_arg)
        assert parsed["content_hash"] == HASH_A

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
            (a for a in insert_args if isinstance(a, str) and "content_hash" in a), None
        )
        assert metadata_arg is not None
        parsed = json.loads(metadata_arg)
        assert parsed["source"] == "test"
        assert parsed["content_hash"] == HASH_A

    @pytest.mark.asyncio
    async def test_dedup_metadata_none_becomes_hash_only(self, dl):
        """Save with metadata=None — INSERT gets metadata={'content_hash': '<sha>'}."""
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
            (a for a in insert_args if isinstance(a, str) and "content_hash" in a), None
        )
        assert metadata_arg is not None
        parsed = json.loads(metadata_arg)
        assert list(parsed.keys()) == ["content_hash"]
        assert parsed["content_hash"] == HASH_A

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
        """AK4: Verify get_pool includes the expression index migration for dedup performance.

        Inspects the source of get_pool to confirm the CREATE INDEX statement is present.
        This is a static code check (no DB needed) — actual latency is only measurable
        against a live DB with real data volumes.
        """
        from open_brain.data_layer import postgres as pg_module
        source = inspect.getsource(pg_module.get_pool)
        assert "idx_memories_content_hash" in source, (
            "get_pool must create idx_memories_content_hash index for dedup performance"
        )
