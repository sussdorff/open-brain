"""Tests for evolution.py — Self-Improvement Loop: engagement tracking + weekly behavior proposals.

TDD Red-Green cycle per acceptance criterion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, call

import pytest

from open_brain.data_layer.interface import Memory, SaveMemoryParams, SaveMemoryResult, SearchResult

NOW = datetime(2026, 4, 3, 12, 0, 0, tzinfo=UTC)


def _make_memory(
    mid: int = 1,
    *,
    type: str = "briefing",
    created_at: datetime | None = None,
    metadata: dict | None = None,
    content: str = "Briefing content",
    title: str | None = "Test Briefing",
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
        access_count=0,
        last_accessed_at=None,
        created_at=created_at.isoformat(),
        updated_at=created_at.isoformat(),
    )


# ─── AK1: Briefing saved with user_responded metadata ─────────────────────────


def test_evolution_briefing_log():
    """AK1: When a briefing is saved, metadata must include briefing_type and user_responded."""
    # This test documents/validates the expected metadata shape for briefing memories.
    # The convention: any memory with type="briefing" MUST have these metadata keys.
    from open_brain.evolution import BRIEFING_METADATA_REQUIRED_KEYS

    required = BRIEFING_METADATA_REQUIRED_KEYS
    assert "briefing_type" in required
    assert "user_responded" in required


def test_evolution_briefing_metadata_shape():
    """AK1: Validate that a sample briefing metadata has the required keys."""
    from open_brain.evolution import validate_briefing_metadata

    valid_metadata = {"briefing_type": "weekly_digest", "user_responded": True}
    errors = validate_briefing_metadata(valid_metadata)
    assert errors == []

    missing_metadata = {"briefing_type": "weekly_digest"}
    errors = validate_briefing_metadata(missing_metadata)
    assert len(errors) > 0
    assert any("user_responded" in e for e in errors)

    empty_metadata: dict = {}
    errors = validate_briefing_metadata(empty_metadata)
    assert len(errors) == 2  # both keys missing


# ─── AK2: Weekly analysis counts response rates by type ───────────────────────


@pytest.mark.asyncio
async def test_evolution_weekly_analysis():
    """AK2: Response rates calculated correctly per briefing type."""
    from open_brain.evolution import EngagementReport, analyze_engagement

    briefings = [
        # weekly_digest: 2 responded, 1 not → 66.7%
        _make_memory(1, metadata={"briefing_type": "weekly_digest", "user_responded": True}),
        _make_memory(2, metadata={"briefing_type": "weekly_digest", "user_responded": True}),
        _make_memory(3, metadata={"briefing_type": "weekly_digest", "user_responded": False}),
        # daily_summary: 0 responded, 3 not → 0%
        _make_memory(4, metadata={"briefing_type": "daily_summary", "user_responded": False}),
        _make_memory(5, metadata={"briefing_type": "daily_summary", "user_responded": False}),
        _make_memory(6, metadata={"briefing_type": "daily_summary", "user_responded": False}),
    ]

    dl = AsyncMock()
    dl.search.return_value = SearchResult(results=briefings, total=len(briefings))

    report = await analyze_engagement(dl, days_back=7)

    assert isinstance(report, EngagementReport)
    assert report.total_briefings == 6
    assert report.period_days == 7

    by_type = {e.briefing_type: e for e in report.by_type}
    assert "weekly_digest" in by_type
    assert "daily_summary" in by_type

    wd = by_type["weekly_digest"]
    assert wd.total_count == 3
    assert wd.responded_count == 2
    assert abs(wd.response_rate - 2 / 3) < 0.001

    ds = by_type["daily_summary"]
    assert ds.total_count == 3
    assert ds.responded_count == 0
    assert ds.response_rate == 0.0


@pytest.mark.asyncio
async def test_evolution_weekly_analysis_project_filter():
    """AK2: analyze_engagement passes project filter to search."""
    from open_brain.evolution import analyze_engagement

    dl = AsyncMock()
    dl.search.return_value = SearchResult(results=[], total=0)

    await analyze_engagement(dl, days_back=7, project="my-project")

    # Verify search was called with project filter
    assert dl.search.called
    call_params = dl.search.call_args[0][0]
    assert call_params.project == "my-project"
    assert call_params.type == "briefing"


# ─── AK3: Low engagement identified and removal proposed ──────────────────────


@pytest.mark.asyncio
async def test_evolution_removal_proposal():
    """AK3: Type with < 30% response rate → removal proposed."""
    from open_brain.evolution import EngagementReport, EvolutionSuggestion, generate_suggestion
    from open_brain.evolution import BriefingEngagement

    # daily_summary has 10% response rate (well below 30%)
    report = EngagementReport(
        period_days=7,
        by_type=[
            BriefingEngagement("weekly_digest", total_count=10, responded_count=8, response_rate=0.8),
            BriefingEngagement("daily_summary", total_count=10, responded_count=1, response_rate=0.1),
        ],
        total_briefings=20,
        has_sufficient_data=True,
    )

    dl = AsyncMock()
    # No recent evolution suggestion
    dl.search.return_value = SearchResult(results=[], total=0)
    dl.save_memory.return_value = SaveMemoryResult(id=99, message="saved")

    suggestion = await generate_suggestion(report, dl)

    assert suggestion is not None
    assert isinstance(suggestion, EvolutionSuggestion)
    assert suggestion.action == "remove"
    assert suggestion.briefing_type == "daily_summary"
    assert suggestion.response_rate == 0.1
    assert "low" in suggestion.reason.lower() or "engagement" in suggestion.reason.lower()


@pytest.mark.asyncio
async def test_evolution_expansion_proposal():
    """AK3: All types >= 50% → expand top type."""
    from open_brain.evolution import EngagementReport, EvolutionSuggestion, generate_suggestion
    from open_brain.evolution import BriefingEngagement

    report = EngagementReport(
        period_days=7,
        by_type=[
            BriefingEngagement("weekly_digest", total_count=10, responded_count=9, response_rate=0.9),
            BriefingEngagement("daily_summary", total_count=10, responded_count=6, response_rate=0.6),
        ],
        total_briefings=20,
        has_sufficient_data=True,
    )

    dl = AsyncMock()
    dl.search.return_value = SearchResult(results=[], total=0)
    dl.save_memory.return_value = SaveMemoryResult(id=99, message="saved")

    suggestion = await generate_suggestion(report, dl)

    assert suggestion is not None
    assert suggestion.action == "expand"
    assert suggestion.briefing_type == "weekly_digest"  # highest engagement


# ─── AK4: Only ONE suggestion per 7 days ──────────────────────────────────────


@pytest.mark.asyncio
async def test_evolution_rate_limit():
    """AK4: No second suggestion within 7 days."""
    from open_brain.evolution import EngagementReport, generate_suggestion
    from open_brain.evolution import BriefingEngagement

    report = EngagementReport(
        period_days=7,
        by_type=[
            BriefingEngagement("daily_summary", total_count=10, responded_count=0, response_rate=0.0),
        ],
        total_briefings=10,
        has_sufficient_data=True,
    )

    dl = AsyncMock()
    # Simulate: a suggestion was already made 3 days ago
    recent_suggestion = _make_memory(
        50,
        type="evolution",
        created_at=NOW - timedelta(days=3),
        metadata={"evolution_type": "suggestion"},
    )
    dl.search.return_value = SearchResult(results=[recent_suggestion], total=1)

    suggestion = await generate_suggestion(report, dl)

    assert suggestion is None, "Should return None when a suggestion was already made within 7 days"


@pytest.mark.asyncio
async def test_evolution_rate_limit_respects_7_day_window():
    """AK4: Suggestion older than 7 days → new suggestion allowed."""
    from open_brain.evolution import EngagementReport, generate_suggestion
    from open_brain.evolution import BriefingEngagement

    report = EngagementReport(
        period_days=7,
        by_type=[
            BriefingEngagement("daily_summary", total_count=10, responded_count=0, response_rate=0.0),
        ],
        total_briefings=10,
        has_sufficient_data=True,
    )

    dl = AsyncMock()
    # Old suggestion (8 days ago) — outside 7-day window
    # search for recent suggestions returns empty
    dl.search.return_value = SearchResult(results=[], total=0)
    dl.save_memory.return_value = SaveMemoryResult(id=100, message="saved")

    suggestion = await generate_suggestion(report, dl)

    assert suggestion is not None, "Should allow new suggestion when last one was > 7 days ago"


@pytest.mark.asyncio
async def test_evolution_no_suggestion_insufficient_data():
    """AK4: Insufficient data (< 7 days of briefings) → None returned."""
    from open_brain.evolution import EngagementReport, generate_suggestion
    from open_brain.evolution import BriefingEngagement

    report = EngagementReport(
        period_days=3,
        by_type=[
            BriefingEngagement("daily_summary", total_count=2, responded_count=0, response_rate=0.0),
        ],
        total_briefings=2,
        has_sufficient_data=False,
    )

    dl = AsyncMock()
    dl.search.return_value = SearchResult(results=[], total=0)

    suggestion = await generate_suggestion(report, dl)

    assert suggestion is None, "Should return None when insufficient data"


# ─── AK5: Approved changes logged as type=evolution ──────────────────────────


@pytest.mark.asyncio
async def test_evolution_approval():
    """AK5: Approval logged as type=evolution with correct metadata."""
    from open_brain.evolution import log_evolution_approval

    dl = AsyncMock()
    dl.save_memory.return_value = SaveMemoryResult(id=42, message="saved")

    await log_evolution_approval(dl, suggestion_id=99, approved=True)

    assert dl.save_memory.called
    params: SaveMemoryParams = dl.save_memory.call_args[0][0]
    assert params.type == "evolution"
    assert params.metadata is not None
    assert params.metadata["evolution_type"] == "approval"
    assert params.metadata["suggestion_id"] == "99"
    assert params.metadata["approved"] == "true"


@pytest.mark.asyncio
async def test_evolution_rejection():
    """AK5: Rejection also logged as type=evolution with approved=False."""
    from open_brain.evolution import log_evolution_approval

    dl = AsyncMock()
    dl.save_memory.return_value = SaveMemoryResult(id=43, message="saved")

    await log_evolution_approval(dl, suggestion_id=99, approved=False)

    params: SaveMemoryParams = dl.save_memory.call_args[0][0]
    assert params.type == "evolution"
    assert params.metadata["approved"] == "false"


@pytest.mark.asyncio
async def test_evolution_approval_with_project():
    """AK5: project is passed through to save_memory."""
    from open_brain.evolution import log_evolution_approval

    dl = AsyncMock()
    dl.save_memory.return_value = SaveMemoryResult(id=44, message="saved")

    await log_evolution_approval(dl, suggestion_id=99, approved=True, project="open-brain")

    params: SaveMemoryParams = dl.save_memory.call_args[0][0]
    assert params.project == "open-brain"


# ─── AK6: Evolution history queryable ────────────────────────────────────────


@pytest.mark.asyncio
# Integration MoC: verifies the exact DataLayer query contract for evolution history retrieval
async def test_evolution_search():
    """AK6: Evolution history queryable — verify exact SearchParams passed to data layer.

    This test verifies the integration between query_evolution_history and the
    DataLayer interface contract: type=evolution filter, correct limit, and
    descending order (newest first) must all be set.
    """
    from open_brain.data_layer.interface import SearchParams
    from open_brain.evolution import query_evolution_history

    evolution_memories = [
        _make_memory(
            10,
            type="evolution",
            created_at=NOW - timedelta(days=1),
            metadata={"evolution_type": "suggestion", "action": "remove", "briefing_type": "daily_summary"},
            title="Evolution suggestion",
        ),
        _make_memory(
            11,
            type="evolution",
            created_at=NOW - timedelta(days=1),
            metadata={"evolution_type": "approval", "suggestion_id": "10", "approved": "true"},
            title="Evolution approval",
        ),
    ]

    dl = AsyncMock()
    dl.search.return_value = SearchResult(results=evolution_memories, total=2)

    history = await query_evolution_history(dl, limit=20)

    assert len(history) == 2
    assert history[0].id == 10
    assert history[1].id == 11

    # Verify exact SearchParams passed to the data layer
    assert dl.search.called
    call_params: SearchParams = dl.search.call_args[0][0]
    assert call_params.type == "evolution", "Must filter by type=evolution"
    assert call_params.limit == 20, "Must pass limit to data layer"
    assert call_params.order_by == "newest", "Must order by newest first (DESC created_at)"


@pytest.mark.asyncio
async def test_evolution_search_project_filter():
    """AK6: Evolution history supports project filter."""
    from open_brain.evolution import query_evolution_history

    dl = AsyncMock()
    dl.search.return_value = SearchResult(results=[], total=0)

    await query_evolution_history(dl, limit=10, project="open-brain")

    call_params = dl.search.call_args[0][0]
    assert call_params.project == "open-brain"
    assert call_params.type == "evolution"


# ─── AK3: 30-day rejection suppression ───────────────────────────────────────


@pytest.mark.asyncio
async def test_evolution_rejection_suppression():
    """AK3: Rejected briefing_type is not re-proposed for 30 days."""
    from open_brain.evolution import BriefingEngagement, EngagementReport, generate_suggestion

    # daily_summary has very low engagement (should normally be removed)
    report = EngagementReport(
        period_days=7,
        by_type=[
            BriefingEngagement("weekly_digest", total_count=10, responded_count=8, response_rate=0.8),
            BriefingEngagement("daily_summary", total_count=10, responded_count=1, response_rate=0.1),
        ],
        total_briefings=20,
        has_sufficient_data=True,
    )

    dl = AsyncMock()

    # First call: rate-limit check (no recent suggestion)
    # Second call: rejection suppression check (daily_summary was rejected 10 days ago)
    rejected_memory = _make_memory(
        55,
        type="evolution",
        created_at=NOW - timedelta(days=10),
        metadata={"evolution_type": "approval", "approved": "false", "briefing_type": "daily_summary"},
    )
    dl.search.side_effect = [
        SearchResult(results=[], total=0),           # 7-day rate limit: no recent suggestion
        SearchResult(results=[rejected_memory], total=1),  # 30-day rejection check
    ]
    dl.save_memory.return_value = SaveMemoryResult(id=99, message="saved")

    suggestion = await generate_suggestion(report, dl)

    # daily_summary is suppressed; weekly_digest has 80% response rate → expand
    assert suggestion is not None
    assert suggestion.briefing_type != "daily_summary", "Rejected type must not be re-proposed within 30 days"


@pytest.mark.asyncio
async def test_evolution_rejection_suppression_all_types_rejected():
    """AK3: If all types are rejected within 30 days, return None."""
    from open_brain.evolution import BriefingEngagement, EngagementReport, generate_suggestion

    report = EngagementReport(
        period_days=7,
        by_type=[
            BriefingEngagement("daily_summary", total_count=10, responded_count=1, response_rate=0.1),
        ],
        total_briefings=10,
        has_sufficient_data=True,
    )

    dl = AsyncMock()
    rejected_memory = _make_memory(
        56,
        type="evolution",
        created_at=NOW - timedelta(days=5),
        metadata={"evolution_type": "approval", "approved": "false", "briefing_type": "daily_summary"},
    )
    dl.search.side_effect = [
        SearchResult(results=[], total=0),           # 7-day rate limit: no recent suggestion
        SearchResult(results=[rejected_memory], total=1),  # 30-day rejection check
    ]

    suggestion = await generate_suggestion(report, dl)

    assert suggestion is None, "All types suppressed → no suggestion"


# ─── Advisory: sufficient data heuristic ─────────────────────────────────────


@pytest.mark.asyncio
async def test_evolution_sufficient_data_requires_multiple_days():
    """Advisory 4: has_sufficient_data requires >= 3 briefings across >= 2 different days."""
    from open_brain.evolution import analyze_engagement

    # All briefings on the same day — not sufficient even if count >= 3
    same_day = NOW - timedelta(days=3)
    briefings = [
        _make_memory(i, created_at=same_day, metadata={"briefing_type": "daily_summary", "user_responded": True})
        for i in range(1, 6)
    ]

    dl = AsyncMock()
    dl.search.return_value = SearchResult(results=briefings, total=len(briefings))

    report = await analyze_engagement(dl, days_back=7)

    assert not report.has_sufficient_data, "Single day of briefings must not be sufficient"


@pytest.mark.asyncio
async def test_evolution_sufficient_data_two_days():
    """Advisory 4: 3 briefings across 2 days is sufficient."""
    from open_brain.evolution import analyze_engagement

    day1 = NOW - timedelta(days=5)
    day2 = NOW - timedelta(days=2)
    briefings = [
        _make_memory(1, created_at=day1, metadata={"briefing_type": "daily_summary", "user_responded": True}),
        _make_memory(2, created_at=day1, metadata={"briefing_type": "daily_summary", "user_responded": False}),
        _make_memory(3, created_at=day2, metadata={"briefing_type": "daily_summary", "user_responded": True}),
    ]

    dl = AsyncMock()
    dl.search.return_value = SearchResult(results=briefings, total=len(briefings))

    report = await analyze_engagement(dl, days_back=7)

    assert report.has_sufficient_data, "3 briefings across 2 days should be sufficient"


# ─── Advisory: 30-50% dead zone → expand suggestion ──────────────────────────


@pytest.mark.asyncio
async def test_evolution_moderate_engagement_generates_expand():
    """Advisory 5: When some types are 30-50%, still suggest expand for the top type."""
    from open_brain.evolution import BriefingEngagement, EngagementReport, generate_suggestion

    report = EngagementReport(
        period_days=7,
        by_type=[
            BriefingEngagement("weekly_digest", total_count=10, responded_count=7, response_rate=0.7),
            BriefingEngagement("daily_summary", total_count=10, responded_count=4, response_rate=0.4),
        ],
        total_briefings=20,
        has_sufficient_data=True,
    )

    dl = AsyncMock()
    dl.search.return_value = SearchResult(results=[], total=0)
    dl.save_memory.return_value = SaveMemoryResult(id=99, message="saved")

    suggestion = await generate_suggestion(report, dl)

    assert suggestion is not None, "Should generate suggestion even when some types are 30-50%"
    assert suggestion.action == "expand"
    assert suggestion.briefing_type == "weekly_digest"  # highest engagement


# ─── Advisory: scenario test ─────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_evolution_search_integ():
    """AK6 Integration: verifies evolution history queryable via real DataLayer.

    Requires DATABASE_URL env var pointing to a real Postgres instance.
    Skipped automatically when running with '-m not integration'.
    """
    import os
    import asyncpg
    from open_brain.data_layer.postgres import PostgresDataLayer

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url or database_url.startswith("postgresql://test:test@"):
        pytest.skip("Requires real DATABASE_URL (not test placeholder)")

    pool = await asyncpg.create_pool(database_url)
    try:
        dl = PostgresDataLayer(pool=pool)
        from open_brain.evolution import query_evolution_history
        history = await query_evolution_history(dl, limit=5)
        # Only assert the return type — history may be empty in a fresh DB
        assert isinstance(history, list)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_evolution_scenario_realistic():
    """Advisory 3: Realistic scenario — midday_check_in has 14% response rate → removal proposed."""
    from open_brain.evolution import BriefingEngagement, EngagementReport, generate_suggestion

    # 20 morning briefings (18 responded) = 90%
    # 14 midday check-ins (2 responded) = 14.3% → below 30% threshold
    # 10 pre-meeting preps (10 responded) = 100%
    report = EngagementReport(
        period_days=7,
        by_type=[
            BriefingEngagement("morning_briefing", total_count=20, responded_count=18, response_rate=18 / 20),
            BriefingEngagement("midday_check_in", total_count=14, responded_count=2, response_rate=2 / 14),
            BriefingEngagement("pre_meeting_prep", total_count=10, responded_count=10, response_rate=1.0),
        ],
        total_briefings=44,
        has_sufficient_data=True,
    )

    dl = AsyncMock()
    dl.search.return_value = SearchResult(results=[], total=0)
    dl.save_memory.return_value = SaveMemoryResult(id=99, message="saved")

    suggestion = await generate_suggestion(report, dl)

    assert suggestion is not None
    assert suggestion.action == "remove"
    assert suggestion.briefing_type == "midday_check_in"
    assert suggestion.response_rate == pytest.approx(2 / 14, rel=1e-3)
