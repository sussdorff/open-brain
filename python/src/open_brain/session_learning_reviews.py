"""Explicit, append-only review state for derived session-learning clusters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from open_brain.data_layer.postgres import get_pool

REVIEW_KEY_PREFIX = "session-learning:v1:"
LEARNING_REVIEW_DECISIONS: frozenset[str] = frozenset(
    {"accept", "covered_obsolete", "project_only", "dismiss"}
)


def build_review_key(source_memory_ids: list[int]) -> str:
    """Build a stable review identity from an exact source-memory set."""
    if not source_memory_ids or any(
        isinstance(memory_id, bool) or not isinstance(memory_id, int) or memory_id < 1
        for memory_id in source_memory_ids
    ):
        raise ValueError("source_memory_ids must contain positive integers")
    normalized = sorted(set(source_memory_ids))
    return f"{REVIEW_KEY_PREFIX}{','.join(str(memory_id) for memory_id in normalized)}"


def parse_review_key(review_key: str) -> list[int]:
    """Parse and canonicalize a v1 review key."""
    if not isinstance(review_key, str) or not review_key.startswith(REVIEW_KEY_PREFIX):
        raise ValueError(f"review_key must start with {REVIEW_KEY_PREFIX!r}")
    raw_ids = review_key.removeprefix(REVIEW_KEY_PREFIX)
    try:
        source_ids = [int(item) for item in raw_ids.split(",") if item]
    except ValueError as exc:
        raise ValueError("review_key contains a non-integer source ID") from exc
    canonical = build_review_key(source_ids)
    if canonical != review_key:
        raise ValueError("review_key source IDs must be unique and sorted")
    return source_ids


@dataclass(frozen=True)
class LearningReviewParams:
    """Validated input for one explicit review classification."""

    review_key: str
    decision: str
    reason: str
    canonical_learning: str
    reviewed_by: str

    def __post_init__(self) -> None:
        parse_review_key(self.review_key)
        if self.decision not in LEARNING_REVIEW_DECISIONS:
            raise ValueError(f"Unknown learning review decision: {self.decision!r}")
        reason = self.reason.strip()
        canonical_learning = self.canonical_learning.strip()
        reviewer = self.reviewed_by.strip() if isinstance(self.reviewed_by, str) else ""
        if not reason:
            raise ValueError("reason must not be empty")
        if not canonical_learning:
            raise ValueError("canonical_learning must not be empty")
        if not reviewer:
            raise ValueError("reviewed_by must contain an OAuth reviewer identity")
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "canonical_learning", canonical_learning)
        object.__setattr__(self, "reviewed_by", reviewer)

    @property
    def source_memory_ids(self) -> list[int]:
        """Return source IDs encoded in the validated key."""
        return parse_review_key(self.review_key)


@dataclass(frozen=True)
class LearningReviewRecord:
    """One immutable row in the session-learning review ledger."""

    id: int
    review_key: str
    source_memory_ids: list[int]
    decision: str
    reason: str
    canonical_learning: str
    reviewed_by: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe audit payload."""
        return {
            "id": self.id,
            "review_key": self.review_key,
            "source_memory_ids": list(self.source_memory_ids),
            "decision": self.decision,
            "reason": self.reason,
            "canonical_learning": self.canonical_learning,
            "reviewed_by": self.reviewed_by,
            "created_at": self.created_at,
        }


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _row_to_record(row: Any) -> LearningReviewRecord:
    return LearningReviewRecord(
        id=int(row["id"]),
        review_key=str(row["review_key"]),
        source_memory_ids=[int(value) for value in row["source_memory_ids"]],
        decision=str(row["decision"]),
        reason=str(row["reason"]),
        canonical_learning=str(row["canonical_learning"]),
        reviewed_by=str(row["reviewed_by"]) if row["reviewed_by"] else None,
        created_at=_timestamp(row["created_at"]),
    )


async def record_session_learning_review(
    params: LearningReviewParams,
) -> LearningReviewRecord:
    """Append one manual decision without touching memories or lifecycle state."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO session_learning_reviews (
                review_key,
                source_memory_ids,
                decision,
                reason,
                canonical_learning,
                reviewed_by
            )
            VALUES ($1, $2::bigint[], $3, $4, $5, $6)
            RETURNING id, review_key, source_memory_ids, decision, reason,
                      canonical_learning, reviewed_by, created_at
            """,
            params.review_key,
            params.source_memory_ids,
            params.decision,
            params.reason,
            params.canonical_learning,
            params.reviewed_by,
        )
    if row is None:
        raise RuntimeError("session-learning review insert returned no row")
    return _row_to_record(row)


async def list_latest_session_learning_reviews(
    review_keys: list[str],
    *,
    allow_missing_table: bool = False,
) -> dict[str, LearningReviewRecord]:
    """Return the latest append-only decision for each requested review key."""
    if not review_keys:
        return {}
    normalized_keys = sorted(set(review_keys))
    for review_key in normalized_keys:
        parse_review_key(review_key)
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (review_key)
                       id, review_key, source_memory_ids, decision, reason,
                       canonical_learning, reviewed_by, created_at
                FROM session_learning_reviews
                WHERE review_key = ANY($1::text[])
                ORDER BY review_key, created_at DESC, id DESC
                """,
                normalized_keys,
            )
        except asyncpg.UndefinedTableError:
            if not allow_missing_table:
                raise
            return {}
    records = [_row_to_record(row) for row in rows]
    return {record.review_key: record for record in records}
