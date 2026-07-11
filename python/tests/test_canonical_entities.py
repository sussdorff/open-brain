"""Tests for canonical entity identity and approved maintenance paths."""

from unittest.mock import AsyncMock, MagicMock, patch
import json

import pytest

from open_brain.data_layer.interface import Memory, SearchResult


def _memory(
    memory_id: int,
    *,
    memory_type: str = "observation",
    metadata: dict | None = None,
    title: str = "Canonical entity",
) -> Memory:
    return Memory(
        id=memory_id,
        index_id=1,
        session_id=None,
        type=memory_type,
        title=title,
        subtitle=None,
        narrative=None,
        content="Entity content",
        metadata=metadata or {},
        priority=0.8,
        stability="stable",
        access_count=0,
        last_accessed_at=None,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        user_id=None,
        importance="medium",
    )


def test_canonical_entity_identity_uses_memory_id_and_metadata_kind() -> None:
    """Canonical entity identity must reuse memory.id and metadata.canonical_kind."""
    from open_brain.data_layer.interface import (
        CANONICAL_KINDS,
        canonical_entity_identity,
    )

    memory = _memory(
        314,
        memory_type="person",
        metadata={"canonical_entity": True, "canonical_kind": "person"},
    )

    assert CANONICAL_KINDS == frozenset({"person", "project", "organization", "concept"})
    assert canonical_entity_identity(memory) == {"id": 314, "kind": "person"}


def test_invalid_canonical_kind_is_not_exposed_as_identity() -> None:
    """Invalid canonical_kind values fail closed instead of producing identities."""
    from open_brain.data_layer.interface import canonical_entity_identity

    memory = _memory(
        315,
        metadata={"canonical_entity": True, "canonical_kind": "topic"},
    )

    assert canonical_entity_identity(memory) is None


@pytest.mark.asyncio
async def test_get_observations_exposes_canonical_entity_read_payload() -> None:
    """Supported observation reads expose stable id and canonical kind."""
    from open_brain import server

    memory = _memory(
        501,
        memory_type="project",
        metadata={"canonical_entity": True, "canonical_kind": "project"},
        title="Open Brain",
    )
    fake_dl = AsyncMock()
    fake_dl.get_observations = AsyncMock(return_value=[memory])

    with patch("open_brain.server.get_dl", return_value=fake_dl):
        payload = json.loads(await server.get_observations([501]))

    assert payload[0]["id"] == 501
    assert payload[0]["metadata"]["canonical_kind"] == "project"
    assert payload[0]["canonical_entity"] == {"id": 501, "kind": "project"}


@pytest.mark.asyncio
async def test_search_exposes_canonical_entity_read_payload() -> None:
    """Supported search reads expose stable id and canonical kind."""
    from open_brain import server

    memory = _memory(
        601,
        memory_type="organization",
        metadata={"canonical_entity": True, "canonical_kind": "organization"},
        title="Cognovis",
    )
    fake_dl = AsyncMock()
    fake_dl.search = AsyncMock(return_value=SearchResult(results=[memory], total=1))

    with patch("open_brain.server.get_dl", return_value=fake_dl):
        payload = json.loads(await server.search(query="cognovis"))

    assert payload["results"][0]["id"] == 601
    assert payload["results"][0]["metadata"]["canonical_kind"] == "organization"
    assert payload["results"][0]["canonical_entity"] == {
        "id": 601,
        "kind": "organization",
    }


@pytest.mark.asyncio
async def test_approved_canonical_update_preserves_id_and_appends_audit() -> None:
    """Explicit approved updates preserve memory.id and append audit metadata."""
    from open_brain.data_layer.interface import ApprovedCanonicalEntityUpdateParams
    from open_brain.data_layer.postgres import PostgresDataLayer

    existing = {
        "id": 701,
        "metadata": {"canonical_entity": True, "canonical_kind": "concept"},
        "title": "Original",
        "subtitle": None,
        "narrative": None,
        "content": "Original content",
    }
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=existing)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
        result = await PostgresDataLayer().approved_update_canonical_entity(
            ApprovedCanonicalEntityUpdateParams(
                id=701,
                actor="test-runner",
                note="Approved correction",
                metadata={"canonical_kind": "concept", "reviewed": True},
            )
        )

    assert result.id == 701
    sql, metadata_arg, *_ = conn.execute.call_args.args
    assert "canonical_entity" not in sql
    assert metadata_arg["canonical_entity"] is True
    assert metadata_arg["canonical_kind"] == "concept"
    assert metadata_arg["reviewed"] is True
    assert metadata_arg["audit"][-1]["op"] == "update"
    assert metadata_arg["audit"][-1]["actor"] == "test-runner"
    assert metadata_arg["audit"][-1]["note"] == "Approved correction"


@pytest.mark.asyncio
async def test_approved_canonical_archive_sets_status_and_preserves_audit() -> None:
    """Explicit archival is soft archival with append-only audit metadata."""
    from open_brain.data_layer.interface import ApprovedCanonicalEntityUpdateParams
    from open_brain.data_layer.postgres import PostgresDataLayer

    existing_audit = [{"op": "update", "at": "2026-01-01T00:00:00+00:00", "actor": "a", "note": "old"}]
    existing = {
        "id": 702,
        "metadata": {
            "canonical_entity": True,
            "canonical_kind": "project",
            "audit": existing_audit,
        },
        "title": "Project",
        "subtitle": None,
        "narrative": None,
        "content": "Project content",
    }
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=existing)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
        result = await PostgresDataLayer().approved_update_canonical_entity(
            ApprovedCanonicalEntityUpdateParams(
                id=702,
                actor="test-runner",
                note="Approved archive",
                operation="archive",
            )
        )

    assert result.id == 702
    metadata_arg = conn.execute.call_args.args[1]
    assert metadata_arg["status"] == "archived"
    assert metadata_arg["audit"][0] == existing_audit[0]
    assert metadata_arg["audit"][1]["op"] == "archive"
    assert metadata_arg["audit"][1]["actor"] == "test-runner"


@pytest.mark.asyncio
async def test_approved_canonical_update_tool_uses_dedicated_data_layer_path() -> None:
    """The MCP tool surface uses the explicit approved bypass method."""
    from open_brain import server
    from open_brain.data_layer.interface import SaveMemoryResult

    fake_dl = AsyncMock()
    fake_dl.approved_update_canonical_entity = AsyncMock(
        return_value=SaveMemoryResult(id=801, message="Canonical entity update approved")
    )

    with patch("open_brain.server.get_dl", return_value=fake_dl):
        payload = json.loads(
            await server.approved_canonical_entity_update(
                id=801,
                actor="test-runner",
                note="Approved archive",
                operation="archive",
            )
        )

    assert payload == {"id": 801, "message": "Canonical entity update approved"}
    params = fake_dl.approved_update_canonical_entity.await_args.args[0]
    assert params.id == 801
    assert params.operation == "archive"
    assert params.note == "Approved archive"
