"""Tests for Codex transcript parsing in the Codex Stop hook."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "hooks" / "scripts"))

from codex_session_summary import _filter_codex_turns, _session_meta


def _line(kind: str, payload: dict) -> str:
    return json.dumps({"timestamp": "2026-05-14T08:00:00Z", "type": kind, "payload": payload})


def test_filter_codex_turns_extracts_user_and_assistant_messages():
    lines = [
        _line(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Please inspect hooks."}],
            },
        ),
        _line("event_msg", {"type": "token_count"}),
        _line(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "The Codex hook needs additionalContext."}],
            },
        ),
    ]

    turns = _filter_codex_turns(lines)

    assert turns == [
        {"type": "user", "content": "Please inspect hooks.", "isMeta": False},
        {"type": "assistant", "content": "The Codex hook needs additionalContext.", "isMeta": False},
    ]


def test_filter_codex_turns_skips_non_messages_and_empty_content():
    lines = [
        "not json",
        _line("session_meta", {"id": "session-1"}),
        _line("response_item", {"type": "function_call", "name": "exec_command"}),
        _line("response_item", {"type": "message", "role": "assistant", "content": []}),
        _line("response_item", {"type": "message", "role": "system", "content": "skip"}),
    ]

    assert _filter_codex_turns(lines) == []


def test_session_meta_returns_payload():
    lines = [
        _line("session_meta", {"id": "session-1", "cwd": "/tmp/repo"}),
        _line("response_item", {"type": "message", "role": "user", "content": "hello"}),
    ]

    assert _session_meta(lines) == {"id": "session-1", "cwd": "/tmp/repo"}
