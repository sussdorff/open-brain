"""Tests for ingest_email_inbox MCP tool and CLI ingest email command.

Acceptance criteria covered (bead open-brain-353):
1. MCP tool ingest_email_inbox registered and callable
2. CLI command ob ingest email --config <op_ref> --max-messages <N> works end-to-end (mocked)
3. run_id auto-injected via ingest_run context manager when memories are saved
4. skipped_count field in IngestResult tracks duplicates
"""

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_mock_imap(
    search_result: list[int] | None = None,
    fetch_result: dict | None = None,
) -> MagicMock:
    """Build a mock IMAPClient with search/fetch configured."""
    mock = MagicMock()
    mock.login.return_value = b"OK"
    mock.select_folder.return_value = {}
    mock.search.return_value = search_result or [1, 2, 3]
    mock.fetch.return_value = fetch_result or {}
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


# ─── AC4: skipped_count in IngestResult ──────────────────────────────────────


class TestIngestResultSkippedCount:
    """AC4: IngestResult.skipped_count field is present and defaults to 0."""

    def test_ingest_result_has_skipped_count_field(self):
        """IngestResult dataclass has skipped_count field defaulting to 0."""
        from open_brain.ingest.models import IngestResult

        result = IngestResult(meeting_memory_id=0)
        assert hasattr(result, "skipped_count")
        assert result.skipped_count == 0

    def test_ingest_result_skipped_count_can_be_set(self):
        """skipped_count can be set to a positive integer."""
        from open_brain.ingest.models import IngestResult

        result = IngestResult(meeting_memory_id=0, skipped_count=5)
        assert result.skipped_count == 5


# ─── AC4: _ingest_single_email returns bool ───────────────────────────────────


