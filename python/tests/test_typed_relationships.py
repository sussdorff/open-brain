"""Unit tests for typed-relationship API (create_relationship, traverse, get_relationships).

Tests use mocked asyncpg pool — no real DB connections.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.interface import VALID_LINK_TYPES
from open_brain.data_layer.postgres import PostgresDataLayer


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Build a properly structured asyncpg pool mock (mirrors test_postgres.py pattern)."""

    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


def _make_rel_row(overrides: dict | None = None) -> MagicMock:
    """Create a mock asyncpg Record for a memory_relationships row."""
    data: dict = {
        "id": 1,
        "source_id": 10,
        "target_id": 20,
        "link_type": "similar_to",
        "relation_type": "similar_to",
        "confidence": 0.9,
    }
    if overrides:
        data.update(overrides)
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    row.get = lambda key, default=None: data.get(key, default)
    return row


# ─── AC1: VALID_LINK_TYPES exported from interface ───────────────────────────


class TestValidLinkTypes:
    def test_valid_link_types_is_frozenset(self):
        assert isinstance(VALID_LINK_TYPES, frozenset)

    def test_contains_expected_values(self):
        expected = {
            "similar_to",
            "attended_by",
            "mentioned_in",
            "spawned_task",
            "supersedes",
            "contradicts",
            "co_occurs",
        }
        assert expected == VALID_LINK_TYPES


# ─── AC2: create_relationship rejects unknown link_types ─────────────────────


class TestCreateRelationship:
    @pytest.mark.asyncio
    async def test_rejects_unknown_link_type(self):
        dl = PostgresDataLayer()
        with pytest.raises(ValueError, match="link_type"):
            await dl.create_relationship(source_id=1, target_id=2, link_type="unknown_type")

    @pytest.mark.asyncio
    async def test_accepts_valid_link_type(self):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=42)
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.create_relationship(
                source_id=10, target_id=20, link_type="similar_to"
            )
        assert result == 42

    @pytest.mark.asyncio
    async def test_inserts_with_correct_sql(self):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=99)
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            await dl.create_relationship(
                source_id=10, target_id=20, link_type="attended_by", metadata={"note": "test"}
            )

        conn.fetchval.assert_called_once()
        call_args = conn.fetchval.call_args
        sql = call_args[0][0]
        assert "memory_relationships" in sql
        assert "link_type" in sql

    @pytest.mark.asyncio
    async def test_all_valid_link_types_accepted(self):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=1)
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            for link_type in VALID_LINK_TYPES:
                result = await dl.create_relationship(
                    source_id=1, target_id=2, link_type=link_type
                )
                assert isinstance(result, int)


# ─── AC3: traverse depth=1 returns direct neighbors ─────────────────────────


