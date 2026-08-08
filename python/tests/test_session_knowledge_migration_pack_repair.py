"""Executive Pack repair coverage for open-brain-ekn.8."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from open_brain.data_layer.interface import validate_origin_provenance
from open_brain.session_knowledge_migration import (
    FIXED_CONTROL_QUERIES,
    PostgresMigrationStore,
    evaluate_migration_gate,
    transform_legacy_memory,
)
from tests.test_session_knowledge_migration_k1_repair import (
    PROVIDER_META,
    FakeMigrationStore,
    _apply,
    _dry,
    _evidence,
    _learning,
)


class ContentDependentControl:
    """MAX-over-documents control matching the production aggregation shape."""

    instrument = "content-dependent-max-overlap.v1"

    async def measure(
        self, *, control: str, query: str, documents: list[str]
    ) -> float:
        del control
        query_words = set(query.lower().split())
        return max(
            (
                len(query_words & set(document.lower().split())) / len(query_words)
                for document in documents
            ),
            default=0.0,
        )


@pytest.mark.asyncio
async def test_content_dependent_controls_complete_across_two_batches() -> None:
    store = FakeMigrationStore(
        [
            _learning(3101, "Unrelated first batch content."),
            _learning(3102, "Session decisions inferred learning archival."),
        ]
    )
    control = ContentDependentControl()
    report = await _dry(store, control)
    evidence = _evidence(report, batch_scope={"limit": 1, "after_id": 0})

    first = await _apply(store, report, control=control, evidence=evidence)
    assert first["status"] == "running"
    assert first["cursor"] == "3101"
    first_tokens = first["counters"]["provider_metrics"]["tokens"]
    assert store.memories[3101].metadata["status"] == "archived"
    assert store.memories[3102].metadata.get("status") != "archived"

    second = await _apply(store, report, control=control, evidence=evidence)
    assert second["status"] == "completed"
    assert second["cursor"] == "3102"
    assert second["counters"]["provider_metrics"]["tokens"] > first_tokens
    assert store.memories[3102].metadata["status"] == "archived"

    replay = await _apply(store, report, control=control, evidence=evidence)
    assert replay["status"] == "replayed"


def test_legacy_origin_fallback_is_canonical_and_keeps_basis_outside_origin() -> None:
    memory = SimpleNamespace(
        id=3201,
        type="learning",
        content="A legacy learning with a bare session reference.",
        title="",
        metadata={},
        session_ref="bare-session-id",
    )

    plan = transform_legacy_memory(memory)

    assert plan.origin == validate_origin_provenance(plan.origin)
    assert set(plan.origin) == {"producer", "source_ref"}
    assert plan.origin["source_ref"] == "legacy-session:bare-session-id"
    for output in plan.outputs:
        if output.persist:
            origin = output.metadata["provenance"]["origin"]
            assert origin == validate_origin_provenance(origin)
            assert output.metadata["provenance"]["migration"]["origin_inferred"] is True


def test_legacy_metadata_project_precedes_default_physical_index() -> None:
    memory = SimpleNamespace(
        id=3202,
        type="learning",
        content="A project-scoped legacy learning.",
        title="",
        metadata={"project": "logical-project"},
        session_ref="session-3202",
        project="default",
    )

    plan = transform_legacy_memory(memory)

    persisted = [output for output in plan.outputs if output.persist]
    assert persisted
    assert all(output.metadata["project"] == "logical-project" for output in persisted)


@pytest.mark.asyncio
async def test_postgres_save_derived_resolves_project_scope_on_insert_and_replay(
) -> None:
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {"id": 17},
        {"id": 3301},
        {"id": 18},
    ]
    conn.fetchval.side_effect = [None, 3301]

    @asynccontextmanager
    async def acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = acquire
    store = PostgresMigrationStore(pool)
    identity = f"pack-repair:{uuid4()}"
    metadata = {
        "project": "project-a",
        "session_knowledge_record_identity": identity,
        "provenance": {
            "origin": {"producer": "pack-repair", "source_ref": "test:project-a"}
        },
    }

    created = await store.save_derived(
        memory_type="learning",
        content="Project-scoped derived learning.",
        metadata=metadata,
    )
    assert created == 3301

    replayed = await store.save_derived(
        memory_type="learning",
        content="Project-scoped derived learning.",
        metadata={**metadata, "project": "project-b"},
    )
    assert replayed == 3301

    project_queries = [
        call.args
        for call in conn.fetchrow.await_args_list
        if "memory_indexes" in str(call.args[0])
    ]
    assert project_queries[0][1] == "project-a"
    assert project_queries[1][1] == "project-b"
    insert_call = next(
        call
        for call in conn.fetchrow.await_args_list
        if "INSERT INTO memories" in str(call.args[0])
    )
    assert insert_call.args[6] == 17
    replay_update = next(
        call
        for call in conn.execute.await_args_list
        if "SET index_id" in str(call.args[0])
    )
    assert replay_update.args[2] == 18


def test_fixed_control_queries_stay_bound_for_content_dependent_probe() -> None:
    assert set(FIXED_CONTROL_QUERIES) == {"lexical", "vector", "rerank"}


@pytest.mark.asyncio
async def test_gate_rejects_missing_per_source_control_baselines() -> None:
    store = FakeMigrationStore([_learning(3401, "A scoped learning.")])
    report = await _dry(store, ContentDependentControl())
    evidence = _evidence(report)
    report.pop("retrieval_control_source_baselines")

    gate = evaluate_migration_gate(
        decision="ALLOW",
        dry_run_report=report,
        evidence=evidence,
        configured_provider_metadata=PROVIDER_META,
    )

    assert gate["writes_authorized"] is False
    assert "retrieval_control_source_baselines_missing" in gate["reasons"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_derived_record_keeps_non_default_project_scope(
    integration_database_url: str,
) -> None:
    from open_brain.data_layer.postgres import close_pool, get_pool

    with patch.dict(
        "os.environ",
        {"DATABASE_URL": integration_database_url},
        clear=False,
    ):
        await close_pool()
        pool = await get_pool()
        store = PostgresMigrationStore(pool)
        token = uuid4().hex
        identity = f"pack-project-scope:{token}"
        project_a = f"pack-project-a-{token}"
        project_b = f"pack-project-b-{token}"
        metadata = {
            "project": project_a,
            "session_knowledge_record_identity": identity,
            "provenance": {
                "origin": {
                    "producer": "pack-repair",
                    "source_ref": f"test:{token}",
                }
            },
        }

        memory_id = await store.save_derived(
            memory_type="note",
            content="Project-scoped migration output.",
            metadata=metadata,
        )
        async with pool.acquire() as conn:
            observed_project = await conn.fetchval(
                """
                SELECT mi.name
                  FROM memories AS m
                  JOIN memory_indexes AS mi ON mi.id = m.index_id
                 WHERE m.id = $1
                """,
                memory_id,
            )
        assert observed_project == project_a

        replayed_id = await store.save_derived(
            memory_type="note",
            content="Project-scoped migration output.",
            metadata={**metadata, "project": project_b},
        )
        assert replayed_id == memory_id
        async with pool.acquire() as conn:
            replayed_project = await conn.fetchval(
                """
                SELECT mi.name
                  FROM memories AS m
                  JOIN memory_indexes AS mi ON mi.id = m.index_id
                 WHERE m.id = $1
                """,
                memory_id,
            )
        assert replayed_project == project_b
        await close_pool()
