"""Tests for scripts/merge_persons.py — person merge pure logic and idempotency.

Tests cover:
- validate_pair: type checks, same-id rejection
- compute_merged_aliases: dedup, case-insensitive, source name/aliases included
- is_already_merged: with/without merged_into
- name_length_warning: target shorter/longer/equal
- format_dry_run_report: output contains key info
- Idempotency via is_already_merged
- Dry-run mode (patches DB functions to verify no writes)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.people import merge as mp


# ---------------------------------------------------------------------------
# Fixtures — representative person memory row shapes
# ---------------------------------------------------------------------------

SOURCE_PERSON = {
    "id": 17692,
    "type": "person",
    "title": "Dr. Dr. Stephan Weihe",
    "content": "Stephan Weihe — ICRD / medworkx.digital",
    "metadata": {
        "name": "Dr. Dr. Stephan Weihe",
        "org": "ICRD / medworkx.digital",
        "person_ref": "person-weihe-dr-dr-stephan",
        "aliases": ["Stephan Weihe"],
        "schema_version": "people-v1",
    },
}

TARGET_PERSON = {
    "id": 17700,
    "type": "person",
    "title": "Stephan Weihe",
    "content": "Stephan Weihe, ICRD",
    "metadata": {
        "name": "Stephan Weihe",
        "org": "ICRD",
        "person_ref": "person-weihe-stephan",
        "aliases": ["S. Weihe"],
        "schema_version": "people-v1",
    },
}

NON_PERSON_ROW = {
    "id": 99,
    "type": "meeting",
    "title": "Some meeting",
    "content": "...",
    "metadata": {},
}

ALREADY_MERGED_SOURCE = {
    "id": 17692,
    "type": "person",
    "title": "Dr. Dr. Stephan Weihe",
    "content": "...",
    "metadata": {
        "name": "Dr. Dr. Stephan Weihe",
        "person_ref": "person-weihe-dr-dr-stephan",
        "aliases": ["Stephan Weihe"],
        "merged_into": 17700,
        "merged_at": "2026-04-30T10:00:00",
    },
}

SOURCE_NO_ALIASES = {
    "id": 500,
    "type": "person",
    "title": "Alice Smith",
    "content": "Alice Smith",
    "metadata": {
        "name": "Alice Smith",
        "person_ref": "person-smith-alice",
        "aliases": [],
    },
}

TARGET_NO_ALIASES = {
    "id": 501,
    "type": "person",
    "title": "Alice",
    "content": "Alice",
    "metadata": {
        "name": "Alice",
        "person_ref": "person-alice",
        "aliases": [],
    },
}

SOURCE_WITH_ALIASES = {
    "id": 600,
    "type": "person",
    "title": "Robert Johnson",
    "content": "Robert Johnson",
    "metadata": {
        "name": "Robert Johnson",
        "person_ref": "person-johnson-robert",
        "aliases": ["Rob Johnson", "Bobby J"],
    },
}

TARGET_WITH_ALIASES = {
    "id": 601,
    "type": "person",
    "title": "Bob Johnson",
    "content": "Bob Johnson",
    "metadata": {
        "name": "Bob Johnson",
        "person_ref": "person-johnson-bob",
        "aliases": ["Bob J"],
    },
}


# ---------------------------------------------------------------------------
# TestValidatePair: AK 5 & 6
# ---------------------------------------------------------------------------


class TestValidatePair:
    """validate_pair returns empty list for valid pairs, errors otherwise."""

    def test_valid_pair_returns_no_errors(self):
        errors = mp.validate_pair(SOURCE_PERSON, TARGET_PERSON)
        assert errors == []

    def test_source_not_person_returns_error(self):
        errors = mp.validate_pair(NON_PERSON_ROW, TARGET_PERSON)
        assert len(errors) > 0
        assert any("source" in e.lower() and "person" in e.lower() for e in errors)

    def test_target_not_person_returns_error(self):
        errors = mp.validate_pair(SOURCE_PERSON, NON_PERSON_ROW)
        assert len(errors) > 0
        assert any("target" in e.lower() and "person" in e.lower() for e in errors)

    def test_same_id_returns_error(self):
        same = {**SOURCE_PERSON, "id": 17700}
        errors = mp.validate_pair(same, TARGET_PERSON)
        assert len(errors) > 0
        assert any("same" in e.lower() or "equal" in e.lower() or "identical" in e.lower() for e in errors)

    def test_both_non_person_returns_two_errors(self):
        meeting1 = {**NON_PERSON_ROW, "id": 1}
        meeting2 = {**NON_PERSON_ROW, "id": 2}
        errors = mp.validate_pair(meeting1, meeting2)
        assert len(errors) >= 2

    def test_already_merged_source_is_still_valid(self):
        # validate_pair only checks type and id equality — idempotency handled elsewhere
        errors = mp.validate_pair(ALREADY_MERGED_SOURCE, TARGET_PERSON)
        assert errors == []


# ---------------------------------------------------------------------------
# TestComputeMergedAliases: AK 3 (alias merging)
# ---------------------------------------------------------------------------


class TestComputeMergedAliases:
    """compute_merged_aliases returns deduplicated alias list for target after merge."""

    def test_source_name_added_to_target_aliases(self):
        aliases = mp.compute_merged_aliases(SOURCE_PERSON, TARGET_PERSON)
        # Source name "Dr. Dr. Stephan Weihe" should appear
        assert "Dr. Dr. Stephan Weihe" in aliases

    def test_source_aliases_added_to_target_aliases(self):
        aliases = mp.compute_merged_aliases(SOURCE_PERSON, TARGET_PERSON)
        # Source alias "Stephan Weihe" should appear
        assert "Stephan Weihe" in aliases

    def test_target_existing_aliases_preserved(self):
        aliases = mp.compute_merged_aliases(SOURCE_PERSON, TARGET_PERSON)
        # Target existing alias "S. Weihe" should be preserved
        assert "S. Weihe" in aliases

    def test_case_insensitive_dedup(self):
        source = {
            **SOURCE_NO_ALIASES,
            "metadata": {**SOURCE_NO_ALIASES["metadata"], "aliases": ["alice smith"]},
        }
        target = {
            **TARGET_NO_ALIASES,
            "metadata": {**TARGET_NO_ALIASES["metadata"], "aliases": ["Alice Smith"]},
        }
        aliases = mp.compute_merged_aliases(source, target)
        # "alice smith" and "Alice Smith" are duplicates — should appear only once
        lower_aliases = [a.lower() for a in aliases]
        assert lower_aliases.count("alice smith") == 1

    def test_no_duplicates_in_result(self):
        aliases = mp.compute_merged_aliases(SOURCE_WITH_ALIASES, TARGET_WITH_ALIASES)
        # All aliases should be unique (case-insensitive)
        lower = [a.lower() for a in aliases]
        assert len(lower) == len(set(lower))

    def test_both_source_aliases_included(self):
        aliases = mp.compute_merged_aliases(SOURCE_WITH_ALIASES, TARGET_WITH_ALIASES)
        assert "Rob Johnson" in aliases
        assert "Bobby J" in aliases

    def test_source_name_not_duplicated_if_already_alias(self):
        # Source name already appears in target aliases → should not be duplicated
        source = {
            **SOURCE_NO_ALIASES,
            "metadata": {**SOURCE_NO_ALIASES["metadata"], "name": "Alice Smith", "aliases": []},
        }
        target = {
            **TARGET_NO_ALIASES,
            "metadata": {**TARGET_NO_ALIASES["metadata"], "name": "Alice", "aliases": ["Alice Smith"]},
        }
        aliases = mp.compute_merged_aliases(source, target)
        lower = [a.lower() for a in aliases]
        assert lower.count("alice smith") == 1

    def test_empty_source_aliases_no_extra_blanks(self):
        aliases = mp.compute_merged_aliases(SOURCE_NO_ALIASES, TARGET_NO_ALIASES)
        assert all(a.strip() for a in aliases)  # no empty strings


# ---------------------------------------------------------------------------
# TestIsAlreadyMerged: AK 4 (idempotency)
# ---------------------------------------------------------------------------


class TestIsAlreadyMerged:
    """is_already_merged returns True if source already has merged_into=target_id."""

    def test_not_merged_returns_false(self):
        assert mp.is_already_merged(SOURCE_PERSON, 17700) is False

    def test_merged_into_target_returns_true(self):
        assert mp.is_already_merged(ALREADY_MERGED_SOURCE, 17700) is True

    def test_merged_into_different_target_returns_false(self):
        # merged_into points to a different ID — not the same target
        assert mp.is_already_merged(ALREADY_MERGED_SOURCE, 99999) is False

    def test_no_metadata_merged_into_returns_false(self):
        row = {**SOURCE_PERSON, "metadata": {}}
        assert mp.is_already_merged(row, 17700) is False

    def test_merged_into_as_string_id(self):
        # merged_into stored as string "17700" should also match int 17700
        source = {
            **SOURCE_PERSON,
            "metadata": {**SOURCE_PERSON["metadata"], "merged_into": "17700"},
        }
        assert mp.is_already_merged(source, 17700) is True


# ---------------------------------------------------------------------------
# TestNameLengthWarning: bead additional requirement
# ---------------------------------------------------------------------------


class TestNameLengthWarning:
    """name_length_warning returns a warning if target name is shorter than source."""

    def test_target_shorter_returns_warning(self):
        # SOURCE name "Dr. Dr. Stephan Weihe" is longer than TARGET name "Stephan Weihe"
        warning = mp.name_length_warning(SOURCE_PERSON, TARGET_PERSON)
        assert warning is not None
        assert isinstance(warning, str)
        assert len(warning) > 0

    def test_target_longer_returns_none(self):
        warning = mp.name_length_warning(TARGET_PERSON, SOURCE_PERSON)
        assert warning is None

    def test_equal_length_names_returns_none(self):
        # Both names are exactly 10 characters
        same_source = {
            **SOURCE_PERSON,
            "metadata": {**SOURCE_PERSON["metadata"], "name": "Alice Name"},
        }
        same_target = {
            **TARGET_PERSON,
            "metadata": {**TARGET_PERSON["metadata"], "name": "Other Name"},
        }
        result = mp.name_length_warning(same_source, same_target)
        assert result is None

    def test_warning_message_contains_names(self):
        warning = mp.name_length_warning(SOURCE_PERSON, TARGET_PERSON)
        assert warning is not None
        # Should mention both names or at least indicate the issue
        assert "Stephan Weihe" in warning or "shorter" in warning.lower()


# ---------------------------------------------------------------------------
# TestFormatDryRunReport: AK 1
# ---------------------------------------------------------------------------


class TestFormatDryRunReport:
    """format_dry_run_report returns human-readable report with key information."""

    def test_contains_source_id(self):
        report = mp.format_dry_run_report(SOURCE_PERSON, TARGET_PERSON, 5, 3)
        assert "17692" in report

    def test_contains_target_id(self):
        report = mp.format_dry_run_report(SOURCE_PERSON, TARGET_PERSON, 5, 3)
        assert "17700" in report

    def test_contains_interaction_count(self):
        report = mp.format_dry_run_report(SOURCE_PERSON, TARGET_PERSON, 5, 3)
        assert "5" in report

    def test_contains_relationship_count(self):
        report = mp.format_dry_run_report(SOURCE_PERSON, TARGET_PERSON, 5, 3)
        assert "3" in report

    def test_contains_source_name(self):
        report = mp.format_dry_run_report(SOURCE_PERSON, TARGET_PERSON, 5, 3)
        assert "Dr. Dr. Stephan Weihe" in report or "Stephan Weihe" in report

    def test_contains_target_name(self):
        report = mp.format_dry_run_report(SOURCE_PERSON, TARGET_PERSON, 5, 3)
        assert "Stephan Weihe" in report

    def test_zero_counts_still_works(self):
        report = mp.format_dry_run_report(SOURCE_PERSON, TARGET_PERSON, 0, 0)
        assert report is not None
        assert len(report) > 0

    def test_contains_dry_run_indicator(self):
        report = mp.format_dry_run_report(SOURCE_PERSON, TARGET_PERSON, 5, 3)
        assert "dry" in report.lower() or "would" in report.lower() or "DRY" in report


# ---------------------------------------------------------------------------
# TestIdempotency: AK 4 via is_already_merged
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Re-running merge on an already-merged source is a no-op."""

    def test_already_merged_source_detected(self):
        assert mp.is_already_merged(ALREADY_MERGED_SOURCE, 17700) is True

    def test_not_yet_merged_not_detected(self):
        assert mp.is_already_merged(SOURCE_PERSON, 17700) is False

    def test_idempotent_on_repeated_check(self):
        """Two consecutive is_already_merged calls return same result."""
        result1 = mp.is_already_merged(ALREADY_MERGED_SOURCE, 17700)
        result2 = mp.is_already_merged(ALREADY_MERGED_SOURCE, 17700)
        assert result1 == result2 is True


