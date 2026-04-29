"""Tests for direct-mode CLI bypass (open-brain-c5e).

Verifies that `ob ingest transcript --direct` routes to the in-process
PostgresDataLayer instead of the MCP transport, and that the returned dict
has the same shape as IngestResult produced by the MCP path.
"""

import dataclasses
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.cli.main import _build_parser, _cmd_ingest_transcript
from open_brain.ingest.models import IngestResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse(args: list[str]) -> Any:
    """Parse CLI args using the ob parser."""
    return _build_parser().parse_args(args)


def _make_ingest_result() -> IngestResult:
    """Build a minimal IngestResult for testing."""
    return IngestResult(
        meeting_memory_id=42,
        person_memory_ids=[1, 2],
        mention_memory_ids=[3],
        interaction_memory_ids=[4, 5],
        relationship_ids=[10, 11],
        follow_up_candidates=[{"task": "follow up with Alice"}],
        run_id="abc-123",
        skipped_count=0,
    )


# ---------------------------------------------------------------------------
# _load_database_url tests
# ---------------------------------------------------------------------------


class TestLoadDatabaseUrl:
    def test_reads_from_env(self, monkeypatch):
        """DATABASE_URL in environment is returned directly."""
        monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost/db")
        from open_brain.cli.direct import load_database_url

        result = load_database_url()
        assert result == "postgres://user:pass@localhost/db"

    def test_reads_from_env_file(self, tmp_path, monkeypatch):
        """DATABASE_URL in .env file is found when env var is absent."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text('DATABASE_URL=postgres://from-file/testdb\n')

        # Change cwd so .env lookup finds the file
        monkeypatch.chdir(tmp_path)
        from open_brain.cli.direct import load_database_url

        result = load_database_url()
        assert result == "postgres://from-file/testdb"

    def test_returns_empty_string_when_neither_set(self, tmp_path, monkeypatch):
        """Returns empty string when DATABASE_URL is absent in env and .env."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.chdir(tmp_path)  # no .env file here
        from open_brain.cli.direct import load_database_url

        result = load_database_url()
        assert result == ""

    def test_env_takes_priority_over_file(self, tmp_path, monkeypatch):
        """Env var takes priority over .env file value."""
        monkeypatch.setenv("DATABASE_URL", "postgres://from-env/db")
        env_file = tmp_path / ".env"
        env_file.write_text("DATABASE_URL=postgres://from-file/db\n")
        monkeypatch.chdir(tmp_path)
        from open_brain.cli.direct import load_database_url

        result = load_database_url()
        assert result == "postgres://from-env/db"

    def test_ignores_commented_lines_in_env_file(self, tmp_path, monkeypatch):
        """Commented-out DATABASE_URL lines in .env are ignored."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("# DATABASE_URL=postgres://commented/db\n")
        monkeypatch.chdir(tmp_path)
        from open_brain.cli.direct import load_database_url

        result = load_database_url()
        assert result == ""

    def test_load_database_url_from_dotenv_file_handles_quoted_values(
        self, tmp_path, monkeypatch
    ):
        """Double-quoted and single-quoted DATABASE_URL values in .env are stripped correctly."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        from open_brain.cli.direct import load_database_url

        # Double-quoted value
        env_file = tmp_path / ".env"
        env_file.write_text('DATABASE_URL="postgres://quoted"\n')
        monkeypatch.chdir(tmp_path)
        result = load_database_url()
        assert result == "postgres://quoted"

        # Single-quoted value
        env_file.write_text("DATABASE_URL='postgres://single-quoted'\n")
        result = load_database_url()
        assert result == "postgres://single-quoted"


# ---------------------------------------------------------------------------
# run_ingest_transcript_direct tests
# ---------------------------------------------------------------------------


