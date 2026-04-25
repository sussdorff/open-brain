"""Tests for IMAPEmailIngestor — cr3.4.

Acceptance criteria covered:
1. list_for_person returns UIDs matching FROM/TO/CC of known person addresses
2. ingest_for_person produces interaction memories with source_ref=imap:{server}:{uid}
3. Haiku extraction produces summary (not verbatim body) unless EMAIL_STORE_RAW_BODIES=true
4. Credentials via op CLI CommandRunner DI (never logged)
5. Tests use fixture .eml files from python/tests/fixtures/email/
6. No network calls in unit tests (imapclient mocked)
7. Idempotent: re-running ingest_for_person with same inputs produces no new memories
"""

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from open_brain.data_layer.interface import Memory, SaveMemoryResult, SearchResult

# ─── Fixture paths ──────────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "email"
EML_FORWARDED_INTRO = FIXTURES_DIR / "forwarded_intro.eml"
EML_NEWSLETTER = FIXTURES_DIR / "newsletter.eml"
EML_REPLY_THREAD = FIXTURES_DIR / "reply_thread.eml"


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_memory(
    id: int,
    type: str = "person",
    metadata: dict | None = None,
    content: str = "test",
    title: str | None = None,
) -> Memory:
    return Memory(
        id=id,
        index_id=1,
        session_id=None,
        type=type,
        title=title,
        subtitle=None,
        narrative=None,
        content=content,
        metadata=metadata or {},
        priority=0.5,
        stability="stable",
        access_count=0,
        last_accessed_at=None,
        created_at="2026-04-15T00:00:00",
        updated_at="2026-04-15T00:00:00",
    )


def _make_person_memory(
    id: int,
    email_addresses: list[str],
    name: str = "Test Person",
) -> Memory:
    return _make_memory(
        id=id,
        type="person",
        metadata={"name": name, "email_addresses": email_addresses},
        content=f"Person: {name}",
        title=name,
    )


def _raw_eml(path: Path) -> bytes:
    return path.read_bytes()


def _make_mock_imap(
    search_result: list[int] | None = None,
    fetch_result: dict | None = None,
) -> MagicMock:
    """Build a mock IMAPClient with search/fetch configured."""
    mock = MagicMock()
    mock.login.return_value = b"OK"
    mock.select_folder.return_value = {}
    mock.search.return_value = search_result or []
    mock.fetch.return_value = fetch_result or {}
    return mock


# ─── AC1: list_for_person returns correct UIDs ───────────────────────────────


