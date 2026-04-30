"""Tests for ob ingest macwhisper CLI subcommands."""

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.cli.main import (
    _build_parser,
    _cmd_ingest_macwhisper_ingest,
    _cmd_ingest_macwhisper_list,
)
from open_brain.ingest.adapters.macwhisper import TranscriptRef


def parse(args: list[str]) -> Any:
    """Parse CLI args using the ob parser."""
    return _build_parser().parse_args(args)


class TestIngestMacWhisperArgParsing:
    def test_list_defaults(self):
        args = parse(["ingest", "macwhisper", "list"])

        assert args.command == "ingest"
        assert args.ingest_command == "macwhisper"
        assert args.macwhisper_command == "list"
        assert args.limit == 10
        assert args.history_path is None

    def test_list_with_limit_and_history_path(self):
        args = parse([
            "ingest",
            "macwhisper",
            "list",
            "--limit=5",
            "--history-path=/tmp/macwhisper",
        ])

        assert args.limit == 5
        assert args.history_path == "/tmp/macwhisper"

    def test_list_json_flag(self):
        args = parse(["ingest", "macwhisper", "list", "--json"])

        assert args.json_output is True

    def test_list_status_flags(self):
        args = parse([
            "ingest",
            "macwhisper",
            "list",
            "--status",
            "--not-ingested",
            "--scan-limit=25",
        ])

        assert args.status is True
        assert args.not_ingested is True
        assert args.scan_limit == 25

    def test_ingest_entry_defaults(self):
        args = parse(["ingest", "macwhisper", "entry", "abc123"])

        assert args.command == "ingest"
        assert args.ingest_command == "macwhisper"
        assert args.macwhisper_command == "entry"
        assert args.entry_id == "abc123"
        assert args.source_ref is None
        assert args.medium_hint is None
        assert args.direct is False

    def test_ingest_entry_json_flag(self):
        args = parse(["ingest", "macwhisper", "entry", "abc123", "--json"])

        assert args.json_output is True

    def test_ingest_entry_legacy_alias(self):
        args = parse(["ingest", "macwhisper", "ingest", "abc123"])

        assert args.macwhisper_command == "ingest"
        assert args.entry_id == "abc123"

    def test_ingest_entry_overrides(self):
        args = parse([
            "ingest",
            "macwhisper",
            "entry",
            "abc123",
            "--history-path=/tmp/macwhisper",
            "--source-ref=custom-ref",
            "--medium-hint=dictation",
            "--direct",
        ])

        assert args.history_path == "/tmp/macwhisper"
        assert args.source_ref == "custom-ref"
        assert args.medium_hint == "dictation"
        assert args.direct is True


