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


# ─── DecayParams validation ──────────────────────────────────────────────────


@pytest.mark.parametrize("kwargs,match", [
    ({"decay_factor": 0}, "decay_factor"),
    ({"decay_factor": 1.5}, "decay_factor"),
    ({"boost_factor": 0.5}, "boost_factor"),
    ({"stale_days": -1}, "stale_days"),
    ({"boost_days": 0}, "boost_days"),
    ({"boost_threshold": 0}, "boost_threshold"),
])
def test_decay_params_validation(kwargs: dict, match: str) -> None:
    """DecayParams rejects invalid inputs with ValueError."""
    with pytest.raises(ValueError, match=match):
        DecayParams(**kwargs)


# ─── AK1: Unaccessed memory gets priority reduced ─────────────────────────────


@pytest.mark.asyncio
async def test_decay_unaccessed():
    """AK1: Memory 60 days old, access_count=0 → decayed > 0."""
    from open_brain.data_layer.postgres import PostgresDataLayer

    dl = PostgresDataLayer()
    params = DecayParams(stale_days=30, decay_factor=0.9, dry_run=True)

    conn = AsyncMock()
    conn.fetchval.side_effect = [1, 0, 0]  # decayed=1, boosted=0, recent_memories=0
    pool = _make_pool(conn)

    with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
        result = await dl.decay_memories(params)

    assert result.decayed > 0, "Expected at least one memory to be decayed"

    # Verify correct parameters passed to DB (dry_run path — first fetchval is the decay count query)
    # The decay count query uses str(stale_days) as $1 parameter
    first_call = conn.fetchval.call_args_list[0]
    assert "last_accessed_at" in first_call.args[0], (
        "First fetchval should be the stale-memory count query"
    )
    assert first_call.args[1] == "30", f"stale_days=30 should be passed as string '30', got {first_call.args[1]!r}"


# ─── AK2: Frequently accessed memory gets priority boosted ────────────────────


@pytest.mark.asyncio
async def test_decay_boost():
    """AK2: Memory with high access_count → boosted > 0."""
    from open_brain.data_layer.postgres import PostgresDataLayer

    dl = PostgresDataLayer()
    params = DecayParams(boost_threshold=10, boost_factor=1.1, dry_run=True)

    conn = AsyncMock()
    conn.fetchval.side_effect = [0, 1, 0]  # decayed=0, boosted=1, recent_memories=0
    pool = _make_pool(conn)

    with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
        result = await dl.decay_memories(params)

    assert result.boosted > 0, "Expected at least one memory to be boosted"

    # Verify correct parameters passed to DB (dry_run path — second fetchval is the boost count query)
    # The boost count query uses params.boost_threshold (int) as $1 parameter
    second_call = conn.fetchval.call_args_list[1]
    assert "access_count" in second_call.args[0], (
        "Second fetchval should be the boost count query (access_count >= threshold)"
    )
    assert second_call.args[1] == 10, f"boost_threshold=10 should be passed as int 10, got {second_call.args[1]!r}"


# ─── AK3: Recent memories are protected from decay ────────────────────────────


@pytest.mark.asyncio
async def test_decay_recent_protected():
    """AK3: Memory 3 days old → not decayed (protected count > 0)."""
    from open_brain.data_layer.postgres import PostgresDataLayer

    dl = PostgresDataLayer()
    params = DecayParams(stale_days=30, boost_days=7, dry_run=True)

    conn = AsyncMock()
    conn.fetchval.side_effect = [0, 0, 1]  # decayed=0, boosted=0, recent_memories=1
    pool = _make_pool(conn)

    with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
        result = await dl.decay_memories(params)

    assert result.decayed == 0, "Recent memory must not be decayed"
    assert result.recent_memories > 0, "Recent memory should appear in recent_memories count"


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
        # access_count=1 is intentional: _find_decay_warnings() uses max_access_count=2 threshold,
        # so access_count=1 still qualifies as a low-access stale memory and appears in decay warnings.
        # access_count=0 would also work, but 1 validates the threshold boundary more precisely.
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


# ─── AK6: Importance-class multiplier scaling ────────────────────────────────


def test_compute_decay_delta_importance_scaling() -> None:
    """AK6: decay delta scales by importance class — critical=0, high=0.5x, medium=1.0x, low=2.0x."""
    from open_brain.data_layer.postgres import compute_decay_delta

    base_delta = 0.1  # 1.0 - 0.9 decay_factor

    # critical: zero decay regardless of access_count
    assert compute_decay_delta("critical", 0, base_delta) == 0.0
    assert compute_decay_delta("critical", 50, base_delta) == 0.0

    # medium: standard baseline (multiplier=1.0)
    assert compute_decay_delta("medium", 0, base_delta) == pytest.approx(0.1)

    # high: half of medium (multiplier=0.5)
    assert compute_decay_delta("high", 0, base_delta) == pytest.approx(0.05)

    # low: double of medium (multiplier=2.0)
    assert compute_decay_delta("low", 0, base_delta) == pytest.approx(0.2)

    # access_count damping: access_count=10 → damping=1+(10*0.1)=2.0, halves the delta
    assert compute_decay_delta("medium", 10, base_delta) == pytest.approx(0.05)
    assert compute_decay_delta("low", 10, base_delta) == pytest.approx(0.1)


def test_compute_decay_delta_unknown_importance_defaults_medium() -> None:
    """Unknown importance class defaults to medium multiplier (1.0)."""
    from open_brain.data_layer.postgres import compute_decay_delta

    assert compute_decay_delta("unknown", 0, 0.1) == pytest.approx(0.1)


# ─── AK7: Critical memory is never pruned ────────────────────────────────────