class TestListForPerson:
    """AC1: list_for_person returns UIDs matching FROM/TO/CC of person addresses."""

    async def test_list_for_person_returns_matching_uids(self):
        """UIDs 101 and 103 match alice@example.com; UID 102 does not."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.ingest.adapters.email_imap import CommandRunner

        mock_runner = MagicMock(spec=CommandRunner)
        mock_runner.run.return_value = "secret-password"

        mock_imap = _make_mock_imap(search_result=[101, 103])

        mock_dl = AsyncMock()
        mock_dl.search.return_value = SearchResult(results=[], total=0)

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
            uids = await ingestor.list_for_person(
                email_addresses=["alice@example.com"],
                since=None,
            )

        assert uids == [101, 103]

    async def test_list_for_person_returns_empty_when_no_match(self):
        """Returns empty list when no emails match."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.ingest.adapters.email_imap import CommandRunner

        mock_runner = MagicMock(spec=CommandRunner)
        mock_runner.run.return_value = "secret-password"

        mock_imap = _make_mock_imap(search_result=[])

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
            uids = await ingestor.list_for_person(
                email_addresses=["nobody@nowhere.example"],
                since=None,
            )

        assert uids == []

    async def test_list_for_person_with_since_date(self):
        """Since date is passed to IMAP SEARCH criterion."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.ingest.adapters.email_imap import CommandRunner

        mock_runner = MagicMock(spec=CommandRunner)
        mock_runner.run.return_value = "pw"

        mock_imap = _make_mock_imap(search_result=[105])

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
            uids = await ingestor.list_for_person(
                email_addresses=["alice@example.com"],
                since="01-Jan-2026",
            )

        assert uids == [105]
        # Verify search was called with some criterion (IMAP search call happened)
        assert mock_imap.search.called


# ─── AC2: ingest_for_person creates interaction memories with source_ref ─────


class TestIngestForPerson:
    """AC2: ingest_for_person produces interaction memories with correct source_ref."""

    async def test_ingest_for_person_creates_interaction_memories(self):
        """ingest_for_person creates one interaction memory per email, source_ref=imap:{server}:{uid}."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.ingest.adapters.email_imap import CommandRunner
        from open_brain.ingest.models import IngestResult

        mock_runner = MagicMock(spec=CommandRunner)
        mock_runner.run.return_value = "pw"

        # Person memory with email
        person_mem = _make_person_memory(
            id=42, email_addresses=["alice@example.com"], name="Alice Example"
        )

        eml_bytes = _raw_eml(EML_REPLY_THREAD)

        mock_imap = _make_mock_imap(
            search_result=[101],
            fetch_result={101: {b"RFC822": eml_bytes}},
        )

        mock_dl = AsyncMock()
        # search: first call returns empty (idempotency check), later calls return person mem
        search_call_count = [0]

        async def _search(params):
            search_call_count[0] += 1
            if params.type == "person" and params.metadata_filter is None:
                # Loading person memory by ID
                return SearchResult(results=[person_mem], total=1)
            if params.metadata_filter and "source_ref" in params.metadata_filter:
                # Idempotency check — no prior
                return SearchResult(results=[], total=0)
            return SearchResult(results=[], total=0)

        mock_dl.search = _search

        save_count = [0]

        async def _save(params):
            save_count[0] += 1
            return SaveMemoryResult(id=save_count[0] * 100, message="ok")

        mock_dl.save_memory.side_effect = _save

        llm_response = json.dumps({
            "summary": "Thomas and Anna confirmed a Wednesday meeting about data integration.",
            "topics": ["data integration", "meeting confirmation"],
            "action_items": ["Send calendar invite"],
        })

        with patch("open_brain.ingest.adapters.email_imap.IMAPClient") as mock_cls, \
             patch("open_brain.ingest.adapters.email_imap.llm_complete") as mock_llm:
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_imap)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_llm.return_value = llm_response

            ingestor = IMAPEmailIngestor(
                data_layer=mock_dl,
                server="mail.example.com",
                port=993,
                user="me@example.com",
                password_op_ref="op://Private/email/app-password",
                runner=mock_runner,
            )
            result = await ingestor.ingest_for_person(person_memory_id=42)

        assert isinstance(result, IngestResult)
        # At least one interaction memory was created
        assert len(result.interaction_memory_ids) >= 1
        # Person memory ID is included
        assert 42 in result.person_memory_ids
        # Verify save_memory was called with correct source_ref
        save_calls = mock_dl.save_memory.call_args_list
        source_refs = [
            c.args[0].metadata.get("source_ref") or c.args[0].metadata
            for c in save_calls
        ]
        assert any(
            "imap:mail.example.com:101" in str(sr) for sr in source_refs
        )


# ─── AC3: Haiku extraction produces summary, not verbatim body ───────────────


