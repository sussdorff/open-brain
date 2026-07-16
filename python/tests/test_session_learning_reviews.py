"""Tests for explicit session-learning cluster review state."""

from __future__ import annotations

import json
import re
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
    ("field", "value", "message"),
    [
        ("decision", "archive", "Unknown learning review decision"),
        ("reason", "  ", "reason must not be empty"),
        ("canonical_learning", "", "canonical_learning must not be empty"),
        ("review_key", "session-learning:v2:21617,26870", "review_key must start"),
        ("reviewed_by", None, "reviewed_by must contain"),
        ("reviewed_by", "  ", "reviewed_by must contain"),
    ],
)
def test_review_params_reject_invalid_manual_write(
    field: str, value: object, message: str
) -> None:
    values: dict[str, object] = {
        "review_key": "session-learning:v1:21617,26870",
        "decision": "covered_obsolete",
        "reason": "Reviewed manually.",
        "canonical_learning": "A durable learning snapshot.",
        "reviewed_by": "oauth-user",
    }
    values[field] = value
    with pytest.raises(ValueError, match=message):
        LearningReviewParams(**values)  # type: ignore[arg-type]


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
    assert "Reviewed by: oauth-user" in cli_main._render_learning_analysis(report)



def test_duplicate_review_keys_fail_open_as_active_identity_conflicts() -> None:
    candidates, clusters = _clusters(duplicate_source_set=True)
    key = "session-learning:v1:21617,26870"

    report = analysis.build_analysis_report([], candidates, clusters, {key: _record()})

    active = report["queues"]["reviewable_learning_clusters"]
    assert len(active) == 2
    assert all(item["review_identity_conflict"] is True for item in active)
    assert all(
        item["conflicting_review"]["decision"] == "covered_obsolete"
        for item in active
    )
    rendered = cli_main._render_learning_analysis(report)
    assert "Review identity conflict" in rendered
    assert "Prior review (identity conflict): covered_obsolete" in rendered

    assert report["queues"]["reviewed_learning_clusters"] == []


def test_cross_run_canonical_drift_fails_open_with_stale_review_context() -> None:
    candidates, clusters = _clusters()
    key = "session-learning:v1:21617,26870"
    stale_review = _record(canonical_learning="A different prior learning.")

    report = analysis.build_analysis_report([], candidates, clusters, {key: stale_review})

    active = report["queues"]["reviewable_learning_clusters"]
    assert len(active) == 1
    assert active[0]["review_canonical_drift"] is True
    assert active[0]["stale_review"]["canonical_learning"] == "A different prior learning."
    rendered = cli_main._render_learning_analysis(report)
    assert "Prior review (stale): covered_obsolete" in rendered
    assert "A different prior learning." in rendered

    assert report["queues"]["reviewed_learning_clusters"] == []


def test_cross_run_canonical_paraphrase_preserves_review() -> None:
    candidates, clusters = _clusters()
    key = "session-learning:v1:21617,26870"
    clusters[0] = analysis.replace(
        clusters[0],
        canonical_learning=(
            "Closed bead != landed code (~42% of closed-bead worktrees examined "
            "had real unmerged diffs)."
        ),
    )
    prior_review = _record(
        canonical_learning=(
            "Closed bead != landed code: a meaningful fraction (10/24, ~42%) of "
            "closed-bead worktrees in this repo had real unmerged diffs against main."
        )
    )

    report = analysis.build_analysis_report(
        [], candidates, clusters, {key: prior_review}
    )

    assert report["queues"]["reviewable_learning_clusters"] == []
    reviewed = report["queues"]["reviewed_learning_clusters"]
    assert len(reviewed) == 1
    assert reviewed[0]["review_canonical_drift"] is False
    assert reviewed[0]["review_canonical_paraphrased"] is True
    rendered = cli_main._render_learning_analysis(report)
    assert "Review match: bounded canonical paraphrase" in rendered
    assert "Approved snapshot: Closed bead != landed code:" in rendered


@pytest.mark.parametrize(
    "current_learning",
    [
        "Closed bead proves landed code (~42% of worktrees were clean).",
        "Closed bead != landed code (~5% had real unmerged diffs).",
    ],
)
def test_material_canonical_change_still_fails_open(
    current_learning: str,
) -> None:
    candidates, clusters = _clusters()
    key = "session-learning:v1:21617,26870"
    clusters[0] = analysis.replace(
        clusters[0], canonical_learning=current_learning
    )
    prior_review = _record(
        canonical_learning=(
            "Closed bead != landed code (~42% of closed-bead worktrees had real "
            "unmerged diffs)."
        )
    )

    report = analysis.build_analysis_report(
        [], candidates, clusters, {key: prior_review}
    )

    active = report["queues"]["reviewable_learning_clusters"]
    assert len(active) == 1
    assert active[0]["review_canonical_drift"] is True


