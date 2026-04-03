"""Weekly briefing digest — aggregates memory insights over a time window."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from open_brain.data_layer.interface import DataLayer, SearchParams


# ─── Domain dataclasses ────────────────────────────────────────────────────────


@dataclass
class WeeklyBriefing:
    """Structured weekly briefing of memory activity and insights."""

    period: dict[str, Any]
    memory_counts: dict[str, Any]
    top_entities: dict[str, list[dict[str, Any]]]
    theme_trends: dict[str, list[dict[str, Any]]]
    open_loops: list[dict[str, Any]]
    cross_project_connections: list[dict[str, Any]]
    decay_warnings: list[dict[str, Any]]


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _parse_dt(value: str | None) -> datetime | None:
    """Parse ISO datetime string to UTC-aware datetime, or None."""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def _aggregate_entities(memories: list) -> dict[str, Counter]:
    """Aggregate entity frequencies from memory metadata."""
    counters: dict[str, Counter] = {}
    for mem in memories:
        entities = mem.metadata.get("entities", {})
        for category, names in entities.items():
            if category not in counters:
                counters[category] = Counter()
            for name in names:
                counters[category][name] += 1
    return counters


def _top_entities(counters: dict[str, Counter], top_n: int = 10) -> dict[str, list[dict[str, Any]]]:
    """Convert entity counters to sorted top-N lists."""
    result: dict[str, list[dict[str, Any]]] = {}
    for category, counter in counters.items():
        result[category] = [
            {"name": name, "freq": freq}
            for name, freq in counter.most_common(top_n)
        ]
    return result


def _compute_trends(
    current: dict[str, Counter],
    previous: dict[str, Counter],
) -> dict[str, list[dict[str, Any]]]:
    """Compute emerging and declining entity trends across all categories."""
    emerging: list[dict[str, Any]] = []
    declining: list[dict[str, Any]] = []

    all_categories = set(current) | set(previous)
    for category in all_categories:
        curr = current.get(category, Counter())
        prev = previous.get(category, Counter())

        all_names = set(curr) | set(prev)
        for name in all_names:
            c_freq = curr.get(name, 0)
            p_freq = prev.get(name, 0)

            if c_freq > 0 and (p_freq == 0 or c_freq > 2 * p_freq):
                emerging.append({"name": name, "category": category, "current_freq": c_freq, "previous_freq": p_freq})
            elif p_freq > 0 and (c_freq == 0 or p_freq > 2 * c_freq):
                declining.append({"name": name, "category": category, "current_freq": c_freq, "previous_freq": p_freq})

    return {"emerging": emerging, "declining": declining}


def _find_open_loops(memories: list, now: datetime, top_n: int = 10) -> list[dict[str, Any]]:
    """Find memories with action_items that may have open follow-ups."""
    loops: list[dict[str, Any]] = []
    for mem in memories:
        action_items = mem.metadata.get("action_items", [])
        if not action_items:
            continue

        created_dt = _parse_dt(mem.created_at)
        if created_dt is None:
            age_days = 0
        else:
            age_days = max(0, (now - created_dt).days)

        loops.append({
            "memory_id": mem.id,
            "title": mem.title,
            "action_items": action_items,
            "age_days": age_days,
        })

    # Sort by age descending (oldest first = most overdue)
    loops.sort(key=lambda x: x["age_days"], reverse=True)
    return loops[:top_n]


def _find_decay_warnings(
    memories: list,
    now: datetime,
    stale_days: int = 30,
    max_access_count: int = 2,
) -> list[dict[str, Any]]:
    """Find memories that haven't been accessed recently and have low access count."""
    warnings: list[dict[str, Any]] = []
    for mem in memories:
        last_accessed = _parse_dt(mem.last_accessed_at)
        created_dt = _parse_dt(mem.created_at)

        if last_accessed is not None:
            days_unaccessed = (now - last_accessed).days
        elif created_dt is not None:
            days_unaccessed = (now - created_dt).days
        else:
            days_unaccessed = 0

        if days_unaccessed >= stale_days and mem.access_count <= max_access_count:
            warnings.append({
                "memory_id": mem.id,
                "title": mem.title,
                "days_unaccessed": days_unaccessed,
                "access_count": mem.access_count,
            })

    warnings.sort(key=lambda x: x["days_unaccessed"], reverse=True)
    return warnings


