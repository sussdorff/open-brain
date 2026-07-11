"""Tests for canonical entity identity and approved maintenance paths."""

from unittest.mock import AsyncMock, patch
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
