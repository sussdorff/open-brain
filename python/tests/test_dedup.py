"""Tests for auto-deduplication at store time (open-brain-qrw).

9 test variants:
1. skip-unchanged (regression): dedup_mode="skip" (default), hybrid_search NOT called.
2. merge-hit: dedup_mode="merge", similarity > threshold → returned id equals existing_id.
3. merge-miss: dedup_mode="merge", similarity below threshold → new row inserted.
4. duplicate_of precedence: SaveMemoryParams(duplicate_of=42, dedup_mode="merge") → embed_query NOT called.
5. access_count invariant: After merge, the UPDATE SQL must NOT include "access_count" or "last_accessed_at".
6. importance rank preservation (higher wins): incoming higher → post-merge uses incoming.
7. importance rank preservation (existing wins): existing higher → post-merge uses existing.
8. updated_at-only constraint: After merge, only updated_at/importance/priority/content may change.
9. default value: SaveMemoryParams(text=...) without dedup_mode → behaves as skip (no embed call).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from open_brain.data_layer.interface import SaveMemoryParams
from open_brain.data_layer.postgres import PostgresDataLayer


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Build a properly structured asyncpg pool mock."""

    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


def _make_insert_row(memory_id: int = 99) -> MagicMock:
    """Row returned by INSERT RETURNING id."""
    row = MagicMock()
    row.__getitem__ = lambda self, key: memory_id if key == "id" else None
    return row


def _make_match_row(
    memory_id: int = 42,
    importance: str = "medium",
    priority: float = 0.5,
    content: str = "existing content",
    similarity: float = 0.95,
) -> MagicMock:
    """Row returned by vector similarity SELECT."""
    data = {
        "id": memory_id,
        "importance": importance,
        "priority": priority,
        "content": content,
        "similarity": similarity,
    }
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    row.get = lambda key, default=None: data.get(key, default)
    return row


# ─── Test 1: skip-unchanged (regression) ─────────────────────────────────────


class TestDedupSkipUnchanged:
    """T1: dedup_mode='skip' (default) — hybrid_search / embed_query NOT called."""

    @pytest.fixture
    def dl(self) -> PostgresDataLayer:
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_skip_mode_does_not_call_embed(self, dl: PostgresDataLayer) -> None:
        """dedup_mode='skip' must NOT trigger embed_query_with_usage."""
        inserted_row = _make_insert_row(55)
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,         # content hash dedup check (no project → _resolve_index_id skips DB)
            inserted_row, # INSERT RETURNING id
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
            patch("open_brain.data_layer.postgres.embed_query_with_usage") as mock_embed,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(SaveMemoryParams(text="hello world", dedup_mode="skip"))

        mock_embed.assert_not_called()
        assert result.duplicate_of is None
        assert result.id == 55


# ─── Test 2: merge-hit ────────────────────────────────────────────────────────


class TestDedupMergeHit:
    """T2: dedup_mode='merge', similarity > threshold → returns existing id, no new row."""

    @pytest.fixture
    def dl(self) -> PostgresDataLayer:
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_merge_hit_returns_existing_id(self, dl: PostgresDataLayer) -> None:
        """When vector similarity >= DEDUP_THRESHOLD, merge and return existing id."""
        match_row = _make_match_row(memory_id=42, similarity=0.95)
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            match_row, # semantic dedup SELECT (no project → _resolve_index_id skips DB)
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
            patch(
                "open_brain.data_layer.postgres.embed_query_with_usage",
                new_callable=AsyncMock,
                return_value=([0.1] * 1024, 5),
            ),
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(text="very similar content", dedup_mode="merge")
            )

        assert result.id == 42
        assert result.duplicate_of == 42
        # No INSERT should have happened (fetchrow should not have been called for INSERT)
        # The last fetchrow call was the similarity check, no INSERT call
        assert "INSERT" not in str(conn.fetchrow.call_args_list[-1])


# ─── Test 3: merge-miss ───────────────────────────────────────────────────────


