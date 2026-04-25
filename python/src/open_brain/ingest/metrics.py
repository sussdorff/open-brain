"""Ingest observability metrics — in-process counters (no Prometheus dependency).

Tracks six metric families:
- ingests_total: counter, label=adapter
- llm_calls_total: counter, label=purpose
- dedup_decisions_total: counter, label=action
- relationships_written_total: counter, label=link_type
- memories_written_total: counter, label=type
- ingest_duration_seconds: histogram/list, label=adapter

All counters are module-level dicts protected by a single threading.Lock.
Call reset_all() between tests for isolation.
"""

import dataclasses
import threading
from collections import defaultdict
from dataclasses import dataclass, field

_lock = threading.Lock()

_ingests_total: dict[str, int] = defaultdict(int)
_llm_calls_total: dict[str, int] = defaultdict(int)
_dedup_decisions_total: dict[str, int] = defaultdict(int)
_relationships_written_total: dict[str, int] = defaultdict(int)
_memories_written_total: dict[str, int] = defaultdict(int)
_ingest_duration_seconds: dict[str, list[float]] = defaultdict(list)


def record_ingest(adapter: str) -> None:
    """Increment ingests_total for the given adapter."""
    with _lock:
        _ingests_total[adapter] += 1


def record_llm_call(purpose: str) -> None:
    """Increment llm_calls_total for the given purpose (extract|dedup_confirm|relationship_classify)."""
    with _lock:
        _llm_calls_total[purpose] += 1


def record_dedup_decision(action: str) -> None:
    """Increment dedup_decisions_total for the given action (auto_merge|llm_confirm|new|ambiguous)."""
    with _lock:
        _dedup_decisions_total[action] += 1


def record_relationship_written(link_type: str) -> None:
    """Increment relationships_written_total for the given link_type."""
    with _lock:
        _relationships_written_total[link_type] += 1


def record_memory_written(memory_type: str) -> None:
    """Increment memories_written_total for the given memory type."""
    with _lock:
        _memories_written_total[memory_type] += 1


def record_ingest_duration(adapter: str, duration: float) -> None:
    """Append a duration sample (seconds) to ingest_duration_seconds for the given adapter."""
    with _lock:
        _ingest_duration_seconds[adapter].append(duration)


def reset_all() -> None:
    """Zero all counters. Use in test teardown / setup for isolation."""
    with _lock:
        _ingests_total.clear()
        _llm_calls_total.clear()
        _dedup_decisions_total.clear()
        _relationships_written_total.clear()
        _memories_written_total.clear()
        _ingest_duration_seconds.clear()


@dataclass(slots=True, kw_only=True)
class IngestStats:
    """Structured snapshot of all six ingest metric families."""

    ingests_total: dict[str, int] = field(default_factory=dict)
    llm_calls_total: dict[str, int] = field(default_factory=dict)
    dedup_decisions_total: dict[str, int] = field(default_factory=dict)
    relationships_written_total: dict[str, int] = field(default_factory=dict)
    memories_written_total: dict[str, int] = field(default_factory=dict)
    ingest_duration_seconds: dict[str, list[float]] = field(default_factory=dict)


def get_stats() -> IngestStats:
    """Return a snapshot of all six metric families as an IngestStats dataclass.

    Returns a consistent JSON-serialisable structure regardless of which
    counters are zero — missing keys simply map to empty dicts/lists.
    """
    with _lock:
        return IngestStats(
            ingests_total=dict(_ingests_total),
            llm_calls_total=dict(_llm_calls_total),
            dedup_decisions_total=dict(_dedup_decisions_total),
            relationships_written_total=dict(_relationships_written_total),
            memories_written_total=dict(_memories_written_total),
            ingest_duration_seconds={k: list(v) for k, v in _ingest_duration_seconds.items()},
        )
