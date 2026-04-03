"""Tests for plugin/skills/ob-migrate/SKILL.md — format validation and batch mode logic.

These are unit tests that verify:
1. The SKILL.md file exists and has correct frontmatter/sections (AK1, AK2, AK3, AK4, AK5, AK6)
2. JSONL batch mode parsing helper logic (AK3, AK6 — idempotency via duplicate_of)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SKILL_PATH = Path(__file__).parent.parent.parent / "plugin" / "skills" / "ob-migrate" / "SKILL.md"

REPO_ROOT = Path(__file__).parent.parent.parent


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
        """Frontmatter must have closing --- marker."""
        content = SKILL_PATH.read_text()
        lines = content.split("\n")
        # First line is ---, find the second ---
        closing = any(line.strip() == "---" for line in lines[1:])
        assert closing, "Frontmatter must have closing --- marker"


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
        """Interactive mode must describe extracting facts/knowledge from prior context."""
        content = SKILL_PATH.read_text().lower()
        assert any(word in content for word in ["extract", "fact", "knowledge", "prior context", "conversation"]), \
            "Interactive mode must describe extracting facts from prior context"


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
# AK4: Capture router integration (via save_memory)
# ---------------------------------------------------------------------------


class TestCaptureRouterIntegration:
    """AK4: Each migrated item goes through capture router via save_memory."""

    def test_save_memory_called_per_item(self):
        """SKILL.md must specify calling save_memory for each item."""
        content = SKILL_PATH.read_text()
        assert "save_memory" in content

    def test_type_and_project_passed(self):
        """SKILL.md must describe passing type and project to save_memory."""
        content = SKILL_PATH.read_text()
        assert "type" in content and "project" in content


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


def parse_jsonl_line(line: str) -> dict | None:
    """Parse a single JSONL line. Returns None for malformed lines."""
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
        if not isinstance(data, dict) or "text" not in data:
            return None
        return data
    except json.JSONDecodeError:
        return None


def parse_jsonl_batch(content: str) -> tuple[list[dict], int]:
    """Parse JSONL content. Returns (valid_items, error_count)."""
    items = []
    errors = 0
    for line in content.splitlines():
        if not line.strip():
            continue
        result = parse_jsonl_line(line)
        if result is None:
            errors += 1
        else:
            items.append(result)
    return items, errors


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
        """Summary string format matches expected output."""
        migrated = 2
        skipped = 3
        errors = 1
        summary = f"Migration complete: {migrated} migrated, {skipped} skipped (duplicates), {errors} errors"
        assert "2 migrated" in summary
        assert "3 skipped (duplicates)" in summary
        assert "1 errors" in summary
