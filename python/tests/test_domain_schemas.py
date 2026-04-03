"""Tests for domain-typed metadata schemas (open-brain-17x).

AK1: save_memory with type=event validates when field is ISO datetime
AK2: save_memory with type=person stores structured person metadata
AK3: search with type=event returns only event memories
AK4: Unknown types still work (no breaking change)
AK5: Schemas documented in MCP tool description
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from open_brain.data_layer.interface import (
    Memory,
    SaveMemoryResult,
    SearchResult,
)


def _make_memory(id: int = 1, type: str = "observation", metadata: dict | None = None, **kwargs) -> Memory:
    """Create a sample Memory for testing."""
    defaults = dict(
        index_id=1,
        session_id=None,
        type=type,
        title="Test Memory",
        subtitle=None,
        narrative=None,
        content="Test content",
        metadata=metadata or {},
        priority=0.5,
        stability="stable",
        access_count=0,
        last_accessed_at=None,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    defaults.update(kwargs)
    return Memory(id=id, **defaults)


@pytest.fixture
def mock_dl():
    """Mock DataLayer."""
    dl = AsyncMock()
    dl.save_memory.return_value = SaveMemoryResult(id=42, message="Memory saved")
    dl.update_memory.return_value = SaveMemoryResult(id=42, message="Memory updated")
    dl.search.return_value = SearchResult(results=[], total=0)
    return dl


# ─── AK1: Event type validates `when` field as ISO datetime ───────────────────

class TestEventValidation:
    @pytest.mark.asyncio
    async def test_event_with_valid_when_saves_without_warning(self, mock_dl):
        """AK1: type=event with valid ISO datetime in `when` saves cleanly."""
        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", new=AsyncMock(return_value={})),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            result = await save_memory(
                text="Team meeting tomorrow",
                type="event",
                metadata={"when": "2026-04-15T10:00:00", "who": ["Alice", "Bob"]},
            )
            data = json.loads(result)
            assert data["id"] == 42
            # No warning key expected for valid datetime
            assert "warning" not in data

    @pytest.mark.asyncio
    async def test_event_with_invalid_when_saves_with_warning(self, mock_dl):
        """AK1: type=event with non-ISO `when` field saves but includes a warning."""
        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", new=AsyncMock(return_value={})),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            result = await save_memory(
                text="Team meeting sometime",
                type="event",
                metadata={"when": "next Tuesday"},
            )
            data = json.loads(result)
            assert data["id"] == 42
            # Warning expected for invalid ISO datetime
            assert "warning" in data
            assert "when" in data["warning"].lower()

    @pytest.mark.asyncio
    async def test_event_without_when_saves_with_warning(self, mock_dl):
        """AK1: type=event without `when` field saves but includes a warning."""
        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", new=AsyncMock(return_value={})),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            result = await save_memory(
                text="Some event without time",
                type="event",
                metadata={"who": ["Charlie"]},
            )
            data = json.loads(result)
            assert data["id"] == 42
            assert "warning" in data
            assert "when" in data["warning"].lower()

    @pytest.mark.asyncio
    async def test_event_with_no_metadata_saves_with_warning(self, mock_dl):
        """AK1: type=event with no metadata at all saves but warns about missing `when`."""
        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", new=AsyncMock(return_value={})),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            result = await save_memory(
                text="Birthday party",
                type="event",
            )
            data = json.loads(result)
            assert data["id"] == 42
            assert "warning" in data


# ─── AK2: Person type stores structured metadata ──────────────────────────────

class TestPersonMetadata:
    @pytest.mark.asyncio
    async def test_person_metadata_stored_as_is(self, mock_dl):
        """AK2: type=person with structured metadata is saved without modification."""
        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", new=AsyncMock(return_value={})),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            person_meta = {
                "name": "Alice Smith",
                "org": "Acme Corp",
                "role": "CTO",
                "relationship": "client",
                "last_contact": "2026-04-01T14:00:00",
            }
            result = await save_memory(
                text="Met with Alice from Acme Corp",
                type="person",
                metadata=person_meta,
            )
            data = json.loads(result)
            assert data["id"] == 42
            # Verify save_memory was called with the person metadata
            call_args = mock_dl.save_memory.call_args[0][0]
            assert call_args.metadata == person_meta
            assert call_args.type == "person"

    @pytest.mark.asyncio
    async def test_person_with_invalid_last_contact_saves_with_warning(self, mock_dl):
        """AK2: type=person with non-ISO last_contact saves but warns."""
        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", new=AsyncMock(return_value={})),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            result = await save_memory(
                text="Called Bob last week",
                type="person",
                metadata={"name": "Bob", "last_contact": "last week"},
            )
            data = json.loads(result)
            assert data["id"] == 42
            assert "warning" in data


# ─── AK3: Search with type=event returns only event memories ─────────────────

class TestSearchTypeFilter:
    @pytest.mark.asyncio
    async def test_search_type_event_passes_filter_to_data_layer(self, mock_dl):
        """AK3: search(type='event') passes the type filter through to the DataLayer."""
        event_memory = _make_memory(id=10, type="event", metadata={"when": "2026-04-15T10:00:00"})
        mock_dl.search.return_value = SearchResult(results=[event_memory], total=1)

        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import search
            result = await search(query="meeting", type="event")
            data = json.loads(result)

        assert data["total"] == 1
        assert data["results"][0]["type"] == "event"
        # Verify the data layer was called with type="event"
        call_args = mock_dl.search.call_args[0][0]
        assert call_args.type == "event"

    @pytest.mark.asyncio
    async def test_search_type_event_excludes_other_types(self, mock_dl):
        """AK3: structural test — when DL filters by type, only event memories come back."""
        # DL returns only event memories when type="event" is passed
        mock_dl.search.return_value = SearchResult(results=[], total=0)

        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import search
            result = await search(query="meeting", type="event")
            data = json.loads(result)

        assert data["total"] == 0
        call_args = mock_dl.search.call_args[0][0]
        assert call_args.type == "event"


# ─── AK4: Unknown types work without breaking ─────────────────────────────────

class TestUnknownTypes:
    @pytest.mark.asyncio
    async def test_unknown_type_saves_without_validation_error(self, mock_dl):
        """AK4: type='custom_thing' (unknown) passes through without error or warning."""
        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", new=AsyncMock(return_value={})),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            result = await save_memory(
                text="Some custom memory",
                type="custom_thing",
                metadata={"foo": "bar"},
            )
            data = json.loads(result)
            assert data["id"] == 42
            assert "warning" not in data

    @pytest.mark.asyncio
    async def test_none_type_saves_without_validation_error(self, mock_dl):
        """AK4: type=None (no type) saves without any warning."""
        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", new=AsyncMock(return_value={})),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            result = await save_memory(
                text="A memory with no type",
                metadata={"some": "data"},
            )
            data = json.loads(result)
            assert data["id"] == 42
            assert "warning" not in data


# ─── AK5: Schemas documented in MCP tool description ─────────────────────────

class TestToolDescriptionDocumentation:
    def test_save_memory_description_mentions_event_schema(self):
        """AK5: save_memory tool description documents the event metadata schema."""
        import open_brain.server as srv
        # Get the registered tool function and check its description
        tool_fn = srv.save_memory
        # The MCP tool description is stored as a decorator attribute
        # We check the module-level tool registry or the function docstring/description
        description = _get_tool_description(srv, "save_memory")
        assert description is not None
        assert "event" in description.lower()
        assert "when" in description.lower()

    def test_save_memory_description_mentions_person_schema(self):
        """AK5: save_memory tool description documents the person metadata schema."""
        import open_brain.server as srv
        description = _get_tool_description(srv, "save_memory")
        assert description is not None
        assert "person" in description.lower()

    def test_save_memory_description_mentions_domain_schemas(self):
        """AK5: save_memory tool description mentions the domain schema concept."""
        import open_brain.server as srv
        description = _get_tool_description(srv, "save_memory")
        assert description is not None
        # Should mention metadata structure / schemas
        assert any(term in description.lower() for term in ["schema", "metadata", "structured"])


def _get_tool_description(srv_module, tool_name: str) -> str | None:
    """Extract the MCP tool description by inspecting the mcp server registry."""
    mcp = srv_module.mcp
    # FastMCP stores tools in _tool_manager or similar; try common locations
    for attr in ("_tool_manager", "tool_manager", "_tools", "tools"):
        manager = getattr(mcp, attr, None)
        if manager is not None:
            tools = getattr(manager, "_tools", None) or getattr(manager, "tools", None)
            if isinstance(tools, dict) and tool_name in tools:
                tool = tools[tool_name]
                return getattr(tool, "description", None)
    return None


# ─── Interface: validate_domain_metadata function ─────────────────────────────

class TestValidateDomainMetadata:
    def test_validate_event_with_valid_when_returns_no_warnings(self):
        """validate_domain_metadata returns [] for valid event with ISO when."""
        from open_brain.data_layer.interface import validate_domain_metadata
        warnings = validate_domain_metadata("event", {"when": "2026-04-15T10:00:00"})
        assert warnings == []

    def test_validate_event_without_when_returns_warning(self):
        """validate_domain_metadata returns warning for event without `when`."""
        from open_brain.data_layer.interface import validate_domain_metadata
        warnings = validate_domain_metadata("event", {})
        assert len(warnings) == 1
        assert "when" in warnings[0].lower()

    def test_validate_event_with_invalid_when_returns_warning(self):
        """validate_domain_metadata returns warning for event with non-ISO `when`."""
        from open_brain.data_layer.interface import validate_domain_metadata
        warnings = validate_domain_metadata("event", {"when": "next Tuesday"})
        assert len(warnings) == 1
        assert "when" in warnings[0].lower()

    def test_validate_person_with_valid_last_contact_returns_no_warnings(self):
        """validate_domain_metadata returns [] for person with valid last_contact."""
        from open_brain.data_layer.interface import validate_domain_metadata
        warnings = validate_domain_metadata("person", {"last_contact": "2026-04-01T14:00:00"})
        assert warnings == []

    def test_validate_person_with_invalid_last_contact_returns_warning(self):
        """validate_domain_metadata returns warning for person with non-ISO last_contact."""
        from open_brain.data_layer.interface import validate_domain_metadata
        warnings = validate_domain_metadata("person", {"last_contact": "last week"})
        assert len(warnings) == 1
        assert "last_contact" in warnings[0].lower()

    def test_validate_unknown_type_returns_no_warnings(self):
        """validate_domain_metadata returns [] for unknown types."""
        from open_brain.data_layer.interface import validate_domain_metadata
        warnings = validate_domain_metadata("custom_thing", {"foo": "bar"})
        assert warnings == []

    def test_validate_none_type_returns_no_warnings(self):
        """validate_domain_metadata returns [] when type is None."""
        from open_brain.data_layer.interface import validate_domain_metadata
        warnings = validate_domain_metadata(None, {"foo": "bar"})
        assert warnings == []

    def test_validate_household_with_valid_warranty_expiry_returns_no_warnings(self):
        """validate_domain_metadata returns [] for household with valid warranty_expiry."""
        from open_brain.data_layer.interface import validate_domain_metadata
        warnings = validate_domain_metadata("household", {"item": "Washing machine", "warranty_expiry": "2028-06-01T00:00:00"})
        assert warnings == []

    def test_validate_household_with_invalid_warranty_expiry_returns_warning(self):
        """validate_domain_metadata returns warning for household with non-ISO warranty_expiry."""
        from open_brain.data_layer.interface import validate_domain_metadata
        warnings = validate_domain_metadata("household", {"item": "Dishwasher", "warranty_expiry": "next year"})
        assert len(warnings) == 1
        assert "warranty_expiry" in warnings[0].lower()

    def test_validate_household_without_warranty_expiry_returns_no_warnings(self):
        """validate_domain_metadata returns [] for household with no warranty_expiry (optional field)."""
        from open_brain.data_layer.interface import validate_domain_metadata
        warnings = validate_domain_metadata("household", {"item": "Chair"})
        assert warnings == []

    def test_validate_meeting_with_valid_date_returns_no_warnings(self):
        """validate_domain_metadata returns [] for meeting with valid ISO date."""
        from open_brain.data_layer.interface import validate_domain_metadata
        warnings = validate_domain_metadata("meeting", {"date": "2026-04-10T09:00:00", "topic": "Quarterly review"})
        assert warnings == []

    def test_validate_meeting_with_invalid_date_returns_warning(self):
        """validate_domain_metadata returns warning for meeting with non-ISO date."""
        from open_brain.data_layer.interface import validate_domain_metadata
        warnings = validate_domain_metadata("meeting", {"date": "tomorrow morning"})
        assert len(warnings) == 1
        assert "date" in warnings[0].lower()

    def test_domain_typed_dicts_importable(self):
        """EventMetadata, PersonMetadata etc. are importable from interface."""
        from open_brain.data_layer.interface import (
            DecisionMetadata,
            EventMetadata,
            HouseholdMetadata,
            MeetingMetadata,
            PersonMetadata,
        )
        # Verify they are TypedDict-like (have __annotations__)
        assert hasattr(EventMetadata, "__annotations__")
        assert "when" in EventMetadata.__annotations__
        assert hasattr(PersonMetadata, "__annotations__")
        assert "name" in PersonMetadata.__annotations__
        assert hasattr(HouseholdMetadata, "__annotations__")
        assert hasattr(DecisionMetadata, "__annotations__")
        assert hasattr(MeetingMetadata, "__annotations__")
