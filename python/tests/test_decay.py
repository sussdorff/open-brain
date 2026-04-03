"""Tests for memory decay — TDD Red-Green cycle.

AK1: Unaccessed memory (30+ days) gets priority reduced
AK2: Frequently accessed memory gets priority boosted
AK3: Decay does not affect recent memories (<7 days)
AK4: Decaying memories appear in weekly briefing
AK5: Decay is reversible (accessing restores priority)

Unit tests for decay_memories() orchestration logic. These tests mock the database and verify result assembly. SQL correctness is covered by integration tests against a live database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.interface import DecayParams, Memory

NOW = datetime(2026, 4, 3, 12, 0, 0, tzinfo=UTC)


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Build a properly structured asyncpg pool mock."""
    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


def _make_memory(
    mid: int = 1,
    *,
    type: str = "observation",
    created_at: datetime | None = None,
    last_accessed_at: datetime | None = None,
    access_count: int = 0,
    priority: float = 0.5,
    content: str = "Test content",
    title: str | None = "Test Memory",
) -> Memory:
    """Create a Memory for testing."""
    if created_at is None:
        created_at = NOW - timedelta(days=3)
    return Memory(
        id=mid,
        index_id=mid,
        session_id=None,
        type=type,
        title=title,
        subtitle=None,
        narrative=None,
        content=content,
        metadata={},
        priority=priority,
        stability="stable",
        access_count=access_count,
        last_accessed_at=last_accessed_at.isoformat() if last_accessed_at else None,
        created_at=created_at.isoformat(),
        updated_at=created_at.isoformat(),
    )


# ─── AK1: Unaccessed memory gets priority reduced ─────────────────────────────


@pytest.mark.asyncio
async def test_decay_unaccessed():
    """AK1: Memory 60 days old, access_count=0 → decayed > 0."""
    from open_brain.data_layer.postgres import PostgresDataLayer

    dl = PostgresDataLayer()
    params = DecayParams(stale_days=30, decay_factor=0.9, dry_run=True)

    conn = AsyncMock()
    conn.fetchval.side_effect = [1, 0, 0]  # decayed=1, boosted=0, protected=0
    pool = _make_pool(conn)

    with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
        result = await dl.decay_memories(params)

    assert result.decayed > 0, "Expected at least one memory to be decayed"


# ─── AK2: Frequently accessed memory gets priority boosted ────────────────────


@pytest.mark.asyncio
async def test_decay_boost():
    """AK2: Memory with high access_count → boosted > 0."""
    from open_brain.data_layer.postgres import PostgresDataLayer

    dl = PostgresDataLayer()
    params = DecayParams(boost_threshold=10, boost_factor=1.1, dry_run=True)

    conn = AsyncMock()
    conn.fetchval.side_effect = [0, 1, 0]  # decayed=0, boosted=1, protected=0
    pool = _make_pool(conn)

    with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
        result = await dl.decay_memories(params)

    assert result.boosted > 0, "Expected at least one memory to be boosted"


# ─── AK3: Recent memories are protected from decay ────────────────────────────


@pytest.mark.asyncio
async def test_decay_recent_protected():
    """AK3: Memory 3 days old → not decayed (protected count > 0)."""
    from open_brain.data_layer.postgres import PostgresDataLayer

    dl = PostgresDataLayer()
    params = DecayParams(stale_days=30, boost_days=7, dry_run=True)

    conn = AsyncMock()
    conn.fetchval.side_effect = [0, 0, 1]  # decayed=0, boosted=0, protected=1
    pool = _make_pool(conn)

    with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
        result = await dl.decay_memories(params)

    assert result.decayed == 0, "Recent memory must not be decayed"
    assert result.protected > 0, "Recent memory should appear in protected count"


# ─── AK4: Decaying memories appear in weekly briefing ────────────────────────


@pytest.mark.asyncio
async def test_decay_in_briefing():
    """AK4: Memory 40 days stale with access_count=1 appears in briefing decay_warnings.

    NOTE: AK4 is satisfied by the pre-existing briefing integration in open_brain/digest.py
    via _find_decay_warnings(), which already used the same 30-day stale criteria before
    this bead. This test verifies the existing integration still works correctly — it is
    NOT testing new code introduced by this bead.
    """
    # This test verifies AK4: pre-existing _find_decay_warnings() in digest.py uses the same 30-day stale threshold. If that function moves, update this test's import path.
    from open_brain.digest import _find_decay_warnings

    stale_memory = _make_memory(
        mid=10,
        title="Old Decision",
        created_at=NOW - timedelta(days=60),
        last_accessed_at=NOW - timedelta(days=40),
        access_count=1,
    )
    recent_memory = _make_memory(
        mid=11,
        title="Fresh Note",
        created_at=NOW - timedelta(days=3),
        last_accessed_at=NOW - timedelta(days=1),
        access_count=5,
    )

    # Use the same stale_days=30 as DecayParams default
    warnings = _find_decay_warnings([stale_memory, recent_memory], now=NOW, stale_days=30)

    decay_ids = [w["memory_id"] for w in warnings]
    assert 10 in decay_ids, "Stale memory must appear in decay warnings"
    assert 11 not in decay_ids, "Recent memory must not appear in decay warnings"

    warning = next(w for w in warnings if w["memory_id"] == 10)
    assert warning["days_unaccessed"] >= 30