class TestRunIngestTranscriptDirect:
    @pytest.mark.asyncio
    async def test_returns_dict_with_ingest_result_shape(self, monkeypatch):
        """Direct mode returns a dict with the same keys as IngestResult."""
        monkeypatch.setenv("DATABASE_URL", "postgres://localhost/test")
        monkeypatch.setenv("VOYAGE_API_KEY", "vk-test")

        ingest_result = _make_ingest_result()
        expected_dict = dataclasses.asdict(ingest_result)

        mock_ingestor = MagicMock()
        mock_ingestor.ingest = AsyncMock(return_value=ingest_result)

        with patch("open_brain.cli.direct.TranscriptIngestor", return_value=mock_ingestor):
            with patch("open_brain.cli.direct.PostgresDataLayer"):
                with patch("open_brain.cli.direct.close_pool", new_callable=AsyncMock):
                    from open_brain.cli.direct import run_ingest_transcript_direct

                    result = await run_ingest_transcript_direct(
                        text="Hello, this is a test transcript.",
                        source_ref="test-ref",
                    )

        assert result == expected_dict
        assert set(result.keys()) == {
            "meeting_memory_id",
            "person_memory_ids",
            "mention_memory_ids",
            "interaction_memory_ids",
            "relationship_ids",
            "follow_up_candidates",
            "run_id",
            "skipped_count",
        }

    @pytest.mark.asyncio
    async def test_passes_medium_hint_to_ingestor(self, monkeypatch):
        """medium_hint is forwarded to TranscriptIngestor.ingest()."""
        monkeypatch.setenv("DATABASE_URL", "postgres://localhost/test")
        monkeypatch.setenv("VOYAGE_API_KEY", "vk-test")

        ingest_result = _make_ingest_result()
        mock_ingestor = MagicMock()
        mock_ingestor.ingest = AsyncMock(return_value=ingest_result)

        with patch("open_brain.cli.direct.TranscriptIngestor", return_value=mock_ingestor):
            with patch("open_brain.cli.direct.PostgresDataLayer"):
                with patch("open_brain.cli.direct.close_pool", new_callable=AsyncMock):
                    from open_brain.cli.direct import run_ingest_transcript_direct

                    await run_ingest_transcript_direct(
                        text="Transcript text.",
                        source_ref="ref-x",
                        medium_hint="macwhisper",
                    )

        mock_ingestor.ingest.assert_called_once_with(
            text="Transcript text.",
            source_ref="ref-x",
            medium_hint="macwhisper",
        )

    @pytest.mark.asyncio
    async def test_close_pool_called_even_on_error(self, monkeypatch):
        """close_pool() is always called (finally block), even when ingest raises."""
        monkeypatch.setenv("DATABASE_URL", "postgres://localhost/test")
        monkeypatch.setenv("VOYAGE_API_KEY", "vk-test")

        mock_ingestor = MagicMock()
        mock_ingestor.ingest = AsyncMock(side_effect=RuntimeError("DB error"))

        close_mock = AsyncMock()
        with patch("open_brain.cli.direct.TranscriptIngestor", return_value=mock_ingestor):
            with patch("open_brain.cli.direct.PostgresDataLayer"):
                with patch("open_brain.cli.direct.close_pool", close_mock):
                    from open_brain.cli.direct import run_ingest_transcript_direct

                    with pytest.raises(RuntimeError, match="DB error"):
                        await run_ingest_transcript_direct(
                            text="Some text.",
                            source_ref="ref-err",
                        )

        close_mock.assert_called_once()


# ---------------------------------------------------------------------------
# _cmd_ingest_transcript routing tests
# ---------------------------------------------------------------------------


