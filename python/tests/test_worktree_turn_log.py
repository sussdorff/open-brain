"""Tests for plugin/scripts/worktree_turn_log.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PLUGIN_SCRIPTS = Path(__file__).parent.parent.parent / "plugin" / "scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def fake_worktree(tmp_path):
    """Create a fake worktree directory structure."""
    # Structure: <base>/.claude/worktrees/bead-open-brain-abc/<worktree>
    worktree_path = tmp_path / ".claude" / "worktrees" / "bead-open-brain-abc"
    worktree_path.mkdir(parents=True)
    return worktree_path


@pytest.fixture()
def fake_transcript(tmp_path):
    """Create a fake transcript JSONL file with user + assistant + tool_use messages."""
    transcript_path = tmp_path / "session.jsonl"
    lines = [
        # user message
        json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": "Please edit the server.py file to add a new endpoint",
            },
        }),
        # assistant message with text + tool_use
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I will add the endpoint now."},
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {"file_path": "server.py", "old_string": "x", "new_string": "y"},
                    },
                ],
            },
        }),
        # another assistant message
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Done! The endpoint has been added."},
                ],
            },
        }),
    ]
    transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return transcript_path


@pytest.fixture()
def fake_git_dirs(tmp_path):
    """Mock git rev-parse outputs for common-dir and git-dir."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "info").mkdir()
    exclude_file = git_dir / "info" / "exclude"
    exclude_file.write_text("# git ls-files will not list these files\n", encoding="utf-8")
    return git_dir


# ─── Worktree detection ────────────────────────────────────────────────────────


class TestWorktreeDetection:
    def test_noop_when_not_in_worktree(self, tmp_path, capsys):
        """When cwd is not in a worktree path, exit 0 without writing any file."""
        import worktree_turn_log

        hook_data = {
            "cwd": str(tmp_path / "regular-project"),
            "session_id": "sess-abc",
            "hook_event_name": "Stop",
            "transcript_path": "",
        }

        with patch("worktree_turn_log._is_worktree", return_value=False):
            result = worktree_turn_log.handle(hook_data)

        assert result is None or result == {}
        # No file should have been written
        assert not list(tmp_path.rglob(".worktree-turns.jsonl"))

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output.get("continue") is True

    def test_detects_worktree_from_cwd_path(self, fake_worktree):
        """Returns True when '.claude/worktrees/' is in cwd."""
        import worktree_turn_log

        result = worktree_turn_log._is_worktree(str(fake_worktree))
        assert result is True

    def test_not_worktree_for_regular_path(self, tmp_path):
        """Returns False for a path without '.claude/worktrees/'."""
        import worktree_turn_log

        result = worktree_turn_log._is_worktree(str(tmp_path / "src"))
        assert result is False


# ─── Bead ID extraction ────────────────────────────────────────────────────────


class TestBeadIdExtraction:
    def test_extracts_bead_id_from_path(self):
        import worktree_turn_log

        path = "/home/user/.claude/worktrees/bead-open-brain-krx/src"
        assert worktree_turn_log._extract_bead_id(path) == "open-brain-krx"

    def test_extracts_bead_id_with_numbers(self):
        import worktree_turn_log

        path = "/repos/.claude/worktrees/bead-open-brain-9tt"
        assert worktree_turn_log._extract_bead_id(path) == "open-brain-9tt"

    def test_returns_none_when_no_match(self, tmp_path):
        import worktree_turn_log

        assert worktree_turn_log._extract_bead_id(str(tmp_path)) is None


# ─── Transcript parsing ────────────────────────────────────────────────────────


