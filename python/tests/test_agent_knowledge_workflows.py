"""End-to-end evidence for agent knowledge capture and inbox review workflows."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from open_brain.data_layer.postgres import PostgresDataLayer


RESOURCE_URL = "https://fhir-community.example/ig/stationaer-2026"


def _workflow_doc_text() -> str:
    """Return the agent workflow documentation text."""
    doc = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "features"
        / "agent-knowledge-workflows.md"
    )
    assert doc.exists()
    return doc.read_text(encoding="utf-8")


async def _delete_project_index(project: str) -> None:
    """Delete a test-only project index after its run memories have been removed."""
    from open_brain.data_layer.postgres import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM memory_indexes WHERE name = $1", project)


def _capture_payloads(run_id: str) -> list[dict[str, Any]]:
    """Return deterministic pre-structured capture payloads for the agent workflow."""
    return [
        {
            "text": f"Idea capture for agent workflows {run_id}",
            "type": "observation",
            "title": "Idea capture",
            "metadata": {
                "capture_template": "observation",
                "status": "open",
                "entities": {"projects": ["Open Brain"]},
            },
        },
        {
            "text": f"Journal reflection for agent workflows {run_id}",
            "type": "journal",
            "title": "Journal capture",
            "metadata": {
                "capture_template": "journal",
                "entry_date": "2026-07-12T08:00:00",
                "mood": "focused",
                "themes": ["agent workflows"],
                "reflection": "Agents can replace manual note capture.",
                "status": "open",
                "entities": {"projects": ["Open Brain"]},
            },
        },
        {
            "text": f"URL resource capture for agent workflows {run_id}",
            "type": "resource",
            "title": "URL resource capture",
            "metadata": {
                "capture_template": "resource",
                "url": RESOURCE_URL,
                "source_type": "web",
                "summary": "Reference resource captured by an agent.",
                "status": "open",
                "entities": {"projects": ["Open Brain"]},
            },
        },
        {
            "text": f"Structured concept capture for agent workflows {run_id}",
            "type": "concept",
            "title": "Structured knowledge capture",
            "metadata": {
                "capture_template": "concept",
                "name": "Agent knowledge workflows",
                "domain": "personal knowledge",
                "summary": "Supported operations for capture and review through agents.",
                "related_concepts": ["Open Brain"],
                "status": "open",
                "entities": {
                    "projects": ["Open Brain"],
                    "concepts": ["Agent knowledge workflows"],
                },
            },
        },
    ]


def test_documentation_covers_agent_capture_and_inbox_review_operations() -> None:
    """Agent docs name every supported AC1/AC2 operation."""
    text = _workflow_doc_text()

    for expected in (
        "Idea capture",
        "Journal capture",
        "URL resource capture",
        "structured knowledge item",
        "review outstanding inbox captures",
        "save_memory",
        'search(capture_status="inbox"',
        "set_capture_status",
    ):
        assert expected in text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_can_capture_supported_knowledge_items(
    integration_database_url: str,
) -> None:
    """An agent can capture idea, journal, URL resource, and structured knowledge items."""
    from open_brain import server
    from open_brain.ingest.runs import ingest_run

    assert integration_database_url
    dl = PostgresDataLayer()
    run_id: str | None = None
    project: str | None = None

    try:
        with ingest_run() as current_run_id:
            run_id = current_run_id
            project = f"open-brain-5qo-agent-{run_id}"
            with (
                patch("open_brain.server.get_dl", return_value=dl),
                patch.object(
                    PostgresDataLayer, "_embed_and_link", new_callable=AsyncMock
                ),
            ):
                saved_ids: list[int] = []
                for payload in _capture_payloads(run_id):
                    result = json.loads(
                        await server.save_memory(
                            project=project,
                            **payload,
                            provenance={
                                "producer": "test-suite",
                                "source_ref": "test-suite:test_agent_knowledge_workflows",
                            },
                        )
                    )
                    saved_ids.append(result["id"])

                inbox = json.loads(
                    await server.search(
                        project=project,
                        capture_status="inbox",
                        metadata_filter={"run_id": run_id},
                        limit=10,
                    )
                )

        inbox_by_id = {entry["id"]: entry for entry in inbox["results"]}
        assert set(saved_ids) == set(inbox_by_id)
        assert {entry["type"] for entry in inbox_by_id.values()} == {
            "observation",
            "journal",
            "resource",
            "concept",
        }
        resource = next(
            entry for entry in inbox_by_id.values() if entry["type"] == "resource"
        )
        assert resource["metadata"]["url"] == RESOURCE_URL
        assert all(
            entry["metadata"]["capture_status"] == "inbox"
            for entry in inbox_by_id.values()
        )
    finally:
        if run_id is not None:
            await dl.delete_by_run_id(run_id)
        if project is not None:
            await _delete_project_index(project)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_can_review_and_process_outstanding_inbox_captures(
    integration_database_url: str,
) -> None:
    """Inbox review processes captures without mutating lifecycle status."""
    from open_brain import server
    from open_brain.ingest.runs import ingest_run

    assert integration_database_url
    dl = PostgresDataLayer()
    run_id: str | None = None
    project: str | None = None

    try:
        with ingest_run() as current_run_id:
            run_id = current_run_id
            project = f"open-brain-5qo-inbox-{run_id}"
            with (
                patch("open_brain.server.get_dl", return_value=dl),
                patch.object(
                    PostgresDataLayer, "_embed_and_link", new_callable=AsyncMock
                ),
            ):
                saved_ids: list[int] = []
                for payload in _capture_payloads(run_id):
                    result = json.loads(
                        await server.save_memory(
                            project=project,
                            **payload,
                            provenance={
                                "producer": "test-suite",
                                "source_ref": "test-suite:test_agent_knowledge_workflows",
                            },
                        )
                    )
                    saved_ids.append(result["id"])

                for memory_id in saved_ids:
                    transition = json.loads(
                        await server.set_capture_status(
                            memory_id=memory_id,
                            capture_status="processed",
                        )
                    )
                    assert transition["id"] == memory_id

                inbox_after = json.loads(
                    await server.search(
                        project=project,
                        capture_status="inbox",
                        metadata_filter={"run_id": run_id},
                        limit=10,
                    )
                )
                all_captures = json.loads(
                    await server.search(
                        project=project,
                        metadata_filter={"run_id": run_id},
                        limit=10,
                    )
                )

        assert inbox_after["results"] == []
        by_id = {entry["id"]: entry for entry in all_captures["results"]}
        assert set(by_id) == set(saved_ids)
        for entry in by_id.values():
            assert entry["metadata"]["capture_status"] == "processed"
            assert entry["metadata"]["status"] == "open"
    finally:
        if run_id is not None:
            await dl.delete_by_run_id(run_id)
        if project is not None:
            await _delete_project_index(project)
