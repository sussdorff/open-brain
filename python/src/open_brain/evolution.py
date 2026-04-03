"""Self-improvement loop — engagement tracking and weekly behavior proposals.

Tracks briefing response rates by type and generates one suggestion per 7 days
to remove low-engagement briefing types or expand high-engagement ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from open_brain.data_layer.interface import DataLayer, Memory, SaveMemoryParams, SearchParams


# ─── Briefing metadata convention ────────────────────────────────────────────


BRIEFING_METADATA_REQUIRED_KEYS: tuple[str, ...] = ("briefing_type", "user_responded")
"""Required metadata keys for memories with type='briefing'."""


def validate_briefing_metadata(metadata: dict[str, Any]) -> list[str]:
    """Validate that briefing metadata has all required keys.

    Returns a list of human-readable error strings (empty = valid).
    """
    errors: list[str] = []
    for key in BRIEFING_METADATA_REQUIRED_KEYS:
        if key not in metadata:
            errors.append(f"briefing metadata missing required field '{key}'")
    return errors


# ─── Domain dataclasses ────────────────────────────────────────────────────────


@dataclass
class BriefingEngagement:
    """Engagement stats for a single briefing type."""

    briefing_type: str
    total_count: int
    responded_count: int
    response_rate: float  # 0.0 - 1.0


@dataclass
class EngagementReport:
    """Aggregated engagement report over a time period."""

    period_days: int
    by_type: list[BriefingEngagement]
    total_briefings: int
    has_sufficient_data: bool  # True if >= 7 days of data


@dataclass
class EvolutionSuggestion:
    """A single behavior-change suggestion."""

    action: str  # "remove" | "expand"
    briefing_type: str
    reason: str
    response_rate: float


# ─── Core functions ────────────────────────────────────────────────────────────


async def analyze_engagement(
    dl: DataLayer,
    days_back: int = 7,
    project: str | None = None,
) -> EngagementReport:
    """Analyze briefing engagement: response rates by type over last N days.

    Searches memories with type='briefing' from the last days_back days,
    groups by metadata['briefing_type'], and calculates per-type response rates.
    """
    now = datetime.now(tz=UTC)
    since = now - timedelta(days=days_back)

    result = await dl.search(SearchParams(
        type="briefing",
        date_start=since.isoformat(),
        date_end=now.isoformat(),
        project=project,
        limit=500,
    ))

    briefings = result.results
    total = len(briefings)

    # Group by briefing_type
    counts: dict[str, int] = {}
    responded: dict[str, int] = {}

    for mem in briefings:
        btype = mem.metadata.get("briefing_type", "unknown")
        counts[btype] = counts.get(btype, 0) + 1
        if mem.metadata.get("user_responded") is True:
            responded[btype] = responded.get(btype, 0) + 1

    by_type: list[BriefingEngagement] = []
    for btype, total_count in counts.items():
        responded_count = responded.get(btype, 0)
        rate = responded_count / total_count if total_count > 0 else 0.0
        by_type.append(BriefingEngagement(
            briefing_type=btype,
            total_count=total_count,
            responded_count=responded_count,
            response_rate=rate,
        ))

    # Sort by briefing_type for deterministic ordering
    by_type.sort(key=lambda x: x.briefing_type)

    has_sufficient_data = days_back >= 7 and total > 0

    return EngagementReport(
        period_days=days_back,
        by_type=by_type,
        total_briefings=total,
        has_sufficient_data=has_sufficient_data,
    )


async def generate_suggestion(
    report: EngagementReport,
    dl: DataLayer,
    project: str | None = None,
) -> EvolutionSuggestion | None:
    """Generate ONE self-improvement suggestion based on engagement.

    Rate-limited to 1 suggestion per 7 days. Returns None if:
    - A suggestion was already made in the last 7 days
    - Insufficient data (has_sufficient_data=False)
    - No actionable suggestion (all types have good engagement and nothing to expand)

    Strategy:
    - If any type has < 30% response rate → propose removal of the worst offender
    - If all types have >= 50% response rate → propose expansion of the top type
    """
    if not report.has_sufficient_data:
        return None

    # Check rate limit: was a suggestion already made in the last 7 days?
    now = datetime.now(tz=UTC)
    since = now - timedelta(days=7)

    recent_result = await dl.search(SearchParams(
        type="evolution",
        metadata_filter={"evolution_type": "suggestion"},
        date_start=since.isoformat(),
        date_end=now.isoformat(),
        project=project,
        limit=1,
    ))

    if recent_result.results:
        return None  # Rate limit: already suggested within 7 days

    if not report.by_type:
        return None

    # Find the lowest-engagement type
    lowest = min(report.by_type, key=lambda e: e.response_rate)

    if lowest.response_rate < 0.30:
        suggestion = EvolutionSuggestion(
            action="remove",
            briefing_type=lowest.briefing_type,
            reason=(
                f"Low engagement: only {lowest.response_rate:.0%} response rate "
                f"({lowest.responded_count}/{lowest.total_count} briefings responded). "
                f"Consider removing this briefing type to reduce noise."
            ),
            response_rate=lowest.response_rate,
        )
    else:
        # All types have >= 30%; check if all are >= 50%
        all_good = all(e.response_rate >= 0.50 for e in report.by_type)
        if not all_good:
            return None  # Some moderate engagement, no clear action

        # Propose expanding the top-performing type
        highest = max(report.by_type, key=lambda e: e.response_rate)
        suggestion = EvolutionSuggestion(
            action="expand",
            briefing_type=highest.briefing_type,
            reason=(
                f"High engagement: {highest.response_rate:.0%} response rate "
                f"({highest.responded_count}/{highest.total_count} briefings responded). "
                f"Consider expanding or enriching this briefing type."
            ),
            response_rate=highest.response_rate,
        )

    # Log the suggestion as a memory
    await dl.save_memory(SaveMemoryParams(
        text=suggestion.reason,
        type="evolution",
        project=project,
        title=f"Evolution suggestion: {suggestion.action} {suggestion.briefing_type}",
        metadata={
            "evolution_type": "suggestion",
            "action": suggestion.action,
            "briefing_type": suggestion.briefing_type,
            "response_rate": suggestion.response_rate,
        },
    ))

    return suggestion


async def log_evolution_approval(
    dl: DataLayer,
    suggestion_id: int,
    approved: bool,
    project: str | None = None,
) -> None:
    """Log approval/rejection of an evolution suggestion.

    Saves a memory with type='evolution', metadata.evolution_type='approval'.
    """
    action_label = "approved" if approved else "rejected"
    await dl.save_memory(SaveMemoryParams(
        text=f"Evolution suggestion #{suggestion_id} was {action_label}.",
        type="evolution",
        project=project,
        title=f"Evolution {action_label}: suggestion #{suggestion_id}",
        metadata={
            "evolution_type": "approval",
            "suggestion_id": suggestion_id,
            "approved": approved,
        },
    ))


async def query_evolution_history(
    dl: DataLayer,
    limit: int = 20,
    project: str | None = None,
) -> list[Memory]:
    """Query evolution history: past suggestions and approvals.

    Returns memories with type='evolution', ordered by most recent first.
    """
    result = await dl.search(SearchParams(
        type="evolution",
        project=project,
        limit=limit,
        order_by=None,
    ))
    return result.results
