"""Tests for plugin hook_runner filtering logic."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Import functions directly from the plugin script.
# Because the plugin lives outside the Python package tree we manipulate sys.path.
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "plugin" / "scripts"))

from hook_runner import content_hash, is_duplicate, should_skip, truncate


# ─── truncate ─────────────────────────────────────────────────────────────────

class TestTruncate:
    def test_short_string_is_returned_unchanged(self):
        assert truncate("hello", 100) == "hello"

    def test_exact_length_string_is_not_truncated(self):
        s = "a" * 100
        assert truncate(s, 100) == s

    def test_long_string_is_truncated_with_ellipsis(self):
        s = "a" * 200
        result = truncate(s, 100)
        assert result == "a" * 100 + "..."
        assert len(result) == 103

    def test_empty_string_returns_empty(self):
        assert truncate("", 100) == ""

    def test_none_returns_empty(self):
        assert truncate(None, 100) == ""  # type: ignore[arg-type]

    def test_default_max_chars_is_4000(self):
        s = "x" * 5000
        result = truncate(s)
        assert len(result) == 4003  # 4000 + "..."
        assert result.endswith("...")

    def test_non_string_is_coerced(self):
        result = truncate(12345, 10)
        assert isinstance(result, str)


# ─── should_skip ──────────────────────────────────────────────────────────────

class TestShouldSkip:
    def _config(self, extra: dict | None = None) -> dict:
        base = {
            "skip_tools": [
                "Read", "Glob", "Grep", "Skill", "ToolSearch",
                "TaskCreate", "TaskUpdate", "TaskGet",
            ],
            "bash_output_max_kb": 10,
        }
        if extra:
            base.update(extra)
        return base

    def test_returns_true_for_tool_in_skip_list(self):
        assert should_skip("Read", "output", self._config()) is True

    def test_returns_true_for_glob(self):
        assert should_skip("Glob", "output", self._config()) is True

    def test_returns_false_for_edit(self):
        assert should_skip("Edit", "output", self._config()) is False

    def test_returns_false_for_write(self):
        assert should_skip("Write", "output", self._config()) is False

    def test_returns_false_for_bash(self):
        """Small Bash output should NOT be skipped."""
        assert should_skip("Bash", "small output", self._config()) is False

    def test_returns_false_for_agent(self):
        assert should_skip("Agent", "output", self._config()) is False

    def test_returns_true_for_bash_with_large_output(self):
        """Bash with >10 KB output should be skipped."""
        large_output = "x" * (10 * 1024 + 1)
        assert should_skip("Bash", large_output, self._config()) is True

    def test_bash_output_exactly_at_limit_is_not_skipped(self):
        """Bash output at exactly 10 KB should NOT be skipped (boundary: strictly greater)."""
        boundary = "x" * (10 * 1024)
        assert should_skip("Bash", boundary, self._config()) is False

    def test_bash_max_kb_is_configurable(self):
        config = self._config({"bash_output_max_kb": 1})
        large = "x" * 1025
        assert should_skip("Bash", large, config) is True

    def test_empty_skip_list_allows_all_tools(self):
        config = {"skip_tools": [], "bash_output_max_kb": 10}
        assert should_skip("Read", "output", config) is False


# ─── content_hash ─────────────────────────────────────────────────────────────

class TestContentHash:
    def test_returns_string(self):
        h = content_hash({"tool_name": "Edit", "tool_input": "some code"})
        assert isinstance(h, str)
        assert len(h) > 0

    def test_same_input_produces_same_hash(self):
        data = {"tool_name": "Bash", "tool_input": "ls -la"}
        assert content_hash(data) == content_hash(data)

    def test_different_tool_name_produces_different_hash(self):
        a = content_hash({"tool_name": "Edit", "tool_input": "x"})
        b = content_hash({"tool_name": "Write", "tool_input": "x"})
        assert a != b

    def test_different_input_produces_different_hash(self):
        a = content_hash({"tool_name": "Edit", "tool_input": "file_a.py"})
        b = content_hash({"tool_name": "Edit", "tool_input": "file_b.py"})
        assert a != b

    def test_missing_keys_do_not_raise(self):
        h = content_hash({})
        assert isinstance(h, str)

    def test_long_input_is_truncated_for_hash(self):
        """Two inputs that differ only after 500 chars should produce the same hash."""
        base = "a" * 500
        a = content_hash({"tool_name": "Edit", "tool_input": base + "different_suffix_A"})
        b = content_hash({"tool_name": "Edit", "tool_input": base + "different_suffix_B"})
        assert a == b


# ─── is_duplicate ─────────────────────────────────────────────────────────────

class TestIsDuplicate:
    def test_first_occurrence_is_not_duplicate(self, tmp_path):
        dedup_file = tmp_path / "ob-dedup.json"
        with patch("hook_runner.DEDUP_FILE", dedup_file):
            assert is_duplicate("unique-hash-1") is False

    def test_second_occurrence_within_window_is_duplicate(self, tmp_path):
        dedup_file = tmp_path / "ob-dedup.json"
        with patch("hook_runner.DEDUP_FILE", dedup_file):
            is_duplicate("unique-hash-2")
            assert is_duplicate("unique-hash-2") is True

    def test_expired_entry_is_not_duplicate(self, tmp_path):
        dedup_file = tmp_path / "ob-dedup.json"
        # Write an entry with a timestamp 60 seconds in the past (> 30s window)
        old_time = time.time() - 60
        dedup_file.write_text(json.dumps({"stale-hash": old_time}))
        with patch("hook_runner.DEDUP_FILE", dedup_file):
            assert is_duplicate("stale-hash") is False

    def test_recent_entry_within_window_is_duplicate(self, tmp_path):
        dedup_file = tmp_path / "ob-dedup.json"
        recent_time = time.time() - 5  # 5 seconds ago
        dedup_file.write_text(json.dumps({"recent-hash": recent_time}))
        with patch("hook_runner.DEDUP_FILE", dedup_file):
            assert is_duplicate("recent-hash") is True

    def test_different_hashes_are_independent(self, tmp_path):
        dedup_file = tmp_path / "ob-dedup.json"
        with patch("hook_runner.DEDUP_FILE", dedup_file):
            is_duplicate("hash-a")
            assert is_duplicate("hash-b") is False

    def test_missing_dedup_file_is_handled_gracefully(self, tmp_path):
        missing_file = tmp_path / "nonexistent.json"
        with patch("hook_runner.DEDUP_FILE", missing_file):
            # Should not raise even if file doesn't exist
            result = is_duplicate("any-hash")
            assert result is False

    def test_corrupted_dedup_file_is_handled_gracefully(self, tmp_path):
        dedup_file = tmp_path / "ob-dedup.json"
        dedup_file.write_text("this is not json {{{{")
        with patch("hook_runner.DEDUP_FILE", dedup_file):
            result = is_duplicate("any-hash")
            assert result is False
