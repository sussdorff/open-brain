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

import open_brain.capture_router as capture_router
from open_brain.capture_router import (
    canonical_type_for_capture_template,
    classify_and_extract,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _mock_llm(response_dict: dict):
    """Return a patch that makes llm_complete return a JSON-encoded response_dict."""
    return patch(
        "open_brain.capture_router.llm_complete",
        new=AsyncMock(return_value=json.dumps(response_dict)),
    )


# ─── AK1: Decision classification ─────────────────────────────────────────────

class TestCanonicalVocabularyClassification:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("capture_name", "expected_template", "text", "fields"),
        [
            (
                "project",
                "project",
                "Project Atlas needs an owner, a launch status, and next actions.",
                {
                    "name": "Project Atlas",
                    "status": "planning",
                    "owner": "Malte",
                    "goals": ["Launch the Atlas workspace"],
                    "next_actions": ["Assign technical owner"],
                    "repository": "open-brain",
                },
            ),
            (
                "resource",
                "resource",
                "Resource: the pgvector indexing guide explains HNSW tradeoffs.",
                {
                    "title": "pgvector indexing guide",
                    "url": "https://example.invalid/pgvector",
                    "source_type": "documentation",
                    "author": None,
                    "summary": "Explains HNSW tradeoffs.",
                    "published_at": None,
                },
            ),
            (
                "concept",
                "concept",
                "Concept: reciprocal rank fusion combines vector and text search.",
                {
                    "name": "Reciprocal rank fusion",
                    "domain": "search",
                    "summary": "Combines vector and text search rankings.",
                    "related_concepts": ["hybrid search"],
                },
            ),
            (
                "journal",
                "journal",
                "Journal: today I felt focused while simplifying the memory workflow.",
                {
                    "entry_date": "2026-07-11",
                    "mood": "focused",
                    "themes": ["memory workflow"],
                    "reflection": "Simplifying the workflow helped maintain focus.",
                },
            ),
            (
                "correspondence",
                "correspondence",
                "Email from Alice about the Q3 roadmap needs a follow-up tomorrow.",
                {
                    "with": ["Alice"],
                    "channel": "email",
                    "direction": "inbound",
                    "subject": "Q3 roadmap",
                    "summary": "Alice asked about the Q3 roadmap.",
                    "occurred_at": None,
                    "follow_up_needed": True,
                },
            ),
            (
                "prompt",
                "prompt",
                "Prompt for Codex: summarize the bead and list the tests to run.",
                {
                    "purpose": "Summarize a bead",
                    "prompt_text": "Summarize the bead and list the tests to run.",
                    "target_model": "Codex",
                    "variables": ["bead"],
                    "constraints": ["list tests"],
                },
            ),
            (
                "decision",
                "decision",
                "Decision: use PostgreSQL because pgvector is available.",
                {
                    "what": "Use PostgreSQL",
                    "context": "pgvector availability",
                    "owner": "team",
                    "alternatives": ["SQLite"],
                    "rationale": "pgvector is available.",
                },
            ),
            (
                "meeting",
                "meeting",
                "Meeting with Alice and Bob about Q3 planning.",
                {
                    "attendees": ["Alice", "Bob"],
                    "topic": "Q3 planning",
                    "key_points": ["Planning reviewed"],
                    "action_items": ["Share notes"],
                },
            ),
            (
                "event",
                "event",
                "Event: launch review on 2026-08-01 with the product team.",
                {
                    "what": "Launch review",
                    "when": "2026-08-01T10:00:00",
                    "who": ["product team"],
                    "where": None,
                    "recurrence": None,
                },
            ),
            (
                "person",
                "person_context",
                "Person: Alice is the product owner and prefers concise email updates.",
                {
                    "person": "Alice",
                    "relationship": "product owner",
                    "detail": "Prefers concise email updates.",
                },
            ),
        ],
    )
    async def test_prompt_lists_canonical_template_and_preserves_type_specific_fields(
        self,
        capture_name,
        expected_template,
        text,
        fields,
    ):
        """Representative personal-knowledge captures are routed through canonical templates."""
        llm_response = {"capture_template": expected_template, **fields}
        captured_prompts: list[str] = []

        async def mock_llm(*args, **kwargs):
            captured_prompts.append(kwargs["messages"][0].content)
            return json.dumps(llm_response)

        with patch("open_brain.capture_router.llm_complete", new=mock_llm):
            result = await classify_and_extract(text)

        assert captured_prompts, "classifier did not call the LLM"
        assert f"- {expected_template}:" in captured_prompts[0]
        assert capture_name in captured_prompts[0]
        assert result["capture_template"] == expected_template
        for field_name, value in fields.items():
            assert result[field_name] == value


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