@pytest.mark.parametrize(
    ("stored", "current"),
    [
        (
            "Closed tracker state doesn't prove landed code, so verify git before close.",
            "Closed tracker state does prove landed code, so verify git before close.",
        ),
        (
            "Closed tracker state did not prove landed code, so do not close before git verification.",
            "Closed tracker state did prove landed code, so do not close before git verification.",
        ),
        (
            "Closed bead != landed code because worktrees had real unmerged diffs.",
            "Closed bead != landed code because worktrees had real merged diffs.",
        ),
        (
            "Reject noncompliant payloads before materialization starts.",
            "Reject compliant payloads before materialization starts.",
        ),
        (
            "Closed bead != landed code: 10 of 24 worktrees had unmerged diffs.",
            "Closed bead != landed code: 20 of 24 worktrees had unmerged diffs.",
        ),
        (
            "Retry after 30s when the remote queue remains unavailable.",
            "Retry after 60s when the remote queue remains unavailable.",
        ),
        (
            "Keep p95 latency < 200 ms or the queue backs up.",
            "Keep p95 latency > 200 ms or the queue backs up.",
        ),
        (
            "Apply offset -1 when replaying the prior cursor position.",
            "Apply offset 1 when replaying the prior cursor position.",
        ),
        (
            "Use pg17 for the migration compatibility baseline.",
            "Use pg18 for the migration compatibility baseline.",
        ),
    ],
)
def test_adversarial_material_canonical_changes_fail_open(
    stored: str,
    current: str,
) -> None:
    assert analysis._canonical_learnings_equivalent(stored, current) is False


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
        patch.object(analysis, "find_existing_learning_matches", new_callable=AsyncMock, return_value={}),
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

    def normalized_ddl(sql: str, pattern: str) -> str:
        match = re.search(pattern, sql, re.DOTALL)
        assert match is not None
        return " ".join(match.group(0).split())

    table_pattern = (
        r"CREATE TABLE IF NOT EXISTS session_learning_reviews\s*\(.*?\n\s*\);"
    )
    index_pattern = (
        r"CREATE INDEX IF NOT EXISTS idx_session_learning_reviews_key_created.*?;"
    )
    assert normalized_ddl(runtime_sql, table_pattern) == normalized_ddl(
        bootstrap_sql, table_pattern
    )
    assert normalized_ddl(runtime_sql, index_pattern) == normalized_ddl(
        bootstrap_sql, index_pattern
    )
    assert "SET reviewed_by = 'legacy-unattributed'" in runtime_sql
    assert "ALTER COLUMN reviewed_by SET NOT NULL" in runtime_sql
    assert (
        "ADD CONSTRAINT session_learning_reviews_reviewed_by_not_blank"
        in runtime_sql
    )


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


@pytest.mark.asyncio
async def test_review_tool_rejects_anonymous_evolution_credentials() -> None:
    import open_brain.server as server

    tool = getattr(server, "review_session_learning")
    scopes = server._current_scopes.set(("memory", "evolution"))
    user = server._current_user_id.set(None)
    try:
        with patch.object(server, "_record_session_learning_review", new_callable=AsyncMock) as record:
            with pytest.raises(ValueError, match="OAuth reviewer identity"):
                await tool(
                    review_key="session-learning:v1:21617,26870",
                    decision="covered_obsolete",
                    reason="Receipt-bound ship now precedes finalize.",
                    canonical_learning="Closed tracker state did not prove landed code.",
                )
    finally:
        server._current_user_id.reset(user)
        server._current_scopes.reset(scopes)

    record.assert_not_awaited()

@pytest.mark.asyncio
async def test_review_tool_rejects_memory_only_scope_before_write() -> None:
    import open_brain.server as server

    tool = getattr(server, "review_session_learning")
    scopes = server._current_scopes.set(("memory",))
    user = server._current_user_id.set("oauth-user")
    try:
        with patch.object(server, "_record_session_learning_review", new_callable=AsyncMock) as record:
            with pytest.raises(server.ScopeDeniedError, match="evolution"):
                await tool(
                    review_key="session-learning:v1:21617,26870",
                    decision="covered_obsolete",
                    reason="Receipt-bound ship now precedes finalize.",
                    canonical_learning="Closed tracker state did not prove landed code.",
                )
    finally:
        server._current_user_id.reset(user)
        server._current_scopes.reset(scopes)

    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_review_table_is_optional_only_when_explicitly_requested() -> None:
    import asyncpg

    conn = AsyncMock()
    conn.fetch.side_effect = asyncpg.UndefinedTableError("missing review ledger")
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire

    with patch("open_brain.session_learning_reviews.get_pool", new_callable=AsyncMock, return_value=pool):
        result = await list_latest_session_learning_reviews(
            ["session-learning:v1:21617,26870"],
            allow_missing_table=True,
        )
        with pytest.raises(asyncpg.UndefinedTableError):
            await list_latest_session_learning_reviews(
                ["session-learning:v1:21617,26870"]
            )

    assert result == {}



@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_review_history_is_append_only_and_memory_neutral(
    bootstrapped_database_url: str,
) -> None:
    import asyncpg

    from open_brain.data_layer.postgres import close_pool

    key = "session-learning:v1:900000001,900000002"
    conn = await asyncpg.connect(bootstrapped_database_url)
    await close_pool()
    try:
        await conn.execute(
            "DELETE FROM session_learning_reviews WHERE review_key = $1",
            key,
        )
        memory_count_before = await conn.fetchval("SELECT COUNT(*) FROM memories")

        first = await record_session_learning_review(
            LearningReviewParams(
                review_key=key,
                decision="accept",
                reason="First explicit review.",
                canonical_learning="A stable integration-test learning.",
                reviewed_by="integration-oauth-user",
            )
        )
        second = await record_session_learning_review(
            LearningReviewParams(
                review_key=key,
                decision="project_only",
                reason="Reclassified with additional operator context.",
                canonical_learning="A stable integration-test learning.",
                reviewed_by="integration-oauth-user",
            )
        )
        latest = await list_latest_session_learning_reviews([key])

        history_count = await conn.fetchval(
            "SELECT COUNT(*) FROM session_learning_reviews WHERE review_key = $1",
            key,
        )
        memory_count_after = await conn.fetchval("SELECT COUNT(*) FROM memories")
        assert second.id > first.id
        assert history_count == 2
        assert latest[key].decision == "project_only"
        assert memory_count_after == memory_count_before
    finally:
        await conn.execute(
            "DELETE FROM session_learning_reviews WHERE review_key = $1",
            key,
        )
        await conn.close()
        await close_pool()