class TestIngestMacWhisperHandlers:
    @pytest.mark.asyncio
    async def test_list_returns_recent_entries(self, tmp_path: Path):
        connector = MagicMock()
        connector.discover_history_path.return_value = tmp_path
        connector.list_recent = AsyncMock(return_value=[
            TranscriptRef(
                entry_id="abc123",
                created_at="2026-04-30T10:00:00",
                text_preview="Meeting transcript",
                title="Planning Sync",
                source_type="recorded_meeting",
                source_app="Teams",
                duration_seconds=1800.0,
                participants=["Alice", "Bob"],
            )
        ])
        args = parse([
            "ingest",
            "macwhisper",
            "list",
            "--limit=1",
            f"--history-path={tmp_path}",
        ])

        with patch(
            "open_brain.cli.main._new_macwhisper_connector",
            return_value=connector,
        ):
            result = await _cmd_ingest_macwhisper_list(args)

        connector.list_recent.assert_awaited_once_with(n=1)
        assert result == {
            "history_path": str(tmp_path),
            "count": 1,
            "items": [
                {
                    "entry_id": "abc123",
                    "created_at": "2026-04-30T10:00:00",
                    "text_preview": "Meeting transcript",
                    "title": "Planning Sync",
                    "source_type": "recorded_meeting",
                    "source_app": "Teams",
                    "duration_seconds": 1800.0,
                    "participants": ["Alice", "Bob"],
                }
            ],
        }

    @pytest.mark.asyncio
    async def test_list_status_enriches_entries(self, tmp_path: Path):
        connector = MagicMock()
        connector.discover_history_path.return_value = tmp_path
        connector.list_recent = AsyncMock(return_value=[
            TranscriptRef(
                entry_id="session:abc123",
                created_at="2026-04-30T10:00:00",
                text_preview="Meeting transcript",
            )
        ])
        args = parse(["ingest", "macwhisper", "list", "--status"])

        with (
            patch(
                "open_brain.cli.main._new_macwhisper_connector",
                return_value=connector,
            ),
            patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call,
        ):
            mock_call.return_value = {
                "items": [
                    {
                        "source_ref": "macwhisper:session:abc123",
                        "ingested": True,
                        "memory_id": 42,
                        "run_id": "run-123",
                        "ingested_at": "2026-04-30T12:00:00",
                        "title": "Meeting: macwhisper:session:abc123",
                    }
                ]
            }
            result = await _cmd_ingest_macwhisper_list(args)

        mock_call.assert_awaited_once_with(
            "ingest_status",
            {"source_refs": ["macwhisper:session:abc123"]},
        )
        assert result["scanned_count"] == 1
        assert result["items"][0]["ingested"] is True
        assert result["items"][0]["memory_id"] == 42

    @pytest.mark.asyncio
    async def test_list_not_ingested_filters_and_scans_more_than_limit(self, tmp_path: Path):
        connector = MagicMock()
        connector.discover_history_path.return_value = tmp_path
        connector.list_recent = AsyncMock(return_value=[
            TranscriptRef(
                entry_id="session:old",
                created_at="2026-04-30T10:00:00",
                text_preview="Old meeting",
            ),
            TranscriptRef(
                entry_id="session:new",
                created_at="2026-04-29T10:00:00",
                text_preview="New meeting",
            ),
        ])
        args = parse([
            "ingest",
            "macwhisper",
            "list",
            "--limit=1",
            "--not-ingested",
            "--scan-limit=2",
        ])

        with (
            patch(
                "open_brain.cli.main._new_macwhisper_connector",
                return_value=connector,
            ),
            patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call,
        ):
            mock_call.return_value = {
                "items": [
                    {
                        "source_ref": "macwhisper:session:old",
                        "ingested": True,
                        "memory_id": 42,
                    },
                    {
                        "source_ref": "macwhisper:session:new",
                        "ingested": False,
                        "memory_id": None,
                    },
                ]
            }
            result = await _cmd_ingest_macwhisper_list(args)

        connector.list_recent.assert_awaited_once_with(n=2)
        assert result["count"] == 1
        assert result["scanned_count"] == 2
        assert result["items"][0]["entry_id"] == "session:new"
        assert result["items"][0]["ingested"] is False

    @pytest.mark.asyncio
    async def test_history_path_env_override_is_restored(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        monkeypatch.setenv("MACWHISPER_HISTORY_PATH", "before")
        connector = MagicMock()
        connector.discover_history_path.return_value = tmp_path
        connector.list_recent = AsyncMock(return_value=[])
        args = parse([
            "ingest",
            "macwhisper",
            "list",
            f"--history-path={tmp_path}",
        ])

        with patch(
            "open_brain.cli.main._new_macwhisper_connector",
            return_value=connector,
        ):
            await _cmd_ingest_macwhisper_list(args)

        assert os.environ["MACWHISPER_HISTORY_PATH"] == "before"

    @pytest.mark.asyncio
    async def test_ingest_reads_local_entry_and_calls_server_tool(self):
        connector = MagicMock()
        connector.read_entry.return_value = (
            "Transcript content.",
            {"medium": "meeting"},
        )
        args = parse(["ingest", "macwhisper", "entry", "abc123"])

        with (
            patch(
                "open_brain.cli.main._new_macwhisper_connector",
                return_value=connector,
            ),
            patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call,
        ):
            mock_call.return_value = {"status": "ok"}
            result = await _cmd_ingest_macwhisper_ingest(args)

        connector.read_entry.assert_called_once_with("abc123")
        mock_call.assert_awaited_once_with(
            "ingest_transcript",
            {
                "text": "Transcript content.",
                "source_ref": "macwhisper:abc123",
                "medium_hint": "meeting",
            },
        )
        assert result == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_ingest_uses_overrides_and_default_medium_hint(self):
        connector = MagicMock()
        connector.read_entry.return_value = ("Transcript content.", {})
        args = parse([
            "ingest",
            "macwhisper",
            "entry",
            "abc123",
            "--source-ref=custom-ref",
            "--medium-hint=dictation",
        ])

        with (
            patch(
                "open_brain.cli.main._new_macwhisper_connector",
                return_value=connector,
            ),
            patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call,
        ):
            mock_call.return_value = {"status": "ok"}
            await _cmd_ingest_macwhisper_ingest(args)

        mock_call.assert_awaited_once_with(
            "ingest_transcript",
            {
                "text": "Transcript content.",
                "source_ref": "custom-ref",
                "medium_hint": "dictation",
            },
        )

    @pytest.mark.asyncio
    async def test_ingest_defaults_medium_hint_to_macwhisper(self):
        connector = MagicMock()
        connector.read_entry.return_value = ("Transcript content.", {})
        args = parse(["ingest", "macwhisper", "entry", "abc123"])

        with (
            patch(
                "open_brain.cli.main._new_macwhisper_connector",
                return_value=connector,
            ),
            patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call,
        ):
            mock_call.return_value = {"status": "ok"}
            await _cmd_ingest_macwhisper_ingest(args)

        call_kwargs = mock_call.call_args[0][1]
        assert call_kwargs["medium_hint"] == "macwhisper"