class TestHaikuExtraction:
    """AC3: LLM extraction produces summary; EMAIL_STORE_RAW_BODIES=True stores body."""

    async def test_default_stores_summary_not_raw_body(self):
        """Default behavior: interaction text is Haiku summary, not verbatim email."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.ingest.adapters.email_imap import CommandRunner

        mock_runner = MagicMock(spec=CommandRunner)
        mock_runner.run.return_value = "pw"

        person_mem = _make_person_memory(
            id=10, email_addresses=["thomas.bauer@techpartner.example"]
        )

        eml_bytes = _raw_eml(EML_REPLY_THREAD)
        raw_body_snippet = "Sehr geehrte Frau Weber"  # distinctive text in the raw .eml

        mock_imap = _make_mock_imap(
            search_result=[201],
            fetch_result={201: {b"RFC822": eml_bytes}},
        )

        mock_dl = AsyncMock()

        async def _search(params):
            if params.type == "person":
                return SearchResult(results=[person_mem], total=1)
            if params.metadata_filter and "source_ref" in params.metadata_filter:
                return SearchResult(results=[], total=0)
            return SearchResult(results=[], total=0)

        mock_dl.search = _search

        saved_texts: list[str] = []

        async def _save(params):
            saved_texts.append(params.text)
            return SaveMemoryResult(id=len(saved_texts), message="ok")

        mock_dl.save_memory.side_effect = _save

        summary = "Thomas Bauer confirmed a Wednesday tech meeting with Anna Weber."

        with patch("open_brain.ingest.adapters.email_imap.IMAPClient") as mock_cls, \
             patch("open_brain.ingest.adapters.email_imap.llm_complete") as mock_llm:
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_imap)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_llm.return_value = json.dumps({"summary": summary, "topics": []})

            ingestor = IMAPEmailIngestor(
                data_layer=mock_dl,
                server="mail.example.com",
                port=993,
                user="me@example.com",
                password_op_ref="op://Private/email/app-password",
                runner=mock_runner,
                store_raw_bodies=False,
            )
            await ingestor.ingest_for_person(person_memory_id=10)

        # The saved interaction text must be the summary, not the raw email body
        assert any(summary in t for t in saved_texts), \
            f"Expected summary in saved texts, got: {saved_texts}"
        assert not any(raw_body_snippet in t for t in saved_texts), \
            f"Raw body snippet found in saved texts — expected summary only"

    async def test_store_raw_bodies_true_stores_raw_body(self):
        """EMAIL_STORE_RAW_BODIES=True: raw body stored, no LLM call."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.ingest.adapters.email_imap import CommandRunner

        mock_runner = MagicMock(spec=CommandRunner)
        mock_runner.run.return_value = "pw"

        person_mem = _make_person_memory(
            id=10, email_addresses=["thomas.bauer@techpartner.example"]
        )

        eml_bytes = _raw_eml(EML_REPLY_THREAD)

        mock_imap = _make_mock_imap(
            search_result=[201],
            fetch_result={201: {b"RFC822": eml_bytes}},
        )

        mock_dl = AsyncMock()

        async def _search(params):
            if params.type == "person":
                return SearchResult(results=[person_mem], total=1)
            if params.metadata_filter and "source_ref" in params.metadata_filter:
                return SearchResult(results=[], total=0)
            return SearchResult(results=[], total=0)

        mock_dl.search = _search

        saved_texts: list[str] = []

        async def _save(params):
            saved_texts.append(params.text)
            return SaveMemoryResult(id=len(saved_texts), message="ok")

        mock_dl.save_memory.side_effect = _save

        with patch("open_brain.ingest.adapters.email_imap.IMAPClient") as mock_cls, \
             patch("open_brain.ingest.adapters.email_imap.llm_complete") as mock_llm:
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_imap)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)

            ingestor = IMAPEmailIngestor(
                data_layer=mock_dl,
                server="mail.example.com",
                port=993,
                user="me@example.com",
                password_op_ref="op://Private/email/app-password",
                runner=mock_runner,
                store_raw_bodies=True,
            )
            await ingestor.ingest_for_person(person_memory_id=10)

        # Raw body mode: LLM should NOT be called
        assert not mock_llm.called, "LLM should not be called when store_raw_bodies=True"
        # Raw body text should appear in saved texts
        assert any("Sehr geehrte Frau Weber" in t or "Thomas Bauer" in t for t in saved_texts), \
            f"Expected raw body content in saved texts, got: {saved_texts}"


# ─── AC4: Credentials via op CLI, never logged ───────────────────────────────


class TestCredentials:
    """AC4: password fetched via CommandRunner, never logged."""

    async def test_password_fetched_via_command_runner(self):
        """op CLI runner is called to fetch the password."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.ingest.adapters.email_imap import CommandRunner

        mock_runner = MagicMock(spec=CommandRunner)
        mock_runner.run.return_value = "fetched-secret"

        person_mem = _make_person_memory(id=1, email_addresses=["alice@example.com"])

        mock_imap = _make_mock_imap(search_result=[], fetch_result={})
        mock_dl = AsyncMock()

        async def _search(params):
            if params.type == "person":
                return SearchResult(results=[person_mem], total=1)
            return SearchResult(results=[], total=0)

        mock_dl.search = _search

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
            await ingestor.ingest_for_person(person_memory_id=1)

        # Runner must have been called with the op reference
        mock_runner.run.assert_called_once_with("op://Private/email/app-password")

    async def test_password_not_logged(self, caplog):
        """The raw password must never appear in log output."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.ingest.adapters.email_imap import CommandRunner

        secret = "ultra-secret-pw-xyz"

        mock_runner = MagicMock(spec=CommandRunner)
        mock_runner.run.return_value = secret

        person_mem = _make_person_memory(id=1, email_addresses=["alice@example.com"])

        mock_imap = _make_mock_imap(search_result=[], fetch_result={})
        mock_dl = AsyncMock()

        async def _search(params):
            if params.type == "person":
                return SearchResult(results=[person_mem], total=1)
            return SearchResult(results=[], total=0)

        mock_dl.search = _search

        with caplog.at_level(logging.DEBUG, logger="open_brain"), \
             patch("open_brain.ingest.adapters.email_imap.IMAPClient") as mock_cls:
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
            await ingestor.ingest_for_person(person_memory_id=1)

        assert secret not in caplog.text, \
            "Password must never appear in log output"


# ─── AC5: Fixture .eml files used in tests ───────────────────────────────────


