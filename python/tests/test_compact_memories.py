"""Tests for compact_memories: union-find clustering + postgres implementation."""

from __future__ import annotations

import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from open_brain.data_layer.interface import (
    CompactParams,
    CompactResult,
    ClusterPlan,
)
from open_brain.data_layer.postgres import PostgresDataLayer


# ─── Mock helpers ─────────────────────────────────────────────────────────────

def _make_pool(conn: AsyncMock) -> MagicMock:
    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


def _make_memory_row(
    id: int,
    access_count: int = 0,
    created_at: str = "2026-01-01",
    content: str = "test",
    index_id: int = 1,
    metadata: dict | None = None,
) -> MagicMock:
    data = {
        "id": id,
        "index_id": index_id,
        "session_id": None,
        "type": "session_summary",
        "title": f"Memory {id}",
        "subtitle": None,
        "narrative": None,
        "content": content,
        "metadata": metadata or {},
        "priority": 0.5,
        "stability": "stable",
        "access_count": access_count,
        "last_accessed_at": None,
        "created_at": created_at,
        "updated_at": created_at,
    }
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    row.get = lambda key, default=None: data.get(key, default)
    return row


def _make_sim_row(id1: int, id2: int, similarity: float) -> MagicMock:
    data = {"id1": id1, "id2": id2, "similarity": similarity}
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    return row


# ─── Pure union-find tests ─────────────────────────────────────────────────────

class TestUnionFind:
    """Test the pure Python union-find clustering logic directly."""

    def test_empty_edges_returns_no_clusters(self):
        from open_brain.data_layer.postgres import _build_clusters
        ids = [1, 2, 3]
        edges = []
        clusters = _build_clusters(ids, edges)
        assert clusters == []

    def test_single_pair_forms_cluster(self):
        from open_brain.data_layer.postgres import _build_clusters
        ids = [1, 2]
        edges = [(1, 2)]
        clusters = _build_clusters(ids, edges)
        assert len(clusters) == 1
        assert set(clusters[0]) == {1, 2}

    def test_transitive_closure_five_members(self):
        """A-B, B-C, C-D, D-E → all five in one cluster (even if A-E not directly connected)."""
        from open_brain.data_layer.postgres import _build_clusters
        ids = [1, 2, 3, 4, 5]
        edges = [(1, 2), (2, 3), (3, 4), (4, 5)]  # chain, no A-E direct edge
        clusters = _build_clusters(ids, edges)
        assert len(clusters) == 1
        assert set(clusters[0]) == {1, 2, 3, 4, 5}

    def test_two_clusters_plus_isolated(self):
        """Two clusters + isolated node → 2 clusters, isolated node not in any cluster."""
        from open_brain.data_layer.postgres import _build_clusters
        ids = [1, 2, 3, 4, 5]
        edges = [(1, 2), (3, 4)]  # IDs 5 is isolated
        clusters = _build_clusters(ids, edges)
        assert len(clusters) == 2
        cluster_sets = [set(c) for c in clusters]
        assert {1, 2} in cluster_sets
        assert {3, 4} in cluster_sets


# ─── Strategy tests ────────────────────────────────────────────────────────────

