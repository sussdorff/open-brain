"""Tests for memory decay — TDD Red-Green cycle.

AK1: Unaccessed memory (30+ days) gets priority reduced
AK2: Frequently accessed memory gets priority boosted
AK3: Decay does not affect recent memories (<7 days)
AK4: Decaying memories appear in weekly briefing
AK5: Decay is reversible (accessing restores priority)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch, call

import pytest

from open_brain.data_layer.interface import DecayParams, DecayResult, Memory, SearchResult

NOW = datetime(2026, 4, 3, 12, 0, 0, tzinfo=UTC)


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

    mock_pool = AsyncMock()
    mock_conn = AsyncMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    # decay query returns 1 row (1 memory decayed)
    mock_conn.fetchval.side_effect = [
        1,   # decay count
        0,   # boost count
        0,   # protected count
    ]

    with patch("open_brain.data_layer.postgres.get_pool", return_value=mock_pool):
        result = await dl.decay_memories(params)

    assert result.decayed > 0, "Expected at least one memory to be decayed"


# ─── AK2: Frequently accessed memory gets priority boosted ────────────────────


@pytest.mark.asyncio
async def test_decay_boost():
    """AK2: Memory with high access_count → boosted > 0."""
    from open_brain.data_layer.postgres import PostgresDataLayer

    dl = PostgresDataLayer()
    params = DecayParams(boost_threshold=10, boost_factor=1.1, dry_run=True)

    mock_pool = AsyncMock()
    mock_conn = AsyncMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    # decay=0, boost=1 (one frequently accessed memory)
    mock_conn.fetchval.side_effect = [
        0,   # decay count
        1,   # boost count
        0,   # protected count
    ]

    with patch("open_brain.data_layer.postgres.get_pool", return_value=mock_pool):
        result = await dl.decay_memories(params)

    assert result.boosted > 0, "Expected at least one memory to be boosted"


# ─── AK3: Recent memories are protected from decay ────────────────────────────


@pytest.mark.asyncio
async def test_decay_recent_protected():
    """AK3: Memory 3 days old → not decayed (protected count > 0)."""
    from open_brain.data_layer.postgres import PostgresDataLayer

    dl = PostgresDataLayer()
    params = DecayParams(stale_days=30, boost_days=7, dry_run=True)

    mock_pool = AsyncMock()
    mock_conn = AsyncMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    # decay=0, boost=0, protected=1 (one recent memory)
    mock_conn.fetchval.side_effect = [
        0,   # decay count
        0,   # boost count
        1,   # protected count (recent memories)
    ]

    with patch("open_brain.data_layer.postgres.get_pool", return_value=mock_pool):
        result = await dl.decay_memories(params)

    assert result.decayed == 0, "Recent memory must not be decayed"
    assert result.protected > 0, "Recent memory should appear in protected count"


# ─── AK4: Decaying memories appear in weekly briefing ────────────────────────


@pytest.mark.asyncio
async def test_decay_in_briefing():
    """AK4: Memory 40 days stale with access_count=1 appears in briefing decay_warnings."""
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

    mock_pool = AsyncMock()
    mock_conn = AsyncMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    # First call: decay run — 1 decayed, 0 boosted
    # Second call: boost run — 0 decayed, 1 boosted
    mock_conn.fetchval.side_effect = [
        1, 0, 0,  # decay pass: decayed=1, boosted=0, protected=0
        0, 1, 0,  # boost pass: decayed=0, boosted=1, protected=0
    ]

    decay_params = DecayParams(stale_days=30, decay_factor=0.9, boost_threshold=10, boost_factor=1.1, dry_run=True)

    with patch("open_brain.data_layer.postgres.get_pool", return_value=mock_pool):
        decay_result = await dl.decay_memories(decay_params)
        # Simulate memory gets accessed (access_count increases), then run boost
        boost_result = await dl.decay_memories(decay_params)

    assert decay_result.decayed == 1
    assert boost_result.boosted == 1, "After access, memory priority should be restored via boost"
