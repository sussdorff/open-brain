"""Tests for capture inbox status handling."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.interface import SaveMemoryParams, SearchParams
from open_brain.data_layer.postgres import PostgresDataLayer


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Build a mock asyncpg pool."""
    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


def _make_row(overrides: dict | None = None) -> MagicMock:
    """Create a mock asyncpg Record."""
    data = {
        "id": 1,
        "index_id": 1,
        "session_id": None,
        "type": "observation",
        "title": "Capture",
        "subtitle": None,
        "narrative": None,
        "content": "captured text",
        "metadata": {"capture_status": "inbox", "status": "open"},
        "priority": 0.5,
        "stability": "stable",
        "access_count": 0,
        "last_accessed_at": None,
        "last_decay_at": None,
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
        "user_id": None,
        "importance": "medium",
    }
    if overrides:
        data.update(overrides)
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    row.get = lambda key, default=None: data.get(key, default)
    return row


def _count_row(total: int) -> MagicMock:
    """Create a mock COUNT row."""
    row = MagicMock()
    row.__getitem__ = lambda self, key: total if key == "total" else None
    return row


class TestCaptureInboxSearch:
    @pytest.fixture
    def dl(self) -> PostgresDataLayer:
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_save_memory_writes_capture_status_on_normal_insert(self, dl: PostgresDataLayer) -> None:
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 42 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,
            inserted_row,
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(
                    text="new capture",
                    metadata={"status": "archived"},
                    capture_status="inbox",
                )
            )

        assert result.id == 42
        insert_args = conn.fetchrow.call_args_list[-1][0]
        metadata_arg = next(
            arg for arg in insert_args if isinstance(arg, dict) and "content_hash" in arg
        )
        assert metadata_arg["capture_status"] == "inbox"
        assert metadata_arg["status"] == "archived"

    @pytest.mark.asyncio
    async def test_save_memory_defaults_normal_capture_to_inbox(self, dl: PostgresDataLayer) -> None:
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 43 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,
            inserted_row,
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            await dl.save_memory(SaveMemoryParams(text="default inbox capture"))

        insert_args = conn.fetchrow.call_args_list[-1][0]
        metadata_arg = next(
            arg for arg in insert_args if isinstance(arg, dict) and "content_hash" in arg
        )
        assert metadata_arg["capture_status"] == "inbox"

    @pytest.mark.asyncio
    async def test_search_capture_status_uses_exact_match_independent_of_lifecycle_status(
        self,
        dl: PostgresDataLayer,
    ) -> None:
        conn = AsyncMock()
        conn.fetch.return_value = [
            _make_row({"metadata": {"capture_status": "inbox", "status": "archived"}})
        ]
        conn.fetchrow.return_value = _count_row(1)
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.search(SearchParams(capture_status="inbox"))

        assert result.total == 1
        assert result.results[0].metadata["status"] == "archived"
        fetch_sql = conn.fetch.call_args[0][0]
        assert "m.metadata->>'capture_status' =" in fetch_sql
        assert "metadata @>" not in fetch_sql
        assert "metadata->>'status'" not in fetch_sql

    @pytest.mark.asyncio
    async def test_search_query_capture_status_prefiltered_inside_hybrid_search(
        self,
        dl: PostgresDataLayer,
    ) -> None:
        # Regression guard (codex adversarial finding): in query mode capture_status must be
        # threaded into hybrid_search() as $7 so it gates candidate selection BEFORE the
        # function's internal `LIMIT match_limit * 2` truncation. Applying it only as an outer
        # post-WHERE filter would drop valid inbox rows that higher-ranked processed/dismissed
        # memories crowd out of the candidate window.
        conn = AsyncMock()
        conn.fetch.return_value = [
            _make_row({"metadata": {"capture_status": "inbox", "status": "archived"}})
        ]
        pool = _make_pool(conn)

        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch(
                "open_brain.data_layer.postgres.embed_query_with_usage",
                new_callable=AsyncMock,
                return_value=([0.1] * 1024, 5),
            ),
            patch(
                "open_brain.data_layer.postgres.get_config",
                return_value=SimpleNamespace(RERANK_ENABLED=False),
            ),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.search(
                SearchParams(query="dentist", capture_status="inbox")
            )

        assert result.total == 1
        fetch_args = conn.fetch.call_args[0]
        fetch_sql = fetch_args[0]
        # capture_status is passed as the 7th positional arg to hybrid_search(), not as an
        # outer WHERE post-filter.
        assert "hybrid_search($1, $2::vector, $3, 60, $4, $5, $6, $7)" in fetch_sql
        assert "m.metadata->>'capture_status' =" not in fetch_sql
        assert "metadata @>" not in fetch_sql
        # $7 (index 7 in the fetch call args, after the SQL string at index 0) is the bound value.
        assert fetch_args[7] == "inbox"

    @pytest.mark.asyncio
    async def test_search_without_capture_status_leaves_legacy_rows_queryable(
        self,
        dl: PostgresDataLayer,
    ) -> None:
        conn = AsyncMock()
        conn.fetch.return_value = [_make_row({"metadata": {"status": "open"}})]
        conn.fetchrow.return_value = _count_row(1)
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.search(SearchParams())

        assert result.total == 1
        assert "capture_status" not in result.results[0].metadata
        fetch_sql = conn.fetch.call_args[0][0]
        assert "capture_status" not in fetch_sql

    @pytest.mark.asyncio
    async def test_save_memory_rejects_unknown_capture_status(self, dl: PostgresDataLayer) -> None:
        with pytest.raises(ValueError, match="Invalid capture_status"):
            await dl.save_memory(
                SaveMemoryParams(text="bad capture", capture_status="pending")
            )


