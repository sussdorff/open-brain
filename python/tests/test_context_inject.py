"""Tests for SessionStart context injection output formats."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "hooks" / "scripts"))

from context_inject import build_output


def test_claude_output_uses_system_message():
    output = build_output("Recent memory", "claude")
    assert output["continue"] is True
    assert "systemMessage" in output
    assert "hookSpecificOutput" not in output
    assert output["systemMessage"].startswith("# open-brain Memory Context")


def test_codex_output_uses_sessionstart_additional_context():
    output = build_output("Recent memory", "codex")
    assert output["continue"] is True
    assert output["hookSpecificOutput"] == {
        "hookEventName": "SessionStart",
        "additionalContext": "# open-brain Memory Context\n\nRecent memory",
    }
    assert "systemMessage" not in output


def test_empty_context_is_noop():
    assert build_output("", "codex") == {"continue": True}
