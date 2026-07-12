"""Tests for agent daily review workflows."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from open_brain.cli.main import _build_parser
from open_brain.data_layer.interface import Memory, SaveMemoryParams, SearchResult


RESOURCE_URL = "https://fhir-community.example/ig/stationaer-2026"


def _memory(
    memory_id: int,
    *,
    memory_type: str = "observation",
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
    created_at: str = "2026-07-12T10:00:00+00:00",
) -> Memory:
    """Create a Memory object for daily review tests."""
    return Memory(
        id=memory_id,
        index_id=1,
        session_id=None,
        type=memory_type,
        title=title or f"Memory {memory_id}",
        subtitle=None,
        narrative=None,
        content=f"Daily review memory {memory_id}",
        metadata=metadata or {},
        priority=0.5,
        stability="stable",
        access_count=0,
        last_accessed_at=None,
        created_at=created_at,
        updated_at=created_at,
        user_id=None,
        importance="medium",
    )


@pytest.mark.asyncio
async def test_generate_daily_review_date_bounds_entries_unresolved_and_sources() -> None:
    """Daily review composes date-bounded entries, unresolved inbox captures, and provenance."""
    from open_brain.digest import generate_daily_review

    entries = [
        _memory(
            1,
            memory_type="resource",
            title="FHIR community resource",
            metadata={"url": RESOURCE_URL, "capture_status": "processed"},
        ),
        _memory(
            2,
            memory_type="meeting",
            title="Transcript import",
            metadata={"source_ref": "macwhisper:session:abc123", "capture_status": "inbox"},
        ),
        _memory(
            3,
            memory_type="paperless_reference",
            title="Paperless contract",
            metadata={
                "paperless_reference": {
                    "document_id": 101,
                    "instance": "paperless-local",
                    "title": "Contract",
                },
                "capture_status": "inbox",
            },
        ),
        _memory(4, memory_type="journal", title="Raw journal", metadata={}),
    ]
    unresolved = [entries[1], entries[2]]
    dl = AsyncMock()
    dl.search.side_effect = [
        SearchResult(results=entries, total=len(entries)),
        SearchResult(results=unresolved, total=len(unresolved)),
    ]

    review = await generate_daily_review(
        dl,
        "2026-07-12",
        project="open-brain",
        tz="UTC",
    )

    assert review.date == "2026-07-12"
    assert review.counts == {
        "entries": 4,
        "unresolved": 2,
        "by_type": {"resource": 1, "meeting": 1, "paperless_reference": 1, "journal": 1},
    }
    assert review.entries[0] == {
        "id": 1,
        "title": "FHIR community resource",
        "type": "resource",
        "created_at": "2026-07-12T10:00:00+00:00",
        "source": {"kind": "url", "url": RESOURCE_URL},
    }
    assert review.entries[1]["source"] == {
        "kind": "source_ref",
        "source_ref": "macwhisper:session:abc123",
    }
    assert review.entries[2]["source"] == {
        "kind": "paperless",
        "document_id": 101,
        "instance": "paperless-local",
        "title": "Contract",
    }
    assert review.entries[3]["source"] is None
    assert review.unresolved_captures == [
        {"id": 2, "title": "Transcript import", "type": "meeting", "capture_status": "inbox"},
        {"id": 3, "title": "Paperless contract", "type": "paperless_reference", "capture_status": "inbox"},
    ]

    first_params = dl.search.await_args_list[0].args[0]
    second_params = dl.search.await_args_list[1].args[0]
    assert first_params.date_start == "2026-07-12T00:00:00+00:00"
    assert first_params.date_end == "2026-07-12T23:59:59.999999+00:00"
    assert first_params.project == "open-brain"
    assert first_params.limit == 200
    assert second_params.capture_status == "inbox"
    assert second_params.date_start == first_params.date_start
    assert second_params.date_end == first_params.date_end


@pytest.mark.asyncio
async def test_daily_review_tool_is_base_memory_scope_and_serializes_result() -> None:
    """The MCP wrapper should not require the evolution scope used by weekly briefing."""
    from open_brain.server import _current_scopes, daily_review

    dl = AsyncMock()
    dl.search.side_effect = [
        SearchResult(results=[_memory(10, metadata={"session_ref": "session-2026-07-12"})], total=1),
        SearchResult(results=[], total=0),
    ]

    token = _current_scopes.set(("memory",))
    try:
        with patch("open_brain.server.get_dl", return_value=dl):
            payload = json.loads(await daily_review("2026-07-12", project="open-brain"))
    finally:
        _current_scopes.reset(token)

    assert payload["date"] == "2026-07-12"
    assert payload["entries"][0]["source"] == {
        "kind": "session_ref",
        "session_ref": "session-2026-07-12",
    }


def test_daily_cli_parser_accepts_optional_date_and_project() -> None:
    """The ob CLI exposes a thin daily_review wrapper."""
    args = _build_parser().parse_args(["daily", "2026-07-12", "--project", "open-brain"])

    assert args.command == "daily"
    assert args.date == "2026-07-12"
    assert args.project == "open-brain"


@pytest.mark.asyncio
async def test_daily_cli_calls_daily_review_tool() -> None:
    """The daily command forwards date and project to the MCP tool."""
    from open_brain.cli.main import _cmd_daily

    args = _build_parser().parse_args(["daily", "2026-07-12", "--project", "open-brain"])
    with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {"date": "2026-07-12"}
        await _cmd_daily(args)

    mock_call.assert_awaited_once_with(
        "daily_review",
        {"date": "2026-07-12", "project": "open-brain"},
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_daily_review_round_trip_uses_real_database(
    integration_database_url: str,
) -> None:
    """Daily review summarizes the selected date with source links and unresolved captures."""
    from open_brain.data_layer.postgres import PostgresDataLayer, get_pool
    from open_brain.digest import generate_daily_review
    from open_brain.ingest.runs import ingest_run

    assert integration_database_url
    dl = PostgresDataLayer()
    project = "open-brain-5qo-daily"
    selected_day = datetime(2026, 7, 12, tzinfo=UTC)
    outside_day = selected_day - timedelta(days=1)
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
            with patch.object(PostgresDataLayer, "_embed_and_link", new_callable=AsyncMock):
                resource = await dl.save_memory(
                    SaveMemoryParams(
                        text=f"Daily resource {run_id}",
                        type="resource",
                        project=project,
                        title="FHIR community resource",
                        metadata={"url": RESOURCE_URL, "capture_template": "resource", "entities": {}},
                        capture_status="processed",
                    )
                )
                unresolved = await dl.save_memory(
                    SaveMemoryParams(
                        text=f"Daily inbox capture {run_id}",
                        type="journal",
                        project=project,
                        title="Unresolved journal",
                        metadata={"capture_template": "journal", "entities": {}},
                        capture_status="inbox",
                    )
                )
                outside = await dl.save_memory(
                    SaveMemoryParams(
                        text=f"Outside inbox capture {run_id}",
                        type="journal",
                        project=project,
                        title="Outside journal",
                        metadata={"capture_template": "journal", "entities": {}},
                        capture_status="inbox",
                    )
                )

        await set_created_at(resource.id, selected_day.replace(hour=9))
        await set_created_at(unresolved.id, selected_day.replace(hour=10))
        await set_created_at(outside.id, outside_day.replace(hour=10))

        review = await generate_daily_review(dl, "2026-07-12", project=project)

        entry_ids = {entry["id"] for entry in review.entries}
        unresolved_ids = {entry["id"] for entry in review.unresolved_captures}
        assert {resource.id, unresolved.id}.issubset(entry_ids)
        assert outside.id not in entry_ids
        assert unresolved_ids == {unresolved.id}
        resource_entry = next(entry for entry in review.entries if entry["id"] == resource.id)
        assert resource_entry["source"] == {"kind": "url", "url": RESOURCE_URL}
    finally:
        if run_id is not None:
            await dl.delete_by_run_id(run_id)
        await delete_project_index()


def test_daily_review_documentation_mentions_agent_operation() -> None:
    """The agent workflow documentation covers daily review."""
    doc = Path(__file__).resolve().parents[2] / "docs" / "features" / "agent-knowledge-workflows.md"

    assert doc.exists()
    text = doc.read_text(encoding="utf-8")
    assert "daily_review" in text
    assert "ob daily" in text