@pytest.mark.integration
class TestCaptureInboxSearchRoundTrip:
    @pytest.mark.asyncio
    async def test_inbox_query_returns_archived_lifecycle_capture_from_real_db(self) -> None:
        import os

        from open_brain.ingest.runs import ingest_run

        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url or database_url.startswith("postgresql://test:test@"):
            pytest.skip("Requires real DATABASE_URL (not the CI test database)")

        dl = PostgresDataLayer()
        run_id: str | None = None
        try:
            with ingest_run() as current_run_id:
                run_id = current_run_id
                inbox = await dl.save_memory(
                    SaveMemoryParams(
                        text=f"capture inbox archived round trip {run_id}",
                        metadata={"status": "archived"},
                        capture_status="inbox",
                    )
                )
                processed = await dl.save_memory(
                    SaveMemoryParams(
                        text=f"capture processed archived round trip {run_id}",
                        metadata={"status": "archived"},
                        capture_status="processed",
                    )
                )

            result = await dl.search(
                SearchParams(
                    capture_status="inbox",
                    metadata_filter={"run_id": run_id},
                    limit=10,
                )
            )

            result_ids = {memory.id for memory in result.results}
            assert inbox.id in result_ids
            assert processed.id not in result_ids

            inbox_memory = next(memory for memory in result.results if memory.id == inbox.id)
            assert inbox_memory.metadata["capture_status"] == "inbox"
            assert inbox_memory.metadata["status"] == "archived"
            assert all(
                memory.metadata.get("capture_status") == "inbox"
                for memory in result.results
            )
        finally:
            if run_id is not None:
                await dl.delete_by_run_id(run_id)


class TestCaptureStatusTransition:
    @pytest.fixture
    def dl(self) -> PostgresDataLayer:
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_set_capture_status_updates_only_capture_status_by_default(
        self,
        dl: PostgresDataLayer,
    ) -> None:
        from open_brain.data_layer.interface import CaptureTransitionParams

        conn = AsyncMock()
        conn.fetchrow.return_value = {"id": 7}
        pool = _make_pool(conn)

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            result = await dl.set_capture_status(
                CaptureTransitionParams(memory_id=7, capture_status="processed")
            )

        assert result.id == 7
        assert result.message == "Capture status updated"
        conn.fetchrow.assert_called_once()
        conn.execute.assert_not_called()
        update_args = conn.fetchrow.call_args[0]
        update_sql = update_args[0]
        assert "jsonb_build_object('capture_status'" in update_sql
        assert "RETURNING id" in update_sql
        assert "'status'" not in update_sql
        assert update_args[1:] == (7, "processed")

    @pytest.mark.asyncio
    async def test_set_capture_status_updates_lifecycle_status_only_when_explicit(
        self,
        dl: PostgresDataLayer,
    ) -> None:
        from open_brain.data_layer.interface import CaptureTransitionParams

        conn = AsyncMock()
        conn.fetchrow.return_value = {"id": 8}
        pool = _make_pool(conn)

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            result = await dl.set_capture_status(
                CaptureTransitionParams(
                    memory_id=8,
                    capture_status="dismissed",
                    lifecycle_status="discarded",
                )
            )

        assert result.id == 8
        conn.fetchrow.assert_called_once()
        conn.execute.assert_not_called()
        update_args = conn.fetchrow.call_args[0]
        update_sql = update_args[0]
        assert "jsonb_build_object('capture_status'" in update_sql
        assert "RETURNING id" in update_sql
        assert "'status'" in update_sql
        assert update_args[1:] == (8, "dismissed", "discarded")

    @pytest.mark.asyncio
    async def test_set_capture_status_rejects_unknown_status_before_db_access(
        self,
        dl: PostgresDataLayer,
    ) -> None:
        from open_brain.data_layer.interface import CaptureTransitionParams

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
        ) as mock_get_pool:
            with pytest.raises(ValueError, match="Invalid capture_status"):
                await dl.set_capture_status(
                    CaptureTransitionParams(memory_id=7, capture_status="pending")
                )

        mock_get_pool.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_capture_status_rejects_unknown_lifecycle_status_before_db_access(
        self,
        dl: PostgresDataLayer,
    ) -> None:
        from open_brain.data_layer.interface import CaptureTransitionParams

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
        ) as mock_get_pool:
            with pytest.raises(ValueError, match="Invalid lifecycle_status"):
                await dl.set_capture_status(
                    CaptureTransitionParams(
                        memory_id=7,
                        capture_status="processed",
                        lifecycle_status="pending",
                    )
                )

        mock_get_pool.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_capture_status_rejects_missing_memory(self, dl: PostgresDataLayer) -> None:
        from open_brain.data_layer.interface import CaptureTransitionParams

        conn = AsyncMock()
        conn.fetchrow.return_value = None
        pool = _make_pool(conn)

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            with pytest.raises(ValueError, match="Memory 404 not found"):
                await dl.set_capture_status(
                    CaptureTransitionParams(memory_id=404, capture_status="processed")
                )

        conn.execute.assert_not_called()