# ─── AC3: Explicit caller type aliases and pre-structured bypass ──────────────

class TestTypeAliasNormalization:
    @pytest.mark.parametrize(
        ("raw_type", "canonical_type"),
        [
            ("note", "journal"),
            ("diary", "journal"),
            ("reference", "resource"),
            ("idea", "concept"),
            ("email", "correspondence"),
            ("letter", "correspondence"),
            ("prompt_template", "prompt"),
        ],
    )
    def test_explicit_type_aliases_normalize_to_canonical_vocabulary(
        self,
        raw_type,
        canonical_type,
    ):
        """Common explicit type aliases normalize to the canonical vocabulary."""
        assert capture_router.normalize_memory_type(raw_type) == canonical_type

    def test_alias_normalization_preserves_prestructured_capture_template_type(self):
        """Pre-structured captures bypass alias normalization and keep caller type."""
        existing_metadata = {
            "capture_template": "journal",
            "entry_date": "2026-07-11",
            "reflection": "Caller already structured this payload.",
        }

        result = capture_router.normalize_memory_type(
            "note",
            existing_metadata=existing_metadata,
        )

        assert result == "note"


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
    async def test_save_memory_normalizes_alias_type_for_unstructured_capture(self):
        """save_memory normalizes explicit aliases before saving unstructured captures."""
        from open_brain.data_layer.interface import SaveMemoryResult

        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=88, message="saved")
        classifier = AsyncMock(return_value={})

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", new=classifier),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            await save_memory(
                text="Diary note about simplifying the capture workflow",
                type="note",
            )

        save_params = mock_dl.save_memory.call_args[0][0]
        assert save_params.type == "journal"
        classifier.assert_awaited_once_with(
            "Diary note about simplifying the capture workflow",
            existing_metadata=None,
            memory_type="journal",
        )

    @pytest.mark.asyncio
    async def test_save_memory_preserves_prestructured_capture_template_type_and_metadata(self):
        """save_memory preserves caller type and metadata when capture_template is already set."""
        from open_brain.data_layer.interface import SaveMemoryResult

        existing_metadata = {
            "capture_template": "journal",
            "entry_date": "2026-07-11",
            "reflection": "Caller already structured this payload.",
        }
        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=89, message="saved")

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
            patch(
                "open_brain.capture_router.llm_complete",
                new=AsyncMock(side_effect=AssertionError("llm_complete must not be called")),
            ),
        ):
            from open_brain.server import save_memory
            await save_memory(
                text="Pre-structured journal entry",
                type="note",
                metadata=existing_metadata,
            )

        save_params = mock_dl.save_memory.call_args[0][0]
        assert save_params.type == "note"
        assert save_params.metadata == existing_metadata
        mock_dl.update_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_raw_project_capture_with_invalid_due_date_warns(self):
        """REGRESSION (Finding 3): classifier-extracted invalid due_date on a raw
        capture (no caller type/metadata) must produce a domain warning."""
        from open_brain.data_layer.interface import SaveMemoryResult

        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=101, message="saved")
        mock_dl.update_memory.return_value = SaveMemoryResult(id=101, message="updated")
        classification = {
            "capture_template": "project",
            "name": "Atlas",
            "due_date": "soon",
        }

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch(
                "open_brain.server.classify_and_extract",
                new=AsyncMock(return_value=classification),
            ),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            result_json = await save_memory(text="Project Atlas is due soon")

        data = json.loads(result_json)
        assert data["id"] == 101
        assert "warning" in data
        assert "due_date" in data["warning"]

    @pytest.mark.asyncio
    async def test_raw_person_capture_validated_as_canonical_person_type(self):
        """REGRESSION (Findings 1+3): a person_context classification is validated
        under the canonical ``person`` vocabulary, so an invalid last_contact warns."""
        from open_brain.data_layer.interface import SaveMemoryResult

        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=102, message="saved")
        mock_dl.update_memory.return_value = SaveMemoryResult(id=102, message="updated")
        classification = {
            "capture_template": "person_context",
            "person": "Alice",
            "last_contact": "last week",
        }

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch(
                "open_brain.server.classify_and_extract",
                new=AsyncMock(return_value=classification),
            ),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            result_json = await save_memory(text="Alice is a colleague I spoke to last week")

        data = json.loads(result_json)
        assert "warning" in data
        assert "last_contact" in data["warning"]

    @pytest.mark.asyncio
    async def test_raw_capture_with_valid_classified_fields_no_warning(self):
        """A raw capture whose classified canonical fields are valid produces no
        spurious warning."""
        from open_brain.data_layer.interface import SaveMemoryResult

        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=103, message="saved")
        mock_dl.update_memory.return_value = SaveMemoryResult(id=103, message="updated")
        classification = {
            "capture_template": "project",
            "name": "Atlas",
            "due_date": "2026-08-01T00:00:00",
        }

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch(
                "open_brain.server.classify_and_extract",
                new=AsyncMock(return_value=classification),
            ),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            result_json = await save_memory(text="Project Atlas launches on schedule")

        data = json.loads(result_json)
        assert "warning" not in data

    @pytest.mark.asyncio
    async def test_prestructured_capture_not_double_validated(self):
        """Pre-structured captures (AC3) use only caller-supplied validation — no
        duplicate warnings for the same field."""
        from open_brain.data_layer.interface import SaveMemoryResult

        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=104, message="saved")
        existing = {"capture_template": "project", "due_date": "soon"}

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
            patch(
                "open_brain.capture_router.llm_complete",
                new=AsyncMock(side_effect=AssertionError("llm_complete must not be called")),
            ),
        ):
            from open_brain.server import save_memory
            result_json = await save_memory(
                text="Pre-structured project payload",
                type="project",
                metadata=existing,
            )

        data = json.loads(result_json)
        # Exactly one due_date warning — caller validation only, classifier path skipped.
        assert data.get("warning", "").count("due_date") == 1

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