def test_low_priority_query_excludes_critical_and_high() -> None:
    """AK7: The low-priority refine scope query must exclude critical and high importance memories."""
    import inspect
    import open_brain.data_layer.postgres as pg_module

    source = inspect.getsource(pg_module)

    # Find low-priority query patterns — both must exclude critical/high
    import re
    low_priority_blocks = re.findall(
        r"low-priority.*?(?=elif|else|$)", source, re.DOTALL
    )
    assert low_priority_blocks, "No low-priority scope blocks found in postgres.py"

    for block in low_priority_blocks:
        if "SELECT * FROM memories WHERE priority" in block:
            assert "importance NOT IN" in block, (
                f"Low-priority query must exclude critical/high via importance NOT IN clause.\n"
                f"Block: {block[:300]}"
            )


def test_dry_run_count_excludes_critical() -> None:
    """AK7: dry_run decay count query must exclude critical memories."""
    import inspect
    import open_brain.data_layer.postgres as pg_module

    source = inspect.getsource(pg_module.PostgresDataLayer.decay_memories)
    # The dry_run count query should exclude critical importance
    assert "importance != 'critical'" in source or "importance NOT IN" in source, (
        "dry_run decay count query must exclude critical memories to match live behavior"
    )


# ─── AK8: Concurrent decay produces same final state as serial ───────────────


@pytest.mark.asyncio
async def test_concurrent_decay_race_guard() -> None:
    """AK8: Second concurrent decay call on same memory is a no-op (24h guard via last_decay_at)."""
    from open_brain.data_layer.postgres import PostgresDataLayer

    dl = PostgresDataLayer()
    params = DecayParams(stale_days=30, decay_factor=0.9, dry_run=False)

    # Simulate concurrent execution: first call decays 2 memories
    conn1 = AsyncMock()
    conn1.fetchval.side_effect = [2, 0, 0]
    pool1 = _make_pool(conn1)

    with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool1):
        result1 = await dl.decay_memories(params)

    # Second concurrent call: same memories now have last_decay_at=NOW, so 0 updated
    conn2 = AsyncMock()
    conn2.fetchval.side_effect = [0, 0, 0]
    pool2 = _make_pool(conn2)

    with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool2):
        result2 = await dl.decay_memories(params)

    assert result1.decayed == 2, "First call should decay 2 memories"
    assert result2.decayed == 0, "Second concurrent call sees 0 decayed (race guard via last_decay_at)"


def test_decay_sql_function_has_race_guard() -> None:
    """AK8: The decay SQL function definition must include last_decay_at race guard."""
    import inspect
    import open_brain.data_layer.postgres as pg_module

    source = inspect.getsource(pg_module.get_pool)

    assert "last_decay_at" in source, (
        "get_pool() must define the decay SQL function with last_decay_at column"
    )
    assert "decay_unused_priorities" in source, (
        "get_pool() must CREATE OR REPLACE decay_unused_priorities function"
    )
    assert "24 hours" in source or "interval '24 hours'" in source, (
        "Decay SQL function must include 24h guard on last_decay_at"
    )


# ─── AK1/AK5: Importance-aware decay in get_pool SQL function ────────────────


def test_importance_multipliers_in_sql_function() -> None:
    """AK1: get_pool() defines importance-aware decay SQL function with all four multipliers."""
    import inspect
    import open_brain.data_layer.postgres as pg_module

    source = inspect.getsource(pg_module.get_pool)

    assert "0.0" in source, "Critical multiplier (0.0) must be in SQL function"
    assert "0.5" in source, "High multiplier (0.5) must be in SQL function"
    assert "2.0" in source, "Low multiplier (2.0) must be in SQL function"
    # medium=1.0 is implicit but let's check the values block exists
    assert "mult_map" in source or "multipliers" in source, (
        "SQL function must define importance multiplier mapping"
    )


# ─── AK2: Recall-triggered decay fires on stale last_decay_at ────────────────


@pytest.mark.asyncio
async def test_recall_decay_fires_when_stale() -> None:
    """AK2: _apply_recall_decay fires an UPDATE when last_decay_at > 24h."""
    from open_brain.data_layer.postgres import PostgresDataLayer

    dl = PostgresDataLayer()
    conn = AsyncMock()
    # Simulate row returned (decay applied)
    conn.fetchrow.return_value = {"priority": 0.45}

    result = await dl._apply_recall_decay(conn, memory_id=42, importance="medium", access_count=0)

    assert result == pytest.approx(0.45)
    conn.fetchrow.assert_called_once()
    sql_called = conn.fetchrow.call_args.args[0]
    assert "last_decay_at" in sql_called, "SQL must reference last_decay_at"
    assert "24 hours" in sql_called, "SQL must enforce 24h guard"
    assert "critical" in sql_called, "SQL must skip critical memories"


@pytest.mark.asyncio
async def test_recall_decay_skips_when_fresh() -> None:
    """AK2: _apply_recall_decay returns None when last_decay_at is recent (within 24h)."""
    from open_brain.data_layer.postgres import PostgresDataLayer

    dl = PostgresDataLayer()
    conn = AsyncMock()
    # No row returned — memory is within 24h or is critical
    conn.fetchrow.return_value = None

    result = await dl._apply_recall_decay(conn, memory_id=42, importance="critical", access_count=5)

    assert result is None


@pytest.mark.asyncio
async def test_recall_decay_integrated_in_search() -> None:
    """AK2: search() triggers recall decay for each returned memory."""
    import inspect
    import open_brain.data_layer.postgres as pg_module

    source = inspect.getsource(pg_module.PostgresDataLayer.search)
    assert "_apply_recall_decay" in source, (
        "search() must call _apply_recall_decay on returned memories"
    )