class TestTraverseDepth1:
    @pytest.mark.asyncio
    async def test_returns_direct_neighbors(self):
        conn = AsyncMock()
        rel_row = _make_rel_row({"id": 1, "source_id": 10, "target_id": 20, "link_type": "similar_to"})
        conn.fetch = AsyncMock(return_value=[rel_row])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            results = await dl.traverse(anchor_id=10, link_types=["similar_to"], depth=1)

        assert len(results) == 1
        assert results[0]["source_id"] == 10
        assert results[0]["target_id"] == 20
        assert results[0]["link_type"] == "similar_to"
        assert results[0]["depth"] == 1

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_neighbors(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            results = await dl.traverse(anchor_id=99, link_types=["similar_to"], depth=1)

        assert results == []

    @pytest.mark.asyncio
    async def test_result_dict_contains_required_fields(self):
        conn = AsyncMock()
        rel_row = _make_rel_row({"id": 5, "source_id": 10, "target_id": 30, "link_type": "attended_by"})
        conn.fetch = AsyncMock(return_value=[rel_row])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            results = await dl.traverse(anchor_id=10, link_types=["attended_by"], depth=1)

        assert len(results) == 1
        r = results[0]
        assert "id" in r
        assert "link_type" in r
        assert "depth" in r
        assert "source_id" in r
        assert "target_id" in r


# ─── AC4: traverse depth=2 returns 2-hop neighbors ───────────────────────────


class TestTraverseDepth2:
    @pytest.mark.asyncio
    async def test_depth2_returns_two_hop_neighbors(self):
        conn = AsyncMock()

        hop1_row = _make_rel_row({"id": 1, "source_id": 10, "target_id": 20, "link_type": "similar_to"})
        hop2_row = _make_rel_row({"id": 2, "source_id": 20, "target_id": 30, "link_type": "similar_to"})

        # First call returns hop-1 edges, second call returns hop-2 edges
        conn.fetch = AsyncMock(side_effect=[[hop1_row], [hop2_row]])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            results = await dl.traverse(anchor_id=10, link_types=["similar_to"], depth=2)

        # Should have both hops
        depths = {r["depth"] for r in results}
        assert 1 in depths
        assert 2 in depths
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_depth2_prevents_cycles(self):
        conn = AsyncMock()

        # Circular: 10 -> 20 -> 10 (cycle back)
        hop1_row = _make_rel_row({"id": 1, "source_id": 10, "target_id": 20, "link_type": "similar_to"})
        # hop2: 20 -> 10 (would cycle back to anchor)
        hop2_cycle = _make_rel_row({"id": 2, "source_id": 20, "target_id": 10, "link_type": "similar_to"})

        conn.fetch = AsyncMock(side_effect=[[hop1_row], [hop2_cycle]])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            results = await dl.traverse(anchor_id=10, link_types=["similar_to"], depth=2)

        # The cycle back to node 10 should NOT be included (visited set prevents it)
        neighbor_ids = {r["target_id"] for r in results}
        assert 20 in neighbor_ids
        # node 10 should not appear as a target because it's already visited (the anchor)
        assert 10 not in neighbor_ids


# ─── AC5: get_relationships returns edges for a memory_id ────────────────────


class TestGetRelationships:
    @pytest.mark.asyncio
    async def test_returns_relationships_for_memory(self):
        conn = AsyncMock()
        row1 = _make_rel_row({"id": 1, "source_id": 10, "target_id": 20, "link_type": "similar_to"})
        row2 = _make_rel_row({"id": 2, "source_id": 5, "target_id": 10, "link_type": "attended_by"})
        conn.fetch = AsyncMock(return_value=[row1, row2])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            results = await dl.get_relationships(memory_id=10)

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_filters_by_link_types(self):
        conn = AsyncMock()
        row = _make_rel_row({"id": 1, "source_id": 10, "target_id": 20, "link_type": "similar_to"})
        conn.fetch = AsyncMock(return_value=[row])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            results = await dl.get_relationships(memory_id=10, link_types=["similar_to"])

        # SQL should have been called with link_type filter
        conn.fetch.assert_called_once()
        call_sql = conn.fetch.call_args[0][0]
        assert "link_type" in call_sql

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_relationships(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            results = await dl.get_relationships(memory_id=999)

        assert results == []


# ─── AC6: Protocol methods declared in DataLayer ─────────────────────────────


class TestProtocolDeclarations:
    def test_create_relationship_in_protocol(self):
        from open_brain.data_layer.interface import DataLayer
        import inspect
        members = dict(inspect.getmembers(DataLayer))
        assert "create_relationship" in members

    def test_traverse_in_protocol(self):
        from open_brain.data_layer.interface import DataLayer
        import inspect
        members = dict(inspect.getmembers(DataLayer))
        assert "traverse" in members

    def test_get_relationships_in_protocol(self):
        from open_brain.data_layer.interface import DataLayer
        import inspect
        members = dict(inspect.getmembers(DataLayer))
        assert "get_relationships" in members


# ─── AC7: Integration test for backfill script ────────────────────────────────


@pytest.mark.integration
class TestBackfillScriptIntegration:
    """Integration test: runs backfill script against a real test DB.

    Requires DATABASE_URL env var pointing to a test database.
    """

    @pytest.mark.asyncio
    async def test_backfill_script_sets_link_type(self):
        import os
        import asyncpg

        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            pytest.skip("DATABASE_URL not set")

        conn = await asyncpg.connect(db_url)
        try:
            # Ensure column exists
            await conn.execute(
                "ALTER TABLE memory_relationships ADD COLUMN IF NOT EXISTS link_type text NOT NULL DEFAULT 'similar_to';"
            )

            # Insert a row with no explicit link_type (uses default)
            row_id = await conn.fetchval(
                """INSERT INTO memory_relationships (source_id, target_id, relation_type, confidence)
                   VALUES (1, 2, 'similar_to', 0.9)
                   ON CONFLICT (source_id, target_id, relation_type) DO UPDATE SET confidence = 0.9
                   RETURNING id"""
            )

            # Run backfill: update rows where link_type is NULL (should be 0 after column default)
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM memory_relationships WHERE link_type IS NULL"
            )
            assert count == 0, f"Expected 0 NULL link_type rows after column default, got {count}"

            # Verify the row we inserted has link_type = 'similar_to'
            link_type = await conn.fetchval(
                "SELECT link_type FROM memory_relationships WHERE id = $1", row_id
            )
            assert link_type == "similar_to"
        finally:
            await conn.close()