class TestTranscriptParsing:
    def test_extracts_last_user_and_assistant_messages(self, fake_transcript):
        import worktree_turn_log

        result = worktree_turn_log._parse_transcript(fake_transcript)

        assert result["user_input_excerpt"].startswith("Please edit")
        assert "endpoint" in result["assistant_summary_excerpt"]

    def test_extracts_tool_calls(self, fake_transcript):
        import worktree_turn_log

        result = worktree_turn_log._parse_transcript(fake_transcript)

        assert len(result["tool_calls"]) >= 1
        edit_call = result["tool_calls"][0]
        assert edit_call["name"] == "Edit"
        assert edit_call["target"] == "server.py"

    def test_returns_empty_on_missing_transcript(self, tmp_path):
        import worktree_turn_log

        missing = tmp_path / "nonexistent.jsonl"
        result = worktree_turn_log._parse_transcript(missing)

        assert result["user_input_excerpt"] == ""
        assert result["assistant_summary_excerpt"] == ""
        assert result["tool_calls"] == []

    def test_returns_empty_on_corrupt_transcript(self, tmp_path):
        import worktree_turn_log

        bad = tmp_path / "bad.jsonl"
        bad.write_text("{{not json}}\n{broken", encoding="utf-8")
        result = worktree_turn_log._parse_transcript(bad)

        assert result["user_input_excerpt"] == ""
        assert result["tool_calls"] == []

    def test_user_input_truncated_at_500_chars(self, tmp_path):
        import worktree_turn_log

        long_text = "A" * 600
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": long_text},
            }) + "\n",
            encoding="utf-8",
        )
        result = worktree_turn_log._parse_transcript(transcript)
        assert len(result["user_input_excerpt"]) <= 500

    def test_tool_target_from_bash_input(self, tmp_path):
        """Bash tool uses 'command' field as target."""
        import worktree_turn_log

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "ls -la"},
                        }
                    ],
                },
            }) + "\n",
            encoding="utf-8",
        )
        result = worktree_turn_log._parse_transcript(transcript)
        assert result["tool_calls"][0]["name"] == "Bash"
        assert result["tool_calls"][0]["target"] == "ls -la"

    def test_tool_target_fallback_to_first_string_value(self, tmp_path):
        """Falls back to first string value when no known key matches."""
        import worktree_turn_log

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "UnknownTool",
                            "input": {"custom_param": "some_value", "count": 42},
                        }
                    ],
                },
            }) + "\n",
            encoding="utf-8",
        )
        result = worktree_turn_log._parse_transcript(transcript)
        assert result["tool_calls"][0]["name"] == "UnknownTool"
        assert result["tool_calls"][0]["target"] == "some_value"

    def test_tool_result_entries_not_identified_as_user_input(self, tmp_path):
        """Realistic transcript: tool_result entries must not be treated as user messages.

        Structure:
          user (text)
          assistant (text + tool_use)
          user (tool_result)   <- NOT a real user message
          assistant (text)     <- this is the final summary
        """
        import worktree_turn_log

        transcript = tmp_path / "realistic.jsonl"
        lines = [
            # Real user message with text content
            json.dumps({
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "Please read the config file and summarize it",
                },
            }),
            # Assistant responds with text + tool_use
            json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I will read the config file."},
                        {
                            "type": "tool_use",
                            "id": "tool-123",
                            "name": "Read",
                            "input": {"file_path": "config.yml"},
                        },
                    ],
                },
            }),
            # tool_result back from user — looks like type=user but is NOT real user input
            json.dumps({
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-123",
                            "content": "database: postgres\nport: 5432\n",
                        }
                    ],
                },
            }),
            # Final assistant message summarizing the result
            json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "The config uses postgres on port 5432."},
                    ],
                },
            }),
        ]
        transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = worktree_turn_log._parse_transcript(transcript)

        # user_input_excerpt must come from the real user text message, not the tool_result
        assert "config file" in result["user_input_excerpt"]
        assert "tool_result" not in result["user_input_excerpt"]
        assert "database" not in result["user_input_excerpt"]

        # assistant_summary_excerpt must come from the final assistant message
        assert "postgres" in result["assistant_summary_excerpt"]
        assert "port 5432" in result["assistant_summary_excerpt"]

    def test_tool_target_truncated_at_200_chars(self, tmp_path):
        """Long command targets are truncated to 200 characters."""
        import worktree_turn_log

        long_cmd = "echo " + "x" * 300
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": long_cmd},
                        }
                    ],
                },
            }) + "\n",
            encoding="utf-8",
        )
        result = worktree_turn_log._parse_transcript(transcript)
        assert len(result["tool_calls"][0]["target"]) <= 200


