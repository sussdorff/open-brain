"""Integration evidence for weekly agent review extensions."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from open_brain.data_layer.interface import Memory, SaveMemoryParams, SearchResult


def _memory(
    memory_id: int,
    *,
    memory_type: str = "observation",
    title: str | None = None,
    content: str = "Weekly review memory",
    metadata: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> Memory:
    """Create a Memory object for weekly review tests."""
    if created_at is None:
        created_at = datetime.now(tz=UTC) - timedelta(days=1)
    return Memory(
        id=memory_id,
        index_id=1,
        session_id=None,
        type=memory_type,
        title=title or f"Memory {memory_id}",
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
        user_id=None,
        importance="medium",
    )


def test_canonical_entity_select_predicate_uses_positive_marker() -> None:
    """The read-side canonical predicate mirrors the existing protection helper."""
    from open_brain.data_layer.postgres import canonical_entity_select_predicate

    assert (
        canonical_entity_select_predicate("m")
        == "m.metadata->>'canonical_entity' = 'true'"
    )
    assert (
        canonical_entity_select_predicate() == "metadata->>'canonical_entity' = 'true'"
    )


@pytest.mark.asyncio
async def test_weekly_briefing_adds_relevant_canonical_entities_and_inbox_state() -> (
    None
):
    """Weekly briefing includes relevant canonical entities and current inbox state."""
    from open_brain.digest import generate_weekly_briefing

    active_memory = _memory(
        10,
        title="Open Brain planning",
        metadata={"entities": {"projects": ["Open Brain"]}},
    )
    relevant_entity = _memory(
        20,
        memory_type="project",
        title="Open Brain",
        content="Canonical project entity for Open Brain",
        metadata={
            "canonical_entity": True,
            "canonical_kind": "project",
            "name": "Open Brain",
        },
    )
    unrelated_entity = _memory(
        21,
        memory_type="concept",
        title="Unrelated Concept",
        content="Canonical concept outside this weekly window",
        metadata={
            "canonical_entity": True,
            "canonical_kind": "concept",
            "name": "Unrelated Concept",
        },
    )
    inbox_memory = _memory(
        30,
        title="Pending weekly capture",
        metadata={"capture_status": "inbox"},
    )
    dl = AsyncMock()
    dl.search.side_effect = [
        SearchResult(results=[active_memory], total=1),
        SearchResult(results=[], total=0),
        SearchResult(results=[active_memory], total=1),
        SearchResult(results=[relevant_entity, unrelated_entity], total=2),
        SearchResult(results=[inbox_memory], total=1),
    ]

    result = await generate_weekly_briefing(dl, weeks_back=1, project="open-brain")

    assert result.canonical_entities == [
        {"id": 20, "title": "Open Brain", "type": "project", "kind": "project"}
    ]
    assert result.inbox_state == {
        "pending": 1,
        "sample": [
            {"id": 30, "title": "Pending weekly capture", "type": "observation"}
        ],
    }

    canonical_params = dl.search.await_args_list[3].args[0]
    inbox_params = dl.search.await_args_list[4].args[0]
    assert canonical_params.metadata_filter == {"canonical_entity": True}
    assert canonical_params.project == "open-brain"
    assert inbox_params.capture_status == "inbox"
    assert inbox_params.project == "open-brain"


@pytest.mark.asyncio
async def test_weekly_briefing_inbox_pending_reflects_search_total_not_capped_results() -> (
    None
):
    """inbox_state.pending reports SearchResult.total, not the capped result length."""
    from open_brain.digest import generate_weekly_briefing

    active_memory = _memory(10, title="Open Brain planning")
    # Inbox search is capped at max_memories (200) but the true backlog is larger.
    inbox_sample = [_memory(100 + i, title=f"Pending {i}") for i in range(200)]
    dl = AsyncMock()
    dl.search.side_effect = [
        SearchResult(results=[active_memory], total=1),
        SearchResult(results=[], total=0),
        SearchResult(results=[active_memory], total=1),
        SearchResult(results=[], total=0),
        SearchResult(results=inbox_sample, total=350),
    ]

    result = await generate_weekly_briefing(dl, weeks_back=1, project="open-brain")

    assert result.inbox_state["pending"] == 350
    # The sample stays bounded to the small preview slice.
    assert len(result.inbox_state["sample"]) == 5
    assert result.inbox_state["sample"][0] == {
        "id": 100,
        "title": "Pending 0",
        "type": "observation",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_weekly_briefing_round_trip_uses_real_database(
    integration_database_url: str,
) -> None:
    """Weekly review uses existing briefing and includes canonical entities plus inbox state."""
    from open_brain.data_layer.postgres import PostgresDataLayer, get_pool
    from open_brain.digest import generate_weekly_briefing
    from open_brain.ingest.runs import ingest_run

    assert integration_database_url
    dl = PostgresDataLayer()
    project = "open-brain-5qo-weekly"
    current_time = datetime.now(tz=UTC) - timedelta(days=1)
    run_id: str | None = None

    async def set_created_at(memory_id: int, created_at: datetime) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE memories SET created_at = $2, updated_at = $2 WHERE id = $1",
                memory_id,
                created_at,
            )

    async def delete_project_index() -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM memory_indexes WHERE name = $1", project)

    try:
        with ingest_run() as current_run_id:
            run_id = current_run_id
            with patch.object(
                PostgresDataLayer, "_embed_and_link", new_callable=AsyncMock
            ):
                canonical = await dl.save_memory(
                    SaveMemoryParams(
                        text=f"Canonical Open Brain project {run_id}",
                        type="project",
                        project=project,
                        title="Open Brain",
                        metadata={
                            "canonical_entity": True,
                            "canonical_kind": "project",
                            "name": "Open Brain",
                            "capture_template": "project",
                            "entities": {},
                        },
                        capture_status="processed",
                        provenance={
                            "producer": "test-suite",
                            "source_ref": "test-suite:test_weekly_briefing",
                        },
                    )
                )
                pending = await dl.save_memory(
                    SaveMemoryParams(
                        text=f"Pending weekly inbox capture {run_id}",
                        type="observation",
                        project=project,
                        title="Pending weekly capture",
                        metadata={"capture_template": "observation", "entities": {}},
                        capture_status="inbox",
                        provenance={
                            "producer": "test-suite",
                            "source_ref": "test-suite:test_weekly_briefing",
                        },
                    )
                )

        await set_created_at(canonical.id, current_time)
        await set_created_at(pending.id, current_time)

        briefing = await generate_weekly_briefing(dl, weeks_back=1, project=project)

        canonical_ids = {entity["id"] for entity in briefing.canonical_entities}
        sample_ids = {entry["id"] for entry in briefing.inbox_state["sample"]}
        assert canonical.id in canonical_ids
        assert briefing.inbox_state["pending"] >= 1
        assert pending.id in sample_ids
    finally:
        if run_id is not None:
            await dl.delete_by_run_id(run_id)
        await delete_project_index()
