"""Tests for WeeklyBriefing digest — TDD Red-Green cycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from open_brain.data_layer.interface import Memory, SearchResult

NOW = datetime(2026, 4, 3, 12, 0, 0, tzinfo=UTC)


def _make_memory(
    mid: int = 1,
    *,
    type: str = "observation",
    created_at: datetime | None = None,
    last_accessed_at: datetime | None = None,
    access_count: int = 0,
    metadata: dict | None = None,
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
        metadata=metadata or {},
        priority=0.5,
        stability="stable",
        access_count=access_count,
        last_accessed_at=last_accessed_at.isoformat() if last_accessed_at else None,
        created_at=created_at.isoformat(),
        updated_at=created_at.isoformat(),
    )


# ─── AK1: All 6 sections present ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_briefing_sections():
    """AK1: Result has all 6 required top-level keys."""
    from open_brain.digest import generate_weekly_briefing

    dl = AsyncMock()
    dl.search.return_value = SearchResult(results=[], total=0)

    result = await generate_weekly_briefing(dl, weeks_back=1)
    from dataclasses import asdict
    data = asdict(result)

    assert "period" in data
    assert "memory_counts" in data
    assert "top_entities" in data
    assert "theme_trends" in data
    assert "open_loops" in data
    assert "cross_project_connections" in data
    assert "decay_warnings" in data


# ─── AK2: Entity frequency aggregation ───────────────────────────────────────

@pytest.mark.asyncio
async def test_briefing_entities():
    """AK2: Entity counts aggregated correctly from metadata."""
    from open_brain.digest import generate_weekly_briefing

    memories = [
        _make_memory(mid=1, metadata={"entities": {"people": ["Alice", "Bob"], "tech": ["Python"]}}),
        _make_memory(mid=2, metadata={"entities": {"people": ["Alice"], "tech": ["Python", "asyncpg"]}}),
        _make_memory(mid=3, metadata={"entities": {"people": ["Charlie"]}}),
    ]

    dl = AsyncMock()
    # asyncio.gather fires current, previous, decay in parallel — side_effect covers all 3
    dl.search.side_effect = [
        SearchResult(results=memories, total=3),  # current
        SearchResult(results=[], total=0),         # previous
        SearchResult(results=memories, total=3),   # decay
    ]

    result = await generate_weekly_briefing(dl, weeks_back=1)

    people = {e["name"]: e["freq"] for e in result.top_entities["people"]}
    assert people["Alice"] == 2
    assert people["Bob"] == 1
    assert people["Charlie"] == 1

    tech = {e["name"]: e["freq"] for e in result.top_entities["tech"]}
    assert tech["Python"] == 2
    assert tech["asyncpg"] == 1


# ─── AK3: Theme trends ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_briefing_trends():
    """AK3: Entity in current but not previous → emerging; in previous not current → declining."""
    from open_brain.digest import generate_weekly_briefing

    current_memories = [
        _make_memory(mid=1, metadata={"entities": {"tech": ["Rust", "Python"]}}),
        _make_memory(mid=2, metadata={"entities": {"tech": ["Rust"]}}),
    ]
    previous_memories = [
        _make_memory(mid=3, metadata={"entities": {"tech": ["Python", "Java"]}}),
    ]

    dl = AsyncMock()
    dl.search.side_effect = [
        SearchResult(results=current_memories, total=2),   # current
        SearchResult(results=previous_memories, total=1),  # previous
        SearchResult(results=current_memories, total=2),   # decay
    ]

    result = await generate_weekly_briefing(dl, weeks_back=1)

    emerging_names = [e["name"] for e in result.theme_trends["emerging"]]
    declining_names = [e["name"] for e in result.theme_trends["declining"]]

    # Rust: appears 2x in current, 0x in previous → emerging
    assert "Rust" in emerging_names
    # Java: in previous not in current → declining
    assert "Java" in declining_names


# ─── AK4: Open loops ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_briefing_open_loops():
    """AK4: Meeting with action_items appears in open_loops."""
    from open_brain.digest import generate_weekly_briefing

    meeting = _make_memory(
        mid=5,
        type="meeting",
        title="Sprint Planning",
        metadata={"action_items": ["Fix bug #123", "Deploy to prod"]},
    )
    obs = _make_memory(mid=6, type="observation", metadata={})

    dl = AsyncMock()
    dl.search.side_effect = [
        SearchResult(results=[meeting, obs], total=2),  # current
        SearchResult(results=[], total=0),               # previous
        SearchResult(results=[meeting, obs], total=2),   # decay
    ]

    result = await generate_weekly_briefing(dl, weeks_back=1)

    loop_ids = [ol["memory_id"] for ol in result.open_loops]
    assert 5 in loop_ids

    loop = next(ol for ol in result.open_loops if ol["memory_id"] == 5)
    assert "Fix bug #123" in loop["action_items"]
    assert loop["title"] == "Sprint Planning"


# ─── AK5: Decay warnings ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_briefing_decay():
    """AK5: Memory accessed 40 days ago with access_count=1 → decay warning; recent memory does not."""
    from open_brain.digest import generate_weekly_briefing

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
        created_at=NOW - timedelta(days=2),
        last_accessed_at=NOW - timedelta(days=1),
        access_count=5,
    )

    dl = AsyncMock()
    dl.search.side_effect = [
        SearchResult(results=[stale_memory, recent_memory], total=2),  # current
        SearchResult(results=[], total=0),                               # previous
        SearchResult(results=[stale_memory, recent_memory], total=2),   # decay
    ]

    result = await generate_weekly_briefing(dl, weeks_back=1)

    decay_ids = [w["memory_id"] for w in result.decay_warnings]
    assert 10 in decay_ids
    assert 11 not in decay_ids

    warning = next(w for w in result.decay_warnings if w["memory_id"] == 10)
    assert warning["days_unaccessed"] >= 30
    assert warning["access_count"] == 1


# ─── AK6: Empty database ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_briefing_empty():
    """AK6: Empty search results → valid briefing with zero counts and empty lists."""
    from open_brain.digest import generate_weekly_briefing

    dl = AsyncMock()
    dl.search.return_value = SearchResult(results=[], total=0)

    result = await generate_weekly_briefing(dl, weeks_back=1)

    assert result.memory_counts["current"] == 0
    assert result.memory_counts["previous"] == 0
    assert result.top_entities == {}
    assert result.theme_trends["emerging"] == []
    assert result.theme_trends["declining"] == []


# ─── AK7: Cross-project connections use index_id ─────────────────────────────

@pytest.mark.asyncio
async def test_cross_project_connections_use_index_id():
    """Cross-project grouping uses mem.index_id, not metadata['project']."""
    from open_brain.digest import generate_weekly_briefing

    # Two memories from index_id=1, one from index_id=2; no 'project' in metadata
    memories = [
        _make_memory(mid=1, metadata={"entities": {"tech": ["Python"]}}),   # index_id=1 (mid=1)
        _make_memory(mid=1, metadata={"entities": {"tech": ["Python"]}}),   # index_id=1 again
        _make_memory(mid=2, metadata={}),                                    # index_id=2
    ]
    # Override index_id to be distinct (mid is used as index_id in _make_memory)
    memories[0] = memories[0].__class__(
        **{**memories[0].__dict__, "index_id": 10}
    )
    memories[1] = memories[1].__class__(
        **{**memories[1].__dict__, "index_id": 10}
    )
    memories[2] = memories[2].__class__(
        **{**memories[2].__dict__, "index_id": 20}
    )

    dl = AsyncMock()
    dl.search.side_effect = [
        SearchResult(results=memories, total=3),
        SearchResult(results=[], total=0),
        SearchResult(results=memories, total=3),
    ]

    result = await generate_weekly_briefing(dl, weeks_back=1)

    # Should produce 2 cross-project groups: index_id=10 (2 memories) and index_id=20 (1 memory)
    assert len(result.cross_project_connections) == 2
    projects = {c["project"]: c["memory_count"] for c in result.cross_project_connections}
    assert projects["10"] == 2
    assert projects["20"] == 1
    assert result.open_loops == []
    assert result.cross_project_connections == []
    assert result.decay_warnings == []


# ─── Validation ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_briefing_weeks_back_zero_raises():
    """weeks_back=0 raises ValueError."""
    from open_brain.digest import generate_weekly_briefing

    dl = AsyncMock()
    with pytest.raises(ValueError, match="weeks_back must be >= 1"):
        await generate_weekly_briefing(dl, weeks_back=0)


# ─── Scenario: Full mixed-type, multi-week DB ─────────────────────────────────

@pytest.mark.asyncio
async def test_briefing_full_scenario():
    """Scenario: 20+ memories of mixed types spanning 2+ weeks — briefing has non-zero counts."""
    from open_brain.digest import generate_weekly_briefing

    # 5 meetings with attendees and action_items
    meetings = [
        _make_memory(
            mid=i,
            type="meeting",
            title=f"Meeting {i}",
            created_at=NOW - timedelta(days=i * 2),
            metadata={
                "entities": {"people": [f"Person{i}", "Alice"]},
                "action_items": [f"Action {i}"],
            },
        )
        for i in range(1, 6)
    ]
    # 3 decisions
    decisions = [
        _make_memory(
            mid=10 + i,
            type="decision",
            title=f"Decision {i}",
            created_at=NOW - timedelta(days=i * 3),
            metadata={"entities": {"tech": ["Python", f"Tech{i}"]}},
        )
        for i in range(1, 4)
    ]
    # 4 persons
    persons = [
        _make_memory(
            mid=20 + i,
            type="person",
            title=f"Person Record {i}",
            created_at=NOW - timedelta(days=i + 5),
            metadata={"entities": {"people": [f"Person{i}", "Bob"]}},
        )
        for i in range(1, 5)
    ]
    # 3 events
    events = [
        _make_memory(
            mid=30 + i,
            type="event",
            title=f"Event {i}",
            created_at=NOW - timedelta(days=i + 10),
            metadata={"entities": {"places": [f"Place{i}"]}},
        )
        for i in range(1, 4)
    ]
    # 5 observations (older, ~2 weeks back)
    observations = [
        _make_memory(
            mid=40 + i,
            type="observation",
            title=f"Observation {i}",
            created_at=NOW - timedelta(days=14 + i),
            metadata={"entities": {"tech": ["Python"]}},
        )
        for i in range(1, 6)
    ]

    all_mems = meetings + decisions + persons + events + observations
    assert len(all_mems) >= 20

    dl = AsyncMock()
    dl.search.side_effect = [
        SearchResult(results=all_mems, total=len(all_mems)),   # current
        SearchResult(results=[], total=0),                      # previous
        SearchResult(results=all_mems, total=len(all_mems)),   # decay
    ]

    result = await generate_weekly_briefing(dl, weeks_back=2)

    assert result.memory_counts["current"] == len(all_mems)
    assert len(result.top_entities) > 0
    people = {e["name"]: e["freq"] for e in result.top_entities.get("people", [])}
    assert people.get("Alice", 0) > 0

    loop_ids = [ol["memory_id"] for ol in result.open_loops]
    assert any(mid in loop_ids for mid in range(1, 6))

    assert result.memory_counts["by_type"].get("meeting", 0) == 5


# ─── Scenario: Single-type DB still produces output ──────────────────────────

@pytest.mark.asyncio
async def test_briefing_single_type():
    """Scenario: 5 memories all of type 'observation' → valid briefing structure."""
    from open_brain.digest import generate_weekly_briefing

    memories = [
        _make_memory(
            mid=i,
            type="observation",
            title=f"Observation {i}",
            metadata={"entities": {"tech": ["Python"]}},
        )
        for i in range(1, 6)
    ]

    dl = AsyncMock()
    dl.search.side_effect = [
        SearchResult(results=memories, total=5),   # current
        SearchResult(results=[], total=0),          # previous
        SearchResult(results=memories, total=5),   # decay
    ]

    result = await generate_weekly_briefing(dl, weeks_back=1)

    assert result.memory_counts["current"] == 5
    assert result.memory_counts["by_type"] == {"observation": 5}
    assert len(result.top_entities) > 0
    assert result.theme_trends["emerging"] is not None
    assert result.theme_trends["declining"] is not None
    assert result.open_loops == []
    assert result.decay_warnings == []


# ─── Scenario: Cross-project aggregation ─────────────────────────────────────

@pytest.mark.asyncio
async def test_briefing_cross_project():
    """Scenario: Memories from different projects → cross_project_connections aggregated."""
    from open_brain.digest import generate_weekly_briefing

    project_a = [
        _make_memory(
            mid=i,
            title=f"Alpha Memory {i}",
            metadata={"project": "alpha", "entities": {"tech": ["Rust"]}},
        )
        for i in range(1, 5)
    ]
    project_b = [
        _make_memory(
            mid=10 + i,
            title=f"Beta Memory {i}",
            metadata={"project": "beta", "entities": {"tech": ["Go", "Python"]}},
        )
        for i in range(1, 4)
    ]
    all_mems = project_a + project_b

    dl = AsyncMock()
    dl.search.side_effect = [
        SearchResult(results=all_mems, total=len(all_mems)),   # current
        SearchResult(results=[], total=0),                      # previous
        SearchResult(results=all_mems, total=len(all_mems)),   # decay
    ]

    result = await generate_weekly_briefing(dl, weeks_back=1)

    projects = {c["project"]: c for c in result.cross_project_connections}
    assert "alpha" in projects
    assert "beta" in projects
    assert projects["alpha"]["memory_count"] == 4
    assert projects["beta"]["memory_count"] == 3
    assert "Rust" in projects["alpha"]["common_entities"]