class TestDedupMergeMiss:
    """T3: dedup_mode='merge', similarity below threshold → new row inserted normally."""

    @pytest.fixture
    def dl(self) -> PostgresDataLayer:
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_merge_miss_inserts_new_row(self, dl: PostgresDataLayer) -> None:
        """When vector similarity < DEDUP_THRESHOLD, insert new row."""
        match_row = _make_match_row(memory_id=42, similarity=0.50)  # below threshold
        inserted_row = _make_insert_row(77)
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            match_row,     # semantic dedup SELECT (low similarity → no match; no project → _resolve_index_id skips DB)
            None,          # content hash dedup check
            inserted_row,  # INSERT RETURNING id
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
            patch(
                "open_brain.data_layer.postgres.embed_query_with_usage",
                new_callable=AsyncMock,
                return_value=([0.1] * 1024, 5),
            ),
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(text="quite different content", dedup_mode="merge")
            )

        assert result.id == 77
        assert result.duplicate_of is None


# ─── Test 4: duplicate_of precedence ─────────────────────────────────────────


class TestDuplicateOfPrecedence:
    """T4: When duplicate_of is set, dedup_mode='merge' is ignored; embed NOT called."""

    @pytest.fixture
    def dl(self) -> PostgresDataLayer:
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_duplicate_of_short_circuits_merge(self, dl: PostgresDataLayer) -> None:
        """duplicate_of=42 with dedup_mode='merge' → embed_query NOT called, result.duplicate_of==42.

        Note: _resolve_index_id is still called (it's before the short-circuit), but no
        DB call happens since no project is passed. Then the short-circuit returns immediately.
        """
        conn = AsyncMock()
        conn.fetchrow.return_value = None  # should not be called at all after short-circuit
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
            patch("open_brain.data_layer.postgres.embed_query_with_usage") as mock_embed,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(text="some text", duplicate_of=42, dedup_mode="merge")
            )

        mock_embed.assert_not_called()
        assert result.duplicate_of == 42
        assert result.id == 42


# ─── Test 5: access_count invariant ──────────────────────────────────────────


class TestMergeAccessCountInvariant:
    """T5: After merge, the UPDATE SQL must NOT include 'access_count' or 'last_accessed_at'."""

    @pytest.fixture
    def dl(self) -> PostgresDataLayer:
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_merge_update_does_not_touch_access_count(self, dl: PostgresDataLayer) -> None:
        """The SQL UPDATE on merge path must not contain access_count or last_accessed_at."""
        match_row = _make_match_row(memory_id=42, similarity=0.95)
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            match_row, # semantic dedup SELECT (no project → _resolve_index_id skips DB)
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
            patch(
                "open_brain.data_layer.postgres.embed_query_with_usage",
                new_callable=AsyncMock,
                return_value=([0.1] * 1024, 5),
            ),
        ):
            mock_asyncio.create_task = MagicMock()
            await dl.save_memory(
                SaveMemoryParams(text="similar content", dedup_mode="merge")
            )

        # conn.execute should have been called with the UPDATE
        conn.execute.assert_called_once()
        update_sql = conn.execute.call_args[0][0]
        assert "access_count" not in update_sql
        assert "last_accessed_at" not in update_sql


# ─── Test 6: importance rank preservation (higher wins) ──────────────────────


class TestMergeImportanceHigherWins:
    """T6: When incoming importance > existing importance, post-merge uses incoming."""

    @pytest.fixture
    def dl(self) -> PostgresDataLayer:
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_incoming_higher_importance_wins(self, dl: PostgresDataLayer) -> None:
        """existing=low, incoming=high → merged importance='high'."""
        match_row = _make_match_row(memory_id=42, importance="low", similarity=0.95)
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            match_row, # semantic dedup SELECT (no project → _resolve_index_id skips DB)
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
            patch(
                "open_brain.data_layer.postgres.embed_query_with_usage",
                new_callable=AsyncMock,
                return_value=([0.1] * 1024, 5),
            ),
        ):
            mock_asyncio.create_task = MagicMock()
            await dl.save_memory(
                SaveMemoryParams(text="important update", importance="high", dedup_mode="merge")
            )

        conn.execute.assert_called_once()
        update_args = conn.execute.call_args[0]
        # The importance argument should be 'high' (higher rank wins)
        assert "high" in update_args, f"Expected 'high' in update args, got: {update_args}"


