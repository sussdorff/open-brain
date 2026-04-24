"""Tests for scripts/migrate_person_memories.py — classification, planning, and idempotency."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make scripts/ importable
SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import migrate_person_memories as mpm


# ---------------------------------------------------------------------------
# Fixtures — real data matching python/tests/fixtures/people/ shapes
# ---------------------------------------------------------------------------

SINGLE_PERSON_ROW = {
    "id": 17692,
    "type": "person",
    "title": "Dr. Dr. Stephan Weihe",
    "content": "Stephan Weihe — ICRD / medworkx.digital",
    "metadata": {
        "name": "Dr. Dr. Stephan Weihe",
        "org": "ICRD / medworkx.digital",
        "linkedin": None,
        "aliases": ["Stephan Weihe"],
    },
}

DIRECTORY_ROW = {
    "id": 18175,
    "type": "person",
    "title": "Polaris Directory",
    "content": "Polaris team directory",
    "metadata": {
        "members": [
            {"name": "Elias Trewin", "org": "Cinify", "linkedin": "elias-trewin", "aliases": []},
            {
                "name": "Dr. Cyrus Alamouti",
                "org": "Dental-Now",
                "linkedin": "dr-cyrus-alamouti-73385773",
                "aliases": ["Cyrus Amadi", "Cyrus Ahmadi", "Cyrus"],
            },
            {"name": "Siamak Ghasemi", "org": "Dental-Now", "linkedin": "siamakghasemi", "aliases": []},
            {
                "name": "Jochen Jungbluth",
                "org": "Dental-Now",
                "linkedin": "jochen-jungbluth-a5a412152",
                "aliases": ["Jochen Jungblut"],
            },
            {
                "name": "Philipp Kuhn-Regnier",
                "org": "Sonia",
                "linkedin": "philipp-kuhn-regnier",
                "aliases": ["Philip Kuhn-Regnier", "Philipp Regnier"],
            },
        ]
    },
}

ALREADY_MIGRATED_ROW = {
    "id": 17700,
    "type": "person",
    "title": "Elias Trewin",
    "content": "Elias Trewin, Cinify",
    "metadata": {
        "name": "Elias Trewin",
        "org": "Cinify",
        "schema_version": "people-v1",
        "person_ref": "person-trewin-elias",
        "aliases": [],
    },
}

ALREADY_ARCHIVED_ROW = {
    "id": 18175,
    "type": "curated_content",
    "title": "Polaris Directory",
    "content": "Polaris team directory",
    "metadata": {
        "schema_version": "people-v1-archived",
        "archival_note": "Original directory archived after split into 5 individual person memories.",
    },
}

DIRECTORY_BY_TITLE_ROW = {
    "id": 18200,
    "type": "person",
    "title": "Acme Corp Directory",
    "content": "Acme Corp staff listing.",
    "metadata": {
        "org": "Acme Corp",
    },
}


# ---------------------------------------------------------------------------
# AK 1 & 4: classify_memory
# ---------------------------------------------------------------------------


class TestClassifyMemory:
    """classify_memory returns 'directory' or 'single'."""

    def test_directory_via_members_key(self):
        assert mpm.classify_memory(DIRECTORY_ROW) == "directory"

    def test_directory_via_title_keyword_directory(self):
        row = dict(DIRECTORY_BY_TITLE_ROW)
        assert mpm.classify_memory(row) == "directory"

    def test_single_person(self):
        assert mpm.classify_memory(SINGLE_PERSON_ROW) == "single"

    def test_already_migrated_is_single(self):
        # classify_memory does not check schema_version — plan_migration handles skip
        assert mpm.classify_memory(ALREADY_MIGRATED_ROW) == "single"

    def test_members_key_with_empty_list_is_not_directory(self):
        row = {**DIRECTORY_ROW, "title": "Person Record", "metadata": {"members": []}}
        assert mpm.classify_memory(row) == "single"

    def test_members_key_with_string_values_is_not_directory(self):
        # Old shape with string list must not be mistaken for real members
        row = {**DIRECTORY_ROW, "title": "Person Record", "metadata": {"members": ["Alice", "Bob"]}}
        assert mpm.classify_memory(row) == "single"


# ---------------------------------------------------------------------------
# derive_person_ref
# ---------------------------------------------------------------------------


class TestDerivePersonRef:
    """derive_person_ref produces stable 'person-<last>-<first>' slugs."""

    def test_two_word_name(self):
        assert mpm.derive_person_ref("Stephan Weihe", 17692) == "person-weihe-stephan"

    def test_two_word_name_lowercase(self):
        assert mpm.derive_person_ref("Elias Trewin", 18175) == "person-trewin-elias"

    def test_single_word_name(self):
        ref = mpm.derive_person_ref("Madonna", 1)
        assert ref == "person-madonna"

    def test_three_word_name(self):
        # Middle name → last word is the family name
        ref = mpm.derive_person_ref("Maria Clara Schneider", 2)
        assert ref == "person-schneider-maria-clara"

    def test_unicode_characters(self):
        ref = mpm.derive_person_ref("Petra Müller", 3)
        assert ref == "person-muller-petra"

    def test_hyphenated_surname(self):
        # Philipp Kuhn-Regnier — hyphen stays in slug
        ref = mpm.derive_person_ref("Philipp Kuhn-Regnier", 18175)
        assert "kuhn" in ref
        assert "regnier" in ref

    def test_title_prefix_stripped_in_slug(self):
        # Dr. Cyrus Alamouti — "Dr." becomes "dr" in slug, last word is family name
        ref = mpm.derive_person_ref("Dr. Cyrus Alamouti", 18175)
        assert ref.startswith("person-alamouti-")

    def test_fallback_uses_id_when_name_empty(self):
        ref = mpm.derive_person_ref("", 42)
        assert ref == "person-42"

    def test_special_characters_stripped(self):
        ref = mpm.derive_person_ref("Hans-Peter Weber", 4)
        assert "person-" in ref


# ---------------------------------------------------------------------------
# plan_migration
# ---------------------------------------------------------------------------


class TestPlanMigration:
    """plan_migration returns the correct action dict for each row type."""

    def test_single_person_plan_action(self):
        plan = mpm.plan_migration(SINGLE_PERSON_ROW)
        assert plan["action"] == "normalize"

    def test_single_person_plan_memory_id(self):
        plan = mpm.plan_migration(SINGLE_PERSON_ROW)
        assert plan["memory_id"] == 17692

    def test_single_person_plan_has_changes(self):
        plan = mpm.plan_migration(SINGLE_PERSON_ROW)
        assert "changes" in plan
        changes = plan["changes"]
        assert changes["schema_version"] == "people-v1"
        assert changes["person_ref"] == "person-weihe-dr-dr-stephan"
        assert "aliases" in changes
        assert isinstance(changes["aliases"], list)
        # Existing aliases must be preserved — not overwritten with []
        assert changes["aliases"] == ["Stephan Weihe"], (
            "plan_migration must preserve existing aliases from metadata, not replace with []"
        )

    def test_directory_plan_action(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        assert plan["action"] == "split_directory"

    def test_directory_plan_memory_id(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        assert plan["memory_id"] == 18175

    def test_directory_plan_members_are_dicts(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        assert "members" in plan
        assert len(plan["members"]) == 5
        for member in plan["members"]:
            assert isinstance(member, dict)
            assert "name" in member

    def test_directory_plan_member_names(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        names = [m["name"] for m in plan["members"]]
        assert "Elias Trewin" in names
        assert "Philipp Kuhn-Regnier" in names
        assert "Dr. Cyrus Alamouti" in names

    def test_directory_plan_archive_original(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        assert plan["archive_original"] is True

    def test_already_migrated_plan_action(self):
        plan = mpm.plan_migration(ALREADY_MIGRATED_ROW)
        assert plan["action"] == "skip"

    def test_already_migrated_plan_has_reason(self):
        plan = mpm.plan_migration(ALREADY_MIGRATED_ROW)
        assert "reason" in plan
        assert "already migrated" in plan["reason"].lower()

    def test_already_migrated_plan_memory_id(self):
        plan = mpm.plan_migration(ALREADY_MIGRATED_ROW)
        assert plan["memory_id"] == 17700

    def test_archived_directory_is_skipped(self):
        plan = mpm.plan_migration(ALREADY_ARCHIVED_ROW)
        assert plan["action"] == "skip"

    def test_archived_directory_skip_reason_mentions_version(self):
        plan = mpm.plan_migration(ALREADY_ARCHIVED_ROW)
        assert "people-v1-archived" in plan["reason"]


# ---------------------------------------------------------------------------
# format_dry_run_plan
# ---------------------------------------------------------------------------


class TestFormatDryRunPlan:
    """format_dry_run_plan produces human-readable output."""

    def test_normalize_output_contains_action(self):
        plan = mpm.plan_migration(SINGLE_PERSON_ROW)
        text = mpm.format_dry_run_plan(plan)
        assert "normalize" in text.lower()

    def test_normalize_output_contains_memory_id(self):
        plan = mpm.plan_migration(SINGLE_PERSON_ROW)
        text = mpm.format_dry_run_plan(plan)
        assert "17692" in text

    def test_normalize_output_contains_person_ref(self):
        plan = mpm.plan_migration(SINGLE_PERSON_ROW)
        text = mpm.format_dry_run_plan(plan)
        assert "person-weihe" in text

    def test_split_directory_output_contains_member_names(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        text = mpm.format_dry_run_plan(plan)
        assert "split" in text.lower()
        assert "Elias Trewin" in text

    def test_split_directory_output_contains_archive(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        text = mpm.format_dry_run_plan(plan)
        assert "archive" in text.lower()

    def test_skip_output_contains_reason(self):
        plan = mpm.plan_migration(ALREADY_MIGRATED_ROW)
        text = mpm.format_dry_run_plan(plan)
        assert "skip" in text.lower()
        assert "17700" in text


# ---------------------------------------------------------------------------
# AK 4: Idempotency — plan_migration skips already-migrated rows
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Re-running plan_migration on migrated memories returns skip."""

    def test_skip_when_schema_version_people_v1(self):
        plan = mpm.plan_migration(ALREADY_MIGRATED_ROW)
        assert plan["action"] == "skip"

    def test_skip_when_schema_version_people_v1_archived(self):
        plan = mpm.plan_migration(ALREADY_ARCHIVED_ROW)
        assert plan["action"] == "skip"

    def test_skip_is_no_op(self):
        """Two consecutive plan_migration calls on same row return identical skip plans."""
        plan1 = mpm.plan_migration(ALREADY_MIGRATED_ROW)
        plan2 = mpm.plan_migration(ALREADY_MIGRATED_ROW)
        assert plan1["action"] == plan2["action"] == "skip"
        assert plan1["memory_id"] == plan2["memory_id"]

    def test_non_migrated_is_not_skip(self):
        plan = mpm.plan_migration(SINGLE_PERSON_ROW)
        assert plan["action"] != "skip"

    def test_directory_without_schema_version_not_skip(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        assert plan["action"] != "skip"


# ---------------------------------------------------------------------------
# AK 3: Polaris directory split produces 5 person entries
# ---------------------------------------------------------------------------


class TestPolarisDirectorySplit:
    """The Polaris directory (5 members) splits into 5 person memories + archive."""

    def test_polaris_splits_into_5_members(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        assert plan["action"] == "split_directory"
        assert len(plan["members"]) == 5

    def test_each_member_has_person_ref(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        # Each member name → derive_person_ref must produce a unique slug
        refs = [mpm.derive_person_ref(m["name"], DIRECTORY_ROW["id"]) for m in plan["members"]]
        assert len(set(refs)) == 5  # all unique

    def test_archive_original_is_true(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        assert plan["archive_original"] is True

    def test_members_carry_aliases(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        cyrus = next(m for m in plan["members"] if m["name"] == "Dr. Cyrus Alamouti")
        assert "Cyrus" in cyrus["aliases"]

    def test_members_carry_org(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        elias = next(m for m in plan["members"] if m["name"] == "Elias Trewin")
        assert elias["org"] == "Cinify"

    def test_hyphenated_member_has_ref(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        philipp = next(m for m in plan["members"] if "Kuhn-Regnier" in m["name"])
        ref = mpm.derive_person_ref(philipp["name"], DIRECTORY_ROW["id"])
        assert ref.startswith("person-")
        assert len(ref) > len("person-")