# ---------------------------------------------------------------------------
# TestDryRunMode: AK 1 — dry-run must not call write functions
# ---------------------------------------------------------------------------


class TestDryRunMode:
    """Dry-run mode: read-only functions called, write functions NOT called."""

    @pytest.mark.asyncio
    async def test_dry_run_does_not_call_repoint_person_refs(self):
        """In dry-run mode, repoint_person_refs must not be called."""
        mock_conn = AsyncMock()

        with (
            patch.object(mp, "fetch_memory") as mock_fetch,
            patch.object(mp, "count_person_ref_rows") as mock_count_interactions,
            patch.object(mp, "count_relationship_rows") as mock_count_rels,
            patch.object(mp, "repoint_person_refs") as mock_repoint,
            patch.object(mp, "repoint_relationships") as mock_repoint_rels,
            patch.object(mp, "update_target_aliases") as mock_update_aliases,
            patch.object(mp, "soft_delete_source") as mock_soft_delete,
        ):
            mock_fetch.side_effect = [
                SOURCE_PERSON,  # fetch source
                TARGET_PERSON,  # fetch target
            ]
            mock_count_interactions.return_value = 5
            mock_count_rels.return_value = 3

            result = await mp.run_dry_run(mock_conn, 17692, 17700)

            # Read functions should be called
            assert mock_fetch.call_count == 2
            mock_count_interactions.assert_called_once()
            mock_count_rels.assert_called_once()

            # Write functions must NOT be called
            mock_repoint.assert_not_called()
            mock_repoint_rels.assert_not_called()
            mock_update_aliases.assert_not_called()
            mock_soft_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_returns_report_string(self):
        """run_dry_run returns a non-empty report string."""
        mock_conn = AsyncMock()

        with (
            patch.object(mp, "fetch_memory") as mock_fetch,
            patch.object(mp, "count_person_ref_rows") as mock_count_interactions,
            patch.object(mp, "count_relationship_rows") as mock_count_rels,
        ):
            mock_fetch.side_effect = [SOURCE_PERSON, TARGET_PERSON]
            mock_count_interactions.return_value = 2
            mock_count_rels.return_value = 1

            result = await mp.run_dry_run(mock_conn, 17692, 17700)

            assert isinstance(result, str)
            assert len(result) > 0

    @pytest.mark.asyncio
    async def test_dry_run_already_merged_returns_skip_message(self):
        """run_dry_run returns skip message if source already merged."""
        mock_conn = AsyncMock()

        with patch.object(mp, "fetch_memory") as mock_fetch:
            mock_fetch.side_effect = [ALREADY_MERGED_SOURCE, TARGET_PERSON]

            result = await mp.run_dry_run(mock_conn, 17692, 17700)

            assert "already" in result.lower() or "skip" in result.lower()

    @pytest.mark.asyncio
    async def test_dry_run_absorb_text_false_no_absorption_line(self):
        """When absorb_text=False, report does not mention text absorption."""
        mock_conn = AsyncMock()

        with (
            patch.object(mp, "fetch_memory") as mock_fetch,
            patch.object(mp, "count_person_ref_rows") as mock_count_interactions,
            patch.object(mp, "count_relationship_rows") as mock_count_rels,
        ):
            mock_fetch.side_effect = [SOURCE_PERSON, TARGET_PERSON]
            mock_count_interactions.return_value = 2
            mock_count_rels.return_value = 1

            result = await mp.run_dry_run(mock_conn, 17692, 17700, absorb_text=False)

            assert "absorb" not in result.lower()

    @pytest.mark.asyncio
    async def test_dry_run_absorb_text_true_includes_absorption_line(self):
        """When absorb_text=True, report includes the absorption line with char count."""
        mock_conn = AsyncMock()

        with (
            patch.object(mp, "fetch_memory") as mock_fetch,
            patch.object(mp, "count_person_ref_rows") as mock_count_interactions,
            patch.object(mp, "count_relationship_rows") as mock_count_rels,
        ):
            mock_fetch.side_effect = [SOURCE_PERSON, TARGET_PERSON]
            mock_count_interactions.return_value = 2
            mock_count_rels.return_value = 1

            result = await mp.run_dry_run(mock_conn, 17692, 17700, absorb_text=True)

            assert "absorb" in result.lower()
            # Should mention the char count from source content
            source_content = SOURCE_PERSON["content"] or ""
            assert str(len(source_content)) in result


