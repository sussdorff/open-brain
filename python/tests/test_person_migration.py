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
# Fixtures
# ---------------------------------------------------------------------------

SINGLE_PERSON_ROW = {
    "id": 17692,
    "type": "person",
    "title": "Stefanie Weihe",
    "content": "Stefanie Weihe works at Polaris GmbH as a senior consultant.",
    "metadata": {
        "name": "Stefanie Weihe",
        "org": "Polaris GmbH",
        "role": "Senior Consultant",
        "relationship": "colleague",
        "last_contact": "2026-03-15T10:00:00+00:00",
    },
}

DIRECTORY_ROW = {
    "id": 18175,
    "type": "person",
    "title": "Polaris GmbH Team Directory",
    "content": (
        "Polaris GmbH staff directory:\n"
        "- Stefanie Weihe (Senior Consultant)\n"
        "- Klaus Braun (CTO)\n"
        "- Maria Schneider (Project Manager)\n"
        "- Hans Weber (Developer)\n"
        "- Petra Müller (Sales)"
    ),
    "metadata": {
        "org": "Polaris GmbH",
        "directory_members": [
            "Stefanie Weihe",
            "Klaus Braun",
            "Maria Schneider",
            "Hans Weber",
            "Petra Müller",
        ],
    },
}

ALREADY_MIGRATED_ROW = {
    "id": 17700,
    "type": "person",
    "title": "Klaus Braun",
    "content": "Klaus Braun is CTO at Polaris GmbH.",
    "metadata": {
        "name": "Klaus Braun",
        "org": "Polaris GmbH",
        "role": "CTO",
        "schema_version": "people-v1",
        "person_ref": "person-braun-klaus",
        "aliases": [],
    },
}

DIRECTORY_BY_TITLE_ROW = {
    "id": 18200,
    "type": "person",
    "title": "Acme Corp Verzeichnis",
    "content": "Acme Corp staff listing.",
    "metadata": {
        "org": "Acme Corp",
    },
}


# ---------------------------------------------------------------------------
# AK 1 & 4: classify_memory
# ---------------------------------------------------------------------------


class TestClassifyMemory:
    """classify_memory returns 'directory', 'single', or 'skip'."""

    def test_directory_via_metadata_key(self):
        assert mpm.classify_memory(DIRECTORY_ROW) == "directory"

    def test_directory_via_title_keyword_directory(self):
        row = dict(DIRECTORY_BY_TITLE_ROW)
        assert mpm.classify_memory(row) == "directory"

    def test_directory_via_title_keyword_verzeichnis(self):
        row = {**SINGLE_PERSON_ROW, "id": 99, "title": "Team Verzeichnis"}
        assert mpm.classify_memory(row) == "directory"

    def test_single_person(self):
        assert mpm.classify_memory(SINGLE_PERSON_ROW) == "single"

    def test_already_migrated_is_single(self):
        # classify_memory does not check schema_version — plan_migration handles skip
        assert mpm.classify_memory(ALREADY_MIGRATED_ROW) == "single"


# ---------------------------------------------------------------------------
# derive_person_ref
# ---------------------------------------------------------------------------


class TestDerivePersonRef:
    """derive_person_ref produces stable 'person-<last>-<first>' slugs."""

    def test_two_word_name(self):
        assert mpm.derive_person_ref("Stefanie Weihe", 17692) == "person-weihe-stefanie"

    def test_two_word_name_lowercase(self):
        assert mpm.derive_person_ref("Klaus Braun", 17700) == "person-braun-klaus"

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

    def test_special_characters_stripped(self):
        ref = mpm.derive_person_ref("Hans-Peter Weber", 4)
        assert "person-" in ref

    def test_fallback_uses_id_when_name_empty(self):
        ref = mpm.derive_person_ref("", 42)
        assert ref == "person-42"


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
        assert changes["person_ref"] == "person-weihe-stefanie"
        assert "aliases" in changes
        assert isinstance(changes["aliases"], list)

    def test_directory_plan_action(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        assert plan["action"] == "split_directory"

    def test_directory_plan_memory_id(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        assert plan["memory_id"] == 18175

    def test_directory_plan_members(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        assert "members" in plan
        assert len(plan["members"]) == 5
        assert "Stefanie Weihe" in plan["members"]

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
        assert "person-weihe-stefanie" in text

    def test_split_directory_output_contains_members(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        text = mpm.format_dry_run_plan(plan)
        assert "split" in text.lower()
        assert "Stefanie Weihe" in text

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

    def test_skip_when_schema_version_set(self):
        plan = mpm.plan_migration(ALREADY_MIGRATED_ROW)
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
        refs = [mpm.derive_person_ref(m, DIRECTORY_ROW["id"]) for m in plan["members"]]
        assert len(set(refs)) == 5  # all unique

    def test_archive_original_is_true(self):
        plan = mpm.plan_migration(DIRECTORY_ROW)
        assert plan["archive_original"] is True
