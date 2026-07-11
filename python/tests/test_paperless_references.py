"""Tests for Paperless-ngx document references in memory metadata."""

from __future__ import annotations


def _valid_reference_metadata() -> dict:
    """Return valid Paperless reference metadata for tests."""
    return {
        "document_id": 17,
        "instance": "paperless.example",
        "title": "Home insurance policy",
        "added": "2026-07-11T09:30:00Z",
    }


class TestPaperlessReferenceMetadataValidation:
    def test_paperless_reference_validates_for_unrelated_memory_type(self):
        """AC1: paperless_reference validation runs for non-paperless memory types."""
        from open_brain.data_layer.interface import validate_domain_metadata

        warnings = validate_domain_metadata(
            "decision",
            {"paperless_reference": _valid_reference_metadata()},
        )

        assert warnings == []

    def test_paperless_reference_validates_when_memory_type_is_none(self):
        """AC1: paperless_reference validation runs before the type=None early return."""
        from open_brain.data_layer.interface import validate_domain_metadata

        warnings = validate_domain_metadata(
            None,
            {
                "paperless_reference": {
                    "document_id": 0,
                    "instance": "",
                    "title": "",
                    "added": "not-a-date",
                }
            },
        )

        assert any("document_id" in warning for warning in warnings)
        assert any("instance" in warning for warning in warnings)
        assert any("title" in warning for warning in warnings)
        assert any("added" in warning for warning in warnings)