# ─── Finding 1: capture_template → canonical type alias ───────────────────────

class TestCanonicalTypeForCaptureTemplate:
    def test_person_context_template_maps_to_canonical_person_type(self):
        """REGRESSION (Finding 1): the historical person_context template maps to
        the canonical ``person`` type for domain-metadata validation."""
        assert canonical_type_for_capture_template("person_context") == "person"

    @pytest.mark.parametrize(
        "template",
        [
            "project",
            "resource",
            "concept",
            "journal",
            "correspondence",
            "prompt",
            "decision",
            "meeting",
            "event",
            "insight",
            "learning",
            "observation",
        ],
    )
    def test_canonical_templates_map_to_themselves(self, template):
        """Templates that already equal their canonical type are returned unchanged."""
        assert canonical_type_for_capture_template(template) == template

    def test_none_maps_to_none(self):
        """None input yields None (no explicit type to validate)."""
        assert canonical_type_for_capture_template(None) is None


# ─── Finding 2: classifier prompt advertises canonical schema fields ──────────

class TestClassifierPromptCanonicalFields:
    @pytest.mark.asyncio
    async def test_prompt_lists_project_due_date_and_prompt_last_used_at(self):
        """REGRESSION (Finding 2): the classifier prompt must advertise
        ``project.due_date`` and ``prompt.last_used_at`` so raw captures reliably
        extract those canonical fields."""
        captured_prompts: list[str] = []

        async def mock_llm(*args, **kwargs):
            captured_prompts.append(kwargs["messages"][0].content)
            return json.dumps({"capture_template": "observation"})

        with patch("open_brain.capture_router.llm_complete", new=mock_llm):
            await classify_and_extract("Some project text")

        assert captured_prompts, "classifier did not call the LLM"
        prompt = captured_prompts[0]

        project_line = next(
            line for line in prompt.splitlines() if line.startswith("- project:")
        )
        assert "due_date" in project_line

        prompt_line = next(
            line for line in prompt.splitlines() if line.startswith("- prompt:")
        )
        assert "last_used_at" in prompt_line