class TestCanonicalStrategy:
    """Test canonical selection strategies."""

    def test_keep_highest_access(self):
        from open_brain.data_layer.postgres import _select_canonical
        rows = {
            1: _make_memory_row(1, access_count=5, created_at="2026-01-01"),
            2: _make_memory_row(2, access_count=10, created_at="2025-01-01"),
            3: _make_memory_row(3, access_count=3, created_at="2026-06-01"),
        }
        canonical = _select_canonical([1, 2, 3], rows, "keep_highest_access")
        assert canonical == 2

    def test_keep_highest_access_tiebreak_by_updated_at(self):
        from open_brain.data_layer.postgres import _select_canonical
        # Build rows manually with distinct updated_at values to actually test the tiebreak
        # Row A: access_count=10, updated_at older (2026-01-15)
        # Row B: access_count=10, updated_at newer (2026-01-20)
        def _make_row_with_updated_at(id: int, access_count: int, updated_at: str) -> MagicMock:
            data = {
                "id": id,
                "index_id": 1,
                "session_id": None,
                "type": "session_summary",
                "title": f"Memory {id}",
                "subtitle": None,
                "narrative": None,
                "content": "test",
                "metadata": {},
                "priority": 0.5,
                "stability": "stable",
                "access_count": access_count,
                "last_accessed_at": None,
                "created_at": "2026-01-01",
                "updated_at": updated_at,
            }
            row = MagicMock()
            row.__getitem__ = lambda self, key: data[key]
            row.get = lambda key, default=None: data.get(key, default)
            return row

        rows = {
            1: _make_row_with_updated_at(1, access_count=10, updated_at="2026-01-15"),
            2: _make_row_with_updated_at(2, access_count=10, updated_at="2026-01-20"),
        }
        canonical = _select_canonical([1, 2], rows, "keep_highest_access")
        # Both have same access_count; newer updated_at wins → row 2
        assert canonical == 2

    def test_keep_latest(self):
        from open_brain.data_layer.postgres import _select_canonical
        rows = {
            1: _make_memory_row(1, created_at="2025-01-01"),
            2: _make_memory_row(2, created_at="2026-06-01"),
            3: _make_memory_row(3, created_at="2026-01-01"),
        }
        canonical = _select_canonical([1, 2, 3], rows, "keep_latest")
        assert canonical == 2

    def test_keep_most_comprehensive(self):
        from open_brain.data_layer.postgres import _select_canonical
        rows = {
            1: _make_memory_row(1, content="short"),
            2: _make_memory_row(2, content="this is a much longer content that has more words"),
            3: _make_memory_row(3, content="medium length content here"),
        }
        canonical = _select_canonical([1, 2, 3], rows, "keep_most_comprehensive")
        assert canonical == 2


# ─── Postgres compact_memories tests ──────────────────────────────────────────

class TestCompactMemoriesEmpty:
    """Empty scope returns no-op result."""

    @pytest.mark.asyncio
    async def test_empty_scope_no_op(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])  # no memories
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.compact_memories(CompactParams())

        assert result.clusters_found == 0
        assert result.memories_deleted == 0
        assert result.memories_kept == []
        assert result.deleted_ids == []
        assert result.plan == []

    @pytest.mark.asyncio
    async def test_single_memory_is_reported_in_memories_kept(self):
        """One memory → no clusters, but the ID must appear in memories_kept.

        Regression: previously returned memories_kept=[] which falsely
        implied the existing memory was dropped.
        """
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[_make_memory_row(42)])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.compact_memories(CompactParams())

        assert result.clusters_found == 0
        assert result.memories_deleted == 0
        assert result.memories_kept == [42]
        assert result.deleted_ids == []
        assert result.plan == []


class TestCompactMemoriesSimplePair:
    """Cluster with 2 members — simplest case."""

    @pytest.mark.asyncio
    async def test_two_member_cluster_dry_run(self):
        conn = AsyncMock()
        # Two memories, same scope
        rows = [
            _make_memory_row(1, access_count=3),
            _make_memory_row(2, access_count=7),
        ]
        sim_rows = [_make_sim_row(1, 2, 0.92)]

        # fetch is called twice: once for scope rows, once for pairwise sim
        conn.fetch = AsyncMock(side_effect=[rows, sim_rows])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.compact_memories(CompactParams(dry_run=True))

        assert result.clusters_found == 1
        assert result.memories_deleted == 0  # dry_run=True → no deletion
        assert 2 in result.memories_kept  # highest access → canonical
        assert 1 in result.deleted_ids or 1 not in result.memories_kept
        assert len(result.plan) == 1
        assert result.plan[0].canonical_id == 2
        assert result.plan[0].to_delete == [1]

    @pytest.mark.asyncio
    async def test_two_member_cluster_execute(self):
        conn = AsyncMock()
        rows = [
            _make_memory_row(1, access_count=3),
            _make_memory_row(2, access_count=7),
        ]
        sim_rows = [_make_sim_row(1, 2, 0.92)]
        conn.fetch = AsyncMock(side_effect=[rows, sim_rows])
        # execute is called 3x: memory_usage_log, memory_relationships, memories
        conn.execute = AsyncMock(
            side_effect=["DELETE 0", "DELETE 0", "DELETE 1"],
        )
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.compact_memories(CompactParams(dry_run=False))

        assert result.clusters_found == 1
        assert result.memories_deleted == 1
        assert 2 in result.memories_kept
        assert 1 in result.deleted_ids
        # Verify dependent rows are deleted BEFORE the memories row
        # (schema has no ON DELETE CASCADE → manual order required).
        assert conn.execute.call_count == 3
        call_args_list = conn.execute.call_args_list
        assert "memory_usage_log" in call_args_list[0][0][0]
        assert "memory_relationships" in call_args_list[1][0][0]
        assert "DELETE FROM memories" in call_args_list[2][0][0]
        # All three deletes target the same IDs
        assert call_args_list[0][0][1] == [1]
        assert call_args_list[1][0][1] == [1]
        assert call_args_list[2][0][1] == [1]


