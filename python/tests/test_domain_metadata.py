"""Backward-compatibility tests for canonical domain metadata.

AC2: Existing capture templates and historical memory types remain readable and valid
while the new canonical personal-knowledge types gain schema coverage.
"""

from __future__ import annotations

import pytest

from open_brain.data_layer.interface import Memory, validate_domain_metadata


def _make_memory(id: int = 1, type: str = "observation", metadata: dict | None = None, **kwargs) -> Memory:
    """Create a sample Memory for compatibility tests."""
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


class TestCanonicalMetadataSchemas:
    def test_new_canonical_typed_dicts_importable(self):
        """New canonical domain metadata schemas are importable and field-specific."""
        from open_brain.data_layer.interface import (
            ConceptMetadata,
            CorrespondenceMetadata,
            JournalMetadata,
            ProjectMetadata,
            PromptMetadata,
            ResourceMetadata,
        )

        assert "name" in ProjectMetadata.__annotations__
        assert "published_at" in ResourceMetadata.__annotations__
        assert "related_concepts" in ConceptMetadata.__annotations__
        assert "entry_date" in JournalMetadata.__annotations__
        assert "occurred_at" in CorrespondenceMetadata.__annotations__
        assert "prompt_text" in PromptMetadata.__annotations__

    @pytest.mark.parametrize(
        ("memory_type", "metadata"),
        [
            (
                "project",
                {
                    "name": "Open Brain",
                    "status": "active",
                    "owner": "Malte",
                    "goals": ["Canonical capture routing"],
                    "next_actions": ["Ship schema coverage"],
                    "repository": "open-brain",
                    "due_date": "2026-08-01T00:00:00",
                },
            ),
            (
                "resource",
                {
                    "title": "pgvector indexing guide",
                    "url": "https://example.invalid/pgvector",
                    "source_type": "documentation",
                    "author": "pgvector maintainers",
                    "summary": "Explains index tradeoffs.",
                    "published_at": "2026-07-01T12:00:00",
                },
            ),
            (
                "concept",
                {
                    "name": "Reciprocal rank fusion",
                    "domain": "search",
                    "summary": "Combines multiple rankings.",
                    "related_concepts": ["hybrid search"],
                },
            ),
            (
                "journal",
                {
                    "entry_date": "2026-07-11",
                    "mood": "focused",
                    "themes": ["implementation"],
                    "reflection": "TDD made the scope explicit.",
                },
            ),
            (
                "correspondence",
                {
                    "with": ["Alice"],
                    "channel": "email",
                    "direction": "inbound",
                    "subject": "Q3 roadmap",
                    "summary": "Alice asked for the roadmap.",
                    "occurred_at": "2026-07-10T09:30:00",
                    "follow_up_needed": True,
                },
            ),
            (
                "prompt",
                {
                    "purpose": "Summarize a bead",
                    "prompt_text": "Summarize the bead and list tests.",
                    "target_model": "Codex",
                    "variables": ["bead"],
                    "constraints": ["include tests"],
                    "last_used_at": "2026-07-11T10:00:00",
                },
            ),
        ],
    )
    def test_new_canonical_types_accept_valid_metadata(self, memory_type, metadata):
        """Valid metadata for new canonical types produces no warnings."""
        assert validate_domain_metadata(memory_type, metadata) == []

    @pytest.mark.parametrize(
        ("memory_type", "metadata", "field_name"),
        [
            ("project", {"due_date": "soon"}, "due_date"),
            ("resource", {"published_at": "recently"}, "published_at"),
            ("journal", {"entry_date": "today"}, "entry_date"),
            ("correspondence", {"occurred_at": "last week"}, "occurred_at"),
            ("prompt", {"last_used_at": "yesterday"}, "last_used_at"),
        ],
    )
    def test_new_canonical_types_warn_for_invalid_datetime_metadata(
        self,
        memory_type,
        metadata,
        field_name,
    ):
        """Datetime-like fields on new canonical types are validated as ISO values."""
        warnings = validate_domain_metadata(memory_type, metadata)

        assert len(warnings) == 1
        assert field_name in warnings[0]


class TestHistoricalMetadataCompatibility:
    @pytest.mark.parametrize(
        "memory_type",
        [
            "person_context",
            "insight",
            "learning",
            "observation",
            "discovery",
            "change",
            "feature",
            "bugfix",
            "refactor",
            "session_summary",
        ],
    )
    def test_historical_and_dev_oriented_types_remain_pass_through(self, memory_type):
        """Historical/free-form types remain valid under the permissive AK4 contract."""
        assert validate_domain_metadata(memory_type, {"capture_template": memory_type}) == []

    @pytest.mark.parametrize(
        ("memory_type", "metadata"),
        [
            ("decision", {"what": "Use PostgreSQL"}),
            ("meeting", {"date": "2026-07-11T10:00:00", "topic": "Planning"}),
            ("person", {"name": "Alice", "last_contact": "2026-07-10T09:00:00"}),
            ("event", {"when": "2026-07-11T12:00:00"}),
            ("household", {"item": "Router", "warranty_expiry": "2028-01-01T00:00:00"}),
            ("mention", {"person_ref": "person-42"}),
            ("interaction", {"person_ref": "person-42", "occurred_at": "2026-07-11T10:00:00"}),
        ],
    )
    def test_existing_structured_types_remain_valid(self, memory_type, metadata):
        """Existing schema branches keep their current warning-free valid cases."""
        assert validate_domain_metadata(memory_type, metadata) == []

    @pytest.mark.parametrize(
        "memory_type",
        [
            "decision",
            "meeting",
            "person_context",
            "person",
            "insight",
            "event",
            "learning",
            "observation",
            "household",
            "mention",
            "interaction",
            "discovery",
            "change",
            "feature",
            "bugfix",
            "refactor",
            "session_summary",
        ],
    )
    def test_historical_memory_types_remain_readable(self, memory_type):
        """Existing memory type strings can still be carried by Memory records."""
        memory = _make_memory(type=memory_type, metadata={"capture_template": memory_type})

        assert memory.type == memory_type
        assert memory.metadata["capture_template"] == memory_type


class TestVocabularyConsolidation:
    """AC1/AC2: interface.py's personal-knowledge TypedDict schemas stay in
    sync with the single canonical vocabulary source
    (open_brain.data_layer.personal_knowledge_vocabulary), and the existing
    validation behavior is unchanged after consolidation."""

    def test_personal_knowledge_schemas_cover_exactly_the_canonical_types(self):
        from open_brain.data_layer.interface import PERSONAL_KNOWLEDGE_METADATA_SCHEMAS
        from open_brain.data_layer.personal_knowledge_vocabulary import (
            CANONICAL_PERSONAL_KNOWLEDGE_TYPES,
        )

        assert set(PERSONAL_KNOWLEDGE_METADATA_SCHEMAS) == set(CANONICAL_PERSONAL_KNOWLEDGE_TYPES)

    @pytest.mark.parametrize(
        "memory_type",
        [
            "project",
            "resource",
            "concept",
            "journal",
            "correspondence",
            "prompt",
            "decision",
            "meeting",
            "event",
            "person",
        ],
    )
    def test_canonical_types_still_validate_without_regression(self, memory_type):
        """Every canonical type is still accepted by validate_domain_metadata
        with no metadata (i.e. no crash / no unexpected required-field warning
        beyond the pre-existing 'event' contract)."""
        warnings = validate_domain_metadata(memory_type, {})
        if memory_type == "event":
            assert warnings == ["event metadata missing required field 'when' (expected ISO datetime, e.g. '2026-04-15T10:00:00')"]
        else:
            assert warnings == []
