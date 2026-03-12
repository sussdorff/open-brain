"""Tests for scripts/migrate_learnings.py — field mapping and idempotency."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Make scripts/ importable
SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import migrate_learnings as ml


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FULL_ENTRY = {
    "id": "lrn-abc123",
    "source": {
        "type": "jsonl",
        "project": "mira",
        "session_id": "sess-xyz",
        "conversation_file": "sess-xyz.jsonl",
        "message_index": None,
        "timestamp": "2026-02-18T09:00:00.000Z",
    },
    "feedback_type": "architecture",
    "content": "Billing amounts must come from the FHIR catalog, not AI responses.",
    "confidence": 0.97,
    "needs_review": False,
    "scope": "project:mira",
    "affected_skills": ["code-review"],
    "language": "de",
    "extracted_at": "2026-02-19T00:00:00.000Z",
    "status": "discarded",
    "discard_reason": "too specific/one-time",
    "content_hash": "23391915b146b9d1",
    "materialized_to": None,
}

MATERIALIZED_ENTRY = {
    "id": "lrn-def456",
    "source": {"project": "open-brain"},
    "content": "Use asyncpg for all DB access.",
    "confidence": 0.95,
    "scope": "project:open-brain",
    "affected_skills": ["code-review"],
    "extracted_at": "2026-03-01T12:00:00.000Z",
    "status": "materialized",
    "materialized_to": "db-patterns.md",
    "content_hash": "aabbccdd",
    "feedback_type": "convention",
}

OPEN_ENTRY = {
    "id": "lrn-ghi789",
    "source": {"project": "claude"},
    "content": "Always write failing tests first.",
    "confidence": 0.9,
    "scope": "global",
    "extracted_at": "2026-03-10T08:00:00.000Z",
    "status": "open",
    "content_hash": "11223344",
}

UNKNOWN_STATUS_ENTRY = {
    "id": "lrn-unk001",
    "content": "Some legacy learning without clear status.",
    "status": "legacy",
    "extracted_at": "2025-12-01T00:00:00.000Z",
}

MINIMAL_ENTRY = {
    "content": "Minimal entry without id or extracted_at.",
}


# ---------------------------------------------------------------------------
# AK 1: Vollständiges Mapping — test_full_metadata_mapping
# ---------------------------------------------------------------------------


class TestFullMetadataMapping:
    """AK 1: All JSONL fields must be present in the mapped metadata."""

    def test_session_ref_format(self):
        """id maps to session_ref directly (ids already carry 'lrn-' prefix)."""
        m = ml.map_entry(FULL_ENTRY)
        assert m["session_ref"] == "lrn-abc123"

    def test_session_ref_plain_id(self):
        """A plain id without lrn- prefix is still used as-is."""
        m2 = ml.map_entry({"id": "plain-id", "content": "x"})
        assert m2["session_ref"] == "plain-id"

    def test_content_mapped(self):
        """content → content field."""
        m = ml.map_entry(FULL_ENTRY)
        assert m["content"] == FULL_ENTRY["content"]

    def test_project_from_source(self):
        """source.project → project."""
        m = ml.map_entry(FULL_ENTRY)
        assert m["project"] == "mira"

    def test_project_fallback_to_top_level(self):
        """Top-level 'project' used when source has no project."""
        entry = {"id": "lrn-x", "content": "test", "project": "fallback-proj"}
        m = ml.map_entry(entry)
        assert m["project"] == "fallback-proj"

    def test_extracted_at_maps_to_created_at(self):
        """extracted_at → created_at as aware datetime."""
        m = ml.map_entry(FULL_ENTRY)
        assert isinstance(m["created_at"], datetime)
        assert m["created_at"].year == 2026
        assert m["created_at"].month == 2
        assert m["created_at"].day == 19

    def test_status_in_metadata(self):
        """status field present in metadata."""
        m = ml.map_entry(FULL_ENTRY)
        assert "status" in m["metadata"]
        assert m["metadata"]["status"] == "discarded"

    def test_confidence_in_metadata(self):
        """confidence preserved in metadata."""
        m = ml.map_entry(FULL_ENTRY)
        assert m["metadata"]["confidence"] == 0.97

    def test_feedback_type_in_metadata(self):
        """feedback_type preserved in metadata."""
        m = ml.map_entry(FULL_ENTRY)
        assert m["metadata"]["feedback_type"] == "architecture"

    def test_scope_in_metadata(self):
        """scope preserved in metadata."""
        m = ml.map_entry(FULL_ENTRY)
        assert m["metadata"]["scope"] == "project:mira"

    def test_affected_skills_in_metadata(self):
        """affected_skills preserved in metadata."""
        m = ml.map_entry(FULL_ENTRY)
        assert m["metadata"]["affected_skills"] == ["code-review"]

    def test_content_hash_in_metadata(self):
        """content_hash preserved in metadata."""
        m = ml.map_entry(FULL_ENTRY)
        assert m["metadata"]["content_hash"] == "23391915b146b9d1"

    def test_extracted_at_in_metadata(self):
        """extracted_at string also preserved in metadata (for reference)."""
        m = ml.map_entry(FULL_ENTRY)
        assert m["metadata"]["extracted_at"] == "2026-02-19T00:00:00.000Z"

    def test_discard_reason_in_metadata(self):
        """discard_reason preserved in metadata."""
        m = ml.map_entry(FULL_ENTRY)
        assert m["metadata"]["discard_reason"] == "too specific/one-time"

    def test_materialized_to_in_metadata(self):
        """materialized_to preserved in metadata when set."""
        m = ml.map_entry(MATERIALIZED_ENTRY)
        assert m["metadata"]["materialized_to"] == "db-patterns.md"

    def test_source_in_metadata(self):
        """Full source dict preserved in metadata."""
        m = ml.map_entry(FULL_ENTRY)
        assert "source" in m["metadata"]
        assert m["metadata"]["source"]["project"] == "mira"
        assert m["metadata"]["source"]["session_id"] == "sess-xyz"

    def test_extra_fields_preserved(self):
        """Extra fields not in the standard mapping are still included in metadata."""
        entry = dict(FULL_ENTRY)
        entry["custom_field"] = "custom_value"
        entry["tags"] = ["tag1", "tag2"]
        m = ml.map_entry(entry)
        assert m["metadata"].get("custom_field") == "custom_value"
        assert m["metadata"].get("tags") == ["tag1", "tag2"]

    def test_none_values_stripped_from_metadata(self):
        """None values are stripped from metadata to keep it clean."""
        m = ml.map_entry(FULL_ENTRY)
        # materialized_to is None in FULL_ENTRY — should be stripped
        assert "materialized_to" not in m["metadata"]

    def test_all_statuses_mapped(self):
        """All three status values map correctly; unknown maps to 'open'."""
        for raw, expected in [
            ("open", "open"),
            ("materialized", "materialized"),
            ("discarded", "discarded"),
            ("legacy", "open"),
            ("unknown", "open"),
        ]:
            entry = {"id": f"lrn-{raw}", "content": "x", "status": raw}
            m = ml.map_entry(entry)
            assert m["metadata"]["status"] == expected, f"status={raw!r} -> {m['metadata']['status']!r}, expected {expected!r}"

    def test_missing_extracted_at_gives_none_created_at(self):
        """Entry without extracted_at produces created_at=None."""
        entry = {"id": "lrn-x", "content": "test"}
        m = ml.map_entry(entry)
        assert m["created_at"] is None

    def test_minimal_entry_does_not_crash(self):
        """A truly minimal entry (just content) maps without error.

        Entries without an id get a content-hash-based session_ref for idempotency.
        """
        m = ml.map_entry(MINIMAL_ENTRY)
        assert m["content"] == MINIMAL_ENTRY["content"]
        assert m["session_ref"] is not None
        assert m["session_ref"].startswith("lrn-")
        assert m["project"] is None
        assert m["created_at"] is None


# ---------------------------------------------------------------------------
# AK 2: Idempotenz — test_import_idempotent
# ---------------------------------------------------------------------------


class TestImportIdempotent:
    """AK 2: Re-running the script must not create duplicate entries."""

    def test_existing_session_refs_skipped(self):
        """Entries whose session_ref is already in the DB are skipped."""
        entries = [FULL_ENTRY, MATERIALIZED_ENTRY, OPEN_ENTRY]
        mapped = [ml.map_entry(e) for e in entries]

        # Actual session_refs produced by map_entry (raw ids from JSONL)
        # FULL_ENTRY id="lrn-abc123", MATERIALIZED id="lrn-def456", OPEN id="lrn-ghi789"
        existing_refs = {"lrn-abc123"}  # FULL_ENTRY already imported
        to_import = [m for m in mapped if m["session_ref"] not in existing_refs]

        assert len(to_import) == 2
        refs = {m["session_ref"] for m in to_import}
        assert "lrn-abc123" not in refs
        assert "lrn-def456" in refs
        assert "lrn-ghi789" in refs

    def test_all_existing_no_import(self):
        """When all entries are already in the DB, nothing is imported."""
        entries = [FULL_ENTRY, MATERIALIZED_ENTRY]
        mapped = [ml.map_entry(e) for e in entries]
        existing_refs = {"lrn-abc123", "lrn-def456"}
        to_import = [m for m in mapped if m["session_ref"] not in existing_refs]
        assert len(to_import) == 0

    def test_session_ref_from_lrn_prefixed_ids(self):
        """JSONL ids with 'lrn-' prefix are used as-is as session_ref."""
        m = ml.map_entry({"id": "lrn-abc123", "content": "x"})
        assert m["session_ref"] == "lrn-abc123"

    def test_no_session_ref_not_matched_by_existing(self):
        """Entry without id has session_ref=None and is not blocked by existing refs."""
        m = ml.map_entry(MINIMAL_ENTRY)
        existing_refs = {"lrn-abc123"}
        # None not in existing_refs → would be imported (no idempotency for no-id entries)
        assert m["session_ref"] not in existing_refs

    def test_parse_dt_z_suffix(self):
        """parse_dt handles 'Z' suffix correctly."""
        dt = ml.parse_dt("2026-02-19T00:00:00.000Z")
        assert dt is not None
        assert dt.year == 2026

    def test_parse_dt_offset(self):
        """parse_dt handles +00:00 offset."""
        dt = ml.parse_dt("2026-02-19T00:00:00+00:00")
        assert dt is not None

    def test_parse_dt_none(self):
        """parse_dt(None) returns None."""
        assert ml.parse_dt(None) is None

    def test_parse_dt_invalid(self):
        """parse_dt with invalid string returns None."""
        assert ml.parse_dt("not-a-date") is None
