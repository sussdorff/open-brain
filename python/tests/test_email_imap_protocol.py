"""Tests for IMAPEmailIngestor ADR-0001 Protocol compliance.

Acceptance criteria:
1. Adapter discoverable via ADAPTERS['email_imap']
2. list_recent(5) returns at most 5 MessageRef instances against mocked IMAP
3. ingest(ref, run_id) sets run_id on resulting IngestResult
4. Sentinel raises RuntimeError when ingest() called without DataLayer
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ---------------------------------------------------------------------------
# AK1: Adapter discoverable via ADAPTERS['email_imap']
# ---------------------------------------------------------------------------


def test_email_imap_registered_in_adapters():
    """IMAPEmailIngestor must be discoverable via ADAPTERS['email_imap']."""
    import open_brain.ingest.adapters  # noqa: F401 — side-effect import for registration
    from open_brain.ingest.adapters import ADAPTERS

    assert "email_imap" in ADAPTERS
    assert ADAPTERS["email_imap"].name == "email_imap"


# ---------------------------------------------------------------------------
# AK2: list_recent(n) returns ≤n MessageRef instances (mocked IMAP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_imap_list_recent_returns_message_refs():
    """list_recent(5) returns at most 5 MessageRef instances from mocked IMAP."""
    from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor, MessageRef, CommandRunner

    mock_runner = MagicMock(spec=CommandRunner)
    mock_runner.run.return_value = "test-password"

    # IMAP has 7 messages; we ask for 5 — should return 5
    mock_imap = MagicMock()
    mock_imap.login.return_value = b"OK"
    mock_imap.select_folder.return_value = {}
    mock_imap.search.return_value = [1, 2, 3, 4, 5, 6, 7]

    # Build ENVELOPE data for each of the 5 most-recent UIDs (3..7)
    envelope = MagicMock()
    envelope.subject = b"Test Subject"
    envelope.date = "Mon, 15 Apr 2026 12:00:00 +0000"
    mailbox = MagicMock()
    mailbox.mailbox = b"sender"
    mailbox.host = b"example.com"
    envelope.from_ = [mailbox]

    fetch_data = {uid: {b"ENVELOPE": envelope} for uid in [3, 4, 5, 6, 7]}
    mock_imap.fetch.return_value = fetch_data

    mock_dl = AsyncMock()

    with patch("open_brain.ingest.adapters.email_imap.IMAPClient") as mock_cls:
        mock_cls.return_value.__enter__ = MagicMock(return_value=mock_imap)
        mock_cls.return_value.__exit__ = MagicMock(return_value=False)

        ingestor = IMAPEmailIngestor(
            data_layer=mock_dl,
            server="mail.example.com",
            port=993,
            user="me@example.com",
            password_op_ref="op://Private/email/app-password",
            runner=mock_runner,
        )
        refs = await ingestor.list_recent(5)

    assert len(refs) <= 5
    assert all(isinstance(r, MessageRef) for r in refs)


# ---------------------------------------------------------------------------
# AK3: ingest(ref, run_id) sets run_id on IngestResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_imap_ingest_sets_run_id():
    """ingest(ref, run_id) must set run_id on the resulting IngestResult."""
    from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor, MessageRef, CommandRunner
    from open_brain.data_layer.interface import SaveMemoryResult, SearchResult

    mock_runner = MagicMock(spec=CommandRunner)
    mock_runner.run.return_value = "test-password"

    ref = MessageRef(uid=42, subject="Test", date="2026-04-15", from_addr="sender@example.com")

    # RFC822 bytes for the fake email
    raw_eml = b"""From: sender@example.com\r\nTo: me@example.com\r\nSubject: Test\r\nDate: Wed, 15 Apr 2026 12:00:00 +0000\r\n\r\nTest body"""

    mock_imap = MagicMock()
    mock_imap.login.return_value = b"OK"
    mock_imap.select_folder.return_value = {}
    mock_imap.fetch.return_value = {42: {b"RFC822": raw_eml}}

    mock_dl = AsyncMock()

    async def _search(params):
        # Idempotency check — no prior
        return SearchResult(results=[], total=0)

    mock_dl.search = _search
    mock_dl.save_memory.return_value = SaveMemoryResult(id=1001, message="ok")

    with patch("open_brain.ingest.adapters.email_imap.IMAPClient") as mock_cls, \
         patch("open_brain.ingest.adapters.email_imap.llm_complete") as mock_llm:
        mock_cls.return_value.__enter__ = MagicMock(return_value=mock_imap)
        mock_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_llm.return_value = '{"summary": "Test email.", "topics": []}'

        ingestor = IMAPEmailIngestor(
            data_layer=mock_dl,
            server="mail.example.com",
            port=993,
            user="me@example.com",
            password_op_ref="op://Private/email/app-password",
            runner=mock_runner,
        )
        result = await ingestor.ingest(ref, "run-abc-123")

    assert result.run_id == "run-abc-123"


# ---------------------------------------------------------------------------
# AK4: Sentinel raises RuntimeError on ingest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_imap_sentinel_raises_on_ingest():
    """Sentinel instance (no data_layer) must raise RuntimeError on ingest()."""
    from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor, MessageRef

    sentinel = IMAPEmailIngestor()
    with pytest.raises(RuntimeError, match="[Ss]entinel"):
        await sentinel.ingest(MessageRef(uid=1), "run-id")