class TestCmdIngestTranscriptDirectRouting:
    @pytest.mark.asyncio
    async def test_direct_flag_routes_to_direct_mode(self, tmp_path, monkeypatch):
        """--direct flag bypasses call_tool and uses run_ingest_transcript_direct."""
        monkeypatch.setenv("DATABASE_URL", "postgres://localhost/test")
        monkeypatch.delenv("OB_DIRECT", raising=False)

        transcript_file = tmp_path / "t.txt"
        transcript_file.write_text("Transcript content here.")
        args = parse([
            "ingest", "transcript",
            "--source-ref=myref",
            f"--file={transcript_file}",
            "--direct",
        ])

        expected_result = {"meeting_memory_id": 99, "run_id": "r1"}

        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call_tool:
            with patch("open_brain.cli.direct.load_database_url", return_value="postgres://localhost/test"):
                with patch("open_brain.cli.direct.prepare_direct_env"):
                    with patch(
                        "open_brain.cli.direct.run_ingest_transcript_direct",
                        new_callable=AsyncMock,
                        return_value=expected_result,
                    ) as mock_direct:
                        result = await _cmd_ingest_transcript(args)

        mock_call_tool.assert_not_called()
        mock_direct.assert_called_once_with(
            text="Transcript content here.",
            source_ref="myref",
            medium_hint=None,
        )
        assert result == expected_result

    @pytest.mark.asyncio
    async def test_ob_direct_env_var_routes_to_direct_mode(self, tmp_path, monkeypatch):
        """OB_DIRECT=1 env var bypasses call_tool and uses run_ingest_transcript_direct."""
        monkeypatch.setenv("OB_DIRECT", "1")
        monkeypatch.setenv("DATABASE_URL", "postgres://localhost/test")

        transcript_file = tmp_path / "t.txt"
        transcript_file.write_text("Transcript via env var.")
        args = parse([
            "ingest", "transcript",
            "--source-ref=envref",
            f"--file={transcript_file}",
        ])

        expected_result = {"meeting_memory_id": 77, "run_id": "r2"}

        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call_tool:
            with patch("open_brain.cli.direct.load_database_url", return_value="postgres://localhost/test"):
                with patch("open_brain.cli.direct.prepare_direct_env"):
                    with patch(
                        "open_brain.cli.direct.run_ingest_transcript_direct",
                        new_callable=AsyncMock,
                        return_value=expected_result,
                    ) as mock_direct:
                        result = await _cmd_ingest_transcript(args)

        mock_call_tool.assert_not_called()
        mock_direct.assert_called_once()
        assert result == expected_result

    @pytest.mark.asyncio
    async def test_without_direct_flag_or_env_var_uses_mcp_path(
        self, tmp_path, monkeypatch
    ):
        """AK2: When NEITHER --direct flag NOR OB_DIRECT=1 env var is set,
        the normal MCP call_tool path is used (behaviour unchanged)."""
        # Explicitly unset OB_DIRECT to document the "neither flag nor env var"
        # case required by AK2 of bead open-brain-c5e.
        monkeypatch.delenv("OB_DIRECT", raising=False)

        transcript_file = tmp_path / "t.txt"
        transcript_file.write_text("Normal MCP transcript.")
        args = parse([
            "ingest", "transcript",
            "--source-ref=normalref",
            f"--file={transcript_file}",
        ])
        # Sanity assertion: --direct flag is not set on parsed args.
        assert args.direct is False

        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call_tool:
            mock_call_tool.return_value = {"status": "ok"}
            result = await _cmd_ingest_transcript(args)

        mock_call_tool.assert_called_once_with(
            "ingest_transcript",
            {"text": "Normal MCP transcript.", "source_ref": "normalref"},
        )
        assert result == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_direct_flag_missing_database_url_exits_with_error(
        self, tmp_path, monkeypatch, capsys
    ):
        """--direct without DATABASE_URL prints an error and exits."""
        monkeypatch.delenv("OB_DIRECT", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.chdir(tmp_path)  # no .env file here

        transcript_file = tmp_path / "t.txt"
        transcript_file.write_text("Some transcript.")
        args = parse([
            "ingest", "transcript",
            "--source-ref=ref",
            f"--file={transcript_file}",
            "--direct",
        ])

        with pytest.raises(SystemExit):
            await _cmd_ingest_transcript(args)

        captured = capsys.readouterr()
        assert "DATABASE_URL" in captured.err


# ---------------------------------------------------------------------------
# Argument parsing: --direct flag existence
# ---------------------------------------------------------------------------


class TestDirectFlagArgParsing:
    def test_direct_flag_is_recognized(self):
        """--direct flag parses without error."""
        args = parse(["ingest", "transcript", "--source-ref=foo", "--stdin", "--direct"])
        assert args.direct is True

    def test_direct_flag_defaults_to_false(self):
        """--direct flag is False when not given."""
        args = parse(["ingest", "transcript", "--source-ref=foo", "--stdin"])
        assert args.direct is False
