"""Tests for the ob CLI — argument parsing, output formatting, and integration."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.cli.client import (
    MCPError,
    call_tool,
    _extract_result,
    _get_server_url,
    _load_api_key,
    _load_token,
    _load_url_token,
    _parse_sse_response,
    _normalize_mcp_url,
    _with_url_token,
)
import open_brain.cli.main as cli_main
from open_brain.cli.main import _build_parser, _output, _output_result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse(args: list[str]) -> Any:
    """Parse CLI args using the ob parser."""
    return _build_parser().parse_args(args)


# ---------------------------------------------------------------------------
# Argument parsing tests
# ---------------------------------------------------------------------------


class TestSearchCommand:
    def test_basic_query(self):
        args = parse(["search", "python async patterns"])
        assert args.command == "search"
        assert args.query == "python async patterns"
        assert args.limit is None
        assert args.project is None
        assert args.type is None

    def test_with_all_flags(self):
        args = parse(["search", "test", "--limit", "5", "--project", "myproj", "--type", "decision"])
        assert args.limit == 5
        assert args.project == "myproj"
        assert args.type == "decision"

    def test_pretty_flag_before_subcommand(self):
        args = parse(["--pretty", "search", "query"])
        assert args.pretty is True
        assert args.command == "search"

    def test_pretty_flag_default_false(self):
        args = parse(["search", "query"])
        assert args.pretty is False


class TestConceptCommand:
    def test_basic(self):
        args = parse(["concept", "semantic query"])
        assert args.command == "concept"
        assert args.query == "semantic query"

    def test_with_limit_and_project(self):
        args = parse(["concept", "q", "--limit", "3", "--project", "p"])
        assert args.limit == 3
        assert args.project == "p"


class TestSaveCommand:
    def test_source_ref_is_required(self):
        with pytest.raises(SystemExit):
            parse(["save", "some text to save"])

    def test_minimal_canonical_origin(self):
        args = parse([
            "save",
            "some text to save",
            "--source-ref",
            "agent-session:codex:session-123",
        ])
        assert args.command == "save"
        assert args.text == "some text to save"
        assert args.project is None
        assert args.type is None
        assert args.title is None
        assert args.producer == "ob-cli"
        assert args.source_ref == "agent-session:codex:session-123"

    def test_with_optional_fields(self):
        args = parse([
            "save", "text", "--project", "proj", "--type", "observation",
            "--title", "My Title", "--producer", "session-close",
            "--source-ref", "agent-session:claude:session-456",
        ])
        assert args.project == "proj"
        assert args.type == "observation"
        assert args.title == "My Title"
        assert args.producer == "session-close"
        assert args.source_ref == "agent-session:claude:session-456"


class TestGetCommand:
    def test_single_id(self):
        args = parse(["get", "42"])
        assert args.command == "get"
        assert args.ids == ["42"]

    def test_multiple_ids(self):
        args = parse(["get", "1", "2", "3"])
        assert args.ids == ["1", "2", "3"]


class TestTimelineCommand:
    def test_with_anchor(self):
        args = parse(["timeline", "--anchor", "10"])
        assert args.command == "timeline"
        assert args.anchor == 10
        assert args.query is None

    def test_with_query(self):
        args = parse(["timeline", "--query", "search term"])
        assert args.query == "search term"
        assert args.anchor is None

    def test_depth_flags(self):
        args = parse(["timeline", "--anchor", "5", "--depth-before", "3", "--depth-after", "2"])
        assert args.depth_before == 3
        assert args.depth_after == 2

    def test_with_project(self):
        args = parse(["timeline", "--project", "myproject"])
        assert args.project == "myproject"


class TestContextCommand:
    def test_no_args(self):
        args = parse(["context"])
        assert args.command == "context"
        assert args.project is None
        assert args.limit is None

    def test_with_project_and_limit(self):
        args = parse(["context", "--project", "proj", "--limit", "20"])
        assert args.project == "proj"
        assert args.limit == 20


class TestStatsCommand:
    def test_no_args(self):
        args = parse(["stats"])
        assert args.command == "stats"


class TestDoctorCommand:
    def test_no_args(self):
        args = parse(["doctor"])
        assert args.command == "doctor"


class TestLearningsCommand:
    def test_analyze_defaults(self):
        args = parse(["learnings", "analyze"])
        assert args.command == "learnings"
        assert args.learnings_command == "analyze"
        assert args.limit == 50
        assert args.project is None
        assert args.source is None
        assert args.model is None
        assert args.direct is False
        assert args.cursor is None
        assert args.run_id is None
        assert args.detach is False

    def test_analyze_filters(self):
        args = parse(
            [
                "learnings",
                "analyze",
                "--limit",
                "25",
                "--project",
                "open-brain",
                "--source",
                "session-close",
                "--model",
                "openai/gpt-4.1-mini",
            ]
        )
        assert args.limit == 25
        assert args.project == "open-brain"
        assert args.source == "session-close"
        assert args.model == "openai/gpt-4.1-mini"

    @pytest.mark.asyncio
    async def test_regression_analyze_uses_remote_tool_by_default(self):
        run_id = "3ea86d12-a68f-4138-b6e7-1a75ca527f15"
        args = parse([
            "learnings", "analyze", "--limit", "10", "--run-id", run_id
        ])
        expected = {"counts": {"source_summaries": 10}, "queues": {}}
        running = {"run_id": run_id, "status": "running", "report": None}
        completed = {"run_id": run_id, "status": "completed", "report": expected}

        with (
            patch(
                "open_brain.cli.main.call_tool",
                new_callable=AsyncMock,
                side_effect=[running, completed],
            ) as call,
            patch(
                "open_brain.cli.main.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await cli_main._cmd_learnings(args)

        assert result["counts"] == expected["counts"]
        assert result["run"]["run_id"] == run_id
        assert call.await_args_list[0].args == (
            "analyze_session_learnings",
            {
                "run_id": run_id,
                "limit": 10,
                "project": None,
                "source": None,
                "model": None,
                "cursor": None,
            },
        )
        assert call.await_args_list[1].args == (
            "get_session_learning_analysis_run",
            {"run_id": run_id},
        )

    @pytest.mark.asyncio
    async def test_detached_analysis_returns_retrievable_run_immediately(self):
        run_id = "3ea86d12-a68f-4138-b6e7-1a75ca527f15"
        args = parse([
            "learnings", "analyze", "--detach", "--run-id", run_id
        ])
        running = {"run_id": run_id, "status": "running", "report": None}
        with patch(
            "open_brain.cli.main.call_tool",
            new_callable=AsyncMock,
            return_value=running,
        ) as call:
            result = await cli_main._cmd_learnings(args)

        assert result == running
        assert call.await_count == 1

    @pytest.mark.asyncio
    async def test_transport_error_preserves_client_generated_run_id(self):
        run_id = "3ea86d12-a68f-4138-b6e7-1a75ca527f15"
        args = parse(["learnings", "analyze", "--run-id", run_id])
        with (
            patch(
                "open_brain.cli.main.call_tool",
                new_callable=AsyncMock,
                side_effect=MCPError("transport lost"),
            ),
            pytest.raises(MCPError, match=run_id) as raised,
        ):
            await cli_main._cmd_learnings(args)

        assert f"ob learnings show {run_id}" in str(raised.value)

    @pytest.mark.asyncio
    async def test_show_returns_saved_completed_report(self):
        run_id = "3ea86d12-a68f-4138-b6e7-1a75ca527f15"
        args = parse(["learnings", "show", run_id])
        run = {
            "run_id": run_id,
            "status": "completed",
            "report": {"counts": {"source_summaries": 50}, "queues": {}},
        }
        with patch(
            "open_brain.cli.main.call_tool",
            new_callable=AsyncMock,
            return_value=run,
        ) as call:
            result = await cli_main._cmd_learnings(args)

        assert result["counts"]["source_summaries"] == 50
        assert result["run"]["run_id"] == run_id
        call.assert_awaited_once_with(
            "get_session_learning_analysis_run",
            {"run_id": run_id},
        )

    @pytest.mark.asyncio
    async def test_explicit_direct_mode_prepares_local_database_analysis(self):
        run_id = "3ea86d12-a68f-4138-b6e7-1a75ca527f15"
        args = parse([
            "learnings", "analyze", "--limit", "10", "--direct",
            "--run-id", run_id,
        ])
        expected = {"counts": {"source_summaries": 10}, "queues": {}}
        running = SimpleNamespace(run_id=run_id)
        completed = SimpleNamespace(
            run_id=run_id,
            to_dict=lambda: {
                "run_id": run_id,
                "status": "completed",
                "report": expected,
            },
        )

        with (
            patch(
                "open_brain.cli.direct.load_database_url",
                return_value="postgresql://local/open_brain",
            ) as load_database_url,
            patch("open_brain.cli.direct.prepare_direct_env") as prepare_direct_env,
            patch(
                "open_brain.session_learning_runs.create_session_learning_run",
                new_callable=AsyncMock,
                return_value=(running, True),
            ) as create_run,
            patch(
                "open_brain.session_learning_runs.execute_session_learning_run",
                new_callable=AsyncMock,
                return_value=completed,
            ) as execute_run,
            patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as call,
        ):
            result = await cli_main._cmd_learnings(args)

        assert result["counts"] == expected["counts"]
        load_database_url.assert_called_once_with()
        prepare_direct_env.assert_called_once_with("postgresql://local/open_brain")
        create_run.assert_awaited_once_with(
            run_id=run_id,
            parameters={
                "limit": 10,
                "project": None,
                "source": None,
                "model": None,
                "cursor": None,
            },
        )
        execute_run.assert_awaited_once()
        call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_mode_fails_clearly_without_database_url(self):
        args = parse(["learnings", "analyze", "--direct"])

        with (
            patch("open_brain.cli.direct.load_database_url", return_value=""),
            patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as call,
            pytest.raises(SystemExit),
        ):
            await cli_main._cmd_learnings(args)

        call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_mode_rejects_detach(self):
        args = parse(["learnings", "analyze", "--direct", "--detach"])

        with (
            patch("open_brain.cli.direct.load_database_url") as load_database_url,
            pytest.raises(SystemExit),
        ):
            await cli_main._cmd_learnings(args)

        load_database_url.assert_not_called()

    def test_show_parses_run_id(self):
        args = parse([
            "learnings", "show", "3ea86d12-a68f-4138-b6e7-1a75ca527f15"
        ])
        assert args.learnings_command == "show"
        assert args.run_id == "3ea86d12-a68f-4138-b6e7-1a75ca527f15"


class TestPortableBackupCommands:
    def test_export_args(self, tmp_path: Path):
        bundle = tmp_path / "bundle"
        args = parse(["export", str(bundle), "--source-label", "fixture"])
        assert args.command == "export"
        assert args.bundle_path == str(bundle)
        assert args.source_label == "fixture"

    def test_restore_args(self, tmp_path: Path):
        bundle = tmp_path / "bundle"
        args = parse(["restore", str(bundle), "--skip-embeddings"])
        assert args.command == "restore"
        assert args.bundle_path == str(bundle)
        assert args.regenerate_embeddings is False

    def test_verify_args(self, tmp_path: Path):
        bundle = tmp_path / "bundle"
        args = parse(["verify", str(bundle)])
        assert args.command == "verify"
        assert args.bundle_path == str(bundle)


class TestServerCommand:
    def test_defaults(self):
        args = parse(["server"])
        assert args.command == "server"
        assert args.host == "0.0.0.0"
        assert args.port is None

    def test_host_and_port(self):
        args = parse(["server", "--host", "127.0.0.1", "--port", "9000"])
        assert args.host == "127.0.0.1"
        assert args.port == 9000


class TestUpdateCommand:
    def test_id_and_text(self):
        args = parse(["update", "7", "--text", "new content"])
        assert args.command == "update"
        assert args.id == "7"
        assert args.text == "new content"

    def test_all_fields(self):
        args = parse(
            [
                "update",
                "7",
                "--text",
                "t",
                "--type",
                "decision",
                "--project",
                "p",
                "--title",
                "T",
                "--subtitle",
                "S",
                "--narrative",
                "N",
                "--metadata",
                '{"status":"discarded"}',
            ]
        )
        assert args.type == "decision"
        assert args.project == "p"
        assert args.title == "T"
        assert args.subtitle == "S"
        assert args.narrative == "N"
        assert args.metadata == {"status": "discarded"}


class TestPeopleCommand:
    def test_list_defaults(self):
        args = parse(["people", "list"])
        assert args.command == "people"
        assert args.people_command == "list"
        assert args.include_merged is False
        assert args.collisions is False
        assert args.json_output is False

    def test_list_flags(self):
        args = parse(["people", "list", "--include-merged", "--collisions"])
        assert args.include_merged is True
        assert args.collisions is True

    def test_list_json_flag_after_subcommand(self):
        args = parse(["people", "list", "--json"])
        assert args.json_output is True

    def test_list_json_flag_before_subcommand(self):
        args = parse(["--json", "people", "list"])
        assert args.json_output is True

    def test_merge_required_args(self):
        args = parse(["people", "merge", "--source", "10", "--target", "20"])
        assert args.command == "people"
        assert args.people_command == "merge"
        assert args.source == 10
        assert args.target == 20
        assert args.dry_run is False
        assert args.absorb_text is False

    def test_merge_flags(self):
        args = parse(
            [
                "people",
                "merge",
                "--source",
                "10",
                "--target",
                "20",
                "--dry-run",
                "--absorb-text",
            ]
        )
        assert args.dry_run is True
        assert args.absorb_text is True


# ---------------------------------------------------------------------------
# Output formatting tests
# ---------------------------------------------------------------------------


class TestOutput:
    def test_compact_json(self, capsys):
        _output({"key": "value", "num": 42}, pretty=False)
        captured = capsys.readouterr()
        assert captured.out.strip() == '{"key": "value", "num": 42}'

    def test_pretty_json(self, capsys):
        _output({"key": "value"}, pretty=True)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed == {"key": "value"}
        # Pretty output has newlines
        assert "\n" in captured.out

    def test_list_output(self, capsys):
        _output([1, 2, 3], pretty=False)
        captured = capsys.readouterr()
        assert captured.out.strip() == "[1, 2, 3]"

    def test_learning_analysis_uses_terminal_display_by_default(self, capsys):
        args = parse(["learnings", "analyze"])
        _output_result(
            {
                "counts": {
                    "source_summaries": 50,
                    "candidates": 12,
                    "reviewable_learning_clusters": 2,
                    "held_learning_clusters": 3,
                    "todos": 4,
                    "decisions": 1,
                    "standard_candidates": 1,
                    "skill_candidates": 0,
                    "duplicate_doctrine": 1,
                    "noise": 0,
                },
                "queues": {
                    "reviewable_learning_clusters": [
                        {
                            "canonical_learning": "Installers must reconcile target state.",
                            "source_memory_ids": [101, 102],
                            "existing_learning_matches": [
                                {
                                    "memory_id": 77,
                                    "content": "Installers converge on target state.",
                                }
                            ],
                            "evidence": ["Two installer runs created duplicate hooks."],
                            "confidence": 0.9,
                            "severity": "high",
                        }
                    ],
                    "held_learning_clusters": [],
                    "todos": [],
                    "decisions": [],
                    "standard_candidates": [],
                    "skill_candidates": [],
                    "duplicate_doctrine": [],
                    "noise": [],
                },
                "write_side_effects": False,
            },
            args,
        )
        captured = capsys.readouterr()
        assert "Session learning analysis" in captured.out
        assert "Source summaries: 50" in captured.out
        assert "Reviewable learning clusters: 2" in captured.out
        assert "Installers must reconcile target state." in captured.out
        assert "Confidence: 0.90" in captured.out
        assert "Evidence: Two installer runs created duplicate hooks." in captured.out
        assert "Existing learning match: [77] Installers converge on target state." in captured.out
        assert "No memories, priorities, lifecycle states, or work items were changed." in captured.out
        assert '"queues"' not in captured.out

    def test_completed_run_reports_ledger_as_operational_write(self):
        run_id = "3ea86d12-a68f-4138-b6e7-1a75ca527f15"
        result = cli_main._analysis_run_output(
            {
                "run_id": run_id,
                "status": "completed",
                "report": {"counts": {}, "queues": {}, "write_side_effects": False},
            }
        )

        assert result["operational_writes"] == ["session_learning_analysis_runs"]

    def test_learning_analysis_json_flag_keeps_json_output(self, capsys):
        args = parse(["--json", "learnings", "analyze"])
        payload = {"counts": {"source_summaries": 1}, "queues": {}}
        _output_result(payload, args)
        captured = capsys.readouterr()
        assert json.loads(captured.out) == payload

    def test_unicode_output(self, capsys):
        _output({"text": "Ümlauts and émojis"}, pretty=False)
        captured = capsys.readouterr()
        assert "Ümlauts" in captured.out

    def test_people_list_uses_terminal_display_by_default(self, capsys):
        args = parse(["people", "list"])
        _output_result(
            {
                "mode": "list",
                "total": 1,
                "active": 1,
                "merged": 0,
                "persons": [
                    {
                        "id": 10,
                        "name": "Ada Lovelace",
                        "org": "Analytical Engines",
                        "aliases": ["A. Lovelace"],
                        "refs": 3,
                        "rels": 1,
                        "merged_into": None,
                    }
                ],
                "collision_groups": 0,
            },
            args,
        )
        captured = capsys.readouterr()
        assert "ID" in captured.out
        assert "Ada Lovelace" in captured.out
        assert '"persons"' not in captured.out

    def test_people_list_json_flag_keeps_json_output(self, capsys):
        args = parse(["people", "list", "--json"])
        _output_result({"mode": "list", "persons": []}, args)
        captured = capsys.readouterr()
        assert captured.out.strip() == '{"mode": "list", "persons": []}'

    def test_people_list_pretty_keeps_terminal_display(self, capsys):
        args = parse(["--pretty", "people", "list"])
        _output_result({"mode": "list", "persons": []}, args)
        captured = capsys.readouterr()
        assert "Total: 0" in captured.out
        assert '"persons"' not in captured.out

    def test_macwhisper_list_uses_terminal_display_by_default(self, capsys):
        args = parse(["ingest", "macwhisper", "list"])
        _output_result(
            {
                "history_path": "/tmp/MacWhisper",
                "count": 1,
                "items": [
                    {
                        "entry_id": "session:abc123",
                        "created_at": "2026-04-30 08:22:41",
                        "text_preview": "A short transcript preview.",
                        "title": "Planning Sync",
                        "source_type": "recorded_meeting",
                        "source_app": "Teams",
                        "duration_seconds": 1800,
                        "participants": ["Alice", "Bob"],
                        "ingested": True,
                        "memory_id": 42,
                        "run_id": "run-123",
                    }
                ],
            },
            args,
        )
        captured = capsys.readouterr()
        assert "MacWhisper history: /tmp/MacWhisper" in captured.out
        assert "session:abc123" in captured.out
        assert "Planning Sync  Teams  30m 00s" in captured.out
        assert "Status: ingested (memory 42, run run-123)" in captured.out
        assert "Participants: Alice, Bob" in captured.out
        assert "ob ingest macwhisper entry <entry-id>" in captured.out
        assert '"items"' not in captured.out

    def test_macwhisper_list_renders_new_status_and_scanned_count(self, capsys):
        args = parse(["ingest", "macwhisper", "list", "--not-ingested"])
        _output_result(
            {
                "history_path": "/tmp/MacWhisper",
                "count": 1,
                "scanned_count": 5,
                "items": [
                    {
                        "entry_id": "session:new",
                        "created_at": "2026-04-30 08:22:41",
                        "text_preview": "A short transcript preview.",
                        "ingested": False,
                    }
                ],
            },
            args,
        )
        captured = capsys.readouterr()
        assert "Entries shown: 1" in captured.out
        assert "Entries scanned: 5" in captured.out
        assert "Status: new" in captured.out

    def test_macwhisper_list_json_flag_keeps_json_output(self, capsys):
        args = parse(["ingest", "macwhisper", "list", "--json"])
        _output_result({"history_path": "/tmp/MacWhisper", "count": 0, "items": []}, args)
        captured = capsys.readouterr()
        assert captured.out.strip() == (
            '{"history_path": "/tmp/MacWhisper", "count": 0, "items": []}'
        )

    def test_macwhisper_entry_uses_terminal_display_by_default(self, capsys):
        args = parse(["ingest", "macwhisper", "entry", "dictation:abc123"])
        _output_result(
            {
                "meeting_memory_id": 42,
                "person_memory_ids": [1, 2],
                "mention_memory_ids": [3],
                "interaction_memory_ids": [4],
                "relationship_ids": [5, 6],
                "follow_up_candidates": [],
                "run_id": "run-123",
            },
            args,
        )
        captured = capsys.readouterr()
        assert "MacWhisper entry ingested" in captured.out
        assert "Entry: dictation:abc123" in captured.out
        assert "Meeting memory: 42" in captured.out
        assert '"meeting_memory_id"' not in captured.out


# ---------------------------------------------------------------------------
# Client utility function tests
# ---------------------------------------------------------------------------


class TestExtractResult:
    def test_text_content_json(self):
        response = {
            "result": {
                "content": [{"type": "text", "text": '{"id": 1, "title": "test"}'}]
            }
        }
        result = _extract_result(response)
        assert result == {"id": 1, "title": "test"}

    def test_text_content_plain(self):
        response = {
            "result": {
                "content": [{"type": "text", "text": "plain text response"}]
            }
        }
        result = _extract_result(response)
        assert result == "plain text response"

    def test_error_response(self):
        response = {"error": {"message": "Tool not found", "code": -32601}}
        with pytest.raises(MCPError, match="Tool not found"):
            _extract_result(response)

    def test_empty_result(self):
        response = {"result": {}}
        result = _extract_result(response)
        assert result == {}

    def test_non_text_content_ignored(self):
        response = {
            "result": {
                "content": [
                    {"type": "image", "data": "base64stuff"},
                    {"type": "text", "text": '"found"'},
                ]
            }
        }
        result = _extract_result(response)
        assert result == "found"


class TestParseSseResponse:
    def test_single_data_line(self):
        sse = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"content":[]}}\n'
        result = _parse_sse_response(sse)
        assert result["id"] == 1

    def test_skips_done_sentinel(self):
        sse = "data: [DONE]\ndata: {}\n"
        result = _parse_sse_response(sse)
        assert result == {}

    def test_no_data_raises(self):
        with pytest.raises(MCPError, match="No valid JSON-RPC result"):
            _parse_sse_response("event: ping\n: comment\n")

    def test_skips_invalid_json(self):
        sse = "data: not-json\ndata: {}\n"
        result = _parse_sse_response(sse)
        assert result == {}


class TestLoadToken:
    def test_env_var_takes_priority(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OB_TOKEN", "env-token")
        # Even if token file exists, env var wins
        assert _load_token() == "env-token"

    def test_reads_token_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OB_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        token_file = tmp_path / ".open-brain" / "token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text("file-token\n")

        with (
            patch("open_brain.cli.client.LEGACY_CONFIG_FILE", tmp_path / "legacy.json"),
            patch("open_brain.cli.client.TOKEN_FILE", token_file),
        ):
            result = _load_token()
        assert result == "file-token"

    def test_reads_xdg_config_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OB_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        config_file = tmp_path / "xdg" / "open-brain" / "config.json"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(json.dumps({"token": "xdg-token"}))

        with (
            patch("open_brain.cli.client.LEGACY_CONFIG_FILE", tmp_path / "legacy.json"),
            patch("open_brain.cli.client.TOKEN_FILE", tmp_path / "legacy-token"),
        ):
            result = _load_token()

        assert result == "xdg-token"

    def test_reads_xdg_token_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OB_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        token_file = tmp_path / "xdg" / "open-brain" / "token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text("xdg-file-token\n")

        with (
            patch("open_brain.cli.client.LEGACY_CONFIG_FILE", tmp_path / "legacy.json"),
            patch("open_brain.cli.client.TOKEN_FILE", tmp_path / "legacy-token"),
        ):
            result = _load_token()

        assert result == "xdg-file-token"

    def test_returns_none_when_no_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OB_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        nonexistent = tmp_path / "no-such-file"

        with (
            patch("open_brain.cli.client.LEGACY_CONFIG_FILE", tmp_path / "legacy.json"),
            patch("open_brain.cli.client.TOKEN_FILE", nonexistent),
        ):
            result = _load_token()

        assert result is None


class TestLoadUrlToken:
    def test_env_var_takes_priority(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OB_URL_TOKEN", "env-url-token")
        token_file = tmp_path / ".open-brain" / "url-token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text("file-url-token\n")

        with patch("open_brain.cli.client.URL_TOKEN_FILE", token_file):
            result = _load_url_token()

        assert result == "env-url-token"

    def test_reads_url_token_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OB_URL_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        token_file = tmp_path / ".open-brain" / "url-token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text("file-url-token\n")

        with (
            patch("open_brain.cli.client.LEGACY_CONFIG_FILE", tmp_path / "legacy.json"),
            patch("open_brain.cli.client.URL_TOKEN_FILE", token_file),
        ):
            result = _load_url_token()

        assert result == "file-url-token"

    def test_reads_xdg_config_url_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OB_URL_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        config_file = tmp_path / "xdg" / "open-brain" / "config.json"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(json.dumps({"url_token": "xdg-url-token"}))

        with (
            patch("open_brain.cli.client.LEGACY_CONFIG_FILE", tmp_path / "legacy.json"),
            patch("open_brain.cli.client.URL_TOKEN_FILE", tmp_path / "legacy-url-token"),
        ):
            result = _load_url_token()

        assert result == "xdg-url-token"

    def test_reads_xdg_url_token_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OB_URL_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        token_file = tmp_path / "xdg" / "open-brain" / "url-token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text("xdg-file-url-token\n")

        with (
            patch("open_brain.cli.client.LEGACY_CONFIG_FILE", tmp_path / "legacy.json"),
            patch("open_brain.cli.client.URL_TOKEN_FILE", tmp_path / "legacy-url-token"),
        ):
            result = _load_url_token()

        assert result == "xdg-file-url-token"

    def test_returns_none_when_no_url_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OB_URL_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        nonexistent = tmp_path / "no-such-file"

        with (
            patch("open_brain.cli.client.LEGACY_CONFIG_FILE", tmp_path / "legacy.json"),
            patch("open_brain.cli.client.URL_TOKEN_FILE", nonexistent),
        ):
            result = _load_url_token()

        assert result is None


class TestLoadApiKey:
    def test_env_var_takes_priority(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OB_API_KEY", "env-api-key")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        config_file = tmp_path / "xdg" / "open-brain" / "config.json"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(json.dumps({"api_key": "xdg-api-key"}))

        assert _load_api_key() == "env-api-key"

    def test_reads_xdg_config_api_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OB_API_KEY", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        config_file = tmp_path / "xdg" / "open-brain" / "config.json"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(json.dumps({"api_key": "xdg-api-key"}))

        with patch("open_brain.cli.client.LEGACY_CONFIG_FILE", tmp_path / "legacy.json"):
            assert _load_api_key() == "xdg-api-key"

    def test_returns_none_when_no_api_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OB_API_KEY", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        with patch("open_brain.cli.client.LEGACY_CONFIG_FILE", tmp_path / "legacy.json"):
            assert _load_api_key() is None


class TestGetServerUrl:
    def test_env_var_takes_priority(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OB_URL", "https://env.example.com/mcp")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        config_file = tmp_path / "xdg" / "open-brain" / "config.json"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(json.dumps({"mcp_url": "https://xdg.example.com/mcp"}))

        assert _get_server_url() == "https://env.example.com/mcp"

    def test_reads_xdg_mcp_url(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OB_URL", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        config_file = tmp_path / "xdg" / "open-brain" / "config.json"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(json.dumps({"mcp_url": "https://xdg.example.com/custom"}))

        with patch("open_brain.cli.client.LEGACY_CONFIG_FILE", tmp_path / "legacy.json"):
            assert _get_server_url() == "https://xdg.example.com/custom"

    def test_normalizes_xdg_server_url_to_mcp_endpoint(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OB_URL", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        config_file = tmp_path / "xdg" / "open-brain" / "config.json"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(json.dumps({"server_url": "https://brain.example.com"}))

        with patch("open_brain.cli.client.LEGACY_CONFIG_FILE", tmp_path / "legacy.json"):
            assert _get_server_url() == "https://brain.example.com/mcp"

    def test_xdg_config_overrides_legacy_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OB_URL", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        legacy_config = tmp_path / "legacy" / "config.json"
        legacy_config.parent.mkdir(parents=True)
        legacy_config.write_text(json.dumps({"server_url": "https://legacy.example.com"}))
        xdg_config = tmp_path / "xdg" / "open-brain" / "config.json"
        xdg_config.parent.mkdir(parents=True)
        xdg_config.write_text(json.dumps({"server_url": "https://xdg.example.com"}))

        with patch("open_brain.cli.client.LEGACY_CONFIG_FILE", legacy_config):
            assert _get_server_url() == "https://xdg.example.com/mcp"


class TestNormalizeMcpUrl:
    def test_appends_mcp_to_base_url(self):
        assert _normalize_mcp_url("https://brain.example.com") == "https://brain.example.com/mcp"

    def test_keeps_explicit_path(self):
        assert _normalize_mcp_url("https://brain.example.com/custom") == "https://brain.example.com/custom"


class TestWithUrlToken:
    def test_appends_token_to_url_without_query(self):
        assert (
            _with_url_token("https://brain.example.com/mcp", "abc")
            == "https://brain.example.com/mcp?token=abc"
        )

    def test_preserves_existing_query_params(self):
        assert (
            _with_url_token("https://brain.example.com/mcp?foo=bar", "abc")
            == "https://brain.example.com/mcp?foo=bar&token=abc"
        )

    def test_does_not_override_existing_token(self):
        url = "https://brain.example.com/mcp?token=existing"
        assert _with_url_token(url, "new") == url


class TestCallToolAuth:
    @pytest.mark.asyncio
    async def test_regression_analysis_tool_uses_batch_timeout(self):
        init_resp = MagicMock()
        init_resp.headers = {"mcp-session-id": "session-1"}
        init_resp.raise_for_status.return_value = None
        notif_resp = MagicMock()
        call_resp = MagicMock()
        call_resp.headers = {"content-type": "application/json"}
        call_resp.json.return_value = {
            "result": {"content": [{"type": "text", "text": "{}"}]}
        }
        call_resp.raise_for_status.return_value = None

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[init_resp, notif_resp, call_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "open_brain.cli.client._get_server_url",
                return_value="https://brain.example.com/mcp",
            ),
            patch("open_brain.cli.client._load_token", return_value="oauth-token"),
            patch(
                "open_brain.cli.client.httpx.AsyncClient",
                return_value=mock_client,
            ) as client_factory,
        ):
            await call_tool("analyze_session_learnings", {"limit": 50})

        assert client_factory.call_args.kwargs["timeout"] == 180.0

    @pytest.mark.asyncio
    async def test_uses_api_key_header_when_configured(self, monkeypatch):
        monkeypatch.setenv("OB_URL", "https://brain.example.com/mcp")
        monkeypatch.delenv("OB_TOKEN", raising=False)
        monkeypatch.delenv("OB_URL_TOKEN", raising=False)
        monkeypatch.setenv("OB_API_KEY", "api-key")

        init_resp = MagicMock()
        init_resp.headers = {"mcp-session-id": "session-1"}
        init_resp.raise_for_status.return_value = None
        notif_resp = MagicMock()
        call_resp = MagicMock()
        call_resp.headers = {"content-type": "application/json"}
        call_resp.json.return_value = {"result": {"content": [{"type": "text", "text": "{}"}]}}
        call_resp.raise_for_status.return_value = None

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[init_resp, notif_resp, call_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("open_brain.cli.client.httpx.AsyncClient", return_value=mock_client):
            await call_tool("stats", {})

        first_headers = mock_client.post.call_args_list[0].kwargs["headers"]
        assert first_headers["X-API-Key"] == "api-key"
        assert "Authorization" not in first_headers


# ---------------------------------------------------------------------------
# Command handler integration (mocked)
# ---------------------------------------------------------------------------


class TestCommandHandlers:
    """Test that command handlers call the correct MCP tools with correct args."""

    @pytest.mark.asyncio
    async def test_search_calls_correct_tool(self):
        args = parse(["search", "query text", "--limit", "5", "--project", "proj"])
        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = []
            from open_brain.cli.main import _cmd_search
            await _cmd_search(args)
            mock_call.assert_called_once_with(
                "search",
                {"query": "query text", "limit": 5, "project": "proj"},
            )

    @pytest.mark.asyncio
    async def test_concept_calls_correct_tool(self):
        args = parse(["concept", "semantic query", "--limit", "3"])
        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = []
            from open_brain.cli.main import _cmd_concept
            await _cmd_concept(args)
            mock_call.assert_called_once_with(
                "search_by_concept",
                {"query": "semantic query", "limit": 3},
            )

    @pytest.mark.asyncio
    async def test_save_calls_correct_tool(self):
        args = parse([
            "save", "my text", "--project", "p", "--type", "observation",
            "--source-ref", "agent-session:codex:session-123",
        ])
        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"id": 99}
            from open_brain.cli.main import _cmd_save
            await _cmd_save(args)
            mock_call.assert_called_once_with(
                "save_memory",
                {
                    "text": "my text",
                    "project": "p",
                    "type": "observation",
                    "provenance": {
                        "producer": "ob-cli",
                        "source_ref": "agent-session:codex:session-123",
                    },
                },
            )

    @pytest.mark.asyncio
    async def test_get_converts_ids_to_int(self):
        args = parse(["get", "1", "2", "3"])
        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = []
            from open_brain.cli.main import _cmd_get
            await _cmd_get(args)
            mock_call.assert_called_once_with("get_observations", {"ids": [1, 2, 3]})

    @pytest.mark.asyncio
    async def test_stats_calls_correct_tool(self):
        args = parse(["stats"])
        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"total": 100}
            from open_brain.cli.main import _cmd_stats
            await _cmd_stats(args)
            mock_call.assert_called_once_with("stats", {})

    @pytest.mark.asyncio
    async def test_doctor_calls_correct_tool(self):
        args = parse(["doctor"])
        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"db_status": "ok"}
            from open_brain.cli.main import _cmd_doctor
            await _cmd_doctor(args)
            mock_call.assert_called_once_with("doctor", {})

    @pytest.mark.asyncio
    async def test_export_calls_portable_backup(self, tmp_path: Path):
        args = parse(["export", str(tmp_path / "bundle"), "--source-label", "fixture"])
        with (
            patch("open_brain.cli.main.PostgresDataLayer") as mock_data_layer,
            patch("open_brain.cli.main.export_bundle", new_callable=AsyncMock) as mock_export,
        ):
            mock_export.return_value = {"bundle_format_version": "1.0.0"}
            from open_brain.cli.main import _cmd_export
            result = await _cmd_export(args)

        mock_export.assert_called_once_with(
            Path(args.bundle_path),
            mock_data_layer.return_value,
            source_label="fixture",
        )
        assert result == {"bundle_format_version": "1.0.0"}

    @pytest.mark.asyncio
    async def test_restore_calls_portable_backup(self, tmp_path: Path):
        args = parse(["restore", str(tmp_path / "bundle"), "--skip-embeddings"])
        with (
            patch("open_brain.cli.main.PostgresDataLayer") as mock_data_layer,
            patch("open_brain.cli.main.restore_bundle", new_callable=AsyncMock) as mock_restore,
        ):
            mock_restore.return_value = {"restored": {"memories": 2}}
            from open_brain.cli.main import _cmd_restore
            result = await _cmd_restore(args)

        mock_restore.assert_called_once_with(
            Path(args.bundle_path),
            mock_data_layer.return_value,
            regenerate_embeddings=False,
        )
        assert result == {"restored": {"memories": 2}}

    @pytest.mark.asyncio
    async def test_verify_calls_portable_backup(self, tmp_path: Path):
        args = parse(["verify", str(tmp_path / "bundle")])
        with (
            patch("open_brain.cli.main.PostgresDataLayer") as mock_data_layer,
            patch("open_brain.cli.main.verify_round_trip", new_callable=AsyncMock) as mock_verify,
        ):
            mock_verify.return_value = {"ok": True}
            from open_brain.cli.main import _cmd_verify
            result = await _cmd_verify(args)

        mock_verify.assert_called_once_with(
            Path(args.bundle_path),
            mock_data_layer.return_value,
        )
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_cmd_export_suppresses_migrations(self, tmp_path: Path):
        args = parse(["export", str(tmp_path / "bundle"), "--source-label", "fixture"])
        call_order: list[str] = []
        fake_data_layer = object()

        def suppress_side_effect() -> None:
            call_order.append("suppress")

        def data_layer_side_effect() -> object:
            call_order.append("data_layer")
            return fake_data_layer

        with (
            patch(
                "open_brain.cli.main.suppress_migrations",
                create=True,
                side_effect=suppress_side_effect,
            ) as suppress,
            patch(
                "open_brain.cli.main.PostgresDataLayer",
                side_effect=data_layer_side_effect,
            ) as mock_data_layer,
            patch("open_brain.cli.main.export_bundle", new_callable=AsyncMock) as mock_export,
        ):
            mock_export.return_value = {"bundle_format_version": "1.0.0"}
            from open_brain.cli.main import _cmd_export
            result = await _cmd_export(args)

        suppress.assert_called_once_with()
        mock_data_layer.assert_called_once_with()
        mock_export.assert_called_once_with(
            Path(args.bundle_path),
            fake_data_layer,
            source_label="fixture",
        )
        assert call_order == ["suppress", "data_layer"]
        assert result == {"bundle_format_version": "1.0.0"}

    @pytest.mark.asyncio
    async def test_cmd_verify_suppresses_migrations(self, tmp_path: Path):
        args = parse(["verify", str(tmp_path / "bundle")])
        call_order: list[str] = []
        fake_data_layer = object()

        def suppress_side_effect() -> None:
            call_order.append("suppress")

        def data_layer_side_effect() -> object:
            call_order.append("data_layer")
            return fake_data_layer

        with (
            patch(
                "open_brain.cli.main.suppress_migrations",
                create=True,
                side_effect=suppress_side_effect,
            ) as suppress,
            patch(
                "open_brain.cli.main.PostgresDataLayer",
                side_effect=data_layer_side_effect,
            ) as mock_data_layer,
            patch("open_brain.cli.main.verify_round_trip", new_callable=AsyncMock) as mock_verify,
        ):
            mock_verify.return_value = {"ok": True}
            from open_brain.cli.main import _cmd_verify
            result = await _cmd_verify(args)

        suppress.assert_called_once_with()
        mock_data_layer.assert_called_once_with()
        mock_verify.assert_called_once_with(
            Path(args.bundle_path),
            fake_data_layer,
        )
        assert call_order == ["suppress", "data_layer"]
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_cmd_restore_keeps_migrations(self, tmp_path: Path):
        args = parse(["restore", str(tmp_path / "bundle"), "--skip-embeddings"])
        with (
            patch("open_brain.cli.main.suppress_migrations") as suppress,
            patch("open_brain.cli.main.PostgresDataLayer") as mock_data_layer,
            patch("open_brain.cli.main.restore_bundle", new_callable=AsyncMock) as mock_restore,
        ):
            mock_restore.return_value = {"restored": {"memories": 2}}
            from open_brain.cli.main import _cmd_restore
            result = await _cmd_restore(args)

        suppress.assert_not_called()
        mock_data_layer.assert_called_once_with()
        mock_restore.assert_called_once_with(
            Path(args.bundle_path),
            mock_data_layer.return_value,
            regenerate_embeddings=False,
        )
        assert result == {"restored": {"memories": 2}}

    @pytest.mark.asyncio
    async def test_cmd_people_enrichment_keeps_migrations(self, capsys):
        args = parse(
            [
                "people",
                "enrich",
                "--auto-apply",
                "--searxng-url",
                "http://searxng.local",
            ]
        )

        with (
            patch("open_brain.cli.main.suppress_migrations") as cli_suppress,
            patch("open_brain.data_layer.postgres.suppress_migrations") as postgres_suppress,
            patch("open_brain.cli.direct.load_database_url", return_value="postgresql://db"),
            patch("open_brain.cli.direct.prepare_direct_env") as prepare_direct_env,
            patch("open_brain.data_layer.postgres.PostgresDataLayer") as mock_data_layer,
            patch("open_brain.data_layer.postgres.close_pool", new_callable=AsyncMock) as close_pool,
            patch(
                "open_brain.people.enrichment.list_enrichment_candidates",
                new_callable=AsyncMock,
                return_value=[],
            ) as list_candidates,
        ):
            from open_brain.cli.main import _cmd_people_enrichment
            result = await _cmd_people_enrichment(args)

        captured = capsys.readouterr()
        cli_suppress.assert_not_called()
        postgres_suppress.assert_not_called()
        prepare_direct_env.assert_called_once_with("postgresql://db")
        mock_data_layer.assert_called_once_with()
        list_candidates.assert_awaited_once_with(mock_data_layer.return_value)
        close_pool.assert_awaited_once_with()
        assert "No enrichment candidates found." in captured.out
        assert result is None

    def test_server_calls_runtime(self):
        args = parse(["server", "--host", "127.0.0.1", "--port", "9000"])
        with patch("open_brain.cli.main.run_server") as mock_run_server:
            from open_brain.cli.main import _cmd_server
            _cmd_server(args)
            mock_run_server.assert_called_once_with(host="127.0.0.1", port=9000)

    @pytest.mark.asyncio
    async def test_update_calls_correct_tool(self):
        args = parse(["update", "7", "--text", "new content", "--type", "decision"])
        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"id": 7}
            from open_brain.cli.main import _cmd_update
            await _cmd_update(args)
            mock_call.assert_called_once_with(
                "update_memory",
                {"id": 7, "text": "new content", "type": "decision"},
            )

    # Guards the bug where ob update exposed fewer fields than update_memory.
    @pytest.mark.asyncio
    async def test_regression_ob_update_forwards_full_mcp_field_set(self):
        args = parse(
            [
                "update",
                "7",
                "--subtitle",
                "New subtitle",
                "--narrative",
                "New narrative",
                "--metadata",
                '{"status":"discarded","discard_reason":"merged into #8"}',
            ]
        )
        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"id": 7}
            from open_brain.cli.main import _cmd_update

            await _cmd_update(args)

        mock_call.assert_called_once_with(
            "update_memory",
            {
                "id": 7,
                "subtitle": "New subtitle",
                "narrative": "New narrative",
                "metadata": {
                    "status": "discarded",
                    "discard_reason": "merged into #8",
                },
            },
        )

    # Guards against malformed lifecycle metadata reaching the MCP transport.
    def test_regression_ob_update_rejects_invalid_metadata(self, capsys):
        with pytest.raises(SystemExit):
            parse(["update", "7", "--metadata", "{not-json}"])
        assert "metadata must be valid JSON" in capsys.readouterr().err

    # Guards against valid JSON values that are not metadata objects.
    def test_regression_ob_update_rejects_non_object_metadata(self, capsys):
        with pytest.raises(SystemExit):
            parse(["update", "7", "--metadata", '["discarded"]'])
        assert "metadata must be a JSON object" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_timeline_with_anchor(self):
        args = parse(["timeline", "--anchor", "5", "--depth-before", "3"])
        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {}
            from open_brain.cli.main import _cmd_timeline
            await _cmd_timeline(args)
            mock_call.assert_called_once_with(
                "timeline",
                {"anchor": 5, "depth_before": 3},
            )

    @pytest.mark.asyncio
    async def test_context_no_args(self):
        args = parse(["context"])
        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = []
            from open_brain.cli.main import _cmd_context
            await _cmd_context(args)
            mock_call.assert_called_once_with("get_context", {})

    @pytest.mark.asyncio
    async def test_people_list_calls_correct_tool(self):
        args = parse(["people", "list", "--include-merged", "--collisions"])
        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"mode": "collisions"}
            from open_brain.cli.main import _cmd_people
            await _cmd_people(args)
            mock_call.assert_called_once_with(
                "people_list",
                {"include_merged": True, "collisions_only": True},
            )

    @pytest.mark.asyncio
    async def test_people_merge_calls_correct_tool(self):
        args = parse(
            [
                "people",
                "merge",
                "--source",
                "10",
                "--target",
                "20",
                "--dry-run",
                "--absorb-text",
            ]
        )
        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"status": "dry_run"}
            from open_brain.cli.main import _cmd_people
            await _cmd_people(args)
            mock_call.assert_called_once_with(
                "people_merge",
                {
                    "source_id": 10,
                    "target_id": 20,
                    "dry_run": True,
                    "absorb_text": True,
                },
            )


# ---------------------------------------------------------------------------
# Integration test (requires live server)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_integration_search():
    """Integration test: calls real open-brain server with search tool.

    Requires OB_TOKEN env var or ~/.open-brain/token file to be set.
    The server must be reachable at OB_URL (default: https://open-brain.sussdorff.org/mcp/mcp).
    """
    result = await call_tool("stats", {})
    assert result is not None
    # Stats should return some count data
    assert isinstance(result, (dict, list, str))