# ─── Phase 10: raw-capture type-column persistence (Design Decision Log 2026-07-11) ──


class TestRawCaptureTypeColumnPersistence:
    """save_memory persists the classified canonical type into the memory's ``type``
    column for raw captures (no caller-supplied capture_template), so type-based
    retrieval / stats / people machinery actually see the classification instead of
    it living only in metadata.capture_template.

    Scoped exception (product decision, 2026-07-11): a raw capture classified as
    ``person_context`` (an incidental / LLM-inferred person mention) MUST keep
    ``type=observation`` and MUST NOT auto-activate the people pipeline — only an
    explicit, caller-structured person capture may set ``type=person``.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "template",
        ["journal", "project", "concept", "correspondence", "decision", "event"],
    )
    async def test_raw_capture_persists_classified_type_to_type_column(self, template):
        """A raw capture classified as a non-person canonical template updates the
        memory's ``type`` column to that template (not the ``observation`` default)."""
        from open_brain.data_layer.interface import SaveMemoryResult

        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=201, message="saved")
        mock_dl.update_memory.return_value = SaveMemoryResult(id=201, message="updated")
        classification = {"capture_template": template}

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch(
                "open_brain.server.classify_and_extract",
                new=AsyncMock(return_value=classification),
            ),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            await save_memory(text=f"A raw {template} capture")

        mock_dl.update_memory.assert_called_once()
        update_params = mock_dl.update_memory.call_args[0][0]
        assert update_params.type == template
        assert update_params.metadata["capture_template"] == template

    @pytest.mark.asyncio
    async def test_raw_person_context_capture_keeps_observation_type(self):
        """A raw capture classified as person_context must NOT be promoted to
        type=person — the ``type`` column is left untouched (stays the observation
        default) while metadata.capture_template=person_context is still recorded.
        Regression guard for the scoped exception / people pipeline."""
        from open_brain.data_layer.interface import SaveMemoryResult

        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=202, message="saved")
        mock_dl.update_memory.return_value = SaveMemoryResult(id=202, message="updated")
        classification = {
            "capture_template": "person_context",
            "person": "Alice",
            "detail": "a colleague",
        }

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch(
                "open_brain.server.classify_and_extract",
                new=AsyncMock(return_value=classification),
            ),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            await save_memory(text="Alice is a colleague I spoke to")

        mock_dl.update_memory.assert_called_once()
        update_params = mock_dl.update_memory.call_args[0][0]
        # type column left at its observation default — NOT promoted to person.
        assert update_params.type is None
        # classification is still recorded in metadata.
        assert update_params.metadata["capture_template"] == "person_context"

    @pytest.mark.asyncio
    async def test_explicit_person_type_capture_unaffected(self):
        """An explicit caller type=person is saved as person and is NOT overwritten
        by the classifier's person_context template (regression guard for AC3 and the
        people pipeline)."""
        from open_brain.data_layer.interface import SaveMemoryResult

        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=203, message="saved")
        mock_dl.update_memory.return_value = SaveMemoryResult(id=203, message="updated")
        classification = {"capture_template": "person_context", "person": "Bob"}

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch(
                "open_brain.server.classify_and_extract",
                new=AsyncMock(return_value=classification),
            ),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            await save_memory(text="Bob is my manager", type="person")

        # Saved with the explicit caller type.
        save_params = mock_dl.save_memory.call_args[0][0]
        assert save_params.type == "person"
        # The post-save update must NOT overwrite the type column (person_context skip).
        mock_dl.update_memory.assert_called_once()
        update_params = mock_dl.update_memory.call_args[0][0]
        assert update_params.type is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caller_type", ["person", "decision"])
    async def test_explicit_type_not_overwritten_by_divergent_classifier(self, caller_type):
        """An explicit caller ``type`` (no caller-supplied capture_template) whose
        classifier result DIVERGES to a different canonical template must NOT have its
        ``type`` column overwritten. Before this bead, classification never touched the
        ``type`` column, so an explicit type was always DB-preserved; the raw-capture
        type-column persistence must stay gated on the caller having supplied NO explicit
        type at all (normalized_type is None). Regression guard for the bead's Scenario:
        an explicit person/decision capture retains its caller-supplied type even when
        the classifier disagrees (e.g. classifies as ``meeting``)."""
        from open_brain.data_layer.interface import SaveMemoryResult

        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=205, message="saved")
        mock_dl.update_memory.return_value = SaveMemoryResult(id=205, message="updated")
        # Classifier diverges from the explicit caller type: returns a non-person,
        # non-matching canonical template (meeting) with type-specific fields.
        classification = {
            "capture_template": "meeting",
            "attendees": ["A", "B"],
            "meeting_date": "2026-07-11",
        }

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch(
                "open_brain.server.classify_and_extract",
                new=AsyncMock(return_value=classification),
            ),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory
            await save_memory(text="Some explicit capture", type=caller_type)

        # Saved with the explicit caller type.
        save_params = mock_dl.save_memory.call_args[0][0]
        assert save_params.type == caller_type
        # Post-save update still writes the classified type-specific metadata,
        # but MUST NOT overwrite the caller's explicit type column.
        mock_dl.update_memory.assert_called_once()
        update_params = mock_dl.update_memory.call_args[0][0]
        assert update_params.type is None, (
            f"explicit caller type={caller_type!r} must not be overwritten by "
            f"divergent classifier template={classification['capture_template']!r}"
        )
        # Classified metadata is still recorded (metadata write unaffected).
        assert update_params.metadata["capture_template"] == "meeting"

    @pytest.mark.asyncio
    async def test_prestructured_person_capture_unaffected(self):
        """A pre-structured capture (caller-supplied capture_template=person) bypasses
        classification entirely; this fix does not touch its type column."""
        from open_brain.data_layer.interface import SaveMemoryResult

        existing_metadata = {"capture_template": "person", "person": "Carol"}
        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=204, message="saved")

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
            patch(
                "open_brain.capture_router.llm_complete",
                new=AsyncMock(side_effect=AssertionError("llm_complete must not be called")),
            ),
        ):
            from open_brain.server import save_memory
            await save_memory(
                text="Carol pre-structured person payload",
                type="person",
                metadata=existing_metadata,
            )

        save_params = mock_dl.save_memory.call_args[0][0]
        assert save_params.type == "person"
        # Bypass path: no post-save update (classification == metadata unchanged).
        mock_dl.update_memory.assert_not_called()


