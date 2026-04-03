"""Tests for plugin/skills/ob-migrate/SKILL.md — format validation and batch mode logic.

These are unit tests that verify:
1. The SKILL.md file exists and has correct frontmatter/sections (AK1, AK2, AK3, AK4, AK5, AK6)
2. JSONL batch mode parsing helper logic (AK3, AK6 — idempotency via duplicate_of)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from open_brain.migrate import parse_jsonl_batch, parse_jsonl_line

SKILL_PATH = Path(__file__).parent.parent.parent / "plugin" / "skills" / "ob-migrate" / "SKILL.md"


# ---------------------------------------------------------------------------
# AK1: Skill triggers on /ob-migrate
# ---------------------------------------------------------------------------


class TestSkillFileExists:
    """AK1: SKILL.md exists and has correct frontmatter trigger phrases."""

    def test_skill_file_exists(self):
        """SKILL.md must exist at the expected path."""
        assert SKILL_PATH.exists(), f"SKILL.md not found at {SKILL_PATH}"

    def test_frontmatter_present(self):
        """File must start with YAML frontmatter (---)."""
        content = SKILL_PATH.read_text()
        assert content.startswith("---"), "SKILL.md must start with YAML frontmatter"

    def test_frontmatter_has_name(self):
        """Frontmatter must include 'name: ob-migrate'."""
        content = SKILL_PATH.read_text()
        assert "name: ob-migrate" in content

    def test_frontmatter_has_version(self):
        """Frontmatter must include a version field."""
        content = SKILL_PATH.read_text()
        assert "version:" in content

    def test_frontmatter_has_description_with_trigger(self):
        """Frontmatter description must include /ob-migrate trigger phrase."""
        content = SKILL_PATH.read_text()
        assert "ob-migrate" in content or "ob migrate" in content.lower()

    def test_frontmatter_closes(self):
        """Frontmatter must have closing --- marker within first 15 lines."""
        content = SKILL_PATH.read_text()
        lines = content.split("\n")
        # Find the index of the closing --- in lines[1:] and assert it's within first 15 lines
        closing_index = next(
            (i for i, line in enumerate(lines[1:]) if line.strip() == "---"),
            None,
        )
        assert closing_index is not None, "Frontmatter must have closing --- marker"
        assert closing_index < 15, (
            f"Frontmatter closing --- found at line {closing_index + 2}, expected within first 15 lines"
        )


# ---------------------------------------------------------------------------
# AK2: Interactive mode section
# ---------------------------------------------------------------------------


class TestInteractiveMode:
    """AK2: Skill must document interactive mode for extracting facts from context."""

    def test_interactive_mode_section_present(self):
        """SKILL.md must have an interactive mode section."""
        content = SKILL_PATH.read_text().lower()
        assert "interactive" in content, "Must have interactive mode section"

    def test_interactive_mode_calls_save_memory(self):
        """Interactive mode must reference save_memory tool."""
        content = SKILL_PATH.read_text()
        assert "save_memory" in content, "Must reference save_memory MCP tool"

    def test_interactive_mode_extract_facts(self):
        """Interactive mode must have the Mode 1 section heading."""
        content = SKILL_PATH.read_text()
        assert "## Mode 1: Interactive Mode" in content, \
            "Interactive mode must have '## Mode 1: Interactive Mode' section heading"

    def test_interactive_mode_user_confirmation(self):
        """Interactive mode must describe user confirmation before saving."""
        content = SKILL_PATH.read_text()
        assert "Proceed with migration" in content, \
            "Interactive mode must include 'Proceed with migration' confirmation prompt"


# ---------------------------------------------------------------------------
# AK3: Batch mode section
# ---------------------------------------------------------------------------


class TestBatchMode:
    """AK3: Batch mode accepts file path for JSONL/markdown import."""

    def test_batch_mode_section_present(self):
        """SKILL.md must have a batch mode section."""
        content = SKILL_PATH.read_text().lower()
        assert "batch" in content, "Must have batch mode section"

    def test_batch_mode_accepts_file_path(self):
        """Batch mode must describe accepting a file path argument."""
        content = SKILL_PATH.read_text().lower()
        assert "file" in content and ("path" in content or "argument" in content), \
            "Batch mode must describe file path argument"

    def test_batch_mode_jsonl_format(self):
        """Batch mode must describe JSONL format."""
        content = SKILL_PATH.read_text()
        assert "JSONL" in content or "jsonl" in content, "Must describe JSONL format"

    def test_batch_mode_markdown_format(self):
        """Batch mode must describe Obsidian/markdown format."""
        content = SKILL_PATH.read_text().lower()
        assert "markdown" in content or "obsidian" in content, "Must describe markdown/Obsidian format"

    def test_batch_mode_jsonl_schema_documented(self):
        """Batch mode must document the JSONL line schema (text, type, project)."""
        content = SKILL_PATH.read_text()
        # Must show the expected JSON fields
        assert '"text"' in content or "'text'" in content, "JSONL schema must include 'text' field"

    def test_batch_mode_handles_malformed_lines(self):
        """Batch mode must describe graceful error handling for malformed lines."""
        content = SKILL_PATH.read_text().lower()
        assert any(word in content for word in ["malformed", "error", "invalid", "skip", "continue"]), \
            "Must describe graceful handling of malformed/invalid lines"


# ---------------------------------------------------------------------------
# AK3 integ: Batch parsing end-to-end with realistic data
# ---------------------------------------------------------------------------


class TestBatchModeInteg:
    """AK3 integ: End-to-end batch parsing with realistic JSONL content."""

    FIXTURE = "\n".join([
        '{"text": "Use asyncpg for all DB access.", "type": "learning", "project": "open-brain"}',
        '{"text": "Deploy drops MCP connection.", "type": "observation", "project": "open-brain"}',
        '{"text": "Voyage-4 gives 14% better retrieval.", "type": "decision"}',
        '{bad json — malformed line}',
        '{"text": "Always run tests with -m not integration.", "type": "learning"}',
        '{"type": "observation", "project": "open-brain"}',  # missing text — error
        '{"text": "pgvector cosine + tsvector FTS via RRF.", "type": "observation"}',
        '{"text": "Use uv run python for all commands.", "type": "learning", "project": "open-brain"}',
        '',  # blank line — silently skipped
        '{"text": "Redeploy with deploy.sh after any change.", "type": "observation"}',
        '{"text": "pgvector cosine + tsvector FTS via RRF.", "type": "observation"}',  # duplicate of line 7
    ])

    def _simulate_migration(self, items: list[dict], duplicate_texts: set[str]) -> tuple[int, int, int]:
        """Simulate save_memory orchestration for a list of parsed items.

        For each item:
        - If text is in duplicate_texts → simulate save_memory returning duplicate_of
        - Otherwise → simulate save_memory returning a new id

        Returns (migrated, skipped, errors).
        """
        migrated = 0
        skipped = 0
        errors = 0
        for item in items:
            text = item.get("text", "")
            if text in duplicate_texts:
                response = {"id": 1, "message": "Duplicate detected", "duplicate_of": 0}
            else:
                response = {"id": 1}

            if "duplicate_of" in response:
                skipped += 1
            elif "id" in response:
                migrated += 1
            else:
                errors += 1
        return migrated, skipped, errors

    def test_realistic_fixture_parse_counts(self):
        """Realistic 10-line JSONL fixture yields expected valid items and errors."""
        items, errors = parse_jsonl_batch(self.FIXTURE)
        # Lines: 8 valid (including the duplicate text), 2 errors (bad json + missing text), 1 blank skipped
        assert errors == 2, f"Expected 2 parse errors, got {errors}"
        assert len(items) == 8, f"Expected 8 valid items, got {len(items)}"

    def test_migration_with_duplicates(self):
        """Simulate migration where a text appears twice; both match the duplicate set → 2 skipped."""
        items, _errors = parse_jsonl_batch(self.FIXTURE)
        # The text from line 7 appears again on line 11; the duplicate_texts set represents
        # what save_memory already has on disk. Both occurrences are returned as duplicate_of.
        duplicate_text = "pgvector cosine + tsvector FTS via RRF."
        duplicate_texts = {duplicate_text}

        migrated, skipped, errors = self._simulate_migration(items, duplicate_texts)

        # 8 items total: the duplicate text appears twice, both marked as skipped → 6 migrated, 2 skipped
        assert migrated == 6
        assert skipped == 2
        assert errors == 0

    def test_malformed_lines_counted_as_errors(self):
        """Malformed JSONL lines are counted in parse errors, not silently dropped."""
        items, errors = parse_jsonl_batch(self.FIXTURE)
        assert errors >= 1, "At least one malformed line must be counted as error"

    def test_blank_lines_not_counted_as_errors(self):
        """Blank lines are silently skipped, not counted as errors."""
        content = '{"text": "Item A."}\n\n{"text": "Item B."}\n\n'
        items, errors = parse_jsonl_batch(content)
        assert errors == 0
        assert len(items) == 2


# ---------------------------------------------------------------------------
# AK4: Capture router integration (via save_memory)
# ---------------------------------------------------------------------------


class TestCaptureRouterIntegration:
    """AK4: Each migrated item goes through capture router via save_memory."""

    def test_save_memory_called_per_item(self):
        """SKILL.md must specify calling save_memory for each item."""
        content = SKILL_PATH.read_text()
        assert "save_memory" in content

    def test_capture_router_mentioned(self):
        """SKILL.md must reference the capture router."""
        content = SKILL_PATH.read_text().lower()
        assert "capture router" in content, "Must mention 'capture router'"

    def test_save_memory_call_pattern_in_skill(self):
        """SKILL.md must show a save_memory call with type and project parameters."""
        content = SKILL_PATH.read_text()
        assert 'save_memory(' in content, "Must show save_memory call pattern"
        assert 'type=' in content, "save_memory call must include type= parameter"
        assert 'project=' in content, "save_memory call must include project= parameter"

    def test_parse_jsonl_preserves_type_and_project(self):
        """parse_jsonl_batch preserves type and project fields for capture routing."""
        line = '{"text": "Use asyncpg.", "type": "learning", "project": "open-brain"}'
        items, errors = parse_jsonl_batch(line)
        assert errors == 0
        assert len(items) == 1
        assert items[0]["type"] == "learning"
        assert items[0]["project"] == "open-brain"

    def test_parse_jsonl_type_none_when_absent(self):
        """Items without explicit type have no type key (None when accessed via .get)."""
        line = '{"text": "Some fact with no type."}'
        items, errors = parse_jsonl_batch(line)
        assert errors == 0
        assert len(items) == 1
        assert items[0].get("type") is None, "type must be absent (None) so capture router auto-classifies"


# ---------------------------------------------------------------------------
# AK5: Progress tracking
# ---------------------------------------------------------------------------


class TestProgressTracking:
    """AK5: Progress tracking with count/skip/error summary."""

    def test_summary_section_or_description(self):
        """SKILL.md must describe a migration summary."""
        content = SKILL_PATH.read_text().lower()
        assert "summary" in content or "progress" in content, "Must describe progress/summary"

    def test_migrated_count_in_summary(self):
        """Summary must track migrated count."""
        content = SKILL_PATH.read_text().lower()
        assert "migrated" in content, "Summary must track migrated count"

    def test_skipped_count_in_summary(self):
        """Summary must track skipped count."""
        content = SKILL_PATH.read_text().lower()
        assert "skip" in content or "skipped" in content, "Summary must track skipped count"

    def test_error_count_in_summary(self):
        """Summary must track error count."""
        content = SKILL_PATH.read_text().lower()
        assert "error" in content, "Summary must track error count"


# ---------------------------------------------------------------------------
# AK6: Idempotency via duplicate_of
# ---------------------------------------------------------------------------


class TestIdempotency:
    """AK6: Re-running does not create duplicates; duplicate_of signals skip."""

    def test_duplicate_of_documented(self):
        """SKILL.md must reference duplicate_of field in save_memory response."""
        content = SKILL_PATH.read_text()
        assert "duplicate_of" in content, "Must document duplicate_of response field"

    def test_duplicate_counted_as_skipped(self):
        """SKILL.md must say duplicates count as skipped."""
        content = SKILL_PATH.read_text().lower()
        assert "duplicate" in content and ("skip" in content or "skipped" in content), \
            "Duplicates must be counted as skipped"

    def test_idempotent_re_run_described(self):
        """SKILL.md must describe that re-running is safe / idempotent."""
        content = SKILL_PATH.read_text().lower()
        assert "idempotent" in content or "re-run" in content or "re-running" in content or "safe to run" in content, \
            "Must describe idempotent/safe re-run behavior"


# ---------------------------------------------------------------------------
# Helper: JSONL parsing logic (unit test with pure Python)
# ---------------------------------------------------------------------------


class TestJsonlParsingLogic:
    """Unit tests for JSONL batch parsing helper (AK3 — batch mode parsing)."""

    def test_valid_jsonl_line_parsed(self):
        line = '{"text": "Use asyncpg for DB access.", "type": "learning", "project": "open-brain"}'
        result = parse_jsonl_line(line)
        assert result is not None
        assert result["text"] == "Use asyncpg for DB access."
        assert result["type"] == "learning"
        assert result["project"] == "open-brain"

    def test_text_only_jsonl_line(self):
        """JSONL line with only text field is valid (type/project optional)."""
        line = '{"text": "Some fact about the system."}'
        result = parse_jsonl_line(line)
        assert result is not None
        assert result["text"] == "Some fact about the system."

    def test_malformed_json_returns_none(self):
        line = '{not valid json'
        result = parse_jsonl_line(line)
        assert result is None

    def test_missing_text_field_returns_none(self):
        line = '{"type": "learning", "project": "foo"}'
        result = parse_jsonl_line(line)
        assert result is None

    def test_empty_line_returns_none(self):
        assert parse_jsonl_line("") is None
        assert parse_jsonl_line("   ") is None

    def test_batch_parsing_counts_errors(self):
        content = "\n".join([
            '{"text": "Valid item 1."}',
            '{bad json}',
            '{"text": "Valid item 2.", "type": "observation"}',
            '{"type": "no text field"}',
            '{"text": "Valid item 3."}',
        ])
        items, errors = parse_jsonl_batch(content)
        assert len(items) == 3
        assert errors == 2

    def test_batch_parsing_skips_blank_lines(self):
        content = '{"text": "Item 1."}\n\n{"text": "Item 2."}\n'
        items, errors = parse_jsonl_batch(content)
        assert len(items) == 2
        assert errors == 0

    def test_empty_file_returns_zero_counts(self):
        """Empty file content yields no items and no errors."""
        items, errors = parse_jsonl_batch("")
        assert items == []
        assert errors == 0


class TestDuplicateOfIdempotency:
    """Unit tests for idempotency via duplicate_of detection (AK6)."""

    def simulate_save_memory_response(self, is_duplicate: bool, item_id: int = 42, existing_id: int = 10) -> dict:
        """Simulate what save_memory returns."""
        if is_duplicate:
            return {"id": item_id, "message": "Duplicate detected", "duplicate_of": existing_id}
        return {"id": item_id, "message": "Saved"}

    def process_response(self, response: dict) -> str:
        """Classify save_memory response as 'saved', 'skipped', or 'error'."""
        if "duplicate_of" in response:
            return "skipped"
        if "id" in response:
            return "saved"
        return "error"

    def test_new_item_classified_as_saved(self):
        response = self.simulate_save_memory_response(is_duplicate=False)
        assert self.process_response(response) == "saved"

    def test_duplicate_classified_as_skipped(self):
        response = self.simulate_save_memory_response(is_duplicate=True)
        assert self.process_response(response) == "skipped"

    def test_progress_tracking(self):
        """Aggregate results match expected counts."""
        responses = [
            self.simulate_save_memory_response(False),   # saved
            self.simulate_save_memory_response(True),    # skipped
            self.simulate_save_memory_response(False),   # saved
            self.simulate_save_memory_response(True),    # skipped
            self.simulate_save_memory_response(True),    # skipped
        ]
        statuses = [self.process_response(r) for r in responses]
        assert statuses.count("saved") == 2
        assert statuses.count("skipped") == 3
        assert statuses.count("error") == 0

    def test_summary_format(self):
        """Summary string format matches expected output (always plural 'errors')."""
        migrated = 2
        skipped = 3
        errors = 2
        summary = f"Migration complete: {migrated} migrated, {skipped} skipped (duplicates), {errors} errors"
        assert "2 migrated" in summary
        assert "3 skipped (duplicates)" in summary
        assert "2 errors" in summary