# ---------------------------------------------------------------------------
# TestFormatDryRunReportAbsorbText: B1 — absorb_text in dry-run report
# ---------------------------------------------------------------------------


class TestFormatDryRunReportAbsorbText:
    """format_dry_run_report absorb_text parameter."""

    def test_absorb_text_false_no_absorption_line(self):
        report = mp.format_dry_run_report(SOURCE_PERSON, TARGET_PERSON, 5, 3, absorb_text=False)
        assert "absorb" not in report.lower()

    def test_absorb_text_true_includes_char_count(self):
        report = mp.format_dry_run_report(SOURCE_PERSON, TARGET_PERSON, 5, 3, absorb_text=True)
        assert "absorb" in report.lower()
        source_content = SOURCE_PERSON["content"] or ""
        assert str(len(source_content)) in report

    def test_absorb_text_default_is_false(self):
        # Default call without absorb_text should not mention absorption
        report = mp.format_dry_run_report(SOURCE_PERSON, TARGET_PERSON, 5, 3)
        assert "absorb" not in report.lower()


# ---------------------------------------------------------------------------
# TestNullMetadataHandling: regression — NULL metadata must not break logic
# ---------------------------------------------------------------------------


class TestNullMetadataHandling:
    """Pure-logic helpers must tolerate rows where metadata is None.

    The DB-side fix wraps `metadata || ...` in `COALESCE(metadata, '{}'::jsonb)`
    so writes succeed when a row has NULL metadata. These tests cover the
    Python-side helpers that read metadata before/after the merge.
    """

    def test_is_already_merged_with_null_metadata_returns_false(self):
        source_row = {"id": 1, "type": "person", "metadata": None}
        assert mp.is_already_merged(source_row, 17700) is False

    def test_is_already_merged_with_missing_metadata_key_returns_false(self):
        source_row = {"id": 1, "type": "person"}
        assert mp.is_already_merged(source_row, 17700) is False

    def test_compute_merged_aliases_source_metadata_none(self):
        source = {"id": 1, "type": "person", "title": "Alice", "metadata": None}
        target = {
            "id": 2,
            "type": "person",
            "title": "Alice T",
            "metadata": {"name": "Alice T", "aliases": ["AT"]},
        }
        aliases = mp.compute_merged_aliases(source, target)
        # Source falls back to title="Alice" since metadata is None
        assert "Alice" in aliases
        assert "AT" in aliases

    def test_compute_merged_aliases_target_metadata_none(self):
        source = {
            "id": 1,
            "type": "person",
            "title": "Alice",
            "metadata": {"name": "Alice", "aliases": ["A."]},
        }
        target = {"id": 2, "type": "person", "title": "Alice T", "metadata": None}
        aliases = mp.compute_merged_aliases(source, target)
        # Target had no aliases (metadata None) — source name + source aliases come through
        assert "Alice" in aliases
        assert "A." in aliases

    def test_compute_merged_aliases_both_metadata_none(self):
        source = {"id": 1, "type": "person", "title": "Alice", "metadata": None}
        target = {"id": 2, "type": "person", "title": "Bob", "metadata": None}
        aliases = mp.compute_merged_aliases(source, target)
        # Falls back to titles for source name; target had no aliases
        assert "Alice" in aliases

    def test_display_name_with_null_metadata(self):
        row = {"id": 1, "type": "person", "title": "Fallback Title", "metadata": None}
        assert mp.display_name(row) == "Fallback Title"

    def test_validate_pair_with_null_metadata(self):
        # validate_pair only inspects type and id — must not crash on None metadata
        source = {"id": 1, "type": "person", "metadata": None}
        target = {"id": 2, "type": "person", "metadata": None}
        errors = mp.validate_pair(source, target)
        assert errors == []


