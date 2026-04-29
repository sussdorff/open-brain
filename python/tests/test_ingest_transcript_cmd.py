"""Tests for ob ingest transcript CLI subcommand."""

import sys
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from open_brain.cli.main import _build_parser, _cmd_ingest_transcript


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse(args: list[str]) -> Any:
    """Parse CLI args using the ob parser."""
    return _build_parser().parse_args(args)


# ---------------------------------------------------------------------------
# Argument parsing tests
# ---------------------------------------------------------------------------


class TestIngestTranscriptArgParsing:
    def test_with_file(self, tmp_path):
        transcript_file = tmp_path / "transcript.txt"
        transcript_file.write_text("hello world")
        args = parse(["ingest", "transcript", "--source-ref=foo", f"--file={transcript_file}"])
        assert args.command == "ingest"
        assert args.ingest_command == "transcript"
        assert args.source_ref == "foo"
        assert args.file == str(transcript_file)
        assert args.stdin is False
        assert args.medium_hint is None

    def test_with_stdin_flag(self):
        args = parse(["ingest", "transcript", "--source-ref=foo", "--stdin"])
        assert args.command == "ingest"
        assert args.ingest_command == "transcript"
        assert args.source_ref == "foo"
        assert args.file is None
        assert args.stdin is True

    def test_default_stdin_when_no_file(self):
        args = parse(["ingest", "transcript", "--source-ref=foo"])
        assert args.file is None
        assert args.stdin is False

    def test_with_medium_hint(self):
        args = parse(["ingest", "transcript", "--source-ref=foo", "--stdin", "--medium-hint=macwhisper"])
        assert args.medium_hint == "macwhisper"

    def test_file_and_stdin_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            parse(["ingest", "transcript", "--source-ref=foo", "--file=/some/path", "--stdin"])

    def test_source_ref_is_required(self):
        with pytest.raises(SystemExit):
            parse(["ingest", "transcript", "--stdin"])

    def test_pretty_flag_before_ingest(self):
        args = parse(["--pretty", "ingest", "transcript", "--source-ref=bar", "--stdin"])
        assert args.pretty is True
        assert args.source_ref == "bar"


# ---------------------------------------------------------------------------
# Command handler tests
# ---------------------------------------------------------------------------


class TestIngestTranscriptHandler:
    @pytest.mark.asyncio
    async def test_reads_from_file(self, tmp_path):
        transcript_file = tmp_path / "transcript.txt"
        transcript_file.write_text("This is the transcript content.")
        args = parse(["ingest", "transcript", "--source-ref=myref", f"--file={transcript_file}"])

        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"status": "ok"}
            await _cmd_ingest_transcript(args)
            mock_call.assert_called_once_with(
                "ingest_transcript",
                {"text": "This is the transcript content.", "source_ref": "myref"},
            )

    @pytest.mark.asyncio
    async def test_reads_from_stdin(self, monkeypatch):
        args = parse(["ingest", "transcript", "--source-ref=stdinref", "--stdin"])
        monkeypatch.setattr("sys.stdin", StringIO("Transcript from stdin."))

        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"status": "ok"}
            await _cmd_ingest_transcript(args)
            mock_call.assert_called_once_with(
                "ingest_transcript",
                {"text": "Transcript from stdin.", "source_ref": "stdinref"},
            )

    @pytest.mark.asyncio
    async def test_includes_medium_hint_when_provided(self, tmp_path):
        transcript_file = tmp_path / "t.txt"
        transcript_file.write_text("content")
        args = parse([
            "ingest", "transcript",
            "--source-ref=ref1",
            f"--file={transcript_file}",
            "--medium-hint=dictation",
        ])

        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"status": "ok"}
            await _cmd_ingest_transcript(args)
            mock_call.assert_called_once_with(
                "ingest_transcript",
                {"text": "content", "source_ref": "ref1", "medium_hint": "dictation"},
            )

    @pytest.mark.asyncio
    async def test_empty_file_raises_error(self, tmp_path):
        empty_file = tmp_path / "empty.txt"
        empty_file.write_text("   \n  ")
        args = parse(["ingest", "transcript", "--source-ref=ref", f"--file={empty_file}"])

        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock):
            with pytest.raises(SystemExit):
                await _cmd_ingest_transcript(args)

    @pytest.mark.asyncio
    async def test_empty_stdin_raises_error(self, monkeypatch):
        args = parse(["ingest", "transcript", "--source-ref=ref", "--stdin"])
        monkeypatch.setattr("sys.stdin", StringIO("   "))

        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock):
            with pytest.raises(SystemExit):
                await _cmd_ingest_transcript(args)

    @pytest.mark.asyncio
    async def test_omits_medium_hint_when_not_provided(self, tmp_path):
        transcript_file = tmp_path / "t.txt"
        transcript_file.write_text("some text")
        args = parse(["ingest", "transcript", "--source-ref=r", f"--file={transcript_file}"])

        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"status": "ok"}
            await _cmd_ingest_transcript(args)
            call_kwargs = mock_call.call_args[0][1]
            assert "medium_hint" not in call_kwargs
