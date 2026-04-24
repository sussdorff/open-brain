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
        # hop2: 20 -> 10 (cycle back to anchor)
        hop2_cycle = _make_rel_row({"id": 2, "source_id": 20, "target_id": 10, "link_type": "similar_to"})

        conn.fetch = AsyncMock(side_effect=[[hop1_row], [hop2_cycle]])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            results = await dl.traverse(anchor_id=10, link_types=["similar_to"], depth=2)

        # All matched edges are reported (including the cycle-back edge), but already-visited
        # neighbors are NOT re-enqueued into the frontier (no infinite expansion).
        # Both edges should be present in results.
        assert len(results) == 2

        # The hop-1 edge (10→20) is reported at depth=1
        hop1_results = [r for r in results if r["depth"] == 1]
        assert len(hop1_results) == 1
        assert hop1_results[0]["source_id"] == 10
        assert hop1_results[0]["target_id"] == 20

        # The cycle-back edge (20→10) is also reported (edge reporting), but
        # node 10 is NOT re-enqueued, so traversal does not expand further.
        hop2_results = [r for r in results if r["depth"] == 2]
        assert len(hop2_results) == 1
        assert hop2_results[0]["source_id"] == 20
        assert hop2_results[0]["target_id"] == 10


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


# ─── A7: direction parameter tests ──────────────────────────────────────────


class TestTraverseDirection:
    @pytest.mark.asyncio
    async def test_traverse_inbound_direction(self):
        """traverse with direction='inbound' queries using target_id = $1."""
        conn = AsyncMock()
        # inbound: source=5 points to anchor=10
        rel_row = _make_rel_row({"id": 3, "source_id": 5, "target_id": 10, "link_type": "attended_by"})
        conn.fetch = AsyncMock(return_value=[rel_row])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            results = await dl.traverse(
                anchor_id=10, link_types=["attended_by"], depth=1, direction="inbound"
            )

        assert len(results) == 1
        # Verify the SQL used target_id for the inbound query
        call_sql = conn.fetch.call_args[0][0]
        assert "target_id" in call_sql

        r = results[0]
        assert r["source_id"] == 5
        assert r["target_id"] == 10
        assert r["depth"] == 1

    @pytest.mark.asyncio
    async def test_traverse_both_direction(self):
        """traverse with direction='both' queries using both source_id and target_id."""
        conn = AsyncMock()
        # 'both': anchor=10 has outbound edge to 20 AND inbound edge from 5
        row_out = _make_rel_row({"id": 1, "source_id": 10, "target_id": 20, "link_type": "similar_to"})
        row_in = _make_rel_row({"id": 2, "source_id": 5, "target_id": 10, "link_type": "similar_to"})
        conn.fetch = AsyncMock(return_value=[row_out, row_in])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            results = await dl.traverse(
                anchor_id=10, link_types=["similar_to"], depth=1, direction="both"
            )

        assert len(results) == 2
        # Verify the SQL uses both source_id and target_id
        call_sql = conn.fetch.call_args[0][0]
        assert "source_id" in call_sql
        assert "target_id" in call_sql

    @pytest.mark.asyncio
    async def test_traverse_invalid_direction_raises(self):
        """traverse raises ValueError for an unrecognized direction value."""
        dl = PostgresDataLayer()
        with pytest.raises(ValueError, match="direction"):
            await dl.traverse(
                anchor_id=10, link_types=["similar_to"], depth=1, direction="sideways"
            )

    @pytest.mark.asyncio
    async def test_traverse_invalid_depth_raises(self):
        """traverse raises ValueError when depth is out of [1, 10] range."""
        dl = PostgresDataLayer()
        with pytest.raises(ValueError, match="depth"):
            await dl.traverse(anchor_id=10, link_types=["similar_to"], depth=0)
        with pytest.raises(ValueError, match="depth"):
            await dl.traverse(anchor_id=10, link_types=["similar_to"], depth=11)


# ─── A9: MCP tool registration test ─────────────────────────────────────────


class TestMCPToolRegistration:
    @pytest.mark.asyncio
    async def test_create_relationship_and_traverse_relationships_registered(self):
        """create_relationship and traverse_relationships appear in mcp.list_tools()."""
        from open_brain.server import mcp, _current_scopes

        token = _current_scopes.set(("memory",))
        try:
            tools = await mcp.list_tools()
            tool_names = {t.name for t in tools}
            assert "create_relationship" in tool_names, (
                f"create_relationship not found in registered tools: {tool_names}"
            )
            assert "traverse_relationships" in tool_names, (
                f"traverse_relationships not found in registered tools: {tool_names}"
            )
        finally:
            _current_scopes.reset(token)


# ─── AC7: Integration test for backfill script ────────────────────────────────


@pytest.mark.integration
class TestBackfillScriptIntegration:
    """Integration test: runs backfill script against a real test DB.

    Requires DATABASE_URL env var pointing to a test database.
    """

    def test_backfill_script_sets_link_type(self):
        import os
        import subprocess
        from pathlib import Path

        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            pytest.skip("DATABASE_URL not set")

        # Locate the backfill script relative to this test file
        # Path(__file__) = python/tests/test_typed_relationships.py
        # parents[2]     = repo root (python/tests -> python -> repo root)
        scripts_dir = Path(__file__).parents[2] / "scripts"
        script_path = scripts_dir / "migrate_relationships_backfill.py"
        assert script_path.exists(), f"Backfill script not found at {script_path}"

        env = {**os.environ, "DATABASE_URL": db_url}

        # First run: should apply migration and report rows updated
        result = subprocess.run(
            ["python", str(script_path)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"Backfill script failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "Rows updated:" in result.stdout, (
            f"Expected 'Rows updated:' in stdout, got: {result.stdout!r}"
        )

        # Second run: idempotency check — should report 0 rows updated
        result2 = subprocess.run(
            ["python", str(script_path)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result2.returncode == 0, (
            f"Backfill script second run failed:\n"
            f"stdout: {result2.stdout}\nstderr: {result2.stderr}"
        )
        assert "Rows updated: 0" in result2.stdout, (
            f"Expected idempotent second run to report 'Rows updated: 0', "
            f"got: {result2.stdout!r}"
        )