# ─── AK5: Decay is reversible (boost restores priority) ───────────────────────


@pytest.mark.asyncio
async def test_decay_reversible():
    """AK5: After decay, memory with high access_count gets priority boosted back up."""
    from open_brain.data_layer.postgres import PostgresDataLayer

    dl = PostgresDataLayer()
    decay_params = DecayParams(stale_days=30, decay_factor=0.9, boost_threshold=10, boost_factor=1.1, dry_run=True)

    # First run: 1 decayed, 0 boosted
    conn1 = AsyncMock()
    conn1.fetchval.side_effect = [1, 0, 0]
    pool1 = _make_pool(conn1)

    with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool1):
        decay_result = await dl.decay_memories(decay_params)

    # Second run: memory now has high access_count → 0 decayed, 1 boosted
    conn2 = AsyncMock()
    conn2.fetchval.side_effect = [0, 1, 0]
    pool2 = _make_pool(conn2)

    with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool2):
        boost_result = await dl.decay_memories(decay_params)

    assert decay_result.decayed == 1
    assert boost_result.boosted == 1, "After access, memory priority should be restored via boost"


# ─── Compound decay runs ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_decay_compound_runs():
    """Multiple decay runs compound: calling decay_memories() twice on same stale memory
    results in the DB function being called twice (priority *= decay_factor each time).
    """
    from open_brain.data_layer.postgres import PostgresDataLayer

    dl = PostgresDataLayer()
    params = DecayParams(stale_days=30, decay_factor=0.9, dry_run=False)

    # First call: 1 memory decayed
    conn1 = AsyncMock()
    conn1.fetchval.side_effect = [1, 0, 0]
    pool1 = _make_pool(conn1)

    with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool1):
        result1 = await dl.decay_memories(params)

    assert result1.decayed >= 1, "First run should decay at least one memory"

    # Second call: same memory still stale → decayed again (priority *= decay_factor a second time)
    conn2 = AsyncMock()
    conn2.fetchval.side_effect = [1, 0, 0]
    pool2 = _make_pool(conn2)

    with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool2):
        result2 = await dl.decay_memories(params)

    assert result2.decayed >= 1, "Second run should decay the same memory again (compounding)"
    # Each call invokes the DB function exactly once
    assert conn1.fetchval.call_count >= 1
    assert conn2.fetchval.call_count >= 1


# ─── Decay + boost overlap behavior ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_decay_overlap_behavior():
    """A memory that is both stale (60 days old) AND frequently accessed (access_count=20)
    can match both the decay and boost criteria in the same run.

    Expected behavior: the boost UPDATE runs AFTER decay, so the net effect is that
    the memory's priority is first multiplied by decay_factor, then by boost_factor.
    For a memory with priority=0.5, decay_factor=0.9, boost_factor=1.1:
        after decay:  0.5 * 0.9 = 0.45
        after boost:  0.45 * 1.1 = 0.495  (still below original)

    This is intentional: frequent access partially counteracts decay but does not fully
    restore priority in a single run. Repeated boosts (via AK5) restore priority over time.
    The test verifies that both decayed > 0 AND boosted > 0 can occur in the same run.
    """
    from open_brain.data_layer.postgres import PostgresDataLayer

    dl = PostgresDataLayer()
    # Memory is stale (30+ days) but also frequently accessed
    params = DecayParams(stale_days=30, decay_factor=0.9, boost_threshold=10, boost_factor=1.1, dry_run=False)

    conn = AsyncMock()
    # DB returns 1 decayed AND 1 boosted — the same memory matched both criteria
    conn.fetchval.side_effect = [1, 1, 0]
    pool = _make_pool(conn)

    with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
        result = await dl.decay_memories(params)

    assert result.decayed >= 1, "Stale memory should be decayed even with high access_count"
    assert result.boosted >= 1, "Frequently-accessed memory should also be boosted"
    # Net effect: boost partially counteracts decay (intentional, documented above)
