"""Tests that save_memory and update_memory pass Python dicts (not JSON strings) to asyncpg.

asyncpg registers a JSONB codec that handles encoding/decoding automatically.
Passing a pre-serialized JSON string causes double-encoding, storing the
metadata as a JSONB string instead of a JSONB object.

These tests verify that the data layer passes dicts directly.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.interface import SaveMemoryParams, UpdateMemoryParams
from open_brain.data_layer.postgres import PostgresDataLayer


def _make_pool(conn: AsyncMock) -> MagicMock:
    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


class TestSaveMemoryPassesDictToJsonb:
    @pytest.fixture
    def dl(self) -> PostgresDataLayer:
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_save_memory_passes_dict_not_string_to_jsonb(
        self, dl: PostgresDataLayer
    ) -> None:
        """save_memory must pass a dict (not a JSON string) as the metadata argument to asyncpg."""
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 99 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,  # index lookup
            {"id": 1},  # index insert / resolve
            None,  # dedup check: no duplicate
            inserted_row,  # INSERT INTO memories
        ]
        pool = _make_pool(conn)

        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(
                    text="test content",
                    project="proj",
                    metadata={"status": "open", "source": "bot"},
                    provenance={
                        "producer": "test-suite",
                        "source_ref": "test-suite:test_metadata_storage",
                    },
                )
            )

        assert result.id == 99

        # Find the INSERT INTO memories call (last fetchrow call)
        insert_call = conn.fetchrow.call_args_list[-1]
        insert_args = insert_call[0]

        # The metadata argument must be a dict, not a JSON string
        metadata_arg = next(
            (a for a in insert_args if isinstance(a, dict) and "content_hash" in a),
            None,
        )
        assert metadata_arg is not None, (
            "Expected a dict with 'content_hash' key passed to asyncpg, "
            f"but found no such arg. Args were: {[type(a).__name__ for a in insert_args]}"
        )
        assert metadata_arg["status"] == "open"
        assert metadata_arg["source"] == "bot"
        assert "content_hash" in metadata_arg

    @pytest.mark.asyncio
    async def test_save_memory_without_metadata_passes_dict(
        self, dl: PostgresDataLayer
    ) -> None:
        """save_memory without caller metadata still passes a dict (not string) to asyncpg."""
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 10 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,
            {"id": 1},
            None,
            inserted_row,
        ]
        pool = _make_pool(conn)

        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            await dl.save_memory(
                SaveMemoryParams(
                    text="No metadata",
                    project="proj",
                    provenance={
                        "producer": "test-suite",
                        "source_ref": "test-suite:test_metadata_storage",
                    },
                )
            )

        insert_call = conn.fetchrow.call_args_list[-1]
        insert_args = insert_call[0]

        metadata_arg = next(
            (a for a in insert_args if isinstance(a, dict) and "content_hash" in a),
            None,
        )
        assert metadata_arg is not None, (
            "Expected a dict with 'content_hash' passed to asyncpg for metadata, "
            f"but found none. Args: {[type(a).__name__ for a in insert_args]}"
        )
        assert "content_hash" in metadata_arg


class TestUpdateMemoryPassesDictToJsonb:
    @pytest.fixture
    def dl(self) -> PostgresDataLayer:
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_update_memory_passes_dict_not_string_to_jsonb(
        self, dl: PostgresDataLayer
    ) -> None:
        """update_memory must pass a dict (not a JSON string) as the metadata arg to asyncpg."""
        existing_row = MagicMock()
        existing_row_data = {
            "id": 5,
            "content": "existing",
            "title": "title",
            "subtitle": None,
            "narrative": None,
        }
        existing_row.__getitem__ = lambda self, key: existing_row_data[key]

        conn = AsyncMock()
        conn.fetchrow.return_value = existing_row
        pool = _make_pool(conn)

        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
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

        conn.execute.assert_called_once()
        update_args = conn.execute.call_args[0]

        # The metadata argument passed to asyncpg must be a dict, not a string
        metadata_arg = next(
            (a for a in update_args if isinstance(a, dict) and "status" in a),
            None,
        )
        assert metadata_arg is not None, (
            "Expected a dict with 'status' key passed to asyncpg for metadata merge, "
            f"but found none. Args: {[type(a).__name__ for a in update_args]}"
        )
        assert metadata_arg["status"] == "closed"
        assert metadata_arg["reviewer"] == "alice"