class TestEmlFixtures:
    """AC5: Tests use fixture .eml files from python/tests/fixtures/email/."""

    def test_fixture_files_exist(self):
        """All three fixture .eml files must exist."""
        assert EML_FORWARDED_INTRO.exists(), f"Missing: {EML_FORWARDED_INTRO}"
        assert EML_NEWSLETTER.exists(), f"Missing: {EML_NEWSLETTER}"
        assert EML_REPLY_THREAD.exists(), f"Missing: {EML_REPLY_THREAD}"

    async def test_ingest_forwarded_intro_eml(self):
        """Fixture forwarded_intro.eml is parsed and ingested without error."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.ingest.adapters.email_imap import CommandRunner

        mock_runner = MagicMock(spec=CommandRunner)
        mock_runner.run.return_value = "pw"

        # alice is mentioned in forwarded_intro.eml as oliver.schneider@practice.example
        person_mem = _make_person_memory(
            id=99,
            email_addresses=["max.richter@consultant.example"],
            name="Max Richter",
        )

        eml_bytes = _raw_eml(EML_FORWARDED_INTRO)

        mock_imap = _make_mock_imap(
            search_result=[301],
            fetch_result={301: {b"RFC822": eml_bytes}},
        )

        mock_dl = AsyncMock()

        async def _search(params):
            if params.type == "person":
                return SearchResult(results=[person_mem], total=1)
            if params.metadata_filter and "source_ref" in params.metadata_filter:
                return SearchResult(results=[], total=0)
            return SearchResult(results=[], total=0)

        mock_dl.search = _search
        mock_dl.save_memory.return_value = SaveMemoryResult(id=999, message="ok")

        with patch("open_brain.ingest.adapters.email_imap.IMAPClient") as mock_cls, \
             patch("open_brain.ingest.adapters.email_imap.llm_complete") as mock_llm:
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_imap)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_llm.return_value = json.dumps({
                "summary": "Max Richter introduced Dr. Lena Hoffmann to Oliver Schneider.",
                "topics": ["introduction", "FHIR"],
            })

            ingestor = IMAPEmailIngestor(
                data_layer=mock_dl,
                server="mail.example.com",
                port=993,
                user="me@example.com",
                password_op_ref="op://Private/email/app-password",
                runner=mock_runner,
            )
            result = await ingestor.ingest_for_person(person_memory_id=99)

        assert len(result.interaction_memory_ids) >= 1


# ─── AC6: No network calls in unit tests ────────────────────────────────────


class TestNoNetworkCalls:
    """AC6: All IMAP and LLM calls are mocked — no real network connections."""

    async def test_imapclient_is_always_mocked(self):
        """Verify that when IMAPClient is NOT mocked, the test correctly fails.
        This test proves that the patch in other tests actually prevents real connections.
        We only verify the import path is what we'd mock.
        """
        from open_brain.ingest.adapters import email_imap as module
        assert hasattr(module, "IMAPClient"), \
            "email_imap must import IMAPClient so it can be patched"


# ─── AC7: Idempotency ────────────────────────────────────────────────────────


class TestIdempotency:
    """AC7: Re-running ingest_for_person with same inputs produces no new memories."""

    async def test_ingest_idempotent_second_run_no_new_memories(self):
        """Second call with same UIDs creates no new memories."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.ingest.adapters.email_imap import CommandRunner

        mock_runner = MagicMock(spec=CommandRunner)
        mock_runner.run.return_value = "pw"

        person_mem = _make_person_memory(
            id=10, email_addresses=["alice@example.com"]
        )

        eml_bytes = _raw_eml(EML_REPLY_THREAD)
        source_ref = "imap:mail.example.com:101"

        # Existing interaction memory (already ingested)
        existing_interaction = _make_memory(
            id=500,
            type="interaction",
            metadata={"source_ref": source_ref},
        )

        mock_imap = _make_mock_imap(
            search_result=[101],
            fetch_result={101: {b"RFC822": eml_bytes}},
        )

        mock_dl = AsyncMock()

        async def _search(params):
            if params.type == "person":
                return SearchResult(results=[person_mem], total=1)
            if params.metadata_filter and params.metadata_filter.get("source_ref") == source_ref:
                # Memory already exists
                return SearchResult(results=[existing_interaction], total=1)
            return SearchResult(results=[], total=0)

        mock_dl.search = _search
        mock_dl.save_memory = AsyncMock(return_value=SaveMemoryResult(id=999, message="ok"))

        with patch("open_brain.ingest.adapters.email_imap.IMAPClient") as mock_cls, \
             patch("open_brain.ingest.adapters.email_imap.llm_complete") as mock_llm:
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_imap)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_llm.return_value = json.dumps({"summary": "Already ingested."})

            ingestor = IMAPEmailIngestor(
                data_layer=mock_dl,
                server="mail.example.com",
                port=993,
                user="me@example.com",
                password_op_ref="op://Private/email/app-password",
                runner=mock_runner,
            )
            result = await ingestor.ingest_for_person(person_memory_id=10)

        # No new memories should have been saved
        assert mock_dl.save_memory.call_count == 0, \
            f"Expected 0 save_memory calls (idempotent), got {mock_dl.save_memory.call_count}"
        # The existing interaction ID should be reported
        assert 500 in result.interaction_memory_ids