class TestCompactMemoriesTransitive:
    """Cluster with 5 members via transitive connections."""

    @pytest.mark.asyncio
    async def test_five_member_transitive_cluster(self):
        conn = AsyncMock()
        rows = [_make_memory_row(i, access_count=i) for i in range(1, 6)]
        # Chain: 1-2, 2-3, 3-4, 4-5 (no direct 1-5 edge)
        sim_rows = [
            _make_sim_row(1, 2, 0.90),
            _make_sim_row(2, 3, 0.91),
            _make_sim_row(3, 4, 0.89),
            _make_sim_row(4, 5, 0.92),
        ]
        conn.fetch = AsyncMock(side_effect=[rows, sim_rows])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.compact_memories(CompactParams(dry_run=True))

        assert result.clusters_found == 1
        assert len(result.plan) == 1
        assert set(result.plan[0].members) == {1, 2, 3, 4, 5}
        # keep_highest_access → id=5 (access_count=5)
        assert result.plan[0].canonical_id == 5
        assert len(result.plan[0].to_delete) == 4


class TestCompactMemoriesMixed:
    """Mixed: 2 clusters + isolated memories."""

    @pytest.mark.asyncio
    async def test_two_clusters_plus_isolated(self):
        conn = AsyncMock()
        # 6 memories: cluster A=(1,2), cluster B=(3,4,5), isolated=6
        rows = [_make_memory_row(i, access_count=i) for i in range(1, 7)]
        sim_rows = [
            _make_sim_row(1, 2, 0.95),   # cluster A
            _make_sim_row(3, 4, 0.88),   # cluster B (partial)
            _make_sim_row(4, 5, 0.91),   # cluster B (transitive)
        ]
        conn.fetch = AsyncMock(side_effect=[rows, sim_rows])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.compact_memories(CompactParams(dry_run=True))

        assert result.clusters_found == 2
        assert len(result.plan) == 2
        # Isolated memory (6) should be in memories_kept but not in any cluster's to_delete
        all_deleted = [id for plan in result.plan for id in plan.to_delete]
        assert 6 not in all_deleted