class TestVocabularyConsolidation:
    """AC1: canonical type list + alias maps live in exactly one source
    location (open_brain.data_layer.personal_knowledge_vocabulary), and
    capture_router.py reads from it instead of redefining the literals."""

    def test_alias_maps_are_imported_from_shared_registry(self):
        from open_brain.data_layer.personal_knowledge_vocabulary import (
            CAPTURE_TEMPLATE_TYPE_ALIASES,
            MEMORY_TYPE_ALIASES,
        )

        assert capture_router._MEMORY_TYPE_ALIASES is MEMORY_TYPE_ALIASES
        assert capture_router._CAPTURE_TEMPLATE_TYPE_ALIASES is CAPTURE_TEMPLATE_TYPE_ALIASES

    def test_capture_template_type_aliases_values_are_canonical(self):
        """Every alias target in CAPTURE_TEMPLATE_TYPE_ALIASES must itself be a
        canonical personal-knowledge type (guards against future drift)."""
        from open_brain.data_layer.personal_knowledge_vocabulary import (
            CANONICAL_PERSONAL_KNOWLEDGE_TYPES,
            CAPTURE_TEMPLATE_TYPE_ALIASES,
        )

        for canonical_target in CAPTURE_TEMPLATE_TYPE_ALIASES.values():
            assert canonical_target in CANONICAL_PERSONAL_KNOWLEDGE_TYPES
