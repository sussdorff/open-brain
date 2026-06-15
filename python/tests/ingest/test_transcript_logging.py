"""Tests for granular logging in TranscriptIngestor + LLM extraction (open-brain-eip).

Acceptance criteria covered:
AK1: ingest() emits a clear phase-boundary log timeline (ingest_start, idempotency_miss,
     llm_extract_start/end, meeting_saved, mentions_saved, relationships_created, ingest_end)
AK2: If LLM call takes >5000ms, WARNING emitted with duration_ms
AK3: request_id propagation — handled by bt9 infrastructure (not tested here)
AK4: No PII at INFO level — names only at DEBUG (dedup_decision)
AK5: Tests verify log timeline for fixture transcript
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from open_brain.data_layer.interface import SaveMemoryResult, SearchResult
from open_brain.ingest.adapters.transcript import TranscriptIngestor

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
TRANSCRIPT_3PERSON = FIXTURES_DIR / "macwhisper" / "sample_transcript_3person_meeting.txt"

LLM_2_ATTENDEES = json.dumps({
    "attendees": ["Alice Smith", "Bob Jones"],
    "mentioned_people": [],
    "topics": ["deadline"],
    "follow_up_tasks": [],
})

LLM_3PERSON = json.dumps({
    "attendees": ["Sarah Hoffmann", "Marcus Berger", "Priya Nair"],
    "mentioned_people": ["Tobias Schreiber"],
    "topics": ["API integration"],
    "follow_up_tasks": [],
})


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


# ─── AK1+AK5: Phase-boundary log timeline ────────────────────────────────────


class TestTranscriptLoggingTimeline:
    """AK1+AK5: ingest() emits phase-boundary log lines in order."""

    @pytest.mark.asyncio
    async def test_ingest_start_logged(self, caplog):
        """ingest_start line emitted at INFO with source_ref and text_len."""
        mock_dl = _make_mock_dl()
        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_2_ATTENDEES
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            with caplog.at_level(logging.INFO, logger="open_brain.ingest.adapters.transcript"):
                await ingestor.ingest(text="Alice and Bob met.", source_ref="log-test-001")
        messages = [r.message for r in caplog.records]
        assert any("ingest_start" in m for m in messages), f"Expected ingest_start, got: {messages}"

    @pytest.mark.asyncio
    async def test_ingest_end_logged_with_duration(self, caplog):
        """ingest_end line emitted at INFO with duration_ms."""
        mock_dl = _make_mock_dl()
        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_2_ATTENDEES
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            with caplog.at_level(logging.INFO, logger="open_brain.ingest.adapters.transcript"):
                await ingestor.ingest(text="Alice and Bob met.", source_ref="log-test-002")
        messages = [r.message for r in caplog.records]
        end_msgs = [m for m in messages if "ingest_end" in m]
        assert end_msgs, f"Expected ingest_end, got: {messages}"
        assert any("duration_ms" in m for m in end_msgs), f"Expected duration_ms in ingest_end: {end_msgs}"

    @pytest.mark.asyncio
    async def test_full_timeline_ordering(self, caplog):
        """Phase-boundary lines appear in chronological order."""
        mock_dl = _make_mock_dl()
        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_2_ATTENDEES
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            with caplog.at_level(logging.INFO, logger="open_brain.ingest.adapters.transcript"):
                await ingestor.ingest(text="Alice and Bob met.", source_ref="log-test-003")
        messages = [r.message for r in caplog.records]
        phases = ["ingest_start", "llm_extract_start", "llm_extract_end", "meeting_saved", "ingest_end"]
        found_indices = []
        for phase in phases:
            idx = next((i for i, m in enumerate(messages) if phase in m), None)
            assert idx is not None, f"Phase '{phase}' not found in: {messages}"
            found_indices.append(idx)
        assert found_indices == sorted(found_indices), (
            f"Phases out of order: {list(zip(phases, found_indices))}"
        )

    @pytest.mark.asyncio
    async def test_idempotency_miss_logged(self, caplog):
        """idempotency_miss line emitted when no prior run found."""
        mock_dl = _make_mock_dl()
        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_2_ATTENDEES
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            with caplog.at_level(logging.INFO, logger="open_brain.ingest.adapters.transcript"):
                await ingestor.ingest(text="Fresh transcript.", source_ref="idempotency-miss-test")
        messages = [r.message for r in caplog.records]
        assert any("idempotency_miss" in m for m in messages), (
            f"Expected idempotency_miss, got: {messages}"
        )

    @pytest.mark.asyncio
    async def test_meeting_saved_includes_meeting_id(self, caplog):
        """meeting_saved line includes the actual meeting_id."""
        mock_dl = _make_mock_dl()
        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_2_ATTENDEES
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            with caplog.at_level(logging.INFO, logger="open_brain.ingest.adapters.transcript"):
                result = await ingestor.ingest(text="Alice met Bob.", source_ref="meeting-id-test")
        messages = [r.message for r in caplog.records]
        saved_msgs = [m for m in messages if "meeting_saved" in m]
        assert saved_msgs, f"Expected meeting_saved log, got: {messages}"
        assert str(result.meeting_memory_id) in saved_msgs[0], (
            f"Expected meeting_id={result.meeting_memory_id} in: {saved_msgs[0]}"
        )

    @pytest.mark.asyncio
    async def test_dedup_decision_at_debug_not_info(self, caplog):
        """AK4: per-attendee dedup decisions must be at DEBUG, not INFO."""
        mock_dl = _make_mock_dl()
        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_2_ATTENDEES
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            with caplog.at_level(logging.DEBUG, logger="open_brain.ingest.adapters.transcript"):
                await ingestor.ingest(text="Alice and Bob met.", source_ref="dedup-debug-test")
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        dedup_debug = [r for r in debug_records if "dedup_decision" in r.message]
        dedup_info = [r for r in info_records if "dedup_decision" in r.message]
        assert dedup_debug, (
            f"Expected dedup_decision at DEBUG, debug msgs: {[r.message for r in debug_records]}"
        )
        assert not dedup_info, (
            f"dedup_decision must not appear at INFO: {[r.message for r in dedup_info]}"
        )

    @pytest.mark.asyncio
    async def test_fixture_transcript_produces_timeline(self, caplog):
        """AK5: fixture transcript (3-person meeting) produces full timeline."""
        assert TRANSCRIPT_3PERSON.exists(), f"Fixture missing: {TRANSCRIPT_3PERSON}"
        transcript_text = TRANSCRIPT_3PERSON.read_text()
        mock_dl = _make_mock_dl()
        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_3PERSON
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            with caplog.at_level(logging.INFO, logger="open_brain.ingest.adapters.transcript"):
                result = await ingestor.ingest(text=transcript_text, source_ref="fixture-3person")
        messages = [r.message for r in caplog.records]
        for expected in ["ingest_start", "llm_extract_start", "meeting_saved", "ingest_end"]:
            assert any(expected in m for m in messages), (
                f"Phase '{expected}' missing from timeline: {messages}"
            )
        end_msgs = [m for m in messages if "ingest_end" in m]
        assert end_msgs, "ingest_end not logged"
        assert str(result.meeting_memory_id) in end_msgs[0], (
            f"Expected meeting_id={result.meeting_memory_id} in ingest_end: {end_msgs[0]}"
        )

    @pytest.mark.asyncio
    async def test_mentions_saved_logged(self, caplog):
        """mentions_saved log line emitted when mentioned people exist."""
        LLM_WITH_MENTIONED = json.dumps({
            "attendees": ["Alice Smith"],
            "mentioned_people": ["Carol White"],
            "topics": ["planning"],
            "follow_up_tasks": [],
        })
        mock_dl = _make_mock_dl()
        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_WITH_MENTIONED
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            with caplog.at_level(logging.INFO, logger="open_brain.ingest.adapters.transcript"):
                await ingestor.ingest(text="Alice and Carol discussed plans.", source_ref="mentions-test")
        messages = [r.message for r in caplog.records]
        assert any("mentions_saved" in m for m in messages), (
            f"Expected mentions_saved, got: {messages}"
        )

    @pytest.mark.asyncio
    async def test_relationships_logged(self, caplog):
        """relationships_created log line emitted with attended_by/mentioned_in counts."""
        mock_dl = _make_mock_dl()
        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_2_ATTENDEES
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            with caplog.at_level(logging.INFO, logger="open_brain.ingest.adapters.transcript"):
                await ingestor.ingest(text="Alice and Bob met.", source_ref="rels-test")
        messages = [r.message for r in caplog.records]
        assert any("relationships_created" in m for m in messages), (
            f"Expected relationships_created, got: {messages}"
        )


# ─── LLM layer logging ────────────────────────────────────────────────────────


class TestLlmLogging:
    """Verify logging in extract.py and llm.py."""

    @pytest.mark.asyncio
    async def test_extract_logs_prompt_size(self, caplog):
        """extract.py logs prompt_chars before llm_complete call."""
        from open_brain.ingest.extract import extract_from_transcript

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = (
                '{"attendees":[],"mentioned_people":[],"topics":[],"follow_up_tasks":[]}'
            )
            with caplog.at_level(logging.INFO, logger="open_brain.ingest.extract"):
                await extract_from_transcript("Some transcript text.")
        messages = [r.message for r in caplog.records]
        assert any("prompt_chars" in m for m in messages), (
            f"Expected prompt_chars log in extract, got: {messages}"
        )

    @pytest.mark.asyncio
    async def test_extract_logs_response_size_and_duration(self, caplog):
        """extract.py logs response_chars and duration_ms after llm_complete."""
        from open_brain.ingest.extract import extract_from_transcript

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = (
                '{"attendees":[],"mentioned_people":[],"topics":[],"follow_up_tasks":[]}'
            )
            with caplog.at_level(logging.INFO, logger="open_brain.ingest.extract"):
                await extract_from_transcript("Some transcript.")
        messages = [r.message for r in caplog.records]
        assert any("response_chars" in m for m in messages), (
            f"Expected response_chars log, got: {messages}"
        )
        assert any("duration_ms" in m for m in messages), (
            f"Expected duration_ms log, got: {messages}"
        )

    @pytest.mark.asyncio
    async def test_llm_debug_logged_before_call(self, caplog):
        """llm.py logs DEBUG with provider/model/max_tokens/msg_count before HTTP call."""
        import json as _json

        import httpx

        from open_brain.data_layer.llm import LlmMessage, _call_anthropic

        body = _json.dumps({"content": [{"type": "text", "text": "ok"}]})
        response = httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/json"}
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=response)

        class FakeClient:
            async def __aenter__(self):
                return mock_client

            async def __aexit__(self, *args):
                pass

        with (
            patch("httpx.AsyncClient", return_value=FakeClient()),
            caplog.at_level(logging.DEBUG, logger="open_brain.data_layer.llm"),
        ):
            await _call_anthropic(
                [LlmMessage(role="user", content="test")],
                model="claude-haiku-4-5",
                max_tokens=100,
            )
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("provider=anthropic" in m for m in debug_msgs), (
            f"Expected provider=anthropic at DEBUG, got: {debug_msgs}"
        )
        assert any("max_tokens" in m for m in debug_msgs), (
            f"Expected max_tokens at DEBUG, got: {debug_msgs}"
        )

    @pytest.mark.asyncio
    async def test_llm_success_logged_at_info(self, caplog):
        """llm.py logs success at INFO level with duration_ms."""
        import json as _json

        import httpx

        from open_brain.data_layer.llm import LlmMessage, _call_anthropic

        body = _json.dumps({"content": [{"type": "text", "text": "ok"}]})
        response = httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/json"}
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=response)

        class FakeClient:
            async def __aenter__(self):
                return mock_client

            async def __aexit__(self, *args):
                pass

        with (
            patch("httpx.AsyncClient", return_value=FakeClient()),
            caplog.at_level(logging.INFO, logger="open_brain.data_layer.llm"),
        ):
            await _call_anthropic(
                [LlmMessage(role="user", content="test")],
                model="claude-haiku-4-5",
                max_tokens=100,
            )
        info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("duration_ms" in m for m in info_msgs), (
            f"Expected duration_ms in INFO log, got: {info_msgs}"
        )

    @pytest.mark.asyncio
    async def test_llm_slow_warning_emitted(self, caplog):
        """AK2: llm.py emits WARNING when duration_ms > 5000."""
        import json as _json

        import httpx

        from open_brain.data_layer.llm import LlmMessage, _call_anthropic

        body = _json.dumps({"content": [{"type": "text", "text": "response"}]})
        fast_response = httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/json"}
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fast_response)

        class FakeClient:
            async def __aenter__(self):
                return mock_client

            async def __aexit__(self, *args):
                pass

        call_count = [0]

        def mock_monotonic():
            call_count[0] += 1
            if call_count[0] == 1:
                return 0.0
            return 5.1  # 5100ms — triggers WARNING

        with (
            patch("httpx.AsyncClient", return_value=FakeClient()),
            patch("open_brain.data_layer.llm.time") as mock_time,
            caplog.at_level(logging.WARNING, logger="open_brain.data_layer.llm"),
        ):
            mock_time.monotonic.side_effect = mock_monotonic
            await _call_anthropic(
                [LlmMessage(role="user", content="test")],
                model="claude-haiku-4-5",
                max_tokens=100,
            )

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records, (
            f"Expected WARNING for slow LLM call, got: {[r.message for r in caplog.records]}"
        )
        assert any("duration_ms" in r.message for r in warning_records), (
            f"Expected duration_ms in WARNING, got: {[r.message for r in warning_records]}"
        )

    @pytest.mark.asyncio
    async def test_llm_error_logged_before_raise(self, caplog):
        """llm.py logs ERROR on HTTP failure before raising RuntimeError."""
        import httpx

        from open_brain.data_layer.llm import LlmMessage, _call_anthropic

        error_response = httpx.Response(500, content=b"Internal Server Error")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=error_response)

        class FakeClient:
            async def __aenter__(self):
                return mock_client

            async def __aexit__(self, *args):
                pass

        with (
            patch("httpx.AsyncClient", return_value=FakeClient()),
            caplog.at_level(logging.ERROR, logger="open_brain.data_layer.llm"),
        ):
            with pytest.raises(RuntimeError):
                await _call_anthropic(
                    [LlmMessage(role="user", content="test")],
                    model="claude-haiku-4-5",
                    max_tokens=100,
                )

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "Expected ERROR log record before RuntimeError raise"

    @pytest.mark.asyncio
    async def test_openrouter_success_logged_at_info(self, caplog):
        """llm.py logs success at INFO for OpenRouter too."""
        import json as _json

        import httpx

        from open_brain.data_layer.llm import LlmMessage, _call_openrouter

        body = _json.dumps({"choices": [{"message": {"content": "ok"}}]})
        response = httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/json"}
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=response)

        class FakeClient:
            async def __aenter__(self):
                return mock_client

            async def __aexit__(self, *args):
                pass

        with (
            patch("httpx.AsyncClient", return_value=FakeClient()),
            patch(
                "open_brain.data_layer.llm.get_config",
                return_value=type(
                    "C", (), {"OPENROUTER_API_KEY": "test-key", "LLM_MODEL": "m", "LLM_PROVIDER": "openrouter", "OPENROUTER_DATA_COLLECTION": "deny", "OPENROUTER_PROVIDER_ORDER": None}
                )(),
            ),
            caplog.at_level(logging.INFO, logger="open_brain.data_layer.llm"),
        ):
            await _call_openrouter(
                [LlmMessage(role="user", content="test")],
                model="test-model",
                max_tokens=100,
            )
        info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("duration_ms" in m for m in info_msgs), (
            f"Expected duration_ms in INFO log for openrouter, got: {info_msgs}"
        )