# ---------------------------------------------------------------------------
# TestRepointRelationships: B2 — UNIQUE constraint safe repoint
# ---------------------------------------------------------------------------


class TestRepointRelationships:
    """repoint_relationships handles self-loops and collision rows."""

    @pytest.mark.asyncio
    async def test_self_loop_rows_are_deleted(self):
        """Rows forming source<->target edges are deleted before the UPDATE."""
        mock_conn = AsyncMock()
        # Each execute call returns "DELETE N" or "UPDATE N"
        mock_conn.execute.side_effect = [
            "DELETE 2",  # self-loop delete
            "DELETE 0",  # source_id collision delete
            "DELETE 0",  # target_id collision delete
            "UPDATE 0",  # final UPDATE (no remaining rows)
        ]

        total = await mp.repoint_relationships(mock_conn, source_id=1, target_id=2)

        assert mock_conn.execute.call_count == 4
        assert total == 2  # 2 self-loop rows deleted + 0 collisions + 0 updated

    @pytest.mark.asyncio
    async def test_collision_rows_are_deleted_before_update(self):
        """Triangle case: collision rows are deleted so UPDATE doesn't violate UNIQUE."""
        mock_conn = AsyncMock()
        mock_conn.execute.side_effect = [
            "DELETE 0",  # no self-loops
            "DELETE 1",  # one source_id collision
            "DELETE 1",  # one target_id collision
            "UPDATE 2",  # remaining rows updated
        ]

        total = await mp.repoint_relationships(mock_conn, source_id=10, target_id=20)

        assert mock_conn.execute.call_count == 4
        assert total == 4  # 0 + 1 + 1 + 2

    @pytest.mark.asyncio
    async def test_no_special_rows_just_updates(self):
        """When there are no self-loops or collisions, all rows are updated."""
        mock_conn = AsyncMock()
        mock_conn.execute.side_effect = [
            "DELETE 0",
            "DELETE 0",
            "DELETE 0",
            "UPDATE 5",
        ]

        total = await mp.repoint_relationships(mock_conn, source_id=100, target_id=200)

        assert total == 5

    @pytest.mark.asyncio
    async def test_returns_combined_affected_count(self):
        """Total returned is sum of deleted + updated rows."""
        mock_conn = AsyncMock()
        mock_conn.execute.side_effect = [
            "DELETE 3",
            "DELETE 2",
            "DELETE 1",
            "UPDATE 4",
        ]

        total = await mp.repoint_relationships(mock_conn, source_id=5, target_id=6)

        assert total == 10  # 3 + 2 + 1 + 4