# ─── Test 7: importance rank preservation (existing wins) ────────────────────


class TestMergeImportanceExistingWins:
    """T7: When existing importance > incoming importance, post-merge uses existing."""

    @pytest.fixture
    def dl(self) -> PostgresDataLayer:
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_existing_higher_importance_preserved(self, dl: PostgresDataLayer) -> None:
        """existing=critical, incoming=medium → merged importance='critical'."""
        match_row = _make_match_row(memory_id=42, importance="critical", similarity=0.95)
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            match_row, # semantic dedup SELECT (no project → _resolve_index_id skips DB)
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
            patch(
                "open_brain.data_layer.postgres.embed_query_with_usage",
                new_callable=AsyncMock,
                return_value=([0.1] * 1024, 5),
            ),
        ):
            mock_asyncio.create_task = MagicMock()
            await dl.save_memory(
                SaveMemoryParams(text="minor update", importance="medium", dedup_mode="merge")
            )

        conn.execute.assert_called_once()
        update_args = conn.execute.call_args[0]
        # The importance argument should be 'critical' (existing wins)
        assert "critical" in update_args, f"Expected 'critical' in update args, got: {update_args}"


# ─── Test 8: updated_at-only constraint ──────────────────────────────────────


class TestMergeUpdatedAtConstraint:
    """T8: After merge, UPDATE SQL must only touch updated_at, importance, priority (not access_count/last_accessed_at)."""

    @pytest.fixture
    def dl(self) -> PostgresDataLayer:
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_merge_update_sql_shape(self, dl: PostgresDataLayer) -> None:
        """Merge UPDATE SQL shape: must set updated_at, must NOT set access_count or last_accessed_at."""
        match_row = _make_match_row(memory_id=42, similarity=0.95)
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            match_row, # semantic dedup SELECT (no project → _resolve_index_id skips DB)
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
            patch(
                "open_brain.data_layer.postgres.embed_query_with_usage",
                new_callable=AsyncMock,
                return_value=([0.1] * 1024, 5),
            ),
        ):
            mock_asyncio.create_task = MagicMock()
            await dl.save_memory(
                SaveMemoryParams(text="content", dedup_mode="merge")
            )

        conn.execute.assert_called_once()
        update_sql = conn.execute.call_args[0][0]
        # Must set updated_at
        assert "updated_at" in update_sql
        # Must NOT set these recall-only fields
        assert "access_count" not in update_sql
        assert "last_accessed_at" not in update_sql


# ─── Test 9: default value (no dedup_mode) ───────────────────────────────────


class TestDedupDefaultValue:
    """T9: SaveMemoryParams without dedup_mode defaults to 'skip' behavior."""

    @pytest.fixture
    def dl(self) -> PostgresDataLayer:
        return PostgresDataLayer()

    @pytest.mark.asyncio
    async def test_default_dedup_mode_is_skip(self, dl: PostgresDataLayer) -> None:
        """SaveMemoryParams(text=...) without dedup_mode → no embed call (skip behavior)."""
        params = SaveMemoryParams(text="default params")
        assert params.dedup_mode == "skip"

        inserted_row = _make_insert_row(88)
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            None,         # content hash dedup check (no project → _resolve_index_id skips DB)
            inserted_row, # INSERT RETURNING id
        ]
        pool = _make_pool(conn)

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
            patch("open_brain.data_layer.postgres.embed_query_with_usage") as mock_embed,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(params)

        mock_embed.assert_not_called()
        assert result.duplicate_of is None
        assert result.id == 88