def _count_by_type(memories: list) -> dict[str, int]:
    """Count memories grouped by type."""
    counts: dict[str, int] = {}
    for mem in memories:
        counts[mem.type] = counts.get(mem.type, 0) + 1
    return counts


def _find_cross_project_connections(memories: list) -> list[dict[str, Any]]:
    """Group memories by project if project metadata is available."""
    projects: dict[str, dict[str, Any]] = {}
    for mem in memories:
        project = mem.metadata.get("project")
        if project is None:
            continue
        if project not in projects:
            projects[project] = {"memory_count": 0, "entity_counter": Counter()}
        projects[project]["memory_count"] += 1
        entities = mem.metadata.get("entities", {})
        for names in entities.values():
            for name in names:
                projects[project]["entity_counter"][name] += 1

    result: list[dict[str, Any]] = []
    for project, data in sorted(projects.items(), key=lambda x: x[1]["memory_count"], reverse=True):
        common_entities = [name for name, _ in data["entity_counter"].most_common(5)]
        result.append({
            "project": project,
            "memory_count": data["memory_count"],
            "common_entities": common_entities,
        })
    return result


# ─── Main function ─────────────────────────────────────────────────────────────


async def generate_weekly_briefing(
    dl: DataLayer,
    weeks_back: int = 1,
    project: str | None = None,
) -> WeeklyBriefing:
    """Generate a weekly briefing with cross-type time-bridged insights.

    Args:
        dl: DataLayer protocol instance.
        weeks_back: How many weeks back to include in the current period.
        project: Optional project filter.

    Returns:
        WeeklyBriefing with all 6 sections populated.
    """
    now = datetime.now(tz=UTC)
    period_start = now - timedelta(weeks=weeks_back)
    prev_period_start = now - timedelta(weeks=weeks_back * 2)

    # ── Fetch current and previous period memories ─────────────────────────────
    current_result = await dl.search(SearchParams(
        date_start=period_start.isoformat(),
        date_end=now.isoformat(),
        project=project,
        limit=200,
    ))
    current_memories = current_result.results

    previous_result = await dl.search(SearchParams(
        date_start=prev_period_start.isoformat(),
        date_end=period_start.isoformat(),
        project=project,
        limit=200,
    ))
    previous_memories = previous_result.results

    # ── Decay: fetch full database ─────────────────────────────────────────────
    decay_result = await dl.search(SearchParams(project=project, limit=200))
    all_memories = decay_result.results

    # ── Memory counts ─────────────────────────────────────────────────────────
    current_by_type = _count_by_type(current_memories)
    previous_by_type = _count_by_type(previous_memories)
    delta = {t: current_by_type.get(t, 0) - previous_by_type.get(t, 0) for t in set(current_by_type) | set(previous_by_type)}

    memory_counts: dict[str, Any] = {
        "current": len(current_memories),
        "previous": len(previous_memories),
        "by_type": current_by_type,
        "delta": delta,
    }

    # ── Entity aggregation ────────────────────────────────────────────────────
    current_counters = _aggregate_entities(current_memories)
    previous_counters = _aggregate_entities(previous_memories)
    top_entities = _top_entities(current_counters)

    # ── Theme trends ──────────────────────────────────────────────────────────
    theme_trends = _compute_trends(current_counters, previous_counters)

    # ── Open loops ────────────────────────────────────────────────────────────
    open_loops = _find_open_loops(current_memories, now)

    # ── Cross-project connections ─────────────────────────────────────────────
    cross_project_connections = _find_cross_project_connections(current_memories)

    # ── Decay warnings ────────────────────────────────────────────────────────
    decay_warnings = _find_decay_warnings(all_memories, now)

    return WeeklyBriefing(
        period={
            "weeks_back": weeks_back,
            "from": period_start.isoformat(),
            "to": now.isoformat(),
        },
        memory_counts=memory_counts,
        top_entities=top_entities,
        theme_trends=theme_trends,
        open_loops=open_loops,
        cross_project_connections=cross_project_connections,
        decay_warnings=decay_warnings,
    )
