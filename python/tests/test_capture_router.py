"""Tests for capture_router: LLM classification + template routing in save_memory.

AK coverage:
- AK1: decision text → capture_template=decision + structured fields
- AK2: meeting text → capture_template=meeting + attendees/action_items
- AK3: person context → capture_template=person_context + person/detail
- AK4: pre-structured metadata preserved (bypass when capture_template already set)
- AK5: classification runs concurrently with save (<200ms added latency) — integration
- AK6: unclassifiable text → capture_template=observation (saves normally)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from open_brain.capture_router import classify_and_extract


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _mock_llm(response_dict: dict):
    """Return a patch that makes llm_complete return a JSON-encoded response_dict."""
    return patch(
        "open_brain.capture_router.llm_complete",
        new=AsyncMock(return_value=json.dumps(response_dict)),
    )


# ─── AK1: Decision classification ─────────────────────────────────────────────

class TestDecisionClassification:
    @pytest.mark.asyncio
    async def test_decision_text_returns_decision_template(self):
        """Decision language → capture_template=decision with structured fields."""
        llm_response = {
            "capture_template": "decision",
            "what": "Use PostgreSQL over MySQL",
            "context": "Performance requirements for high-write workloads",
            "owner": "engineering team",
            "alternatives": ["MySQL", "SQLite"],
            "rationale": "Better JSONB support and pg_vector extension availability",
        }
        with _mock_llm(llm_response):
            result = await classify_and_extract(
                "We decided to use PostgreSQL over MySQL due to better JSONB support"
            )
        assert result["capture_template"] == "decision"
        assert "what" in result
        assert "context" in result
        assert "owner" in result
        assert "alternatives" in result
        assert "rationale" in result

    @pytest.mark.asyncio
    async def test_decision_preserves_extracted_fields(self):
        """Decision fields from LLM are preserved as-is."""
        llm_response = {
            "capture_template": "decision",
            "what": "Switch to async architecture",
            "context": "Scalability requirements",
            "owner": "CTO",
            "alternatives": ["threading", "multiprocessing"],
            "rationale": "Better I/O throughput",
        }
        with _mock_llm(llm_response):
            result = await classify_and_extract(
                "Decided to switch to async architecture for better scalability"
            )
        assert result["what"] == "Switch to async architecture"
        assert result["owner"] == "CTO"
        assert result["rationale"] == "Better I/O throughput"


# ─── AK2: Meeting classification ──────────────────────────────────────────────

class TestMeetingClassification:
    @pytest.mark.asyncio
    async def test_meeting_text_returns_meeting_template(self):
        """Meeting/attendee text → capture_template=meeting."""
        llm_response = {
            "capture_template": "meeting",
            "attendees": ["Alice", "Bob", "Charlie"],
            "topic": "Q2 planning",
            "key_points": ["Budget approved", "Timeline confirmed"],
            "action_items": ["Alice to send agenda", "Bob to prepare slides"],
        }
        with _mock_llm(llm_response):
            result = await classify_and_extract(
                "Meeting with Alice, Bob, Charlie about Q2 planning. "
                "Action items: Alice sends agenda, Bob prepares slides."
            )
        assert result["capture_template"] == "meeting"
        assert "attendees" in result
        assert "action_items" in result

    @pytest.mark.asyncio
    async def test_meeting_fields_populated(self):
        """Meeting extraction includes attendees and action_items."""
        llm_response = {
            "capture_template": "meeting",
            "attendees": ["Malte", "Sarah"],
            "topic": "Sprint review",
            "key_points": ["Demo done"],
            "action_items": ["Malte to write docs"],
        }
        with _mock_llm(llm_response):
            result = await classify_and_extract(
                "Sprint review with Malte and Sarah. Malte will write docs."
            )
        assert result["attendees"] == ["Malte", "Sarah"]
        assert "Malte to write docs" in result["action_items"]


# ─── AK3: Person context classification ───────────────────────────────────────

class TestPersonContextClassification:
    @pytest.mark.asyncio
    async def test_person_context_returns_person_context_template(self):
        """Person context text → capture_template=person_context."""
        llm_response = {
            "capture_template": "person_context",
            "person": "Dr. Smith",
            "relationship": "mentor",
            "detail": "Expert in distributed systems, prefers email communication",
        }
        with _mock_llm(llm_response):
            result = await classify_and_extract(
                "Dr. Smith is my mentor. He's an expert in distributed systems "
                "and prefers email over chat."
            )
        assert result["capture_template"] == "person_context"
        assert "person" in result
        assert "detail" in result

    @pytest.mark.asyncio
    async def test_person_name_extracted(self):
        """Person name is correctly extracted."""
        llm_response = {
            "capture_template": "person_context",
            "person": "Anna Mueller",
            "relationship": "colleague",
            "detail": "Frontend lead at Cognovis, knows React and Vue",
        }
        with _mock_llm(llm_response):
            result = await classify_and_extract(
                "Anna Mueller is the frontend lead, she knows React and Vue."
            )
        assert result["person"] == "Anna Mueller"


# ─── AK4: Pre-structured metadata bypass ──────────────────────────────────────

class TestBypassConditions:
    @pytest.mark.asyncio
    async def test_bypass_when_capture_template_already_set(self):
        """When metadata.capture_template is set, skip classification entirely."""
        existing_metadata = {
            "capture_template": "decision",
            "what": "Pre-existing decision",
            "custom_field": "preserved",
        }
        # llm_complete should NEVER be called
        with patch(
            "open_brain.capture_router.llm_complete",
            new=AsyncMock(side_effect=AssertionError("llm_complete must not be called")),
        ):
            result = await classify_and_extract(
                "Some text about a decision",
                existing_metadata=existing_metadata,
            )
        # Returns the existing metadata unchanged
        assert result == existing_metadata

    @pytest.mark.asyncio
    async def test_bypass_preserves_all_existing_fields(self):
        """All existing metadata fields are preserved when bypass triggered."""
        existing_metadata = {
            "capture_template": "meeting",
            "attendees": ["pre-set-attendee"],
            "custom": "value",
        }
        with patch(
            "open_brain.capture_router.llm_complete",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ):
            result = await classify_and_extract(
                "text",
                existing_metadata=existing_metadata,
            )
        assert result["attendees"] == ["pre-set-attendee"]
        assert result["custom"] == "value"

    @pytest.mark.asyncio
    async def test_bypass_for_session_summary_type(self):
        """type=session_summary → skip classification, return existing_metadata unchanged."""
        with patch(
            "open_brain.capture_router.llm_complete",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ):
            result = await classify_and_extract(
                "Session summary: worked on feature X today",
                memory_type="session_summary",
            )
        # Returns empty dict (no existing_metadata) — no capture_template added
        assert result == {}

    @pytest.mark.asyncio
    async def test_bypass_session_summary_preserves_existing_metadata(self):
        """type=session_summary preserves any existing metadata fields unchanged."""
        existing = {"project": "open-brain", "session_ref": "bead-qt9"}
        with patch(
            "open_brain.capture_router.llm_complete",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ):
            result = await classify_and_extract(
                "Session summary: worked on feature X today",
                existing_metadata=existing,
                memory_type="session_summary",
            )
        assert result == existing

    @pytest.mark.asyncio
    async def test_empty_metadata_without_capture_template_triggers_classification(self):
        """Empty metadata (no capture_template) should trigger LLM classification."""
        llm_response = {"capture_template": "observation"}
        call_count = {"n": 0}

        async def mock_llm(*args, **kwargs):
            call_count["n"] += 1
            return json.dumps(llm_response)

        with patch("open_brain.capture_router.llm_complete", new=mock_llm):
            await classify_and_extract("some text", existing_metadata={})

        assert call_count["n"] == 1


# ─── AK6: Unclassifiable → observation ────────────────────────────────────────

class TestObservationFallback:
    @pytest.mark.asyncio
    async def test_unclassifiable_returns_observation(self):
        """LLM returns observation for unclassifiable text."""
        llm_response = {"capture_template": "observation"}
        with _mock_llm(llm_response):
            result = await classify_and_extract(
                "The sky is blue today and it was a nice walk."
            )
        assert result["capture_template"] == "observation"

    @pytest.mark.asyncio
    async def test_observation_saves_normally_no_extra_fields(self):
        """Observation template has no required extra fields."""
        llm_response = {"capture_template": "observation"}
        with _mock_llm(llm_response):
            result = await classify_and_extract("Just a simple note.")
        assert result["capture_template"] == "observation"

    @pytest.mark.asyncio
    async def test_llm_parse_error_falls_back_to_observation(self):
        """If LLM returns invalid JSON, fall back to observation gracefully."""
        with patch(
            "open_brain.capture_router.llm_complete",
            new=AsyncMock(return_value="this is not json at all {broken"),
        ):
            result = await classify_and_extract("Some text that causes LLM failure")
        assert result["capture_template"] == "observation"

    @pytest.mark.asyncio
    async def test_empty_minimal_text_classifies_as_observation(self):
        """Empty or single-word input falls back to observation without error."""
        llm_response = {"capture_template": "observation"}
        for minimal in ("", " ", "ok"):
            with _mock_llm(llm_response):
                result = await classify_and_extract(minimal)
            assert result["capture_template"] == "observation", f"failed for input: {minimal!r}"

    @pytest.mark.asyncio
    async def test_mixed_signals_returns_valid_template(self):
        """Text with markers for multiple templates picks one valid classification."""
        # Text has both decision language and meeting attendees
        mixed_text = (
            "Meeting with Alice and Bob where we decided to adopt PostgreSQL. "
            "Alternatives were MySQL and SQLite. Bob made the final call."
        )
        # Either decision or meeting is acceptable — both are valid dominant templates
        llm_response = {
            "capture_template": "decision",
            "what": "Adopt PostgreSQL",
            "owner": "Bob",
            "alternatives": ["MySQL", "SQLite"],
            "rationale": None,
            "context": "Discussed in meeting with Alice and Bob",
        }
        with _mock_llm(llm_response):
            result = await classify_and_extract(mixed_text)
        assert result["capture_template"] in ("decision", "meeting", "observation")
        assert "capture_template" in result


# ─── AK5: Concurrency integration test ────────────────────────────────────────

class TestServerIntegration:
    """Tests that save_memory integrates with classify_and_extract correctly."""

    @pytest.mark.asyncio
    async def test_save_memory_uses_classification(self):
        """save_memory calls classify_and_extract and merges result into saved memory."""
        from open_brain.data_layer.interface import SaveMemoryResult

        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=99, message="saved")
        mock_dl.update_memory.return_value = SaveMemoryResult(id=99, message="updated")

        classification_result = {
            "capture_template": "decision",
            "what": "Use async",
            "context": "perf",
            "owner": "team",
            "alternatives": [],
            "rationale": "speed",
        }

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch(
                "open_brain.server.classify_and_extract",
                new=AsyncMock(return_value=classification_result),
            ),
        ):
            from open_brain.server import save_memory
            result_json = await save_memory(
                text="We decided to use async for performance",
                type="decision",
            )

        result = json.loads(result_json)
        assert result["id"] == 99
        # update_memory must be called with classification metadata
        mock_dl.update_memory.assert_called_once()
        call_kwargs = mock_dl.update_memory.call_args[0][0]
        assert call_kwargs.metadata["capture_template"] == "decision"

    @pytest.mark.asyncio
    async def test_save_memory_bypasses_when_metadata_template_set(self):
        """save_memory skips update_memory when classify_and_extract returns metadata unchanged."""
        from open_brain.data_layer.interface import SaveMemoryResult

        existing_metadata = {"capture_template": "decision", "what": "pre-set"}

        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=55, message="saved")
        mock_dl.update_memory.return_value = SaveMemoryResult(id=55, message="updated")

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch(
                "open_brain.server.classify_and_extract",
                # Bypass: returns the original metadata dict unchanged
                new=AsyncMock(return_value=existing_metadata),
            ),
        ):
            from open_brain.server import save_memory
            result_json = await save_memory(
                text="A decision was made",
                metadata=existing_metadata,
            )

        result = json.loads(result_json)
        assert result["id"] == 55
        # Guard skips update_memory: classification == metadata (unchanged)
        mock_dl.update_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_save_memory_bypasses_session_summary_type(self):
        """save_memory skips update_memory for type=session_summary (classification returns {})."""
        from open_brain.data_layer.interface import SaveMemoryResult

        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=77, message="saved")

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch(
                "open_brain.server.classify_and_extract",
                # session_summary bypass: returns empty dict (no metadata provided)
                new=AsyncMock(return_value={}),
            ),
        ):
            from open_brain.server import save_memory
            await save_memory(
                text="Today I worked on X and Y...",
                type="session_summary",
            )

        # Guard skips update_memory: classification ({}) == (metadata or {}) ({})
        mock_dl.update_memory.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_classification_latency_under_200ms(self):
        """AK5: Classification adds <200ms latency by running concurrently with save.

        This test requires real API keys and measures actual wall-clock time.
        """
        import asyncio
        import time
        from open_brain.data_layer.interface import SaveMemoryResult
        from open_brain.capture_router import classify_and_extract

        save_duration_ms = 300  # simulate slow save (embedding call)
        classify_duration_ms = 200  # simulate classification

        async def mock_save(params):
            await asyncio.sleep(save_duration_ms / 1000)
            return SaveMemoryResult(id=1, message="saved")

        async def mock_update(params):
            return SaveMemoryResult(id=1, message="updated")

        async def mock_classify(text, existing_metadata=None, memory_type=None):
            await asyncio.sleep(classify_duration_ms / 1000)
            return {"capture_template": "decision", "what": "test"}

        mock_dl = AsyncMock()
        mock_dl.save_memory.side_effect = mock_save
        mock_dl.update_memory.side_effect = mock_update

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", new=mock_classify),
        ):
            from open_brain.server import save_memory
            start = time.monotonic()
            await save_memory(text="We decided to go async")
            elapsed_ms = (time.monotonic() - start) * 1000

        # If concurrent: max(300, 200) + overhead ≈ 300-350ms
        # If sequential: 300 + 200 = 500ms
        # Allow generous overhead: must be under 450ms (concurrent path)
        assert elapsed_ms < 450, (
            f"Total latency {elapsed_ms:.0f}ms suggests sequential execution "
            f"(expected <450ms for concurrent save+classify)"
        )
