"""Real-Postgres integration coverage for session-knowledge capture.

Marked ``integration``. The orchestrator provisions a disposable DB. Each test
uses a per-run UUID identity so the suite is rerunnable twice on the same
populated database without collisions.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import patch

import pytest

from open_brain.data_layer.interface import SearchParams
from open_brain.data_layer.postgres import PostgresDataLayer, close_pool, get_pool
from open_brain.session_knowledge import (
    capture_identity,
    capture_session_knowledge,
    compute_capture_fingerprint,
    parse_session_knowledge_capture_request,
    record_identity_for,
)

TEST_ACTOR = "test-actor"


def _run_id() -> str:
    return uuid.uuid4().hex


def _payload(run: str, *, what_happened: str | None = None) -> dict[str, Any]:
    session_id = f"sess-ekn9-integ-{run}"
    return {
        "schema_version": "session-knowledge-capture.v1",
        "session_id": session_id,
        "producer": "session-knowledge-capture",
        "source_ref": f"agent-session:codex:{session_id}",
        "project": f"open-brain-integ-{run[:8]}",
        "what_happened": what_happened
        or (
            f"Integration run {run}: focused capture verification passed "
            "and replay invariants held."
        ),
        "decisions": [
            {
                "text": (
                    f"Run {run}: persist decisions under deterministic "
                    "per-record identities."
                ),
                "rationale": "Resume must not duplicate child rows.",
            }
        ],
        "what_was_learned": [
            {
                "text": (
                    f"Run {run}: concurrent identical captures must converge "
                    "to one session_event without raising UniqueViolationError."
                ),
                "evidence": f"conversation://session/{session_id}/learning/0",
            }
        ],
        "unfinished_work": [
            {"text": f"Run {run}: external producer adapter remains pending."}
        ],
    }


@pytest.fixture
async def pg_dl(integration_database_url: str):
    """Postgres data layer with embeddings suppressed for deterministic writes."""
    await close_pool()
    # Warm migrations once so concurrent capture tests do not race DDL.
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Ensure default index exists; per-test projects are still created lazily.
        await conn.execute(
            """
            INSERT INTO memory_indexes (name, description)
            VALUES ('default', 'Default memory index')
            ON CONFLICT (name) DO NOTHING
            """
        )
    dl = PostgresDataLayer()
    with patch(
        "open_brain.data_layer.postgres.asyncio.create_task",
        side_effect=lambda *_a, **_k: None,
    ):
        yield dl
    await close_pool()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_db_identical_concurrent_captures_converge(pg_dl: PostgresDataLayer) -> None:
    run = _run_id()
    payload = _payload(run)
    # Pre-create project index so concurrent writers do not race memory_indexes.
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memory_indexes (name)
            VALUES ($1)
            ON CONFLICT (name) DO NOTHING
            """,
            payload["project"],
        )

    results = await asyncio.gather(
        capture_session_knowledge(payload, data_layer=pg_dl, actor=TEST_ACTOR),
        capture_session_knowledge(payload, data_layer=pg_dl, actor=TEST_ACTOR),
    )
    assert {r.status for r in results} <= {"captured", "replayed"}
    assert results[0].session_event_id == results[1].session_event_id
    assert results[0].session_event_id is not None
    assert results[0].decision_ids == results[1].decision_ids
    assert results[0].learning_ids == results[1].learning_ids
    assert len(results[0].decision_ids) == 1
    assert len(results[0].learning_ids) == 1

    identity = capture_identity(
        TEST_ACTOR,
        payload["producer"],
        payload["source_ref"],
        payload["schema_version"],
    )
    events = await pg_dl.search(
        SearchParams(
            type="session_event",
            metadata_filter={"session_knowledge_capture_identity": identity},
            limit=10,
        )
    )
    assert events.total == 1
    sk = (events.results[0].metadata or {}).get("session_knowledge") or {}
    assert sk.get("capture_status") == "complete"
    assert isinstance(sk.get("capture_result"), dict)

    pool = await get_pool()
    async with pool.acquire() as conn:
        meta_type = await conn.fetchval(
            "SELECT jsonb_typeof(metadata) FROM memories WHERE id=$1",
            results[0].session_event_id,
        )
        assert meta_type == "object"
        child_count = await conn.fetchval(
            """
            SELECT COUNT(*)::int FROM memories
            WHERE metadata->>'session_knowledge_capture_identity' = $1
              AND type IN ('decision', 'learning')
            """,
            identity,
        )
        # Children use record identity; capture_identity may also be present.
        child_by_record = await conn.fetchval(
            """
            SELECT COUNT(*)::int FROM memories
            WHERE metadata->>'session_knowledge_record_identity' LIKE $1
              AND type IN ('decision', 'learning')
            """,
            f"{identity}|%",
        )
        assert child_by_record == 2
        rel_count = await conn.fetchval(
            """
            SELECT COUNT(*)::int FROM memory_relationships
            WHERE target_id = $1 AND relation_type = 'derived_from'
            """,
            results[0].session_event_id,
        )
        assert rel_count == 2
        assert child_count in {0, 2}  # capture_identity on children is optional


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_db_conflicting_concurrent_captures_return_typed_conflict(
    pg_dl: PostgresDataLayer,
) -> None:
    run = _run_id()
    base = _payload(run)
    conflict = _payload(
        run,
        what_happened=(
            f"Integration run {run}: DIFFERENT observed outcomes that must "
            "not silently overwrite the first capture."
        ),
    )
    # Same session/source identity, different payload fingerprint.
    assert base["source_ref"] == conflict["source_ref"]
    req_a, _ = parse_session_knowledge_capture_request(base)
    req_b, _ = parse_session_knowledge_capture_request(conflict)
    assert req_a is not None and req_b is not None
    assert compute_capture_fingerprint(req_a) != compute_capture_fingerprint(req_b)

    first = await capture_session_knowledge(base, data_layer=pg_dl, actor=TEST_ACTOR)
    assert first.status == "captured"
    second = await capture_session_knowledge(conflict, data_layer=pg_dl, actor=TEST_ACTOR)
    assert second.status == "conflict"
    assert any(
        issue.code == "session_knowledge_capture_conflict" for issue in second.issues
    )

    # Concurrent conflicting writers after an established winner.
    results = await asyncio.gather(
        capture_session_knowledge(base, data_layer=pg_dl, actor=TEST_ACTOR),
        capture_session_knowledge(conflict, data_layer=pg_dl, actor=TEST_ACTOR),
    )
    statuses = {r.status for r in results}
    assert "conflict" in statuses
    assert statuses <= {"captured", "replayed", "conflict"}
    for result in results:
        if result.status == "conflict":
            assert any(
                issue.code == "session_knowledge_capture_conflict"
                for issue in result.issues
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_db_rerun_resume_and_unique_record_identities(
    pg_dl: PostgresDataLayer,
) -> None:
    run = _run_id()
    payload = _payload(run)
    request, issues = parse_session_knowledge_capture_request(payload)
    assert request is not None and issues == []
    identity = capture_identity(
        TEST_ACTOR,
        payload["producer"],
        payload["source_ref"],
        payload["schema_version"],
    )

    # First capture, then force incomplete by clearing finalize fields.
    first = await capture_session_knowledge(payload, data_layer=pg_dl, actor=TEST_ACTOR)
    assert first.status == "captured"
    assert first.session_event_id is not None

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memories
            SET metadata = jsonb_set(
                jsonb_set(
                    metadata,
                    '{session_knowledge,capture_status}',
                    '"incomplete"'
                ),
                '{session_knowledge,capture_result}',
                'null'::jsonb,
                true
            )
            WHERE id = $1
            """,
            first.session_event_id,
        )

    resumed = await capture_session_knowledge(payload, data_layer=pg_dl, actor=TEST_ACTOR)
    assert resumed.session_event_id == first.session_event_id
    assert resumed.decision_ids == first.decision_ids
    assert resumed.learning_ids == first.learning_ids
    assert resumed.status in {"captured", "replayed"}

    decision_rid = record_identity_for(
        identity, "session_decision", 0, request.decisions[0].text
    )
    learning_rid = record_identity_for(
        identity, "session_learning", 0, request.what_was_learned[0].text
    )
    async with pool.acquire() as conn:
        decision_rows = await conn.fetchval(
            """
            SELECT COUNT(*)::int FROM memories
            WHERE metadata->>'session_knowledge_record_identity' = $1
            """,
            decision_rid,
        )
        learning_rows = await conn.fetchval(
            """
            SELECT COUNT(*)::int FROM memories
            WHERE metadata->>'session_knowledge_record_identity' = $1
            """,
            learning_rid,
        )
        assert decision_rows == 1
        assert learning_rows == 1
        rel_count = await conn.fetchval(
            """
            SELECT COUNT(*)::int FROM memory_relationships
            WHERE target_id = $1 AND relation_type = 'derived_from'
            """,
            first.session_event_id,
        )
        assert rel_count == 2
        status = await conn.fetchval(
            """
            SELECT metadata->'session_knowledge'->>'capture_status'
            FROM memories WHERE id = $1
            """,
            first.session_event_id,
        )
        assert status == "complete"
        meta_type = await conn.fetchval(
            "SELECT jsonb_typeof(metadata) FROM memories WHERE id=$1",
            first.session_event_id,
        )
        assert meta_type == "object"

    # Full complete replay is a no-op write.
    with patch.object(pg_dl, "save_memory", wraps=pg_dl.save_memory) as wrapped_save:
        replayed = await capture_session_knowledge(payload, data_layer=pg_dl, actor=TEST_ACTOR)
    assert replayed.status == "replayed"
    assert replayed.session_event_id == first.session_event_id
    assert wrapped_save.await_count == 0
