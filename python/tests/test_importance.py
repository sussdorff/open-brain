"""Tests for importance contract — AK8–AK10, V4–V7."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.interface import (
    IMPORTANCE_VALUES,
    Memory,
    SaveMemoryParams,
    rank_importance,
)
from open_brain.data_layer.postgres import PostgresDataLayer, _row_to_memory


# ─── Helpers shared across tests ──────────────────────────────────────────────


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Build a properly structured asyncpg pool mock."""
    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


def _make_row(overrides: dict | None = None) -> MagicMock:
    """Create a mock asyncpg Record with importance field."""
    data = {
        "id": 1, "index_id": 1, "session_id": None, "type": "observation",
        "title": "Test", "subtitle": None, "narrative": None,
        "content": "test content", "metadata": {}, "priority": 0.5,
        "stability": "stable", "access_count": 0,
        "last_accessed_at": None, "created_at": "2026-01-01",
        "updated_at": "2026-01-01", "user_id": None,
        "importance": "medium",
    }
    if overrides:
        data.update(overrides)
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    row.get = lambda key, default=None: data.get(key, default)
    return row


# ─── AK10 + V4 ────────────────────────────────────────────────────────────────


class TestRankImportance:
    """AK10: rank_importance returns exactly critical=3, high=2, medium=1, low=0.
    V4: Unknown input raises ValueError.
    """

    def test_critical_returns_3(self):
        assert rank_importance("critical") == 3

    def test_high_returns_2(self):
        assert rank_importance("high") == 2

    def test_medium_returns_1(self):
        assert rank_importance("medium") == 1

    def test_low_returns_0(self):
        assert rank_importance("low") == 0

    def test_unknown_raises_value_error(self):
        """V4: unknown string raises ValueError, does NOT silently map to 0."""
        with pytest.raises(ValueError):
            rank_importance("urgent")

    def test_empty_string_raises_value_error(self):
        """V4: empty string raises ValueError."""
        with pytest.raises(ValueError):
            rank_importance("")

    def test_none_raises_value_error(self):
        """V4: None raises ValueError (type is str, but guard must handle)."""
        with pytest.raises((ValueError, TypeError)):
            rank_importance(None)  # type: ignore[arg-type]

    def test_mixed_case_raises_value_error(self):
        """V4: 'Medium' (wrong case) raises ValueError — values are case-sensitive."""
        with pytest.raises(ValueError):
            rank_importance("Medium")


# ─── AK8: importance round-trip ───────────────────────────────────────────────


class TestSaveMemoryImportance:
    """AK8: importance round-trips through save_memory and recall."""

    @pytest.fixture
    def dl(self):
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_importance_high_roundtrip(self, dl):
        """save_memory with importance='high' → _row_to_memory returns importance='high'."""
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 42 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,          # dedup check
            inserted_row,  # INSERT RETURNING id
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(text="important memory", importance="high")
            )

        assert result.id == 42
        assert result.duplicate_of is None

        # Verify importance='high' was passed to the INSERT
        insert_call = conn.fetchrow.call_args_list[-1]
        insert_args = insert_call[0]
        assert "high" in insert_args, "importance value 'high' must appear in INSERT args"

    @pytest.mark.asyncio
    async def test_importance_default_is_medium(self, dl):
        """save_memory without importance defaults to 'medium'."""
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 7 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,          # dedup check
            inserted_row,  # INSERT
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            await dl.save_memory(SaveMemoryParams(text="default importance"))

        insert_call = conn.fetchrow.call_args_list[-1]
        insert_args = insert_call[0]
        assert "medium" in insert_args

    def test_row_to_memory_reads_importance(self):
        """_row_to_memory correctly maps importance from DB row."""
        row = _make_row({"importance": "critical"})
        memory = _row_to_memory(row)
        assert memory.importance == "critical"

    def test_row_to_memory_defaults_missing_importance_to_medium(self):
        """AK9: _row_to_memory defaults missing importance column to 'medium'."""
        # Row without importance key at all (pre-migration row)
        data = {
            "id": 1, "index_id": 1, "session_id": None, "type": "observation",
            "title": "Test", "subtitle": None, "narrative": None,
            "content": "old row", "metadata": {}, "priority": 0.5,
            "stability": "stable", "access_count": 0,
            "last_accessed_at": None, "created_at": "2026-01-01",
            "updated_at": "2026-01-01", "user_id": None,
            # NO "importance" key
        }
        row = MagicMock()
        row.__getitem__ = lambda self, key: data[key]
        row.get = lambda key, default=None: data.get(key, default)
        memory = _row_to_memory(row)
        assert memory.importance == "medium"


# ─── AK9: backfill migration ──────────────────────────────────────────────────