class TestCompactMemoriesStrategies:
    """Different strategies produce different canonicals."""

    def _make_strategy_rows(self):
        # Memory 1: access_count=1, old content, old date
        # Memory 2: access_count=5, long content, very old date
        # Memory 3: access_count=2, short content, newest date
        return [
            _make_memory_row(1, access_count=1, created_at="2025-06-01", content="short text"),
            _make_memory_row(2, access_count=5, created_at="2024-01-01", content="this is very long detailed content about something important and comprehensive"),
            _make_memory_row(3, access_count=2, created_at="2026-06-01", content="medium content"),
        ]

    def _make_strategy_sim_rows(self):
        return [
            _make_sim_row(1, 2, 0.93),
            _make_sim_row(2, 3, 0.88),
        ]

    @pytest.mark.asyncio
    async def test_keep_highest_access_selects_by_access_count(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(side_effect=[self._make_strategy_rows(), self._make_strategy_sim_rows()])
        pool = _make_pool(conn)
        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.compact_memories(
                CompactParams(dry_run=True, strategy="keep_highest_access")
            )
        assert result.plan[0].canonical_id == 2   # highest access_count=5

    @pytest.mark.asyncio
    async def test_keep_latest_selects_by_created_at(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(side_effect=[self._make_strategy_rows(), self._make_strategy_sim_rows()])
        pool = _make_pool(conn)
        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.compact_memories(
                CompactParams(dry_run=True, strategy="keep_latest")
            )
        assert result.plan[0].canonical_id == 3   # newest created_at=2026-06-01

    @pytest.mark.asyncio
    async def test_keep_most_comprehensive_selects_by_content_length(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(side_effect=[self._make_strategy_rows(), self._make_strategy_sim_rows()])
        pool = _make_pool(conn)
        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.compact_memories(
                CompactParams(dry_run=True, strategy="keep_most_comprehensive")
            )
        assert result.plan[0].canonical_id == 2   # longest content


class TestCompactMemoriesScope:
    """Scope filtering: project:X, type:Y."""

    @pytest.mark.asyncio
    async def test_project_scope_resolves_index(self):
        conn = AsyncMock()
        # Resolve project → index_id=42
        conn.fetchrow = AsyncMock(return_value=MagicMock(__getitem__=lambda s, k: 42))
        rows = [_make_memory_row(i, index_id=42) for i in range(1, 3)]
        sim_rows = [_make_sim_row(1, 2, 0.95)]
        conn.fetch = AsyncMock(side_effect=[rows, sim_rows])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.compact_memories(
                CompactParams(scope="project:myproject", dry_run=True)
            )

        assert result.clusters_found == 1

    @pytest.mark.asyncio
    async def test_project_scope_missing_returns_empty_no_side_effect(self):
        """Project doesn't exist → no-op result; must NOT auto-create an index row.

        Regression: previously used _resolve_index_id which INSERTed a new
        memory_indexes row as a side effect of a dry-run lookup.
        """
        conn = AsyncMock()
        # Project lookup returns None (project does not exist)
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetch = AsyncMock()
        conn.execute = AsyncMock()
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.compact_memories(
                CompactParams(scope="project:does-not-exist", dry_run=True)
            )

        assert result.clusters_found == 0
        assert result.memories_deleted == 0
        assert result.memories_kept == []
        assert result.deleted_ids == []
        assert result.plan == []
        # Only a SELECT should have happened — no INSERT/UPDATE side effects.
        conn.fetchrow.assert_awaited_once()
        select_sql = conn.fetchrow.await_args[0][0]
        assert "SELECT" in select_sql
        assert "INSERT" not in select_sql
        conn.fetch.assert_not_awaited()
        conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_type_scope(self):
        conn = AsyncMock()
        rows = [_make_memory_row(i) for i in range(1, 3)]
        sim_rows = [_make_sim_row(1, 2, 0.90)]
        conn.fetch = AsyncMock(side_effect=[rows, sim_rows])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.compact_memories(
                CompactParams(scope="type:session_summary", dry_run=True)
            )

        assert result.clusters_found == 1


class TestCompactMemoriesDoNotCompact:
    """Memories with do_not_compact=True metadata must be excluded from compaction."""

    @pytest.mark.asyncio
    async def test_do_not_compact_memory_excluded_from_candidates(self):
        """A memory with do_not_compact=True should not appear in compaction candidates.

        The SQL filter is applied at the DB layer, so we test that the filter string
        contains the do_not_compact exclusion clause.
        """
        from open_brain.data_layer.postgres import _compact_lifecycle_filter
        assert "do_not_compact" in _compact_lifecycle_filter, (
            "_compact_lifecycle_filter must exclude memories with do_not_compact=true"
        )
        assert "'true'" in _compact_lifecycle_filter, (
            "_compact_lifecycle_filter must use string 'true' for JSONB ->> text comparison"
        )

    @pytest.mark.asyncio
    async def test_do_not_compact_memory_not_deleted_when_near_duplicate(self):
        """A do_not_compact memory should NOT be deleted even if a near-duplicate exists.

        Scenario: memory 1 (do_not_compact=True) and memory 2 (normal) are near-dupes.
        The DB query excludes memory 1, so only memory 2 is fetched. No cluster forms
        (only 1 candidate), so nothing is deleted.
        """
        conn = AsyncMock()
        # Only memory 2 returned (memory 1 excluded by SQL do_not_compact filter)
        rows = [_make_memory_row(2, access_count=5)]
        conn.fetch = AsyncMock(return_value=rows)
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.compact_memories(CompactParams(dry_run=True))

        assert result.clusters_found == 0
        assert result.memories_deleted == 0
        assert 2 in result.memories_kept
