"""Tests for explicit session-learning cluster review state."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import open_brain.cli.main as cli_main
import open_brain.session_learning_analysis as analysis
from open_brain.cli.main import _build_parser
from open_brain.session_learning_reviews import (
    LearningReviewParams,
    LearningReviewRecord,
    build_review_key,
    list_latest_session_learning_reviews,
    parse_review_key,
    record_session_learning_review,
)


def _record(**overrides: object) -> LearningReviewRecord:
    values: dict[str, object] = {
        "id": 9,
        "review_key": "session-learning:v1:21617,26870",
        "source_memory_ids": [21617, 26870],
        "decision": "covered_obsolete",
        "reason": "Receipt-bound ship now precedes finalize.",
        "canonical_learning": "Closed tracker state did not prove landed code.",
        "reviewed_by": "oauth-user",
        "created_at": "2026-07-15T12:00:00+00:00",
    }
    values.update(overrides)
    return LearningReviewRecord(**values)  # type: ignore[arg-type]


def _candidate(candidate_id: str, source_memory_id: int, statement: str) -> analysis.LearningCandidate:
    return analysis.LearningCandidate.from_dict(
        {
            "candidate_id": candidate_id,
            "source_memory_id": source_memory_id,
            "kind": "learning",
            "statement": statement,
            "observation": statement,
            "cause": "The workflow allowed state to diverge.",
            "future_behavior": "Require a durable receipt before finalization.",
            "evidence": ["The tracker was closed while the code remained absent from main."],
            "confidence": 0.9,
            "severity": "high",
            "generalizable": True,
        }
    )


def _clusters(*, duplicate_source_set: bool = False) -> tuple[list[analysis.LearningCandidate], list[analysis.LearningCluster]]:
    candidates = [
        _candidate("first-a", 21617, "Closed tracker state did not prove landed code."),
        _candidate("first-b", 26870, "Closed tracker state did not prove landed code."),
    ]
    specs = [
        {
            "candidate_ids": ["first-a", "first-b"],
            "canonical_learning": "Closed tracker state did not prove landed code.",
            "reason": "Same invariant",
        }
    ]
    if duplicate_source_set:
        candidates.extend(
            [
                _candidate("second-a", 21617, "A materially different recurrent learning."),
                _candidate("second-b", 26870, "A materially different recurrent learning."),
            ]
        )
        specs.append(
            {
                "candidate_ids": ["second-a", "second-b"],
                "canonical_learning": "A materially different recurrent learning.",
                "reason": "Different invariant",
            }
        )
    return candidates, analysis.build_learning_clusters(candidates, specs)


def test_review_key_is_order_independent_and_membership_sensitive() -> None:
    assert build_review_key([26870, 21617, 21617]) == "session-learning:v1:21617,26870"
    assert build_review_key([21617, 26870, 30000]) != build_review_key([21617, 26870])
    assert parse_review_key("session-learning:v1:21617,26870") == [21617, 26870]


@pytest.mark.parametrize("source_ids", [[], [0, 2], [-1, 2]])
def test_review_key_rejects_invalid_source_ids(source_ids: list[int]) -> None:
    with pytest.raises(ValueError):
        build_review_key(source_ids)


@pytest.mark.parametrize("decision", ["accept", "covered_obsolete", "project_only", "dismiss"])
def test_review_params_accept_only_explicit_manual_decisions(decision: str) -> None:
    params = LearningReviewParams(
        review_key="session-learning:v1:21617,26870",
        decision=decision,
        reason="Reviewed manually.",
        canonical_learning="A durable learning snapshot.",
        reviewed_by="oauth-user",
    )
    assert params.source_memory_ids == [21617, 26870]


@pytest.mark.parametrize(
    ("field", "value"),
    [("decision", "archive"), ("reason", "  "), ("canonical_learning", ""), ("review_key", "session-learning:v2:21617,26870")],
)
def test_review_params_reject_invalid_manual_write(field: str, value: str) -> None:
    values = {
        "review_key": "session-learning:v1:21617,26870",
        "decision": "covered_obsolete",
        "reason": "Reviewed manually.",
        "canonical_learning": "A durable learning snapshot.",
        "reviewed_by": None,
    }
    values[field] = value
    with pytest.raises(ValueError):
        LearningReviewParams(**values)


@pytest.mark.asyncio
async def test_record_review_appends_only_to_dedicated_ledger() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": 9,
        "review_key": "session-learning:v1:21617,26870",
        "source_memory_ids": [21617, 26870],
        "decision": "covered_obsolete",
        "reason": "Receipt-bound ship now precedes finalize.",
        "canonical_learning": "Closed tracker state did not prove landed code.",
        "reviewed_by": "oauth-user",
        "created_at": "2026-07-15T12:00:00+00:00",
    }
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    params = LearningReviewParams(
        review_key="session-learning:v1:21617,26870",
        decision="covered_obsolete",
        reason="Receipt-bound ship now precedes finalize.",
        canonical_learning="Closed tracker state did not prove landed code.",
        reviewed_by="oauth-user",
    )

    with patch("open_brain.session_learning_reviews.get_pool", new_callable=AsyncMock, return_value=pool):
        result = await record_session_learning_review(params)

    query = conn.fetchrow.await_args.args[0]
    assert "INSERT INTO session_learning_reviews" in query
    assert "UPDATE memories" not in query
    assert "memory_lifecycle_actions" not in query
    assert result == _record()


@pytest.mark.asyncio
async def test_latest_reviews_are_returned_once_per_requested_key() -> None:
    conn = AsyncMock()
    conn.fetch.return_value = [vars(_record())]
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire

    with patch("open_brain.session_learning_reviews.get_pool", new_callable=AsyncMock, return_value=pool):
        records = await list_latest_session_learning_reviews(["session-learning:v1:21617,26870"])

    assert "DISTINCT ON (review_key)" in conn.fetch.await_args.args[0]
    assert records == {"session-learning:v1:21617,26870": _record()}


def test_reviewed_cluster_leaves_active_queue_but_remains_auditable() -> None:
    candidates, clusters = _clusters()
    key = "session-learning:v1:21617,26870"

    report = analysis.build_analysis_report([], candidates, clusters, {key: _record()})

    assert report["queues"]["reviewable_learning_clusters"] == []
    reviewed = report["queues"]["reviewed_learning_clusters"]
    assert len(reviewed) == 1
    assert reviewed[0]["review_key"] == key
    assert reviewed[0]["review"]["decision"] == "covered_obsolete"
    assert report["counts"]["reviewed_learning_clusters"] == 1
    assert report["read_only"] is True
    assert report["write_side_effects"] is False


def test_duplicate_review_keys_fail_open_as_active_identity_conflicts() -> None:
    candidates, clusters = _clusters(duplicate_source_set=True)
    key = "session-learning:v1:21617,26870"

    report = analysis.build_analysis_report([], candidates, clusters, {key: _record()})

    active = report["queues"]["reviewable_learning_clusters"]
    assert len(active) == 2
    assert all(item["review_identity_conflict"] is True for item in active)
    assert report["queues"]["reviewed_learning_clusters"] == []


@pytest.mark.asyncio
async def test_analyzer_reads_latest_reviews_for_emitted_clusters() -> None:
    candidates, clusters = _clusters()
    summary = analysis.SessionSummary(
        id=21617,
        title="Session",
        content="Content",
        narrative=None,
        project="polaris",
        source="session-close",
        session_ref="session-1",
        created_at="2026-07-15T10:00:00+00:00",
    )
    with (
        patch.object(analysis, "fetch_session_summaries", new_callable=AsyncMock, return_value=[summary]),
        patch.object(analysis, "_extract_candidates", new_callable=AsyncMock, return_value=candidates),
        patch.object(analysis, "_cluster_candidates", new_callable=AsyncMock, return_value=clusters),
        patch.object(analysis, "list_latest_session_learning_reviews", new_callable=AsyncMock, return_value={}) as list_reviews,
    ):
        await analysis.analyze_session_learnings(limit=50)

    list_reviews.assert_awaited_once_with(["session-learning:v1:21617,26870"])


def test_cli_parses_and_dispatches_explicit_review() -> None:
    args = _build_parser().parse_args(
        [
            "learnings", "review", "session-learning:v1:21617,26870",
            "--decision", "covered_obsolete",
            "--reason", "Covered by rewritten workflow.",
            "--canonical-learning", "Closed tracker state did not prove landed code.",
        ]
    )
    assert args.learnings_command == "review"


@pytest.mark.asyncio
async def test_cli_review_uses_authenticated_remote_tool() -> None:
    args = _build_parser().parse_args(
        [
            "learnings", "review", "session-learning:v1:21617,26870",
            "--decision", "covered_obsolete",
            "--reason", "Covered by rewritten workflow.",
            "--canonical-learning", "Closed tracker state did not prove landed code.",
        ]
    )
    with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock, return_value={"id": 9}) as call:
        result = await cli_main._cmd_learnings(args)

    assert result == {"id": 9}
    call.assert_awaited_once_with(
        "review_session_learning",
        {
            "review_key": "session-learning:v1:21617,26870",
            "decision": "covered_obsolete",
            "reason": "Covered by rewritten workflow.",
            "canonical_learning": "Closed tracker state did not prove landed code.",
        },
    )


def test_review_ledger_exists_in_runtime_and_bootstrap_schema() -> None:
    root = Path(__file__).resolve().parents[2]
    runtime_sql = (root / "python/src/open_brain/data_layer/postgres.py").read_text()
    bootstrap_sql = (root / "scripts/bootstrap_test_schema.sql").read_text()
    for schema_sql in (runtime_sql, bootstrap_sql):
        assert "CREATE TABLE IF NOT EXISTS session_learning_reviews" in schema_sql
        assert "CHECK (decision IN ('accept', 'covered_obsolete', 'project_only', 'dismiss'))" in schema_sql
        assert "source_memory_ids BIGINT[] NOT NULL" in schema_sql
        assert "idx_session_learning_reviews_key_created" in schema_sql


@pytest.mark.asyncio
async def test_review_tool_is_evolution_scoped_and_forwards_oauth_user() -> None:
    import open_brain.server as server

    tool = getattr(server, "review_session_learning")
    scopes = server._current_scopes.set(("memory", "evolution"))
    user = server._current_user_id.set("oauth-user")
    try:
        with patch.object(server, "_record_session_learning_review", new_callable=AsyncMock, return_value=_record()) as record:
            result = json.loads(
                await tool(
                    review_key="session-learning:v1:21617,26870",
                    decision="covered_obsolete",
                    reason="Receipt-bound ship now precedes finalize.",
                    canonical_learning="Closed tracker state did not prove landed code.",
                )
            )
    finally:
        server._current_user_id.reset(user)
        server._current_scopes.reset(scopes)

    assert result["decision"] == "covered_obsolete"
    assert record.await_args.args[0].reviewed_by == "oauth-user"
    assert "review_session_learning" in server._EVOLUTION_TOOLS