# ─── JSONL output ─────────────────────────────────────────────────────────────


class TestJsonlOutput:
    def test_appends_jsonl_line(self, fake_worktree, fake_transcript, fake_git_dirs):
        """Hook writes a valid JSON line to .worktree-turns.jsonl."""
        import worktree_turn_log

        hook_data = {
            "cwd": str(fake_worktree),
            "session_id": "sess-123",
            "hook_event_name": "Stop",
            "transcript_path": str(fake_transcript),
        }

        def fake_git_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "--git-common-dir" in cmd:
                result.stdout = str(fake_git_dirs) + "\n"
            elif "--show-toplevel" in cmd:
                result.stdout = str(fake_worktree.parent.parent.parent) + "\n"
            else:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_git_run):
            with patch("worktree_turn_log._is_worktree", return_value=True):
                worktree_turn_log.handle(hook_data)

        output_file = fake_worktree / ".worktree-turns.jsonl"
        assert output_file.exists()
        line = json.loads(output_file.read_text(encoding="utf-8").strip())

        assert line["session_id"] == "sess-123"
        assert line["hook_type"] == "Stop"
        assert line["bead_id"] == "open-brain-abc"
        assert line["agent"] == "claude-code"
        assert "ts" in line
        assert "user_input_excerpt" in line
        assert "assistant_summary_excerpt" in line
        assert "tool_calls" in line

    def test_sets_parent_session_id_for_subagent_stop(self, fake_worktree, fake_transcript, fake_git_dirs):
        """parent_session_id is set when hook_event_name is SubagentStop."""
        import worktree_turn_log

        hook_data = {
            "cwd": str(fake_worktree),
            "session_id": "child-sess",
            "parent_session_id": "parent-sess",
            "hook_event_name": "SubagentStop",
            "transcript_path": str(fake_transcript),
        }

        def fake_git_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "--git-common-dir" in cmd:
                result.stdout = str(fake_git_dirs) + "\n"
            elif "--show-toplevel" in cmd:
                result.stdout = str(fake_worktree.parent.parent.parent) + "\n"
            else:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_git_run):
            with patch("worktree_turn_log._is_worktree", return_value=True):
                worktree_turn_log.handle(hook_data)

        output_file = fake_worktree / ".worktree-turns.jsonl"
        line = json.loads(output_file.read_text(encoding="utf-8").strip())
        assert line["parent_session_id"] == "parent-sess"
        assert line["hook_type"] == "SubagentStop"

    def test_appends_multiple_lines(self, fake_worktree, fake_transcript, fake_git_dirs):
        """Multiple calls append new lines without overwriting."""
        import worktree_turn_log

        output_file = fake_worktree / ".worktree-turns.jsonl"

        def fake_git_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "--git-common-dir" in cmd:
                result.stdout = str(fake_git_dirs) + "\n"
            elif "--show-toplevel" in cmd:
                result.stdout = str(fake_worktree.parent.parent.parent) + "\n"
            else:
                result.stdout = ""
            return result

        for i in range(3):
            hook_data = {
                "cwd": str(fake_worktree),
                "session_id": f"sess-{i}",
                "hook_event_name": "Stop",
                "transcript_path": str(fake_transcript),
            }
            with patch("subprocess.run", side_effect=fake_git_run):
                with patch("worktree_turn_log._is_worktree", return_value=True):
                    worktree_turn_log.handle(hook_data)

        lines = [l for l in output_file.read_text(encoding="utf-8").strip().split("\n") if l]
        assert len(lines) == 3


