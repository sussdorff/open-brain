"""Tests for durable, cursor-addressable session-learning analysis runs."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import open_brain.session_learning_analysis as analysis
import open_brain.session_learning_runs as runs


def _summary(memory_id: int, created_at: str = "2026-07-16T08:00:00+00:00") -> analysis.SessionSummary:
    return analysis.SessionSummary(
        id=memory_id,
        title="Session",
        content="A reusable learning about deterministic cursor ordering.",
        narrative=None,
        project="open-brain",
        source="session-close",
        session_ref=f"session-{memory_id}",
        created_at=created_at,
    )


def _run(status: str = "running") -> runs.SessionLearningRun:
    now = datetime.now(timezone.utc).isoformat()
    return runs.SessionLearningRun(
        run_id="3ea86d12-a68f-4138-b6e7-1a75ca527f15",
        status=status,
        parameters={"limit": 50, "cursor": None},
        source_memory_ids=[],
        next_cursor=None,
        report=None,
        error=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )


@asynccontextmanager
async def _acquired_lock(_run_id: str):
    yield True


def test_cursor_round_trip_binds_timestamp_and_memory_id() -> None:
    summary = _summary(42)
    cursor = analysis.encode_summary_cursor(summary)

    decoded = analysis.decode_summary_cursor(cursor)

    assert decoded.created_at.isoformat() == summary.created_at
    assert decoded.memory_id == 42


@pytest.mark.parametrize("cursor", ["", "not-base64", "e30", "W10"])
def test_cursor_rejects_malformed_values(cursor: str) -> None:
    with pytest.raises(ValueError, match="cursor"):
        analysis.decode_summary_cursor(cursor)


@pytest.mark.asyncio
async def test_fetch_cursor_uses_exclusive_composite_order() -> None:
    conn = AsyncMock()
    conn.fetch.return_value = []
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=transaction)

    @asynccontextmanager
    async def acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = acquire
    cursor = analysis.encode_summary_cursor(_summary(42))

    with patch.object(analysis, "get_pool", new_callable=AsyncMock, return_value=pool):
        await analysis.fetch_session_summaries(limit=25, cursor=cursor)

    query, *params = conn.fetch.await_args.args
    normalized = " ".join(query.split())
    assert "(m.created_at, m.id) < ($1::timestamptz, $2::bigint)" in normalized
    assert "ORDER BY m.created_at DESC, m.id DESC" in normalized
    assert params == [datetime.fromisoformat("2026-07-16T08:00:00+00:00"), 42, 25]


@pytest.mark.asyncio
async def test_existing_learning_matches_are_read_only_and_provenance_bearing() -> None:
    cluster = analysis.LearningCluster(
        cluster_id="cluster-1",
        review_key="session-learning:v1:41,42",
        canonical_learning=(
            "Deterministic composite cursor ordering prevents duplicate session windows."
        ),
        reason="Repeated across sessions",
        candidate_ids=["41-1", "42-1"],
        source_memory_ids=[41, 42],
        member_claims=[],
        evidence=[],
        confidence=0.9,
        severity="medium",
        review_eligible=True,
        hold_reason=None,
    )
    conn = AsyncMock()
    conn.fetch.return_value = [
        {
            "id": 99,
            "type": "learning",
            "title": "Composite cursor ordering",
            "text": "Deterministic cursor ordering avoids duplicate windows.",
            "rank": 0.75,
        }
    ]
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=transaction)

    @asynccontextmanager
    async def acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = acquire
    with patch.object(analysis, "get_pool", new_callable=AsyncMock, return_value=pool):
        matches = await analysis.find_existing_learning_matches([cluster])

    assert matches[cluster.review_key][0]["memory_id"] == 99
    assert len(matches[cluster.review_key][0]["shared_terms"]) >= 3
    query = " ".join(conn.fetch.await_args.args[0].lower().split())
    assert "from memories" in query
    assert "type = 'learning'" in query
    assert not any(keyword in query for keyword in ("update ", "insert ", "delete "))


@pytest.mark.asyncio
async def test_execute_persists_completed_report() -> None:
    report = {"source_memory_ids": [3, 2], "next_cursor": "cursor", "queues": {}}
    completed = _run("completed")
    with (
        patch.object(runs, "_analysis_run_lock", _acquired_lock),
        patch.object(runs, "analyze_session_learnings", new_callable=AsyncMock, return_value=report),
        patch.object(runs, "_complete_session_learning_run", new_callable=AsyncMock, return_value=completed) as persist,
    ):
        result = await runs.execute_session_learning_run(
            completed.run_id,
            {"limit": 50, "project": None, "source": None, "model": None, "cursor": None},
        )

    assert result is completed
    persist.assert_awaited_once_with(completed.run_id, report)


@pytest.mark.asyncio
async def test_execute_records_failure_instead_of_losing_run() -> None:
    failed = _run("failed")
    with (
        patch.object(runs, "_analysis_run_lock", _acquired_lock),
        patch.object(
            runs,
            "analyze_session_learnings",
            new_callable=AsyncMock,
            side_effect=RuntimeError("provider unavailable"),
        ),
        patch.object(runs, "_fail_session_learning_run", new_callable=AsyncMock, return_value=failed) as persist,
    ):
        result = await runs.execute_session_learning_run(
            failed.run_id,
            {"limit": 50},
        )

    assert result is failed
    persist.assert_awaited_once_with(failed.run_id, "RuntimeError: provider unavailable")


def test_failure_details_redact_url_credentials() -> None:
    error = RuntimeError("cannot connect to postgresql://user:secret@example.test/db")

    rendered = runs._safe_error(error)

    assert "secret" not in rendered
    assert "postgresql://user:[redacted]@example.test/db" in rendered


def test_runtime_and_bootstrap_schemas_include_identical_run_ledger() -> None:
    root = Path(__file__).resolve().parents[2]
    runtime_sql = (root / "python/src/open_brain/data_layer/postgres.py").read_text()
    bootstrap_sql = (root / "scripts/bootstrap_test_schema.sql").read_text()

    for sql in (runtime_sql, bootstrap_sql):
        assert "CREATE TABLE IF NOT EXISTS session_learning_analysis_runs" in sql
        assert "idx_session_learning_analysis_runs_status_created" in sql
        assert "status IN ('running', 'completed', 'failed')" in sql


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cursor_windows_are_stable_without_overlap_or_gaps(
    bootstrapped_database_url: str,
) -> None:
    import asyncpg

    from open_brain.data_layer.postgres import close_pool

    project = f"cursor-test-{uuid4()}"
    conn = await asyncpg.connect(bootstrapped_database_url)
    await close_pool()
    try:
        index_id = await conn.fetchval(
            "INSERT INTO memory_indexes (name) VALUES ($1) RETURNING id",
            project,
        )
        rows = await conn.fetch(
            """
            INSERT INTO memories (index_id, type, content, metadata, created_at)
            SELECT $1, 'session_summary', 'cursor fixture ' || value,
                   '{"source":"session-close"}'::jsonb,
                   CASE WHEN value <= 4 THEN $2::timestamptz ELSE $3::timestamptz END
              FROM generate_series(1, 5) AS value
            RETURNING id
            """,
            index_id,
            datetime.fromisoformat("2026-07-16T08:00:00+00:00"),
            datetime.fromisoformat("2026-07-15T08:00:00+00:00"),
        )
        expected_ids = sorted(
            (int(row["id"]) for row in rows[:-1]),
            reverse=True,
        ) + [int(rows[-1]["id"])]

        first = await analysis.fetch_session_summaries(limit=2, project=project)
        second = await analysis.fetch_session_summaries(
            limit=2,
            project=project,
            cursor=analysis.encode_summary_cursor(first[-1]),
        )
        third = await analysis.fetch_session_summaries(
            limit=2,
            project=project,
            cursor=analysis.encode_summary_cursor(second[-1]),
        )

        actual_ids = [item.id for item in first + second + third]
        assert actual_ids == expected_ids
        assert len(actual_ids) == len(set(actual_ids))
    finally:
        await conn.execute("DELETE FROM memories WHERE index_id = $1", index_id)
        await conn.execute("DELETE FROM memory_indexes WHERE name = $1", project)
        await conn.close()
        await close_pool()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completed_run_is_retrievable_after_caller_cancellation(
    bootstrapped_database_url: str,
) -> None:
    import asyncpg

    from open_brain.data_layer.postgres import close_pool

    await close_pool()
    conn = await asyncpg.connect(bootstrapped_database_url)
    run_id = str(uuid4())
    parameters = {"limit": 25, "project": None, "source": None, "model": None, "cursor": None}
    report = {
        "source_memory_ids": [91, 90],
        "next_cursor": "next",
        "counts": {"source_summaries": 2},
        "queues": {},
    }
    try:
        memory_count_before = await conn.fetchval("SELECT COUNT(*) FROM memories")
        review_count_before = await conn.fetchval("SELECT COUNT(*) FROM session_learning_reviews")
        lifecycle_count_before = await conn.fetchval("SELECT COUNT(*) FROM memory_lifecycle_actions")

        analysis_started = asyncio.Event()
        allow_completion = asyncio.Event()

        async def delayed_analysis(**_parameters):
            analysis_started.set()
            await allow_completion.wait()
            return report

        caller_started = asyncio.Event()

        async def disconnected_caller() -> None:
            created = await runs.start_session_learning_run(
                run_id=run_id,
                parameters=parameters,
            )
            assert created.status == "running"
            caller_started.set()
            await asyncio.Future()

        with patch.object(runs, "analyze_session_learnings", side_effect=delayed_analysis):
            caller = asyncio.create_task(disconnected_caller())
            await caller_started.wait()
            await analysis_started.wait()
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            allow_completion.set()
            for _attempt in range(100):
                retrieved = await runs.get_session_learning_run(run_id)
                if retrieved is not None and retrieved.status == "completed":
                    break
                await asyncio.sleep(0.01)

        retrieved = await runs.get_session_learning_run(run_id)
        assert retrieved is not None
        assert retrieved.status == "completed"
        assert retrieved.report == report
        assert await conn.fetchval("SELECT COUNT(*) FROM memories") == memory_count_before
        assert await conn.fetchval("SELECT COUNT(*) FROM session_learning_reviews") == review_count_before
        assert await conn.fetchval("SELECT COUNT(*) FROM memory_lifecycle_actions") == lifecycle_count_before
    finally:
        await conn.execute(
            "DELETE FROM session_learning_analysis_runs WHERE run_id = $1::uuid",
            run_id,
        )
        await conn.close()
        await close_pool()
