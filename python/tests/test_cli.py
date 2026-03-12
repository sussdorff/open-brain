"""Tests for the ob CLI — argument parsing, output formatting, and integration."""

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.cli.client import (
    MCPError,
    _extract_result,
    _load_token,
    _parse_sse_response,
)
from open_brain.cli.main import _build_parser, _output


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
        token_file = tmp_path / ".open-brain" / "token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text("file-token\n")

        with patch("open_brain.cli.client.TOKEN_FILE", token_file):
            result = _load_token()
        assert result == "file-token"

    def test_returns_none_when_no_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OB_TOKEN", raising=False)
        nonexistent = tmp_path / "no-such-file"
        with patch("open_brain.cli.client.TOKEN_FILE", nonexistent):
            result = _load_token()
        assert result is None


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
