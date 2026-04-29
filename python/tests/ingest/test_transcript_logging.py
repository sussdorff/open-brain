"""Tests for granular logging in TranscriptIngestor + LLM extraction.

Acceptance criteria:
AC1: ingest_transcript with a 600-line transcript produces clear phase boundary log timeline
AC2: If Haiku call takes longer than 5 seconds, a WARNING is emitted with duration_ms
AC3: Each phase log line includes request_id (via ContextVar injected by RequestIdFilter)
AC4: No PII at INFO level — only counts and IDs at INFO; full content only at DEBUG
AC5: Tests verify log timeline structure for a fixture transcript
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.interface import SaveMemoryResult, SearchResult
from open_brain.ingest.adapters.transcript import TranscriptIngestor


# ─── Canned LLM responses ────────────────────────────────────────────────────

LLM_2_ATTENDEES = json.dumps({
    "attendees": ["Alice Smith", "Bob Jones"],
    "mentioned_people": ["Carol White"],
    "topics": ["deadline", "project status"],
    "follow_up_tasks": ["follow up on deadline"],
})

LLM_NO_ATTENDEES = json.dumps({
    "attendees": [],
    "mentioned_people": [],
    "topics": ["general note"],
    "follow_up_tasks": [],
})


# ─── Helper: build mock DataLayer ────────────────────────────────────────────

def _make_mock_dl() -> AsyncMock:
    """Build a mock DataLayer with auto-incrementing IDs."""
    dl = AsyncMock()
    _counter = [0]

    async def _auto_id(*args, **kwargs):
        _counter[0] += 1
        return SaveMemoryResult(id=_counter[0], message="ok")

    dl.save_memory.side_effect = _auto_id
    dl.search.return_value = SearchResult(results=[], total=0)
    _rel_counter = [100]

    async def _auto_rel(*args, **kwargs):
        _rel_counter[0] += 1
        return _rel_counter[0]

    dl.create_relationship.side_effect = _auto_rel
    dl.get_relationships.return_value = []
    return dl


def _make_600_line_transcript() -> str:
    """Create a 600-line fixture transcript for AC1/AC5 tests."""
    lines = []
    for i in range(1, 601):
        speaker = "Alice Smith" if i % 2 == 1 else "Bob Jones"
        lines.append(f"[00:{i//60:02d}:{i%60:02d}] {speaker}: Line {i} of the transcript.")
    return "\n".join(lines)


# ─── AC1 + AC5: Phase boundary log timeline ──────────────────────────────────

class TestPhaseTimeline:
    """AC1 + AC5: ingest produces clear phase boundary logs in chronological order."""

    @pytest.mark.asyncio
    async def test_ingest_logs_phase_boundaries(self, caplog):
        """AC1: a 600-line transcript produces log entries for each phase boundary."""
        transcript = _make_600_line_transcript()
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_2_ATTENDEES
            ingestor = TranscriptIngestor(data_layer=mock_dl)

            with caplog.at_level(logging.INFO, logger="open_brain"):
                await ingestor.ingest(
                    text=transcript,
                    source_ref="fixture-600-lines",
                )

        messages = [r.message for r in caplog.records]

        # AC1: verify each expected phase boundary appears in log output
        assert any("ingest_start" in m for m in messages), (
            f"Expected ingest_start in logs, got: {messages}"
        )
        assert any("llm_extract_start" in m for m in messages), (
            f"Expected llm_extract_start in logs, got: {messages}"
        )
        assert any("llm_extract_end" in m for m in messages), (
            f"Expected llm_extract_end in logs, got: {messages}"
        )
        assert any("meeting_saved" in m for m in messages), (
            f"Expected meeting_saved in logs, got: {messages}"
        )
        assert any("persons_resolved" in m for m in messages), (
            f"Expected persons_resolved in logs, got: {messages}"
        )
        assert any("mentions_saved" in m for m in messages), (
            f"Expected mentions_saved in logs, got: {messages}"
        )
        assert any("relationships_created" in m for m in messages), (
            f"Expected relationships_created in logs, got: {messages}"
        )
        assert any("ingest_end" in m for m in messages), (
            f"Expected ingest_end in logs, got: {messages}"
        )

    @pytest.mark.asyncio
    async def test_ingest_log_timeline_chronological_order(self, caplog):
        """AC5: phase boundary logs appear in correct chronological order."""
        transcript = _make_600_line_transcript()
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_2_ATTENDEES
            ingestor = TranscriptIngestor(data_layer=mock_dl)

            with caplog.at_level(logging.INFO, logger="open_brain"):
                await ingestor.ingest(
                    text=transcript,
                    source_ref="fixture-600-lines-order",
                )

        messages = [r.message for r in caplog.records]

        def _find_idx(keyword: str) -> int:
            for i, m in enumerate(messages):
                if keyword in m:
                    return i
            return -1

        idx_start = _find_idx("ingest_start")
        idx_llm_start = _find_idx("llm_extract_start")
        idx_llm_end = _find_idx("llm_extract_end")
        idx_meeting_saved = _find_idx("meeting_saved")
        idx_persons = _find_idx("persons_resolved")
        idx_mentions = _find_idx("mentions_saved")
        idx_rels = _find_idx("relationships_created")
        idx_end = _find_idx("ingest_end")

        # All must be present
        assert idx_start >= 0, "ingest_start not found"
        assert idx_llm_start >= 0, "llm_extract_start not found"
        assert idx_llm_end >= 0, "llm_extract_end not found"
        assert idx_meeting_saved >= 0, "meeting_saved not found"
        assert idx_persons >= 0, "persons_resolved not found"
        assert idx_mentions >= 0, "mentions_saved not found"
        assert idx_rels >= 0, "relationships_created not found"
        assert idx_end >= 0, "ingest_end not found"

        # Chronological order: start < llm_start < llm_end < meeting_saved < persons < mentions < rels < end
        assert idx_start < idx_llm_start, "ingest_start must precede llm_extract_start"
        assert idx_llm_start < idx_llm_end, "llm_extract_start must precede llm_extract_end"
        assert idx_llm_end < idx_meeting_saved, "llm_extract_end must precede meeting_saved"
        assert idx_meeting_saved < idx_persons, "meeting_saved must precede persons_resolved"
        assert idx_persons < idx_mentions, "persons_resolved must precede mentions_saved"
        assert idx_mentions < idx_rels, "mentions_saved must precede relationships_created"
        assert idx_rels < idx_end, "relationships_created must precede ingest_end"

    @pytest.mark.asyncio
    async def test_ingest_start_log_includes_text_len_and_source_ref(self, caplog):
        """AC1: ingest_start log includes source_ref and text_len."""
        transcript = _make_600_line_transcript()
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_NO_ATTENDEES
            ingestor = TranscriptIngestor(data_layer=mock_dl)

            with caplog.at_level(logging.INFO, logger="open_brain"):
                await ingestor.ingest(
                    text=transcript,
                    source_ref="ac1-source-ref",
                )

        start_msgs = [r.message for r in caplog.records if "ingest_start" in r.message]
        assert start_msgs, "Expected ingest_start log"
        msg = start_msgs[0]
        assert "ac1-source-ref" in msg, f"Expected source_ref in ingest_start: {msg}"
        assert "text_len=" in msg, f"Expected text_len in ingest_start: {msg}"

    @pytest.mark.asyncio
    async def test_ingest_end_log_includes_duration_ms(self, caplog):
        """AC1: ingest_end log includes duration_ms."""
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_NO_ATTENDEES
            ingestor = TranscriptIngestor(data_layer=mock_dl)

            with caplog.at_level(logging.INFO, logger="open_brain"):
                await ingestor.ingest(
                    text="Short transcript.",
                    source_ref="end-log-test",
                )

        end_msgs = [r.message for r in caplog.records if "ingest_end" in r.message]
        assert end_msgs, "Expected ingest_end log"
        assert "duration_ms=" in end_msgs[0], f"Expected duration_ms in ingest_end: {end_msgs[0]}"

    @pytest.mark.asyncio
    async def test_llm_extract_end_log_includes_duration_ms_and_counts(self, caplog):
        """AC1: llm_extract_end log includes duration_ms and extracted counts."""
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_2_ATTENDEES
            ingestor = TranscriptIngestor(data_layer=mock_dl)

            with caplog.at_level(logging.INFO, logger="open_brain"):
                await ingestor.ingest(
                    text="Alice and Bob met and mentioned Carol.",
                    source_ref="llm-end-test",
                )

        llm_end_msgs = [r.message for r in caplog.records if "llm_extract_end" in r.message]
        assert llm_end_msgs, "Expected llm_extract_end log"
        msg = llm_end_msgs[0]
        assert "duration_ms=" in msg, f"Expected duration_ms in llm_extract_end: {msg}"
        assert "attendees=" in msg, f"Expected attendees= in llm_extract_end: {msg}"


# ─── AC2: Slow LLM WARNING ───────────────────────────────────────────────────

class TestSlowLlmWarning:
    """AC2: LLM calls > 5 seconds emit WARNING with duration_ms."""

    @pytest.mark.asyncio
    async def test_slow_llm_warning_from_transcript_timing(self, caplog):
        """AC2: WARNING emitted when LLM phase takes > 5 seconds (measured in transcript.py)."""
        mock_dl = _make_mock_dl()

        # Patch time.monotonic in transcript.py to simulate >5s LLM phase
        # ingest_start call, then llm_start call, then llm_end call (difference > 5s)
        monotonic_values = iter([
            0.0,     # ingest_start
            1.0,     # llm_start (in ingest())
            7500.0,  # llm_end (7.5 seconds later → 6500ms → > 5000ms)
            7500.0,  # total ingest end
        ])

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_NO_ATTENDEES

            with patch("open_brain.ingest.adapters.transcript.time") as mock_time:
                mock_time.monotonic.side_effect = lambda: next(monotonic_values)

                ingestor = TranscriptIngestor(data_layer=mock_dl)

                with caplog.at_level(logging.WARNING, logger="open_brain"):
                    await ingestor.ingest(
                        text="Transcript with simulated slow LLM.",
                        source_ref="slow-timing-test",
                    )

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        slow_warnings = [r for r in warning_records if "slow_llm" in r.message or "duration_ms" in r.message]
        assert slow_warnings, (
            f"Expected WARNING about slow LLM, got warnings: {[r.message for r in warning_records]}"
        )
        # Verify duration_ms is present in the warning
        assert any("duration_ms" in r.message for r in slow_warnings), (
            f"Expected duration_ms in slow LLM warning, got: {[r.message for r in slow_warnings]}"
        )


# ─── AC3: request_id in phase logs ───────────────────────────────────────────

class TestRequestIdInLogs:
    """AC3: Each phase log line includes request_id via ContextVar."""

    @pytest.mark.asyncio
    async def test_request_id_appears_in_log_records(self, caplog):
        """AC3: Log records have request_id attribute set via RequestIdFilter."""
        from open_brain.logging_config import RequestIdFilter
        from open_brain.server import _request_id

        mock_dl = _make_mock_dl()
        test_rid = "test-request-id-12345"

        # Install the RequestIdFilter on the caplog handler so request_id
        # attribute is added to records
        filter_instance = RequestIdFilter()
        caplog.handler.addFilter(filter_instance)

        try:
            token = _request_id.set(test_rid)
            try:
                with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
                    mock_llm.return_value = LLM_NO_ATTENDEES
                    ingestor = TranscriptIngestor(data_layer=mock_dl)

                    with caplog.at_level(logging.INFO, logger="open_brain"):
                        await ingestor.ingest(
                            text="Transcript for request_id test.",
                            source_ref="rid-test-001",
                        )
            finally:
                _request_id.reset(token)
        finally:
            caplog.handler.removeFilter(filter_instance)

        # All log records should have request_id attribute = test_rid
        ingest_records = [
            r for r in caplog.records
            if r.name.startswith("open_brain.ingest")
        ]
        assert ingest_records, "Expected log records from open_brain.ingest"
        for rec in ingest_records:
            assert hasattr(rec, "request_id"), (
                f"Expected request_id attribute on record: {rec.message}"
            )
            assert rec.request_id == test_rid, (
                f"Expected request_id={test_rid!r}, got {rec.request_id!r} for: {rec.message}"
            )

    @pytest.mark.asyncio
    async def test_request_id_filter_injects_default_when_no_context(self, caplog):
        """AC3: RequestIdFilter injects '-' when no request_id is set in ContextVar."""
        from open_brain.logging_config import RequestIdFilter
        from open_brain.server import _request_id

        # Ensure no request_id is set
        _request_id.set(None)

        filter_instance = RequestIdFilter()
        caplog.handler.addFilter(filter_instance)

        try:
            with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
                mock_llm.return_value = LLM_NO_ATTENDEES
                mock_dl = _make_mock_dl()
                ingestor = TranscriptIngestor(data_layer=mock_dl)

                with caplog.at_level(logging.INFO, logger="open_brain"):
                    await ingestor.ingest(
                        text="Transcript with no request_id.",
                        source_ref="no-rid-test",
                    )
        finally:
            caplog.handler.removeFilter(filter_instance)

        ingest_records = [
            r for r in caplog.records
            if r.name.startswith("open_brain.ingest")
        ]
        assert ingest_records, "Expected log records"
        for rec in ingest_records:
            assert hasattr(rec, "request_id"), "Expected request_id attribute"
            assert rec.request_id == "-", (
                f"Expected '-' when no request_id set, got {rec.request_id!r}"
            )


# ─── AC4: No PII at INFO level ───────────────────────────────────────────────

class TestNoPiiAtInfo:
    """AC4: Full content (transcript body, attendee names) only at DEBUG; INFO has only counts/IDs."""

    @pytest.mark.asyncio
    async def test_attendee_names_not_in_info_logs(self, caplog):
        """AC4: Attendee names do not appear in INFO-level log messages."""
        mock_dl = _make_mock_dl()
        attendee_names = ["Alice Smith", "Bob Jones", "Carol White"]

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_2_ATTENDEES
            ingestor = TranscriptIngestor(data_layer=mock_dl)

            with caplog.at_level(logging.DEBUG, logger="open_brain"):
                await ingestor.ingest(
                    text="Alice and Bob met and mentioned Carol.",
                    source_ref="pii-test-001",
                )

        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        info_messages = [r.message for r in info_records]

        for name in attendee_names:
            for msg in info_messages:
                assert name not in msg, (
                    f"PII leak at INFO: name '{name}' found in message: {msg!r}"
                )

    @pytest.mark.asyncio
    async def test_transcript_body_not_in_info_logs(self, caplog):
        """AC4: Transcript body content does not appear in INFO-level log messages."""
        unique_phrase = "UNIQUE-PHRASE-XYZ-12345-THAT-SHOULD-NOT-LEAK"
        transcript = f"Alice Smith: {unique_phrase} Bob Jones: Some other content."
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_2_ATTENDEES
            ingestor = TranscriptIngestor(data_layer=mock_dl)

            with caplog.at_level(logging.DEBUG, logger="open_brain"):
                await ingestor.ingest(
                    text=transcript,
                    source_ref="pii-body-test",
                )

        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        for rec in info_records:
            assert unique_phrase not in rec.message, (
                f"Transcript body leaked at INFO level: {rec.message!r}"
            )

    @pytest.mark.asyncio
    async def test_info_logs_include_counts_not_names(self, caplog):
        """AC4: INFO logs for persons include counts (total=, new=, reused=) not names."""
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_2_ATTENDEES
            ingestor = TranscriptIngestor(data_layer=mock_dl)

            with caplog.at_level(logging.DEBUG, logger="open_brain"):
                await ingestor.ingest(
                    text="Alice and Bob met and mentioned Carol.",
                    source_ref="counts-test",
                )

        persons_resolved_msgs = [
            r.message for r in caplog.records
            if "persons_resolved" in r.message and r.levelno == logging.INFO
        ]
        assert persons_resolved_msgs, "Expected persons_resolved INFO log"
        msg = persons_resolved_msgs[0]
        assert "total=" in msg, f"Expected total= in persons_resolved: {msg}"
        assert "new=" in msg, f"Expected new= in persons_resolved: {msg}"
        assert "reused=" in msg, f"Expected reused= in persons_resolved: {msg}"


# ─── AC1: LLM extraction logs in extract.py ──────────────────────────────────

class TestExtractLogs:
    """AC1: extract.py emits llm_complete_start and llm_complete_end logs."""

    @pytest.mark.asyncio
    async def test_extract_logs_llm_complete_start_and_end(self, caplog):
        """AC1: extract_from_transcript emits llm_complete_start and llm_complete_end."""
        from open_brain.ingest.extract import extract_from_transcript

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = json.dumps({
                "attendees": ["Alice"],
                "mentioned_people": [],
                "topics": ["test"],
                "follow_up_tasks": [],
            })

            with caplog.at_level(logging.INFO, logger="open_brain.ingest.extract"):
                await extract_from_transcript("Short test transcript.")

        messages = [r.message for r in caplog.records]
        assert any("llm_complete_start" in m for m in messages), (
            f"Expected llm_complete_start in extract logs, got: {messages}"
        )
        assert any("llm_complete_end" in m for m in messages), (
            f"Expected llm_complete_end in extract logs, got: {messages}"
        )

    @pytest.mark.asyncio
    async def test_extract_logs_prompt_chars_in_start(self, caplog):
        """AC1: llm_complete_start log includes prompt_chars count."""
        from open_brain.ingest.extract import extract_from_transcript

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = json.dumps({
                "attendees": [],
                "mentioned_people": [],
                "topics": [],
                "follow_up_tasks": [],
            })

            with caplog.at_level(logging.INFO, logger="open_brain.ingest.extract"):
                await extract_from_transcript("Test text for char count.")

        start_msgs = [r.message for r in caplog.records if "llm_complete_start" in r.message]
        assert start_msgs, "Expected llm_complete_start log"
        assert "prompt_chars=" in start_msgs[0], (
            f"Expected prompt_chars= in llm_complete_start: {start_msgs[0]}"
        )

    @pytest.mark.asyncio
    async def test_extract_logs_response_chars_in_end(self, caplog):
        """AC1: llm_complete_end log includes response_chars and duration_ms."""
        from open_brain.ingest.extract import extract_from_transcript

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = json.dumps({
                "attendees": [],
                "mentioned_people": [],
                "topics": [],
                "follow_up_tasks": [],
            })

            with caplog.at_level(logging.INFO, logger="open_brain.ingest.extract"):
                await extract_from_transcript("Test text.")

        end_msgs = [r.message for r in caplog.records if "llm_complete_end" in r.message]
        assert end_msgs, "Expected llm_complete_end log"
        msg = end_msgs[0]
        assert "response_chars=" in msg, f"Expected response_chars= in end log: {msg}"
        assert "duration_ms=" in msg, f"Expected duration_ms= in end log: {msg}"


# ─── AC2: llm.py slow call warning ───────────────────────────────────────────

class TestLlmProviderLogs:
    """AC2: llm.py emits DEBUG on start, INFO on end, WARNING when > 5 seconds."""

    @pytest.mark.asyncio
    async def test_llm_http_start_logged_at_debug(self, caplog):
        """AC2: llm.py logs llm_http_start at DEBUG before HTTP call."""
        from open_brain.data_layer.llm import _call_anthropic, LlmMessage

        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = {
            "content": [{"text": "response text"}]
        }
        mock_response.content = b'{"content": [{"text": "response text"}]}'
        mock_response.status_code = 200

        with patch("open_brain.data_layer.llm.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with caplog.at_level(logging.DEBUG, logger="open_brain.data_layer.llm"):
                await _call_anthropic(
                    messages=[LlmMessage(role="user", content="test")],
                    model="claude-haiku-4-5",
                    max_tokens=100,
                )

        messages = [r.message for r in caplog.records]
        assert any("llm_http_start" in m for m in messages), (
            f"Expected llm_http_start in llm logs, got: {messages}"
        )
        assert any("llm_http_end" in m for m in messages), (
            f"Expected llm_http_end in llm logs, got: {messages}"
        )

    @pytest.mark.asyncio
    async def test_llm_slow_call_emits_warning(self, caplog):
        """AC2: llm.py emits WARNING with duration_ms when call > 5 seconds."""
        from open_brain.data_layer.llm import _call_anthropic, LlmMessage

        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = {
            "content": [{"text": "slow response"}]
        }
        mock_response.content = b'{"content": [{"text": "slow response"}]}'
        mock_response.status_code = 200

        # Patch time in llm.py to simulate > 5s
        call_count = [0]

        def _fake_monotonic():
            call_count[0] += 1
            if call_count[0] == 1:
                return 0.0   # start
            return 6.0       # end → 6000ms > 5000ms

        with patch("open_brain.data_layer.llm.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with patch("open_brain.data_layer.llm.time") as mock_time:
                mock_time.monotonic.side_effect = _fake_monotonic

                with caplog.at_level(logging.WARNING, logger="open_brain.data_layer.llm"):
                    await _call_anthropic(
                        messages=[LlmMessage(role="user", content="test")],
                        model="claude-haiku-4-5",
                        max_tokens=100,
                    )

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records, (
            f"Expected WARNING for slow LLM call, got records: {[r.message for r in caplog.records]}"
        )
        warn_msg = warning_records[0].message
        assert "duration_ms" in warn_msg, (
            f"Expected duration_ms in slow_llm_call warning: {warn_msg}"
        )

    @pytest.mark.asyncio
    async def test_llm_http_error_logged_at_error(self, caplog):
        """AC2: llm.py emits ERROR log on HTTP failure."""
        from open_brain.data_layer.llm import _call_anthropic, LlmMessage

        mock_response = MagicMock()
        mock_response.is_success = False
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.content = b"Internal Server Error"

        with patch("open_brain.data_layer.llm.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with caplog.at_level(logging.ERROR, logger="open_brain.data_layer.llm"):
                with pytest.raises(RuntimeError):
                    await _call_anthropic(
                        messages=[LlmMessage(role="user", content="test")],
                        model="claude-haiku-4-5",
                        max_tokens=100,
                    )

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, (
            f"Expected ERROR log on HTTP failure, got: {[r.message for r in caplog.records]}"
        )
        assert any("llm_http_error" in r.message for r in error_records), (
            f"Expected llm_http_error in error log: {[r.message for r in error_records]}"
        )

    @pytest.mark.asyncio
    async def test_llm_http_end_includes_duration_ms(self, caplog):
        """AC2: llm_http_end log includes duration_ms."""
        from open_brain.data_layer.llm import _call_anthropic, LlmMessage

        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = {"content": [{"text": "ok"}]}
        mock_response.content = b'{"content": [{"text": "ok"}]}'
        mock_response.status_code = 200

        with patch("open_brain.data_layer.llm.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with caplog.at_level(logging.INFO, logger="open_brain.data_layer.llm"):
                await _call_anthropic(
                    messages=[LlmMessage(role="user", content="test")],
                    model="claude-haiku-4-5",
                    max_tokens=100,
                )

        end_records = [r for r in caplog.records if "llm_http_end" in r.message]
        assert end_records, "Expected llm_http_end log"
        assert "duration_ms=" in end_records[0].message, (
            f"Expected duration_ms= in llm_http_end: {end_records[0].message}"
        )
