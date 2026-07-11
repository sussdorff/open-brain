"""Tests for capture inbox status handling."""

from contextlib import asynccontextmanager
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
