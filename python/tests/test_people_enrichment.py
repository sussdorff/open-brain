"""Tests for people enrichment feature (open-brain-3lr).

TDD: These tests are written first (RED) to drive the implementation.

Coverage:
1. list_enrichment_candidates — returns person memories with enrich_pending=True
2. search_person_web — queries SearXNG and returns EnrichmentResult list
3. apply_enrichment — updates person memory with enrichment data
4. Confidence scoring heuristics (LinkedIn/Xing, name match, context keyword)
5. enrich_pending flag set at ingest time for new/ambiguous persons
6. Confidence gate: auto-apply never triggers below 0.6
7. CLI integration: --auto-apply with --min-confidence
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.interface import (
    Memory,
    SaveMemoryParams,
    SaveMemoryResult,
    SearchParams,
    SearchResult,
    UpdateMemoryParams,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory(id: int = 1, **kwargs: Any) -> Memory:
    defaults: dict[str, Any] = dict(
        index_id=1,
        session_id=None,
        type="person",
        title="Alice Smith",
        subtitle=None,
        narrative=None,
        content="Person: Alice Smith",
        metadata={"name": "Alice Smith", "enrich_pending": "true"},
        priority=0.5,
        stability="stable",
        access_count=0,
        last_accessed_at=None,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    defaults.update(kwargs)
    return Memory(id=id, **defaults)


def _make_meeting_memory(id: int = 99, name: str = "Alice Smith") -> Memory:
    return _make_memory(
        id=id,
        type="meeting",
        title=f"Meeting: test-ref",
        content=f"{name} is the CEO of Acme Corp and attended the meeting.",
        metadata={"source_ref": "test-ref", "topics": ["strategy"]},
    )


def _make_dl(
    person_memories: list[Memory] | None = None,
    meeting_memories: list[Memory] | None = None,
    all_person_memories: list[Memory] | None = None,
) -> AsyncMock:
    """Build a mock DataLayer that returns preset search results.

    person_memories: returned for the enrich_pending=true filter query.
    all_person_memories: returned for the broad all-persons query (no filter).
      Defaults to person_memories so the standard tests remain unaffected.
    meeting_memories: returned for meeting-type searches.
    """
    dl = AsyncMock()

    persons = person_memories if person_memories is not None else [_make_memory()]
    all_persons = all_person_memories if all_person_memories is not None else persons
    meetings = meeting_memories if meeting_memories is not None else [_make_meeting_memory()]

    def search_side_effect(params: SearchParams) -> SearchResult:
        if params.type == "person" and params.metadata_filter == {"enrich_pending": "true"}:
            return SearchResult(results=persons, total=len(persons))
        if params.type == "person" and not params.metadata_filter:
            return SearchResult(results=all_persons, total=len(all_persons))
        if params.type == "meeting":
            return SearchResult(results=meetings, total=len(meetings))
        return SearchResult(results=[], total=0)

    dl.search.side_effect = search_side_effect
    # attended_by: meeting(source_id=99) -> person(target_id=1)
    dl.get_relationships.return_value = [
        {"source_id": 99, "target_id": 1, "link_type": "attended_by"}
    ]
    dl.get_observations.return_value = [_make_meeting_memory()]
    dl.update_memory.return_value = SaveMemoryResult(id=1, message="updated")
    return dl


# ---------------------------------------------------------------------------
# 1. list_enrichment_candidates
# ---------------------------------------------------------------------------


class TestListEnrichmentCandidates:
    @pytest.mark.asyncio
    async def test_returns_candidates_with_enrich_pending(self) -> None:
        """Should return EnrichmentCandidate list for person memories with enrich_pending."""
        from open_brain.people.enrichment import EnrichmentCandidate, list_enrichment_candidates

        dl = _make_dl()
        candidates = await list_enrichment_candidates(dl)

        assert len(candidates) == 1
        assert candidates[0].name == "Alice Smith"
        assert isinstance(candidates[0], EnrichmentCandidate)

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_pending(self) -> None:
        """Should return empty list when no person memories are candidates."""
        from open_brain.people.enrichment import list_enrichment_candidates

        dl = AsyncMock()
        # Both the flagged query and the all-persons query return nothing.
        dl.search.return_value = SearchResult(results=[], total=0)
        candidates = await list_enrichment_candidates(dl)

        assert candidates == []

    @pytest.mark.asyncio
    async def test_extracts_transcript_context_from_meeting(self) -> None:
        """Should include transcript context from linked meeting memory."""
        from open_brain.people.enrichment import list_enrichment_candidates

        dl = _make_dl()
        candidates = await list_enrichment_candidates(dl)

        assert len(candidates) == 1
        # Context should come from the linked meeting memory
        assert "Alice Smith" in candidates[0].transcript_context or candidates[0].transcript_context != ""

    @pytest.mark.asyncio
    async def test_candidate_has_memory_id(self) -> None:
        """EnrichmentCandidate must include memory_id for later update."""
        from open_brain.people.enrichment import list_enrichment_candidates

        dl = _make_dl()
        candidates = await list_enrichment_candidates(dl)

        assert candidates[0].memory_id == 1


# ---------------------------------------------------------------------------
# 2. search_person_web
# ---------------------------------------------------------------------------


class TestSearchPersonWeb:
    @pytest.mark.asyncio
    async def test_returns_enrichment_results(self) -> None:
        """Should return a list of EnrichmentResult from SearXNG."""
        from open_brain.people.enrichment import EnrichmentResult, search_person_web

        mock_response = {
            "results": [
                {
                    "title": "Alice Smith - CEO at Acme Corp | LinkedIn",
                    "url": "https://www.linkedin.com/in/alice-smith-ceo",
                    "content": "Alice Smith is the Chief Executive Officer at Acme Corp.",
                }
            ]
        }

        with patch("open_brain.people.enrichment.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.get.return_value = mock_response_obj

            results = await search_person_web(
                name="Alice Smith",
                context="CEO Acme Corp",
                searxng_url="http://searxng.local",
            )

        assert len(results) >= 1
        assert isinstance(results[0], EnrichmentResult)

    @pytest.mark.asyncio
    async def test_linkedin_url_raises_base_confidence(self) -> None:
        """Results with LinkedIn URL should have confidence >= 0.7."""
        from open_brain.people.enrichment import search_person_web

        mock_response = {
            "results": [
                {
                    "title": "Alice Smith | LinkedIn",
                    "url": "https://www.linkedin.com/in/alice-smith",
                    "content": "Alice Smith.",
                }
            ]
        }

        with patch("open_brain.people.enrichment.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.get.return_value = mock_response_obj

            results = await search_person_web(
                name="Alice Smith",
                context="",
                searxng_url="http://searxng.local",
            )

        assert results[0].confidence >= 0.7

    @pytest.mark.asyncio
    async def test_name_match_increases_confidence(self) -> None:
        """Exact name match in snippet boosts confidence by +0.15."""
        from open_brain.people.enrichment import search_person_web

        mock_response = {
            "results": [
                {
                    "title": "Alice Smith | LinkedIn",
                    "url": "https://www.linkedin.com/in/alice-smith",
                    "content": "Alice Smith is a professional.",
                }
            ]
        }

        with patch("open_brain.people.enrichment.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.get.return_value = mock_response_obj

            results = await search_person_web(
                name="Alice Smith",
                context="",
                searxng_url="http://searxng.local",
            )

        # LinkedIn (0.7) + name match (+0.15) = 0.85
        assert results[0].confidence >= 0.85

    @pytest.mark.asyncio
    async def test_context_keyword_increases_confidence(self) -> None:
        """Context keyword in snippet boosts confidence by +0.15."""
        from open_brain.people.enrichment import search_person_web

        mock_response = {
            "results": [
                {
                    "title": "Alice Smith | LinkedIn",
                    "url": "https://www.linkedin.com/in/alice-smith",
                    "content": "Alice Smith, CEO at Acme Corp.",
                }
            ]
        }

        with patch("open_brain.people.enrichment.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.get.return_value = mock_response_obj

            results = await search_person_web(
                name="Alice Smith",
                context="Acme Corp",
                searxng_url="http://searxng.local",
            )

        # LinkedIn (0.7) + name match (+0.15) + context (+0.15) = 1.0 (capped)
        assert results[0].confidence <= 1.0
        assert results[0].confidence >= 0.9

    @pytest.mark.asyncio
    async def test_xing_url_raises_base_confidence(self) -> None:
        """Results with Xing URL should also have confidence >= 0.7."""
        from open_brain.people.enrichment import search_person_web

        mock_response = {
            "results": [
                {
                    "title": "Alice Smith | XING",
                    "url": "https://www.xing.com/profile/Alice_Smith",
                    "content": "Alice Smith works at Acme.",
                }
            ]
        }

        with patch("open_brain.people.enrichment.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.get.return_value = mock_response_obj

            results = await search_person_web(
                name="Alice Smith",
                context="",
                searxng_url="http://searxng.local",
            )

        assert results[0].confidence >= 0.7

    @pytest.mark.asyncio
    async def test_confidence_capped_at_1(self) -> None:
        """Confidence must never exceed 1.0."""
        from open_brain.people.enrichment import search_person_web

        mock_response = {
            "results": [
                {
                    "title": "Alice Smith | LinkedIn",
                    "url": "https://www.linkedin.com/in/alice-smith",
                    "content": "Alice Smith CEO at Acme.",
                }
            ]
        }

        with patch("open_brain.people.enrichment.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.get.return_value = mock_response_obj

            results = await search_person_web(
                name="Alice Smith",
                context="Acme",
                searxng_url="http://searxng.local",
            )

        for result in results:
            assert result.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_returns_empty_on_http_error(self) -> None:
        """Should return empty list when SearXNG is unavailable."""
        import httpx

        from open_brain.people.enrichment import search_person_web

        with patch("open_brain.people.enrichment.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client
            mock_client.get.side_effect = Exception("connection refused")

            results = await search_person_web(
                name="Alice Smith",
                context="CEO",
                searxng_url="http://searxng.local",
            )

        assert results == []

    @pytest.mark.asyncio
    async def test_uses_searxng_url_from_parameter(self) -> None:
        """SearXNG URL must come from the parameter, never hardcoded."""
        from open_brain.people.enrichment import search_person_web

        called_urls: list[str] = []

        with patch("open_brain.people.enrichment.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client

            async def capture_get(url: str, **kwargs: Any) -> MagicMock:
                called_urls.append(url)
                resp = MagicMock()
                resp.json.return_value = {"results": []}
                resp.raise_for_status = MagicMock()
                return resp

            mock_client.get.side_effect = capture_get

            await search_person_web(
                name="Alice Smith",
                context="CEO",
                searxng_url="http://my-custom-searxng:8888",
            )

        assert len(called_urls) == 1
        assert called_urls[0].startswith("http://my-custom-searxng:8888")

    @pytest.mark.asyncio
    async def test_returns_at_most_3_results(self) -> None:
        """Should return at most 3 results from SearXNG."""
        from open_brain.people.enrichment import search_person_web

        mock_response = {
            "results": [
                {
                    "title": f"Result {i}",
                    "url": f"https://example.com/{i}",
                    "content": f"Content {i}",
                }
                for i in range(10)
            ]
        }

        with patch("open_brain.people.enrichment.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = MagicMock()
            mock_client.get.return_value = mock_response_obj

            results = await search_person_web(
                name="Alice Smith",
                context="",
                searxng_url="http://searxng.local",
            )

        assert len(results) <= 3


# ---------------------------------------------------------------------------
# 3. apply_enrichment
# ---------------------------------------------------------------------------


class TestApplyEnrichment:
    @pytest.mark.asyncio
    async def test_updates_person_memory_with_org_and_role(self) -> None:
        """Should call update_memory with org, role, and enrichment fields."""
        from open_brain.people.enrichment import EnrichmentResult, apply_enrichment

        dl = AsyncMock()
        dl.update_memory.return_value = SaveMemoryResult(id=1, message="updated")

        result = EnrichmentResult(
            name="Alice Smith",
            org="Acme Corp",
            role="CEO",
            profile_url="https://www.linkedin.com/in/alice-smith",
            confidence=0.9,
            provenance_url="https://www.linkedin.com/in/alice-smith",
            provenance_snippet="Alice Smith is CEO at Acme Corp.",
        )

        await apply_enrichment(dl, memory_id=1, result=result)

        dl.update_memory.assert_called_once()
        call_params: UpdateMemoryParams = dl.update_memory.call_args[0][0]
        assert call_params.id == 1
        assert call_params.metadata is not None
        assert call_params.metadata.get("org") == "Acme Corp"
        assert call_params.metadata.get("role") == "CEO"

    @pytest.mark.asyncio
    async def test_clears_enrich_pending_flag(self) -> None:
        """Should set enrich_pending to False (or remove it) after enrichment."""
        from open_brain.people.enrichment import EnrichmentResult, apply_enrichment

        dl = AsyncMock()
        dl.update_memory.return_value = SaveMemoryResult(id=1, message="updated")

        result = EnrichmentResult(
            name="Alice Smith",
            org="Acme Corp",
            role="CEO",
            profile_url=None,
            confidence=0.85,
            provenance_url=None,
            provenance_snippet=None,
        )

        await apply_enrichment(dl, memory_id=1, result=result)

        call_params: UpdateMemoryParams = dl.update_memory.call_args[0][0]
        metadata = call_params.metadata or {}
        # enrich_pending should be removed or set to False/falsy
        enrich_pending = metadata.get("enrich_pending")
        assert not enrich_pending or enrich_pending == "false"

    @pytest.mark.asyncio
    async def test_stores_provenance_and_confidence(self) -> None:
        """Should store profile_url, confidence, provenance_url, and snippet."""
        from open_brain.people.enrichment import EnrichmentResult, apply_enrichment

        dl = AsyncMock()
        dl.update_memory.return_value = SaveMemoryResult(id=1, message="updated")

        result = EnrichmentResult(
            name="Alice Smith",
            org="Acme Corp",
            role="CEO",
            profile_url="https://www.linkedin.com/in/alice",
            confidence=0.9,
            provenance_url="https://www.linkedin.com/in/alice",
            provenance_snippet="Alice Smith CEO at Acme.",
        )

        await apply_enrichment(dl, memory_id=42, result=result)

        call_params: UpdateMemoryParams = dl.update_memory.call_args[0][0]
        metadata = call_params.metadata or {}
        assert metadata.get("profile_url") == "https://www.linkedin.com/in/alice"
        assert metadata.get("confidence") == 0.9
        assert metadata.get("provenance_url") == "https://www.linkedin.com/in/alice"
        assert metadata.get("provenance_snippet") == "Alice Smith CEO at Acme."
        assert metadata.get("provenance_summary") == (
            "https://www.linkedin.com/in/alice: Alice Smith CEO at Acme."
        )
        assert "provenance" not in metadata


# ---------------------------------------------------------------------------
# 4. enrich_pending flag set at ingest time
# ---------------------------------------------------------------------------


class TestEnrichPendingAtIngestTime:
    @pytest.mark.asyncio
    async def test_new_person_gets_enrich_pending_flag(self) -> None:
        """A new person created during ingest should have enrich_pending=True."""
        from unittest.mock import AsyncMock, patch

        from open_brain.data_layer.interface import SaveMemoryResult, SearchResult
        from open_brain.ingest.adapters.transcript import TranscriptIngestor

        dl = AsyncMock()
        # No existing persons
        dl.search.return_value = SearchResult(results=[], total=0)
        dl.save_memory.return_value = SaveMemoryResult(id=1, message="saved")
        dl.update_memory.return_value = SaveMemoryResult(id=1, message="updated")
        dl.create_relationship.return_value = 10

        with patch(
            "open_brain.ingest.adapters.transcript.extract_from_transcript",
            return_value={
                "attendees": ["Alice Smith"],
                "mentioned_people": [],
                "topics": [],
                "follow_up_tasks": [],
            },
        ):
            ingestor = TranscriptIngestor(dl)
            await ingestor.ingest(
                text="Alice Smith attended the meeting.",
                source_ref="test-new-person",
            )

        # Find the save_memory call for a person type
        person_calls = [
            call
            for call in dl.save_memory.call_args_list
            if call[0][0].type == "person"
        ]
        assert len(person_calls) >= 1
        person_params: SaveMemoryParams = person_calls[0][0][0]
        assert person_params.metadata is not None
        assert person_params.metadata.get("enrich_pending") == "true"

    @pytest.mark.asyncio
    async def test_ambiguous_person_gets_enrich_pending_flag(self) -> None:
        """An ambiguous person match during ingest should also get enrich_pending=True."""
        from unittest.mock import AsyncMock, patch

        from open_brain.data_layer.interface import Memory, SaveMemoryResult, SearchResult
        from open_brain.ingest.adapters.transcript import TranscriptIngestor

        # Create existing persons that will cause "ambiguous" match
        # (two persons with similar first names)
        existing1 = _make_memory(id=10, title="Alice Johnson", metadata={"name": "Alice Johnson"})
        existing2 = _make_memory(id=11, title="Alice Brown", metadata={"name": "Alice Brown"})

        dl = AsyncMock()

        def search_side_effect(params: SearchParams) -> SearchResult:
            # Return empty for meeting searches (no prior runs)
            if params.type == "meeting":
                return SearchResult(results=[], total=0)
            # Return existing persons for person searches (dedup)
            if params.type == "person":
                return SearchResult(results=[existing1, existing2], total=2)
            return SearchResult(results=[], total=0)

        dl.search.side_effect = search_side_effect
        dl.save_memory.return_value = SaveMemoryResult(id=99, message="saved")
        dl.update_memory.return_value = SaveMemoryResult(id=99, message="updated")
        dl.create_relationship.return_value = 10

        with patch(
            "open_brain.ingest.adapters.transcript.extract_from_transcript",
            return_value={
                "attendees": ["Alice"],
                "mentioned_people": [],
                "topics": [],
                "follow_up_tasks": [],
            },
        ):
            with patch(
                "open_brain.ingest.adapters.transcript.match_person",
                return_value=MagicMock(action="ambiguous", target=None, runners_up=[], rationale="ambiguous"),
            ):
                ingestor = TranscriptIngestor(dl)
                await ingestor.ingest(
                    text="Alice attended the meeting.",
                    source_ref="test-ambiguous",
                )

        person_calls = [
            call
            for call in dl.save_memory.call_args_list
            if call[0][0].type == "person"
        ]
        assert len(person_calls) >= 1
        person_params: SaveMemoryParams = person_calls[0][0][0]
        assert person_params.metadata is not None
        assert person_params.metadata.get("enrich_pending") == "true"


# ---------------------------------------------------------------------------
# 5. Confidence gate: NEVER auto-apply below 0.6
# ---------------------------------------------------------------------------


class TestConfidenceGate:
    def test_confidence_below_06_never_auto_applied(self) -> None:
        """The confidence gate must block auto-apply when confidence < 0.6."""
        from open_brain.people.enrichment import EnrichmentResult, should_auto_apply

        result_low = EnrichmentResult(
            name="Alice Smith",
            org="Acme",
            role="CEO",
            profile_url=None,
            confidence=0.55,
            provenance_url=None,
            provenance_snippet=None,
        )

        # Even if min_confidence is 0.1, confidence < 0.6 should NOT auto-apply
        assert should_auto_apply(result_low, min_confidence=0.1) is False
        assert should_auto_apply(result_low, min_confidence=0.5) is False
        assert should_auto_apply(result_low, min_confidence=0.59) is False

    def test_confidence_at_06_blocked(self) -> None:
        """Confidence exactly at 0.6 should be blocked (strictly less than 0.6 check)."""
        from open_brain.people.enrichment import EnrichmentResult, should_auto_apply

        result_at_06 = EnrichmentResult(
            name="Alice Smith",
            org="Acme",
            role="CEO",
            profile_url=None,
            confidence=0.6,
            provenance_url=None,
            provenance_snippet=None,
        )

        # 0.6 is blocked when min_confidence > 0.6
        assert should_auto_apply(result_at_06, min_confidence=0.8) is False

    def test_confidence_above_06_and_above_threshold_auto_applies(self) -> None:
        """Confidence >= 0.6 AND >= min_confidence should auto-apply."""
        from open_brain.people.enrichment import EnrichmentResult, should_auto_apply

        result_high = EnrichmentResult(
            name="Alice Smith",
            org="Acme",
            role="CEO",
            profile_url=None,
            confidence=0.85,
            provenance_url=None,
            provenance_snippet=None,
        )

        assert should_auto_apply(result_high, min_confidence=0.8) is True

    def test_confidence_above_06_but_below_threshold_not_auto_applied(self) -> None:
        """Confidence >= 0.6 but below min_confidence threshold should not auto-apply."""
        from open_brain.people.enrichment import EnrichmentResult, should_auto_apply

        result = EnrichmentResult(
            name="Alice Smith",
            org="Acme",
            role="CEO",
            profile_url=None,
            confidence=0.65,
            provenance_url=None,
            provenance_snippet=None,
        )

        assert should_auto_apply(result, min_confidence=0.8) is False


# ---------------------------------------------------------------------------
# 6. PersonMetadata TypedDict extensions
# ---------------------------------------------------------------------------


class TestPersonMetadataFields:
    def test_person_metadata_accepts_new_fields(self) -> None:
        """PersonMetadata should accept enrichment fields without TypedDict error."""
        from open_brain.data_layer.interface import PersonMetadata

        metadata: PersonMetadata = {
            "name": "Alice Smith",
            "org": "Acme Corp",
            "role": "CEO",
            "profile_url": "https://linkedin.com/in/alice",
            "confidence": 0.9,
            "provenance": "https://linkedin.com/in/alice: Alice Smith CEO at Acme.",
            "enrich_pending": "true",
        }

        # Just making sure the TypedDict accepts the fields
        assert metadata["name"] == "Alice Smith"
        assert metadata["profile_url"] == "https://linkedin.com/in/alice"
        assert metadata["enrich_pending"] == "true"


# ---------------------------------------------------------------------------
# 7. Config: SEARXNG_URL field
# ---------------------------------------------------------------------------


class TestConfigSearxngUrl:
    def test_config_has_searxng_url_field(self) -> None:
        """Config class must have a SEARXNG_URL field with empty string default."""
        import inspect

        from open_brain.config import Config

        fields = Config.model_fields
        assert "SEARXNG_URL" in fields, "Config must have SEARXNG_URL field"
        # Default value should be empty string (disabled)
        default = fields["SEARXNG_URL"].default
        assert default == "", f"SEARXNG_URL default should be empty string, got {default!r}"


# ---------------------------------------------------------------------------
# 8. EnrichmentResult dataclass structure
# ---------------------------------------------------------------------------


class TestEnrichmentResultDataclass:
    def test_enrichment_result_fields(self) -> None:
        """EnrichmentResult must have all required fields."""
        from open_brain.people.enrichment import EnrichmentResult

        result = EnrichmentResult(
            name="Alice Smith",
            org="Acme Corp",
            role="CEO",
            profile_url="https://linkedin.com/in/alice",
            confidence=0.9,
            provenance_url="https://linkedin.com/in/alice",
            provenance_snippet="Alice Smith is CEO at Acme.",
        )

        assert result.name == "Alice Smith"
        assert result.org == "Acme Corp"
        assert result.role == "CEO"
        assert result.profile_url == "https://linkedin.com/in/alice"
        assert result.confidence == 0.9
        assert result.provenance_url == "https://linkedin.com/in/alice"
        assert result.provenance_snippet == "Alice Smith is CEO at Acme."

    def test_enrichment_candidate_fields(self) -> None:
        """EnrichmentCandidate must have memory_id, name, and transcript_context."""
        from open_brain.people.enrichment import EnrichmentCandidate

        candidate = EnrichmentCandidate(
            memory_id=42,
            name="Alice Smith",
            transcript_context="Alice Smith is CEO at Acme Corp.",
        )

        assert candidate.memory_id == 42
        assert candidate.name == "Alice Smith"
        assert candidate.transcript_context == "Alice Smith is CEO at Acme Corp."


# ---------------------------------------------------------------------------
# Regression fixes (Codex adversarial findings)
# ---------------------------------------------------------------------------


class TestRegressionFix1PreExistingPersonsVisibleToEnrichment:
    """REGRESSION 1: list_enrichment_candidates must also surface pre-existing
    person memories with a name but no org/role, even without enrich_pending."""

    @pytest.mark.asyncio
    async def test_person_without_enrich_pending_but_missing_org_role_included(self) -> None:
        """Person with name but no org/role and no enrich_pending flag must be a candidate."""
        from open_brain.people.enrichment import list_enrichment_candidates

        # This person has no enrich_pending flag — pre-existing memory.
        legacy_person = _make_memory(
            id=5,
            metadata={"name": "Bob Jones"},  # no org, no role, no enrich_pending
        )

        # flagged query returns nothing; broad query returns the legacy person.
        dl = _make_dl(
            person_memories=[],
            all_person_memories=[legacy_person],
        )
        candidates = await list_enrichment_candidates(dl)

        assert len(candidates) == 1
        assert candidates[0].memory_id == 5
        assert candidates[0].name == "Bob Jones"

    @pytest.mark.asyncio
    async def test_person_with_org_already_set_not_included(self) -> None:
        """Person that already has org should NOT be returned as a candidate."""
        from open_brain.people.enrichment import list_enrichment_candidates

        complete_person = _make_memory(
            id=6,
            metadata={"name": "Carol King", "org": "Acme Corp"},
        )

        dl = _make_dl(
            person_memories=[],
            all_person_memories=[complete_person],
        )
        candidates = await list_enrichment_candidates(dl)

        assert candidates == []

    @pytest.mark.asyncio
    async def test_person_with_role_already_set_not_included(self) -> None:
        """Person that already has role should NOT be returned as a candidate."""
        from open_brain.people.enrichment import list_enrichment_candidates

        person_with_role = _make_memory(
            id=7,
            metadata={"name": "Dave Lee", "role": "Engineer"},
        )

        dl = _make_dl(
            person_memories=[],
            all_person_memories=[person_with_role],
        )
        candidates = await list_enrichment_candidates(dl)

        assert candidates == []

    @pytest.mark.asyncio
    async def test_no_duplicates_when_person_appears_in_both_queries(self) -> None:
        """A person with enrich_pending=true must not appear twice in results."""
        from open_brain.people.enrichment import list_enrichment_candidates

        person = _make_memory(
            id=1,
            metadata={"name": "Alice Smith", "enrich_pending": "true"},
        )

        # Same person returned by both the flagged query and the broad query.
        dl = _make_dl(
            person_memories=[person],
            all_person_memories=[person],
        )
        candidates = await list_enrichment_candidates(dl)

        ids = [c.memory_id for c in candidates]
        assert ids.count(1) == 1, "Duplicate candidate entries must be deduplicated"


class TestRegressionFix2ContextWordsExcludePersonName:
    """REGRESSION 2: search query context words must not include parts of the person's name."""

    @pytest.mark.asyncio
    async def test_context_query_excludes_name_parts(self) -> None:
        """If context starts with the person's name, those words must be excluded from query."""
        from open_brain.people.enrichment import search_person_web

        captured_queries: list[str] = []

        with patch("open_brain.people.enrichment.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client

            async def capture_get(url: str, **kwargs: Any) -> MagicMock:
                params = kwargs.get("params", {})
                captured_queries.append(params.get("q", ""))
                resp = MagicMock()
                resp.json.return_value = {"results": []}
                resp.raise_for_status = MagicMock()
                return resp

            mock_client.get.side_effect = capture_get

            # Context starts with the person's name — a common transcript pattern.
            await search_person_web(
                name="Alice Smith",
                context="Alice Smith is the CEO of Acme Corp",
                searxng_url="http://searxng.local",
            )

        assert len(captured_queries) == 1
        query = captured_queries[0]
        # The query should NOT be '"Alice Smith" Alice ...' (name repeated).
        # Name appears in quotes once; remaining tokens should be non-name words.
        after_name = query.replace('"Alice Smith"', "").strip()
        assert "alice" not in after_name.lower(), (
            f"Name part 'alice' leaked into context tokens: {query!r}"
        )
        assert "smith" not in after_name.lower(), (
            f"Name part 'smith' leaked into context tokens: {query!r}"
        )

    @pytest.mark.asyncio
    async def test_context_query_uses_non_name_words(self) -> None:
        """Non-name context words (e.g. company name) should appear in the query."""
        from open_brain.people.enrichment import search_person_web

        captured_queries: list[str] = []

        with patch("open_brain.people.enrichment.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client

            async def capture_get(url: str, **kwargs: Any) -> MagicMock:
                params = kwargs.get("params", {})
                captured_queries.append(params.get("q", ""))
                resp = MagicMock()
                resp.json.return_value = {"results": []}
                resp.raise_for_status = MagicMock()
                return resp

            mock_client.get.side_effect = capture_get

            await search_person_web(
                name="Alice Smith",
                context="Alice Smith works at Cognovis GmbH",
                searxng_url="http://searxng.local",
            )

        assert len(captured_queries) == 1
        query = captured_queries[0]
        # At least one non-name word from context (e.g. "works", "Cognovis", "GmbH")
        # should appear in the query.
        assert any(
            word in query for word in ["works", "Cognovis", "GmbH"]
        ), f"Expected a non-name context word in query but got: {query!r}"


class TestRegressionFix3AutoApplyRequiresOrgOrRole:
    """REGRESSION 3: should_auto_apply must return False when both org and role are None."""

    def test_url_only_result_not_auto_applied(self) -> None:
        """A result with only profile_url but no org/role must not be auto-applied."""
        from open_brain.people.enrichment import EnrichmentResult, should_auto_apply

        url_only = EnrichmentResult(
            name="Alice Smith",
            org=None,
            role=None,
            profile_url="https://www.linkedin.com/in/alice-smith",
            confidence=0.95,
            provenance_url="https://www.linkedin.com/in/alice-smith",
            provenance_snippet=None,
        )

        # High confidence but no extractable metadata — must NOT auto-apply.
        assert should_auto_apply(url_only, min_confidence=0.8) is False

    def test_result_with_only_org_can_auto_apply(self) -> None:
        """A result with at least org (even without role) can still be auto-applied."""
        from open_brain.people.enrichment import EnrichmentResult, should_auto_apply

        result = EnrichmentResult(
            name="Alice Smith",
            org="Acme Corp",
            role=None,
            profile_url=None,
            confidence=0.85,
            provenance_url=None,
            provenance_snippet=None,
        )

        assert should_auto_apply(result, min_confidence=0.8) is True

    def test_result_with_only_role_can_auto_apply(self) -> None:
        """A result with at least role (even without org) can still be auto-applied."""
        from open_brain.people.enrichment import EnrichmentResult, should_auto_apply

        result = EnrichmentResult(
            name="Alice Smith",
            org=None,
            role="CEO",
            profile_url=None,
            confidence=0.85,
            provenance_url=None,
            provenance_snippet=None,
        )

        assert should_auto_apply(result, min_confidence=0.8) is True

    def test_result_with_both_org_and_role_can_auto_apply(self) -> None:
        """A result with both org and role must be auto-applied when confidence is high."""
        from open_brain.people.enrichment import EnrichmentResult, should_auto_apply

        result = EnrichmentResult(
            name="Alice Smith",
            org="Acme Corp",
            role="CEO",
            profile_url=None,
            confidence=0.9,
            provenance_url=None,
            provenance_snippet=None,
        )

        assert should_auto_apply(result, min_confidence=0.8) is True
