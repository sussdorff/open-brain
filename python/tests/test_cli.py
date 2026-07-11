"""Tests for the ob CLI — argument parsing, output formatting, and integration."""

import json
import os
from pathlib import Path
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
    def test_text_only(self):
        args = parse(["save", "some text to save"])
        assert args.command == "save"
        assert args.text == "some text to save"
        assert args.project is None
        assert args.type is None
        assert args.title is None

    def test_with_optional_fields(self):
        args = parse(["save", "text", "--project", "proj", "--type", "observation", "--title", "My Title"])
        assert args.project == "proj"
        assert args.type == "observation"
        assert args.title == "My Title"


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
        args = parse(["update", "7", "--text", "t", "--type", "decision", "--project", "p", "--title", "T"])
        assert args.type == "decision"
        assert args.project == "p"
        assert args.title == "T"


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
        args = parse(["save", "my text", "--project", "p", "--type", "observation"])
        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"id": 99}
            from open_brain.cli.main import _cmd_save
            await _cmd_save(args)
            mock_call.assert_called_once_with(
                "save_memory",
                {"text": "my text", "project": "p", "type": "observation"},
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