class TestIngestSingleEmailReturnsBool:
    """AC4: _ingest_single_email returns True for new emails, False for skipped."""

    async def test_ingest_single_email_returns_true_for_new(self):
        """_ingest_single_email returns True when email is new (not yet ingested)."""
        import email as email_mod
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.data_layer.interface import SaveMemoryResult, SearchResult

        mock_dl = AsyncMock()
        mock_dl.search.return_value = SearchResult(results=[], total=0)
        mock_dl.save_memory.return_value = SaveMemoryResult(id=42, message="ok")

        ingestor = IMAPEmailIngestor(
            data_layer=mock_dl,
            server="mail.example.com",
            port=993,
            user="me@example.com",
            password_op_ref="op://test",
            store_raw_bodies=True,
        )

        raw = b"From: sender@example.com\r\nSubject: Test\r\nDate: Mon, 1 Jan 2026 00:00:00 +0000\r\n\r\nHello."
        interaction_memory_ids: list[int] = []

        with patch("open_brain.ingest.adapters.email_imap.llm_complete") as mock_llm:
            mock_llm.return_value = json.dumps({"summary": "Test email."})
            result = await ingestor._ingest_single_email(
                uid=101,
                raw=raw,
                person_memory_id=None,
                interaction_memory_ids=interaction_memory_ids,
            )

        assert result is True
        assert 42 in interaction_memory_ids

    async def test_ingest_single_email_returns_false_for_skipped(self):
        """_ingest_single_email returns False when email already ingested (idempotency)."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.data_layer.interface import Memory, SearchResult

        existing = Memory(
            id=500,
            index_id=1,
            session_id=None,
            type="interaction",
            title="Existing",
            subtitle=None,
            narrative=None,
            content="already there",
            metadata={"source_ref": "imap:mail.example.com:101"},
            priority=0.5,
            stability="stable",
            access_count=0,
            last_accessed_at=None,
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )

        mock_dl = AsyncMock()
        mock_dl.search.return_value = SearchResult(results=[existing], total=1)

        ingestor = IMAPEmailIngestor(
            data_layer=mock_dl,
            server="mail.example.com",
            port=993,
            user="me@example.com",
            password_op_ref="op://test",
        )

        raw = b"From: sender@example.com\r\nSubject: Test\r\nDate: Mon, 1 Jan 2026 00:00:00 +0000\r\n\r\nHello."
        interaction_memory_ids: list[int] = []

        result = await ingestor._ingest_single_email(
            uid=101,
            raw=raw,
            person_memory_id=None,
            interaction_memory_ids=interaction_memory_ids,
        )

        assert result is False
        assert 500 in interaction_memory_ids


# ─── AC4: ingest_uids tracks skipped_count ───────────────────────────────────


class TestIngestUidsSkippedCount:
    """AC4: ingest_uids sets skipped_count in IngestResult."""

    async def test_ingest_uids_tracks_skipped_count(self):
        """When one UID is already ingested, skipped_count=1 in result."""
        from pathlib import Path
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.data_layer.interface import Memory, SaveMemoryResult, SearchResult

        existing_source_ref = "imap:mail.example.com:201"
        existing = Memory(
            id=500,
            index_id=1,
            session_id=None,
            type="interaction",
            title="Existing",
            subtitle=None,
            narrative=None,
            content="already there",
            metadata={"source_ref": existing_source_ref},
            priority=0.5,
            stability="stable",
            access_count=0,
            last_accessed_at=None,
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )

        mock_dl = AsyncMock()

        async def _search(params):
            if (
                params.metadata_filter
                and params.metadata_filter.get("source_ref") == existing_source_ref
            ):
                return SearchResult(results=[existing], total=1)
            return SearchResult(results=[], total=0)

        mock_dl.search = _search
        mock_dl.save_memory.return_value = SaveMemoryResult(id=600, message="ok")

        # Use the email fixture
        fixtures_dir = Path(__file__).parent / "fixtures" / "email"
        eml_bytes = (fixtures_dir / "reply_thread.eml").read_bytes()

        mock_imap = _make_mock_imap(
            fetch_result={
                201: {b"RFC822": eml_bytes},
                203: {b"RFC822": eml_bytes},
            }
        )

        ingestor = IMAPEmailIngestor(
            data_layer=mock_dl,
            server="mail.example.com",
            port=993,
            user="me@example.com",
            password_op_ref="op://test",
            store_raw_bodies=True,
        )
        ingestor._fetch_password = MagicMock(return_value="pw")

        with patch("open_brain.ingest.adapters.email_imap.IMAPClient") as mock_cls:
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_imap)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            result = await ingestor.ingest_uids(uids=[201, 203])

        # UID 201 is skipped (existing), UID 203 is new
        assert result.skipped_count == 1
        assert len(result.interaction_memory_ids) == 2


# ─── AC1: MCP tool ingest_email_inbox ────────────────────────────────────────


class TestIngestEmailInboxMcpTool:
    """AC1: ingest_email_inbox MCP tool registered and callable."""

    def test_ingest_email_inbox_is_registered(self):
        """ingest_email_inbox function is importable from server module."""
        from open_brain import server
        assert hasattr(server, "ingest_email_inbox"), (
            "ingest_email_inbox tool must be defined in server.py"
        )

    async def test_ingest_email_inbox_happy_path(self):
        """ingest_email_inbox with 3 new emails returns ingested=3, skipped=0."""
        from open_brain import server

        mock_ingestor = AsyncMock()
        mock_ingestor.ingest_inbox.return_value = (3, 0)

        with (
            patch("open_brain.server.get_dl") as mock_get_dl,
            patch("open_brain.ingest.adapters.email_imap.IMAPEmailIngestor.from_config") as mock_from_cfg,
        ):
            mock_get_dl.return_value = AsyncMock()
            mock_from_cfg.return_value = mock_ingestor

            result_json = await server.ingest_email_inbox(
                config_ref="op://Private/email/app-password",
                max_messages=50,
            )

        result = json.loads(result_json)
        assert result["ingested"] == 3
        assert result["skipped"] == 0
        assert "run_id" in result
        assert result["run_id"] is not None

    async def test_ingest_email_inbox_max_messages_zero(self):
        """ingest_email_inbox with max_messages=0 returns immediately without IMAP call."""
        from open_brain import server

        result_json = await server.ingest_email_inbox(
            config_ref="op://Private/email/app-password",
            max_messages=0,
        )

        result = json.loads(result_json)
        assert result["ingested"] == 0
        assert result["skipped"] == 0
        assert result["run_id"] is None

    async def test_ingest_email_inbox_skips_duplicates(self):
        """ingest_email_inbox with 2 messages, 1 duplicate → ingested=1, skipped=1."""
        from open_brain import server

        mock_ingestor = AsyncMock()
        mock_ingestor.ingest_inbox.return_value = (1, 1)

        with (
            patch("open_brain.server.get_dl") as mock_get_dl,
            patch("open_brain.ingest.adapters.email_imap.IMAPEmailIngestor.from_config") as mock_from_cfg,
        ):
            mock_get_dl.return_value = AsyncMock()
            mock_from_cfg.return_value = mock_ingestor

            result_json = await server.ingest_email_inbox(
                config_ref="op://Private/email/app-password",
                max_messages=10,
            )

        result = json.loads(result_json)
        assert result["ingested"] == 1
        assert result["skipped"] == 1

    async def test_ingest_email_inbox_empty_config_ref_raises(self):
        """ingest_email_inbox with empty config_ref raises ValueError."""
        from open_brain import server

        with pytest.raises(ValueError, match="config_ref"):
            await server.ingest_email_inbox(config_ref="", max_messages=10)

    async def test_ingest_email_inbox_whitespace_config_ref_raises(self):
        """ingest_email_inbox with whitespace-only config_ref raises ValueError."""
        from open_brain import server

        with pytest.raises(ValueError, match="config_ref"):
            await server.ingest_email_inbox(config_ref="   ", max_messages=10)

    async def test_ingest_email_inbox_run_id_in_response(self):
        """ingest_email_inbox returns a non-null run_id in the response."""
        from open_brain import server

        mock_ingestor = AsyncMock()
        mock_ingestor.ingest_inbox.return_value = (2, 0)

        with (
            patch("open_brain.server.get_dl") as mock_get_dl,
            patch("open_brain.ingest.adapters.email_imap.IMAPEmailIngestor.from_config") as mock_from_cfg,
        ):
            mock_get_dl.return_value = AsyncMock()
            mock_from_cfg.return_value = mock_ingestor

            result_json = await server.ingest_email_inbox(
                config_ref="op://Private/email/app-password",
                max_messages=5,
            )

        result = json.loads(result_json)
        assert result["run_id"] is not None
        # run_id should be a UUID4 string
        import uuid
        uuid.UUID(result["run_id"])  # raises if not valid UUID


# ─── AC1: ingest_inbox method on IMAPEmailIngestor ───────────────────────────


class TestIngestInboxMethod:
    """AC1: IMAPEmailIngestor.ingest_inbox() method exists and works correctly."""

    async def test_ingest_inbox_happy_path(self):
        """ingest_inbox connects to IMAP, fetches last N UIDs, calls ingest_uids."""
        from pathlib import Path
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.data_layer.interface import SaveMemoryResult, SearchResult

        mock_dl = AsyncMock()
        mock_dl.search.return_value = SearchResult(results=[], total=0)
        mock_dl.save_memory.return_value = SaveMemoryResult(id=100, message="ok")

        fixtures_dir = Path(__file__).parent / "fixtures" / "email"
        eml_bytes = (fixtures_dir / "reply_thread.eml").read_bytes()

        mock_imap = _make_mock_imap(
            search_result=[1, 2, 3],
            fetch_result={
                1: {b"RFC822": eml_bytes},
                2: {b"RFC822": eml_bytes},
                3: {b"RFC822": eml_bytes},
            },
        )

        ingestor = IMAPEmailIngestor(
            data_layer=mock_dl,
            server="mail.example.com",
            port=993,
            user="me@example.com",
            password_op_ref="op://test",
            store_raw_bodies=True,
        )
        ingestor._fetch_password = MagicMock(return_value="pw")

        with patch("open_brain.ingest.adapters.email_imap.IMAPClient") as mock_cls:
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_imap)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            ingested, skipped = await ingestor.ingest_inbox(max_messages=50)

        assert ingested == 3
        assert skipped == 0

    async def test_ingest_inbox_max_messages_zero(self):
        """ingest_inbox with max_messages=0 returns (0, 0) immediately."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor

        mock_dl = AsyncMock()
        ingestor = IMAPEmailIngestor(
            data_layer=mock_dl,
            server="mail.example.com",
            port=993,
            user="me@example.com",
            password_op_ref="op://test",
        )

        ingested, skipped = await ingestor.ingest_inbox(max_messages=0)

        assert ingested == 0
        assert skipped == 0

    async def test_ingest_inbox_empty_mailbox(self):
        """ingest_inbox with empty mailbox returns (0, 0)."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor

        mock_dl = AsyncMock()
        mock_imap = _make_mock_imap(search_result=[])

        ingestor = IMAPEmailIngestor(
            data_layer=mock_dl,
            server="mail.example.com",
            port=993,
            user="me@example.com",
            password_op_ref="op://test",
        )
        ingestor._fetch_password = MagicMock(return_value="pw")

        with patch("open_brain.ingest.adapters.email_imap.IMAPClient") as mock_cls:
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_imap)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            ingested, skipped = await ingestor.ingest_inbox(max_messages=50)

        assert ingested == 0
        assert skipped == 0

    async def test_ingest_inbox_takes_last_n_uids(self):
        """ingest_inbox takes the N most recent UIDs (ascending order, take last N)."""
        from pathlib import Path
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.data_layer.interface import SaveMemoryResult, SearchResult

        mock_dl = AsyncMock()
        mock_dl.search.return_value = SearchResult(results=[], total=0)
        mock_dl.save_memory.return_value = SaveMemoryResult(id=100, message="ok")

        fixtures_dir = Path(__file__).parent / "fixtures" / "email"
        eml_bytes = (fixtures_dir / "reply_thread.eml").read_bytes()

        # IMAP has 5 messages, we want only last 2
        mock_imap = _make_mock_imap(
            search_result=[10, 20, 30, 40, 50],
            fetch_result={
                40: {b"RFC822": eml_bytes},
                50: {b"RFC822": eml_bytes},
            },
        )

        ingestor = IMAPEmailIngestor(
            data_layer=mock_dl,
            server="mail.example.com",
            port=993,
            user="me@example.com",
            password_op_ref="op://test",
            store_raw_bodies=True,
        )
        ingestor._fetch_password = MagicMock(return_value="pw")

        with patch("open_brain.ingest.adapters.email_imap.IMAPClient") as mock_cls:
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_imap)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            ingested, skipped = await ingestor.ingest_inbox(max_messages=2)

        assert ingested == 2
        assert skipped == 0


# ─── AC2: CLI ingest email command ───────────────────────────────────────────


class TestCliIngestEmail:
    """AC2: CLI command ob ingest email --config <op_ref> --max-messages <N>."""

    def test_cli_ingest_email_calls_tool(self):
        """CLI ingest email subcommand calls ingest_email_inbox tool with correct args."""
        import argparse
        from open_brain.cli.main import _cmd_ingest_email

        args = argparse.Namespace(
            config="op://Private/email/app-password",
            max_messages=25,
        )

        with patch("open_brain.cli.main.call_tool") as mock_call:
            mock_call.return_value = {"ingested": 5, "skipped": 0, "run_id": "abc"}
            import asyncio
            asyncio.run(_cmd_ingest_email(args))

        mock_call.assert_called_once_with(
            "ingest_email_inbox",
            {"config_ref": "op://Private/email/app-password", "max_messages": 25},
        )

    def test_cli_ingest_email_default_max_messages(self):
        """CLI ingest email uses default max_messages=50 when not specified."""
        import argparse
        from open_brain.cli.main import _cmd_ingest_email

        args = argparse.Namespace(
            config="op://Private/email/app-password",
            max_messages=50,  # default
        )

        with patch("open_brain.cli.main.call_tool") as mock_call:
            mock_call.return_value = {"ingested": 0, "skipped": 0, "run_id": None}
            import asyncio
            asyncio.run(_cmd_ingest_email(args))

        call_kwargs = mock_call.call_args[0][1]
        assert call_kwargs["max_messages"] == 50

    def test_cli_build_parser_has_ingest_command(self):
        """_build_parser includes the ingest command with email subcommand."""
        from open_brain.cli.main import _build_parser

        parser = _build_parser()
        # Parse ingest email --config op://test
        args = parser.parse_args(["ingest", "email", "--config", "op://test"])
        assert args.command == "ingest"
        assert args.ingest_command == "email"
        assert args.config == "op://test"
        assert args.max_messages == 50  # default

    def test_cli_build_parser_ingest_email_max_messages(self):
        """--max-messages is parsed correctly by the ingest email subcommand."""
        from open_brain.cli.main import _build_parser

        parser = _build_parser()
        args = parser.parse_args([
            "ingest", "email",
            "--config", "op://Private/email/app-password",
            "--max-messages", "25",
        ])
        assert args.max_messages == 25

    def test_cli_build_parser_ingest_requires_config(self):
        """--config is required for ingest email subcommand."""
        from open_brain.cli.main import _build_parser

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["ingest", "email"])


# ─── AC2: CLI exits 0 on success ─────────────────────────────────────────────


class TestCliExitOnSuccess:
    """AC2: CLI prints JSON and exits 0 on successful email ingest."""

    def test_cli_ingest_email_exits_0_on_success(self, capsys):
        """CLI ingest email prints JSON result when tool succeeds (main returns normally)."""
        from open_brain.cli.main import main

        with (
            patch("sys.argv", ["ob", "ingest", "email", "--config", "op://test"]),
            patch("open_brain.cli.main.call_tool") as mock_call,
        ):
            mock_call.return_value = {"ingested": 3, "skipped": 1, "run_id": "abc123"}
            main()

        captured = capsys.readouterr()
        # Output should be valid JSON
        output = json.loads(captured.out.strip())
        assert output["ingested"] == 3


# ─── AC3: from_config with password_op_ref_override ──────────────────────────


class TestFromConfigPasswordOverride:
    """AC3: from_config() accepts password_op_ref_override parameter."""

    def test_from_config_with_password_op_ref_override(self):
        """from_config with password_op_ref_override uses override instead of config value."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor

        mock_dl = AsyncMock()

        with patch("open_brain.ingest.adapters.email_imap.get_config") as mock_cfg:
            cfg = MagicMock()
            cfg.IMAP_SERVER = "mail.example.com"
            cfg.IMAP_PORT = 993
            cfg.IMAP_USER = "me@example.com"
            cfg.IMAP_PASSWORD_OP = "op://Private/email/default-password"
            cfg.EMAIL_STORE_RAW_BODIES = False
            cfg.EMAIL_EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
            mock_cfg.return_value = cfg

            ingestor = IMAPEmailIngestor.from_config(
                data_layer=mock_dl,
                password_op_ref_override="op://Private/email/override-password",
            )

        assert ingestor._password_op_ref == "op://Private/email/override-password"

    def test_from_config_without_override_uses_config_value(self):
        """from_config without override uses IMAP_PASSWORD_OP from config."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor

        mock_dl = AsyncMock()

        with patch("open_brain.ingest.adapters.email_imap.get_config") as mock_cfg:
            cfg = MagicMock()
            cfg.IMAP_SERVER = "mail.example.com"
            cfg.IMAP_PORT = 993
            cfg.IMAP_USER = "me@example.com"
            cfg.IMAP_PASSWORD_OP = "op://Private/email/config-password"
            cfg.EMAIL_STORE_RAW_BODIES = False
            cfg.EMAIL_EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
            mock_cfg.return_value = cfg

            ingestor = IMAPEmailIngestor.from_config(data_layer=mock_dl)

        assert ingestor._password_op_ref == "op://Private/email/config-password"
