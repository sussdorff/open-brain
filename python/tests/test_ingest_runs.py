"""Tests for ingest run context management and delete_by_run_id.

Tests:
1. test_ingest_run_generates_uuid
2. test_get_current_run_id_outside_context_returns_none
3. test_get_current_run_id_inside_context_returns_runid
4. test_sequential_contexts_have_independent_run_ids
5. test_nested_contexts_restore_outer_run_id
6. test_run_id_cleared_after_context_exits
7. test_delete_by_run_id_returns_correct_counts
8. test_delete_by_run_id_nonexistent_returns_zero
9. test_delete_by_run_id_is_transactional
10. test_ingest_rollback_tool_returns_json
11. test_save_memory_injects_run_id_in_metadata
12. test_ingest_run_round_trip (integration)
"""

from __future__ import annotations

import json
import uuid
import warnings
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.ingest.runs import get_current_run_id, ingest_run
from open_brain.data_layer.interface import DeleteByRunIdResult
from open_brain.data_layer.postgres import PostgresDataLayer


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Build a properly structured asyncpg pool mock."""

    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


def _make_transaction():
    """Return an async context manager that acts as a no-op transaction."""

    @asynccontextmanager
    async def _txn():
        yield

    return _txn()


# ─── 1. ingest_run generates a valid UUID4 string ────────────────────────────


class TestIngestRunGeneratesUUID:
    def test_ingest_run_generates_uuid(self):
        """run_id returned by ingest_run() is a valid UUID4 string.

        Note: The bead spec allows UUID or ULID; UUID4 is used (no external dep needed).
        """
        with ingest_run() as run_id:
            parsed = uuid.UUID(run_id)
            assert parsed.version == 4
            assert run_id == str(parsed)


# ─── 2. get_current_run_id outside context returns None ──────────────────────


class TestGetCurrentRunIdOutsideContext:
    def test_get_current_run_id_outside_context_returns_none(self):
        """get_current_run_id() returns None when no ingest_run context is active."""
        # Ensure we are outside any context
        result = get_current_run_id()
        assert result is None


# ─── 3. get_current_run_id inside context returns run_id ─────────────────────


class TestGetCurrentRunIdInsideContext:
    def test_get_current_run_id_inside_context_returns_runid(self):
        """get_current_run_id() returns the run_id of the active ingest_run context."""
        with ingest_run() as run_id:
            assert get_current_run_id() == run_id
            assert run_id is not None


# ─── 4. Sequential contexts have independent run_ids ─────────────────────────


class TestSequentialContextsHaveIndependentRunIds:
    def test_sequential_contexts_have_independent_run_ids(self):
        """Two sequential ingest_run contexts produce different run_ids."""
        with ingest_run() as run_id_1:
            first = run_id_1

        with ingest_run() as run_id_2:
            second = run_id_2

        assert first != second


# ─── 5. Nested contexts restore the outer run_id on exit ─────────────────────


class TestNestedContextsRestoreOuterRunId:
    def test_nested_contexts_restore_outer_run_id(self):
        """Entering a nested ingest_run restores outer run_id on exit."""
        with ingest_run() as outer_id:
            with ingest_run() as inner_id:
                assert get_current_run_id() == inner_id
                assert outer_id != inner_id
            assert get_current_run_id() == outer_id


# ─── 6. run_id is None after context exits ────────────────────────────────────


class TestRunIdClearedAfterContextExits:
    def test_run_id_cleared_after_context_exits(self):
        """get_current_run_id() returns None after the ingest_run context exits."""
        with ingest_run() as _run_id:
            pass  # context entered and exited

        assert get_current_run_id() is None


# ─── 7. delete_by_run_id returns correct counts ──────────────────────────────


class TestDeleteByRunIdReturnsCounts:
    @pytest.mark.asyncio
    async def test_delete_by_run_id_returns_correct_counts(self):
        """delete_by_run_id parses DELETE command tags and returns correct counts.

        New implementation: fetch memory IDs first, then DELETE relationships,
        usage_log, and memories inside a transaction.
        """
        conn = AsyncMock()
        # fetch() returns a list of rows with 'id' keys
        conn.fetch = AsyncMock(return_value=[{"id": 10}, {"id": 11}, {"id": 12}])
        # execute() is called 3 times: DELETE relationships, DELETE usage_log, DELETE memories
        conn.execute = AsyncMock(side_effect=["DELETE 2", "DELETE 3", "DELETE 3"])
        conn.transaction = MagicMock(return_value=_make_transaction())

        pool = _make_pool(conn)
        dl = PostgresDataLayer()

        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.delete_by_run_id("some-run-id")

        assert isinstance(result, DeleteByRunIdResult)
        assert result.relationships == 2
        assert result.memories == 3

        # Verify the SELECT was called
        conn.fetch.assert_called_once()
        fetch_call_args = conn.fetch.call_args
        assert "SELECT id FROM memories" in fetch_call_args[0][0]
        assert fetch_call_args[0][1] == "some-run-id"

        # Verify 3 execute calls: relationships, usage_log, memories
        assert conn.execute.call_count == 3


# ─── 8. delete_by_run_id non-existent run_id returns zero counts ──────────────


class TestDeleteByRunIdNonExistentReturnsZero:
    @pytest.mark.asyncio
    async def test_delete_by_run_id_nonexistent_returns_zero(self):
        """delete_by_run_id returns zero counts when fetch returns no memory IDs.

        When no memories exist for the run_id, the function returns early
        without entering a transaction.
        """
        conn = AsyncMock()
        # fetch() returns empty list — no memories for this run_id
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        conn.transaction = MagicMock(return_value=_make_transaction())

        pool = _make_pool(conn)
        dl = PostgresDataLayer()

        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.delete_by_run_id("nonexistent-run-id")

        assert result.relationships == 0
        assert result.memories == 0

        # No DELETE calls should have been made
        conn.execute.assert_not_called()
        # No transaction should have been opened
        conn.transaction.assert_not_called()


# ─── 9. delete_by_run_id is transactional ────────────────────────────────────


class TestDeleteByRunIdIsTransactional:
    @pytest.mark.asyncio
    async def test_delete_by_run_id_is_transactional(self):
        """delete_by_run_id wraps DELETE statements in a transaction when memories exist."""
        conn = AsyncMock()
        # fetch() returns one memory row so the transaction path is taken
        conn.fetch = AsyncMock(return_value=[{"id": 42}])
        conn.execute = AsyncMock(side_effect=["DELETE 1", "DELETE 0", "DELETE 1"])

        transaction_entered = False

        @asynccontextmanager
        async def fake_transaction():
            nonlocal transaction_entered
            transaction_entered = True
            yield

        conn.transaction = MagicMock(return_value=fake_transaction())

        pool = _make_pool(conn)
        dl = PostgresDataLayer()

        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            await dl.delete_by_run_id("test-run-id")

        assert transaction_entered, "delete_by_run_id must use conn.transaction() when memories exist"


# ─── 10. ingest_rollback MCP tool returns JSON ───────────────────────────────


class TestIngestRollbackToolReturnsJson:
    @pytest.mark.asyncio
    async def test_ingest_rollback_tool_returns_json(self):
        """ingest_rollback server tool returns a JSON string with deletion counts and run_id."""
        from open_brain.server import mcp, _current_scopes

        fake_result = DeleteByRunIdResult(memories=3, relationships=1)
        mock_dl = MagicMock()
        mock_dl.delete_by_run_id = AsyncMock(return_value=fake_result)

        token = _current_scopes.set(("memory",))
        try:
            with patch("open_brain.server.get_dl", return_value=mock_dl):
                # Get the registered tool and call it directly
                tools = await mcp.list_tools()
                tool_names = {t.name for t in tools}
                assert "ingest_rollback" in tool_names, (
                    f"ingest_rollback not found in tools: {tool_names}"
                )

                # Call the tool function directly
                from open_brain import server as server_module
                result_str = await server_module.ingest_rollback(run_id="abc-123")
        finally:
            _current_scopes.reset(token)

        result = json.loads(result_str)
        assert result["memories_deleted"] == 3
        assert result["relationships_deleted"] == 1
        assert result["run_id"] == "abc-123"


# ─── 11. save_memory injects run_id into metadata ────────────────────────────


class TestSaveMemoryInjectsRunId:
    @pytest.mark.asyncio
    async def test_save_memory_injects_run_id_in_metadata(self):
        """When inside an ingest_run context, save_memory stores run_id in metadata."""
        from open_brain.data_layer.interface import SaveMemoryParams

        captured_metadata: list[str] = []

        # Build an insert_row mock that captures metadata
        insert_row = MagicMock()
        insert_row.__getitem__ = lambda self, k: 42

        conn = AsyncMock()

        async def capturing_fetchrow(sql, *args):
            if "INSERT INTO memories" in sql:
                # metadata is the 8th positional arg ($8 in SQL)
                # args[0]=index_id, [1]=type, [2]=title, [3]=subtitle,
                # [4]=narrative, [5]=content, [6]=session_ref, [7]=metadata, [8]=user_id, [9]=importance
                if len(args) >= 8:
                    captured_metadata.append(str(args[7]))
                return insert_row
            # content-hash dedup check — return None (no dup)
            return None

        conn.fetchrow = capturing_fetchrow

        pool = _make_pool(conn)
        dl = PostgresDataLayer()

        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            with patch("open_brain.data_layer.postgres.asyncio.create_task"):
                with ingest_run() as run_id:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", RuntimeWarning)
                        await dl.save_memory(SaveMemoryParams(text="test memory content"))

        # Verify run_id appears in the captured metadata
        assert len(captured_metadata) >= 1, "No INSERT metadata was captured"
        for meta_str in captured_metadata:
            meta = json.loads(meta_str)
            assert "run_id" in meta, f"run_id not found in metadata: {meta}"
            assert meta["run_id"] == run_id


# ─── 12. Integration: full round-trip ingest → query → rollback ──────────────


@pytest.mark.integration
class TestIngestRunRoundTrip:
    """Integration test: ingest two memories, query by run_id, rollback, verify zero rows.

    Requires DATABASE_URL env var pointing to a real Postgres instance.

    Note: create_relationship requires FK-valid IDs from the same DB, so we test
    memories only (2 rows) and omit the relationship insertion to keep the test
    self-contained. The relationship deletion path is covered by mock tests above.
    """

    @pytest.mark.asyncio
    async def test_ingest_run_round_trip(self):
        import os

        import asyncpg

        from open_brain.data_layer.postgres import PostgresDataLayer, get_pool
        from open_brain.data_layer.interface import SaveMemoryParams

        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url or database_url == "test-placeholder":
            pytest.skip("Requires real DATABASE_URL (not test placeholder)")

        dl = PostgresDataLayer()

        # ── Ingest two memories inside an ingest_run context ─────────────────
        with ingest_run() as run_id:
            r1 = await dl.save_memory(SaveMemoryParams(text="ingest round-trip memory A"))
            r2 = await dl.save_memory(SaveMemoryParams(text="ingest round-trip memory B"))

        assert r1 is not None, "save_memory returned None for memory A"
        assert r2 is not None, "save_memory returned None for memory B"

        # ── Query: expect 2 rows in DB with this run_id ───────────────────────
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id FROM memories WHERE metadata->>'run_id' = $1",
                run_id,
            )
        assert len(rows) == 2, (
            f"Expected 2 memories for run_id={run_id}, got {len(rows)}"
        )

        # ── Rollback ──────────────────────────────────────────────────────────
        result = await dl.delete_by_run_id(run_id)
        assert result.memories == 2, (
            f"Expected 2 deleted memories, got {result.memories}"
        )

        # ── Re-query: expect 0 residual rows ──────────────────────────────────
        async with pool.acquire() as conn:
            remaining = await conn.fetch(
                "SELECT id FROM memories WHERE metadata->>'run_id' = $1",
                run_id,
            )
        assert len(remaining) == 0, (
            f"Expected 0 residual memories after rollback, got {len(remaining)}"
        )