# ─── Self-healing exclude ──────────────────────────────────────────────────────


class TestSelfHealingExclude:
    def test_adds_worktree_turns_to_exclude(self, fake_worktree, fake_transcript, fake_git_dirs):
        """Adds .worktree-turns.jsonl to git exclude file if not present."""
        import worktree_turn_log

        hook_data = {
            "cwd": str(fake_worktree),
            "session_id": "sess-x",
            "hook_event_name": "Stop",
            "transcript_path": str(fake_transcript),
        }

        def fake_git_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "--git-common-dir" in cmd:
                result.stdout = str(fake_git_dirs) + "\n"
            elif "--show-toplevel" in cmd:
                result.stdout = str(fake_worktree.parent.parent.parent) + "\n"
            else:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=fake_git_run):
            with patch("worktree_turn_log._is_worktree", return_value=True):
                worktree_turn_log.handle(hook_data)

        exclude_content = (fake_git_dirs / "info" / "exclude").read_text(encoding="utf-8")
        assert "/.worktree-turns.jsonl" in exclude_content

    def test_creates_exclude_file_when_missing(self, tmp_path):
        """Creates info/exclude file and writes pattern when file doesn't exist."""
        import worktree_turn_log

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        # Intentionally do NOT create info/ or info/exclude

        worktree_turn_log._ensure_exclude(git_dir)

        exclude_file = git_dir / "info" / "exclude"
        assert exclude_file.exists()
        content = exclude_file.read_text(encoding="utf-8")
        assert "/.worktree-turns.jsonl" in content

    def test_exclude_is_idempotent(self, fake_worktree, fake_transcript, fake_git_dirs):
        """Running twice doesn't duplicate the exclude entry."""
        import worktree_turn_log

        hook_data = {
            "cwd": str(fake_worktree),
            "session_id": "sess-y",
            "hook_event_name": "Stop",
            "transcript_path": str(fake_transcript),
        }

        def fake_git_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "--git-common-dir" in cmd:
                result.stdout = str(fake_git_dirs) + "\n"
            elif "--show-toplevel" in cmd:
                result.stdout = str(fake_worktree.parent.parent.parent) + "\n"
            else:
                result.stdout = ""
            return result

        for _ in range(2):
            with patch("subprocess.run", side_effect=fake_git_run):
                with patch("worktree_turn_log._is_worktree", return_value=True):
                    worktree_turn_log.handle(hook_data)

        exclude_content = (fake_git_dirs / "info" / "exclude").read_text(encoding="utf-8")
        count = exclude_content.count("/.worktree-turns.jsonl")
        assert count == 1


# ─── Error tolerance ───────────────────────────────────────────────────────────


class TestErrorTolerance:
    def test_continues_on_transcript_error(self, fake_worktree, capsys):
        """Hook exits 0 even if transcript cannot be parsed."""
        import worktree_turn_log

        hook_data = {
            "cwd": str(fake_worktree),
            "session_id": "sess-err",
            "hook_event_name": "Stop",
            "transcript_path": "/nonexistent/path/transcript.jsonl",
        }

        def fake_git_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = str(fake_worktree) + "\n"
            return result

        with patch("subprocess.run", side_effect=fake_git_run):
            with patch("worktree_turn_log._is_worktree", return_value=True):
                worktree_turn_log.handle(hook_data)

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output.get("continue") is True

    def test_main_prints_continue_true(self, fake_worktree, fake_transcript, capsys):
        """main() always prints {"continue": true} to stdout."""
        import worktree_turn_log

        hook_data = {
            "cwd": str(fake_worktree),
            "session_id": "sess-main",
            "hook_event_name": "Stop",
            "transcript_path": str(fake_transcript),
        }

        def fake_git_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = str(fake_worktree) + "\n"
            return result

        stdin_data = json.dumps(hook_data)
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = stdin_data
            with patch("subprocess.run", side_effect=fake_git_run):
                worktree_turn_log.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output.get("continue") is True