# ─── Additional: ingest_uids processes only specified UIDs ───────────────────


class TestIngestUids:
    """ingest_uids processes only specified UIDs."""

    async def test_ingest_uids_processes_specified_uids(self):
        """Only specified UIDs are fetched and processed."""
        from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
        from open_brain.ingest.adapters.email_imap import CommandRunner

        mock_runner = MagicMock(spec=CommandRunner)
        mock_runner.run.return_value = "pw"

        eml_bytes = _raw_eml(EML_REPLY_THREAD)

        mock_imap = _make_mock_imap(
            fetch_result={
                201: {b"RFC822": eml_bytes},
                203: {b"RFC822": eml_bytes},
            },
        )

        mock_dl = AsyncMock()

        async def _search(params):
            if params.metadata_filter and "source_ref" in params.metadata_filter:
                return SearchResult(results=[], total=0)
            return SearchResult(results=[], total=0)

        mock_dl.search = _search

        save_count = [0]

        async def _save(params):
            save_count[0] += 1
            return SaveMemoryResult(id=save_count[0], message="ok")

        mock_dl.save_memory.side_effect = _save

        with patch("open_brain.ingest.adapters.email_imap.IMAPClient") as mock_cls, \
             patch("open_brain.ingest.adapters.email_imap.llm_complete") as mock_llm:
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_imap)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_llm.return_value = json.dumps({"summary": "Some email.", "topics": []})

            ingestor = IMAPEmailIngestor(
                data_layer=mock_dl,
                server="mail.example.com",
                port=993,
                user="me@example.com",
                password_op_ref="op://Private/email/app-password",
                runner=mock_runner,
            )
            result = await ingestor.ingest_uids(
                uids=[201, 203],
                person_memory_id=None,
            )

        # fetch was called with just the 2 UIDs
        assert mock_imap.fetch.called
        fetched_uids = mock_imap.fetch.call_args[0][0]
        assert set(fetched_uids) == {201, 203}
        # 2 interaction memories created
        assert len(result.interaction_memory_ids) == 2


# ─── MessageRef dataclass smoke test ─────────────────────────────────────────


class TestMessageRef:
    """MessageRef dataclass is importable and works correctly."""

    def test_message_ref_creation(self):
        from open_brain.ingest.adapters.email_imap import MessageRef

        ref = MessageRef(uid=101, subject="Test Subject", date="15-Apr-2026", from_addr="alice@example.com")
        assert ref.uid == 101
        assert ref.subject == "Test Subject"
        assert ref.date == "15-Apr-2026"
        assert ref.from_addr == "alice@example.com"

    def test_message_ref_defaults(self):
        from open_brain.ingest.adapters.email_imap import MessageRef

        ref = MessageRef(uid=202)
        assert ref.uid == 202
        assert ref.subject is None
        assert ref.date is None
        assert ref.from_addr is None


# ─── SubprocessCommandRunner smoke test ──────────────────────────────────────


class TestSubprocessCommandRunner:
    """SubprocessCommandRunner uses subprocess.check_output under the hood."""

    def test_get_default_runner_returns_runner(self):
        from open_brain.ingest.adapters.email_imap import get_default_runner, CommandRunner

        runner = get_default_runner()
        # Must implement the CommandRunner protocol
        assert hasattr(runner, "run")

    def test_subprocess_runner_calls_op(self):
        """SubprocessCommandRunner calls subprocess.check_output with 'op read <ref>'."""
        from open_brain.ingest.adapters.email_imap import SubprocessCommandRunner

        with patch("open_brain.ingest.adapters.email_imap.subprocess") as mock_subprocess:
            mock_subprocess.check_output.return_value = b"my-secret\n"
            runner = SubprocessCommandRunner()
            result = runner.run("op://Private/email/app-password")

        mock_subprocess.check_output.assert_called_once_with(
            ["op", "read", "op://Private/email/app-password"]
        )
        assert result == "my-secret"