class TestImportanceMigration:
    """AK9: backfill migration leaves existing rows at medium."""

    def test_row_without_importance_defaults_to_medium(self):
        """Existing DB rows that predate the column return importance='medium'."""
        data = {
            "id": 99, "index_id": 1, "session_id": None, "type": "decision",
            "title": "Old decision", "subtitle": None, "narrative": None,
            "content": "we chose X", "metadata": {}, "priority": 0.7,
            "stability": "stable", "access_count": 2,
            "last_accessed_at": None, "created_at": "2025-01-01",
            "updated_at": "2025-01-01", "user_id": None,
        }
        row = MagicMock()
        row.__getitem__ = lambda self, key: data[key]
        row.get = lambda key, default=None: data.get(key, default)

        memory = _row_to_memory(row)
        assert memory.importance == "medium"

    def test_importance_field_on_memory_dataclass(self):
        """Memory dataclass has importance field defaulting to 'medium'."""
        m = Memory(
            id=1, index_id=1, session_id=None, type="observation",
            title=None, subtitle=None, narrative=None,
            content="x", metadata={}, priority=0.5, stability="stable",
            access_count=0, last_accessed_at=None,
            created_at="2026-01-01", updated_at="2026-01-01",
        )
        assert m.importance == "medium"

    def test_save_memory_params_importance_field(self):
        """SaveMemoryParams has importance field defaulting to 'medium'."""
        params = SaveMemoryParams(text="hello")
        assert params.importance == "medium"

    def test_importance_values_constant(self):
        """IMPORTANCE_VALUES contains exactly the four valid levels."""
        assert IMPORTANCE_VALUES == frozenset(["critical", "high", "medium", "low"])


# ─── V5: access_count invariant ───────────────────────────────────────────────


class TestAccessCountInvariant:
    """V5: access_count is NOT mutated by save_memory or update_memory."""

    @pytest.fixture
    def dl(self):
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_save_memory_does_not_touch_access_count(self, dl):
        """save_memory INSERT must NOT include access_count in the SET/VALUES."""
        inserted_row = MagicMock()
        inserted_row.__getitem__ = lambda self, key: 5 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,          # dedup check
            inserted_row,  # INSERT
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            await dl.save_memory(SaveMemoryParams(text="test", importance="low"))

        # The INSERT SQL must not reference access_count
        insert_call = conn.fetchrow.call_args_list[-1]
        insert_sql = insert_call[0][0]
        assert "access_count" not in insert_sql

    @pytest.mark.asyncio
    async def test_update_memory_does_not_touch_access_count(self, dl):
        """update_memory UPDATE must NOT include access_count in the SET clause."""
        existing_row = MagicMock()
        existing_row_data = {
            "id": 1, "content": "c", "title": None, "subtitle": None, "narrative": None
        }
        existing_row.__getitem__ = lambda self, key: existing_row_data[key]

        conn = AsyncMock()
        conn.fetchrow.return_value = existing_row
        pool = _make_pool(conn)

        from open_brain.data_layer.interface import UpdateMemoryParams
        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            await dl.update_memory(UpdateMemoryParams(id=1, text="updated"))

        conn.execute.assert_called_once()
        update_sql = conn.execute.call_args[0][0]
        assert "access_count" not in update_sql


# ─── V6: save_memory with invalid importance raises ValueError ────────────────


class TestSaveMemoryInvalidImportance:
    """V6: save_memory with importance='urgent' raises ValueError BEFORE DB access."""

    @pytest.fixture
    def dl(self):
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_invalid_importance_raises_before_db(self, dl):
        """importance='urgent' raises ValueError; no DB calls made."""
        conn = AsyncMock()
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio"),
        ):
            with pytest.raises(ValueError, match="urgent"):
                await dl.save_memory(
                    SaveMemoryParams(text="test", importance="urgent")
                )

        # No DB calls should have been made
        conn.fetchrow.assert_not_called()
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_string_importance_raises(self, dl):
        """importance='' raises ValueError."""
        conn = AsyncMock()
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio"),
        ):
            with pytest.raises(ValueError):
                await dl.save_memory(
                    SaveMemoryParams(text="test", importance="")
                )

        conn.fetchrow.assert_not_called()


# ─── V7: dedup contract preserved ────────────────────────────────────────────


class TestDedupContractPreserved:
    """V7: same-text saves return duplicate_of regardless of importance."""

    @pytest.fixture
    def dl(self):
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_same_text_different_importance_returns_duplicate(self, dl):
        """Save with same text but different importance still triggers dedup."""
        dup_row = MagicMock()
        dup_row.__getitem__ = lambda self, key: 77 if key == "id" else None

        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            dup_row,  # dedup check: content hash matches
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(
                    text="identical text",
                    importance="critical",  # different importance from first save
                )
            )

        assert result.duplicate_of == 77
        assert "Duplicate" in result.message
        # No INSERT — dedup fired before importance could affect anything
        assert conn.fetchrow.call_count == 1
