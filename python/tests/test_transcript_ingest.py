"""Tests for TranscriptIngestor — cr3.3a.

Acceptance criteria covered:
1. test_ingest_returns_populated_ingest_result — basic ingest with 2 attendees
2. test_ingest_idempotency — second call with same source_ref returns same IDs
3. test_ingest_person_dedup_misspelling — "Allice Brown" deduped against "Alice Brown"
4. test_ingest_empty_transcript_raises — empty text raises ValueError
5. test_ingest_no_attendees — transcript with no named persons
6. test_ingest_follow_up_candidates_not_auto_created — follow_up_candidates populated, no bd calls
7. test_ingest_fixture_transcript — fixture from fixtures/macwhisper/sample_transcript_3person_meeting.txt
8. test_ingest_fixture_dictated_note — dictated note fixture (mentioned people, no attendees)
9. test_ingest_fixture_long_meeting — long meeting fixture (4 attendees + 1 mentioned)
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.interface import SaveMemoryResult, SearchResult
from open_brain.ingest.adapters.transcript import TranscriptIngestor
from open_brain.ingest.models import IngestResult

# ─── Fixtures directory ──────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TRANSCRIPT_3PERSON = FIXTURES_DIR / "macwhisper" / "sample_transcript_3person_meeting.txt"
TRANSCRIPT_DICTATED_NOTE = FIXTURES_DIR / "macwhisper" / "sample_transcript_dictated_note.txt"
TRANSCRIPT_LONG_MEETING = FIXTURES_DIR / "macwhisper" / "sample_transcript_long_meeting.txt"

# ─── Canned LLM responses ────────────────────────────────────────────────────

LLM_2_ATTENDEES = json.dumps({
    "attendees": ["Alice Smith", "Bob Jones"],
    "mentioned_people": [],
    "topics": ["deadline", "project status"],
    "follow_up_tasks": ["follow up on deadline", "send status report"],
})

LLM_WITH_MENTIONED = json.dumps({
    "attendees": ["Alice Smith", "Bob Jones"],
    "mentioned_people": ["Carol White"],
    "topics": ["project planning"],
    "follow_up_tasks": ["schedule review with Carol"],
})

LLM_DICTATED_NOTE = json.dumps({
    "attendees": [],
    "mentioned_people": ["David Park", "Emma Torres", "Robert Whitfield"],
    "topics": ["timeline", "data migration", "vendor comparison"],
    "follow_up_tasks": ["send contract to David", "set up 3-way call", "review vendor sheet"],
})

LLM_LONG_MEETING = json.dumps({
    "attendees": ["Katharina Meier", "Stefan Wolf", "Annika Baum", "Michael Torres"],
    "mentioned_people": ["Jochen Jungbluth"],
    "topics": ["Q1 financials", "Q2 roadmap"],
    "follow_up_tasks": ["review Q1 numbers"],
})

LLM_NO_ATTENDEES = json.dumps({
    "attendees": [],
    "mentioned_people": [],
    "topics": ["general note"],
    "follow_up_tasks": [],
})

LLM_WITH_FOLLOWUPS = json.dumps({
    "attendees": ["Alice Smith"],
    "mentioned_people": [],
    "topics": ["planning"],
    "follow_up_tasks": ["send proposal", "book meeting room", "review budget"],
})

LLM_MISSPELLED = json.dumps({
    "attendees": ["Allice Brown"],
    "mentioned_people": [],
    "topics": ["status update"],
    "follow_up_tasks": [],
})

LLM_3PERSON_FIXTURE = json.dumps({
    "attendees": ["Sarah Hoffmann", "Marcus Berger", "Priya Nair"],
    "mentioned_people": ["Tobias Schreiber", "Dr. Cyrus Alamouti", "Lisa Chen", "Jan Kowalski"],
    "topics": ["API integration", "data mapping", "next steps"],
    "follow_up_tasks": [
        "Marcus: finish error handling by Thursday",
        "Priya: send staging credentials",
        "Sarah: set up meeting with Lisa Chen",
        "Sarah: clarify SSL certificate situation",
    ],
})


# ─── Mock DataLayer helpers ───────────────────────────────────────────────────


def _make_mock_dl(
    save_side_effects: list[SaveMemoryResult] | None = None,
    search_results: list | None = None,
    rel_side_effects: list[int] | None = None,
) -> AsyncMock:
    """Build a mock DataLayer with sensible defaults."""
    dl = AsyncMock()

    if save_side_effects is not None:
        dl.save_memory.side_effect = save_side_effects
    else:
        # Default: return unique IDs starting from 1
        _counter = [0]

        async def _auto_id(*args, **kwargs):
            _counter[0] += 1
            return SaveMemoryResult(id=_counter[0], message="ok")

        dl.save_memory.side_effect = _auto_id

    if search_results is not None:
        dl.search.return_value = SearchResult(results=[], total=0)
        # Allow override per call if needed
    else:
        dl.search.return_value = SearchResult(results=[], total=0)

    if rel_side_effects is not None:
        dl.create_relationship.side_effect = rel_side_effects
    else:
        _rel_counter = [100]

        async def _auto_rel(*args, **kwargs):
            _rel_counter[0] += 1
            return _rel_counter[0]

        dl.create_relationship.side_effect = _auto_rel

    dl.get_relationships.return_value = []
    return dl


# ─── Tests ───────────────────────────────────────────────────────────────────


class TestIngestReturnsPopulatedResult:
    """AC1: basic ingest with 2 attendees returns correct IDs."""

    async def test_ingest_returns_populated_ingest_result(self):
        # Call order: meeting → (person1, interaction1) → (person2, interaction2)
        mock_dl = _make_mock_dl(
            save_side_effects=[
                SaveMemoryResult(id=10, message="ok"),  # meeting memory
                SaveMemoryResult(id=20, message="ok"),  # person1 (Alice Smith)
                SaveMemoryResult(id=21, message="ok"),  # interaction1 (Alice)
                SaveMemoryResult(id=22, message="ok"),  # person2 (Bob Jones)
                SaveMemoryResult(id=23, message="ok"),  # interaction2 (Bob)
            ],
            rel_side_effects=[101, 102],  # attended_by rels
        )

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_2_ATTENDEES

            ingestor = TranscriptIngestor(data_layer=mock_dl)
            result = await ingestor.ingest(
                text="Meeting transcript with Alice and Bob discussing the deadline.",
                source_ref="test-ref-001",
            )

        assert isinstance(result, IngestResult)
        assert result.meeting_memory_id == 10
        assert len(result.person_memory_ids) == 2
        assert 20 in result.person_memory_ids
        assert 22 in result.person_memory_ids
        assert len(result.relationship_ids) == 2
        assert result.run_id  # non-empty UUID string
        # run_id should be a valid UUID format
        import uuid
        uuid.UUID(result.run_id)  # raises if invalid

    async def test_ingest_follow_up_candidates_populated(self):
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_WITH_FOLLOWUPS

            ingestor = TranscriptIngestor(data_layer=mock_dl)
            result = await ingestor.ingest(
                text="Meeting where Alice planned some tasks.",
                source_ref="test-ref-followups",
            )

        assert isinstance(result.follow_up_candidates, list)
        assert len(result.follow_up_candidates) == 3


class TestIngestIdempotency:
    """AC2: second call with same source_ref returns same IDs."""

    async def test_ingest_idempotency(self):
        from open_brain.data_layer.interface import Memory

        mock_dl = AsyncMock()

        # Capture what update_memory persists so the second search can return it
        persisted_metadata: dict = {}

        async def _update_memory(params):
            persisted_metadata.update(params.metadata or {})
            return SaveMemoryResult(id=params.id, message="ok")

        mock_dl.update_memory.side_effect = _update_memory

        # search: first call (idempotency check) returns nothing;
        # subsequent calls return the meeting memory with persisted ingest_result
        search_call_count = [0]

        async def _search(params):
            search_call_count[0] += 1
            # First search call is the idempotency check on the first ingest — no prior run
            if search_call_count[0] == 1:
                return SearchResult(results=[], total=0)
            # After first ingest completes, the second ingest's idempotency check
            # (and person search) must distinguish type
            if params.type == "meeting" and persisted_metadata.get("ingest_result"):
                meeting_mem = Memory(
                    id=10,
                    index_id=1,
                    session_id=None,
                    type="meeting",
                    title="Meeting",
                    subtitle=None,
                    narrative=None,
                    content="transcript",
                    metadata={
                        "idempotency_key": "will-be-checked",
                        **persisted_metadata,
                    },
                    priority=0.5,
                    stability="stable",
                    access_count=0,
                    last_accessed_at=None,
                    created_at="2026-01-01T00:00:00",
                    updated_at="2026-01-01T00:00:00",
                )
                return SearchResult(results=[meeting_mem], total=1)
            # Person search during first ingest
            return SearchResult(results=[], total=0)

        mock_dl.search = _search

        # Call order for first ingest (2 attendees, no mentioned people):
        # save(meeting)=10, save(person Alice)=20, save(interaction Alice)=21,
        # save(person Bob)=22, save(interaction Bob)=23
        mock_dl.save_memory.side_effect = [
            SaveMemoryResult(id=10, message="ok"),   # meeting
            SaveMemoryResult(id=20, message="ok"),   # person1 (Alice Smith)
            SaveMemoryResult(id=21, message="ok"),   # interaction1 (Alice)
            SaveMemoryResult(id=22, message="ok"),   # person2 (Bob Jones)
            SaveMemoryResult(id=23, message="ok"),   # interaction2 (Bob)
        ]
        _rel_counter = [100]

        async def _auto_rel(*args, **kwargs):
            _rel_counter[0] += 1
            return _rel_counter[0]

        mock_dl.create_relationship.side_effect = _auto_rel
        mock_dl.get_relationships.return_value = []

        TRANSCRIPT_TEXT = "Transcript text for idempotency test."
        SOURCE_REF = "idempotency-source-ref"

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_2_ATTENDEES

            ingestor = TranscriptIngestor(data_layer=mock_dl)

            # First ingest — fresh run, creates all memories
            result1 = await ingestor.ingest(text=TRANSCRIPT_TEXT, source_ref=SOURCE_REF)

            # Second ingest with same (source_ref, text) — must return same IDs without new saves
            result2 = await ingestor.ingest(text=TRANSCRIPT_TEXT, source_ref=SOURCE_REF)

        assert result1.meeting_memory_id == result2.meeting_memory_id
        assert result1.meeting_memory_id == 10
        # result2 reconstructed from persisted ingest_result
        assert result2.person_memory_ids == result1.person_memory_ids
        assert result2.relationship_ids == result1.relationship_ids
        assert result2.run_id == result1.run_id

        # save_memory should only have been called during the first run
        assert mock_dl.save_memory.call_count == 5  # only from first run


class TestPersonDedup:
    """AC3: misspelled names are deduped against existing person records."""

    async def test_ingest_person_dedup_misspelling(self):
        """'Allice Brown' should dedup against existing 'Alice Brown'."""
        from open_brain.data_layer.interface import Memory

        # Existing person memory for "Alice Brown"
        alice_memory = Memory(
            id=50,
            index_id=1,
            session_id=None,
            type="person",
            title="Alice Brown",
            subtitle=None,
            narrative=None,
            content="Alice Brown, engineer.",
            metadata={"name": "Alice Brown"},
            priority=0.5,
            stability="stable",
            access_count=2,
            last_accessed_at=None,
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )

        mock_dl = AsyncMock()

        async def _search(params):
            if params.type == "person":
                return SearchResult(results=[alice_memory], total=1)
            return SearchResult(results=[], total=0)

        mock_dl.search = _search
        mock_dl.save_memory.side_effect = [
            SaveMemoryResult(id=10, message="ok"),   # meeting
            # No new person memory for "Allice Brown" — should reuse id=50
            SaveMemoryResult(id=30, message="ok"),   # interaction
        ]

        _rel_counter = [100]

        async def _auto_rel(*args, **kwargs):
            _rel_counter[0] += 1
            return _rel_counter[0]

        mock_dl.create_relationship.side_effect = _auto_rel
        mock_dl.get_relationships.return_value = []

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_MISSPELLED

            ingestor = TranscriptIngestor(data_layer=mock_dl)
            result = await ingestor.ingest(
                text="Allice Brown presented the project status update.",
                source_ref="dedup-test-ref",
            )

        # "Allice Brown" should have been matched to existing memory id=50
        assert 50 in result.person_memory_ids
        # No new person memory should have been created for this person
        # (save_memory was called only for meeting + interaction, not a new person)
        save_types = [
            call.args[0].type if call.args else call.kwargs.get("params", MagicMock()).type
            for call in mock_dl.save_memory.call_args_list
        ]
        assert "person" not in save_types


class TestEmptyTranscript:
    """AC4: empty text raises ValueError."""

    async def test_ingest_empty_transcript_raises(self):
        mock_dl = _make_mock_dl()
        ingestor = TranscriptIngestor(data_layer=mock_dl)

        with pytest.raises(ValueError, match="[Ee]mpty|[Bb]lank|[Tt]ext"):
            await ingestor.ingest(text="", source_ref="empty-ref")

    async def test_ingest_whitespace_only_raises(self):
        mock_dl = _make_mock_dl()
        ingestor = TranscriptIngestor(data_layer=mock_dl)

        with pytest.raises(ValueError):
            await ingestor.ingest(text="   \n\t  ", source_ref="whitespace-ref")


class TestNoAttendees:
    """AC5: transcript with no named persons — meeting created, person lists empty."""

    async def test_ingest_no_attendees(self):
        mock_dl = _make_mock_dl(
            save_side_effects=[
                SaveMemoryResult(id=10, message="ok"),  # meeting only
            ],
        )

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_NO_ATTENDEES

            ingestor = TranscriptIngestor(data_layer=mock_dl)
            result = await ingestor.ingest(
                text="A dictated note with no names mentioned.",
                source_ref="no-attendees-ref",
            )

        assert isinstance(result, IngestResult)
        assert result.meeting_memory_id == 10
        assert result.person_memory_ids == []
        assert result.mention_memory_ids == []
        assert result.interaction_memory_ids == []
        assert result.relationship_ids == []


class TestFollowUpCandidates:
    """AC6: follow_up_candidates is populated list, no bd calls made."""

    async def test_ingest_follow_up_candidates_not_auto_created(self):
        """follow_up_candidates must be populated but never auto-created as bd issues."""
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_WITH_FOLLOWUPS

            ingestor = TranscriptIngestor(data_layer=mock_dl)
            result = await ingestor.ingest(
                text="Alice planned: send proposal, book meeting room, review budget.",
                source_ref="followup-test-ref",
            )

        assert isinstance(result.follow_up_candidates, list)
        assert len(result.follow_up_candidates) > 0
        # Each candidate should be a dict with at least a 'task' key
        for candidate in result.follow_up_candidates:
            assert isinstance(candidate, dict)
            # Should have some text content
            assert any(v for v in candidate.values() if isinstance(v, str) and v.strip())


class TestFixtureTranscript:
    """AC7: fixture transcript from sample_transcript_3person_meeting.txt."""

    async def test_ingest_fixture_transcript(self):
        """Ingest the 3-person meeting fixture; verify attendees extracted.

        Call order per ingest code:
          1. save(meeting)
          2. For each attendee: save(person), save(interaction)    → 3 * 2 = 6 saves
          3. For each mentioned: save(person), save(mention)       → 4 * 2 = 8 saves
          Total: 1 + 6 + 8 = 15 saves
        """
        assert TRANSCRIPT_3PERSON.exists(), f"Fixture missing: {TRANSCRIPT_3PERSON}"
        transcript_text = TRANSCRIPT_3PERSON.read_text()

        # 15 saves total; rel_side_effects: 3 attended_by + 4 mentioned_in = 7
        mock_dl = _make_mock_dl(
            save_side_effects=[
                SaveMemoryResult(id=100, message="ok"),  # meeting
                # Sarah Hoffmann (attendee)
                SaveMemoryResult(id=201, message="ok"),  # person
                SaveMemoryResult(id=202, message="ok"),  # interaction
                # Marcus Berger (attendee)
                SaveMemoryResult(id=203, message="ok"),  # person
                SaveMemoryResult(id=204, message="ok"),  # interaction
                # Priya Nair (attendee)
                SaveMemoryResult(id=205, message="ok"),  # person
                SaveMemoryResult(id=206, message="ok"),  # interaction
                # Tobias Schreiber (mentioned)
                SaveMemoryResult(id=207, message="ok"),  # person
                SaveMemoryResult(id=208, message="ok"),  # mention
                # Dr. Cyrus Alamouti (mentioned)
                SaveMemoryResult(id=209, message="ok"),  # person
                SaveMemoryResult(id=210, message="ok"),  # mention
                # Lisa Chen (mentioned)
                SaveMemoryResult(id=211, message="ok"),  # person
                SaveMemoryResult(id=212, message="ok"),  # mention
                # Jan Kowalski (mentioned)
                SaveMemoryResult(id=213, message="ok"),  # person
                SaveMemoryResult(id=214, message="ok"),  # mention
            ],
            rel_side_effects=[501, 502, 503, 504, 505, 506, 507],
        )

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_3PERSON_FIXTURE

            ingestor = TranscriptIngestor(data_layer=mock_dl)
            result = await ingestor.ingest(
                text=transcript_text,
                source_ref="fixture-3person-meeting",
            )

        assert isinstance(result, IngestResult)
        assert result.meeting_memory_id == 100
        # 3 attendees + 4 mentioned → 7 person memory IDs
        # (mentioned people also surface in person_memory_ids so callers can
        # discover person records created for mention-only transcripts.)
        assert len(result.person_memory_ids) == 7
        assert set(result.person_memory_ids) == {201, 203, 205, 207, 209, 211, 213}
        # 4 mentioned people → 4 mention memory IDs
        assert len(result.mention_memory_ids) == 4
        assert set(result.mention_memory_ids) == {208, 210, 212, 214}
        # 3 attendees → 3 interaction memory IDs
        assert len(result.interaction_memory_ids) == 3
        assert set(result.interaction_memory_ids) == {202, 204, 206}
        # 3 attended_by + 4 mentioned_in = 7 relationships
        assert len(result.relationship_ids) == 7
        assert result.run_id
        # Follow-up candidates from 4 tasks
        assert len(result.follow_up_candidates) == 4


class TestRunIdPopulation:
    """AC: run_id populated per cr3.12 contract — generate UUID locally."""

    async def test_run_id_is_valid_uuid(self):
        import uuid

        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_NO_ATTENDEES

            ingestor = TranscriptIngestor(data_layer=mock_dl)
            result = await ingestor.ingest(
                text="Short transcript text.",
                source_ref="run-id-test",
            )

        # Must be a non-empty string that parses as UUID4
        assert result.run_id
        parsed = uuid.UUID(result.run_id)
        assert parsed.version == 4

    async def test_run_id_unique_per_call(self):
        """Two separate ingest calls should produce different run_ids."""
        mock_dl1 = _make_mock_dl()
        mock_dl2 = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_NO_ATTENDEES

            ingestor = TranscriptIngestor(data_layer=mock_dl1)
            result1 = await ingestor.ingest(text="Transcript A.", source_ref="ref-A")

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_NO_ATTENDEES

            ingestor2 = TranscriptIngestor(data_layer=mock_dl2)
            result2 = await ingestor2.ingest(text="Transcript B.", source_ref="ref-B")

        assert result1.run_id != result2.run_id


class TestMentionedPeople:
    """Attendees present + people mentioned but absent."""

    async def test_ingest_with_mentioned_people(self):
        """LLM returns attendees + mentioned people; both sets of memories created."""
        # save order: meeting, person1(Alice), interaction1, person2(Bob), interaction2,
        #             person3(Carol) as mentioned, mention1(Carol)
        mock_dl = _make_mock_dl(
            save_side_effects=[
                SaveMemoryResult(id=10, message="ok"),  # meeting
                SaveMemoryResult(id=20, message="ok"),  # person1 (Alice Smith)
                SaveMemoryResult(id=21, message="ok"),  # interaction1
                SaveMemoryResult(id=22, message="ok"),  # person2 (Bob Jones)
                SaveMemoryResult(id=23, message="ok"),  # interaction2
                SaveMemoryResult(id=24, message="ok"),  # person3 (Carol White)
                SaveMemoryResult(id=25, message="ok"),  # mention1
            ],
            rel_side_effects=[101, 102, 103],  # 2 attended_by + 1 mentioned_in
        )

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_WITH_MENTIONED

            ingestor = TranscriptIngestor(data_layer=mock_dl)
            result = await ingestor.ingest(
                text="Alice and Bob met. They mentioned Carol White.",
                source_ref="mentioned-test-ref",
            )

        assert isinstance(result, IngestResult)
        assert result.meeting_memory_id == 10
        # 2 attendees + 1 mentioned (Carol) → 3 person memory IDs
        assert len(result.person_memory_ids) == 3
        assert set(result.person_memory_ids) == {20, 22, 24}
        assert len(result.mention_memory_ids) == 1  # Carol
        assert len(result.interaction_memory_ids) == 2
        assert len(result.relationship_ids) == 3  # 2 attended_by + 1 mentioned_in
        assert len(result.follow_up_candidates) == 1


class TestFixtureDictatedNote:
    """AC8: dictated note fixture — no attendees, 3 mentioned people."""

    async def test_ingest_fixture_dictated_note(self):
        """Ingest dictated note fixture: 0 attendees, 3 mentioned people.

        Call order:
          1. save(meeting)
          2. For each mentioned: save(person), save(mention)  → 3 * 2 = 6 saves
          Total: 1 + 6 = 7 saves
          Relationships: 0 attended_by + 3 mentioned_in = 3
        """
        assert TRANSCRIPT_DICTATED_NOTE.exists(), f"Fixture missing: {TRANSCRIPT_DICTATED_NOTE}"
        transcript_text = TRANSCRIPT_DICTATED_NOTE.read_text()

        mock_dl = _make_mock_dl(
            save_side_effects=[
                SaveMemoryResult(id=100, message="ok"),  # meeting
                # David Park (mentioned)
                SaveMemoryResult(id=201, message="ok"),  # person
                SaveMemoryResult(id=202, message="ok"),  # mention
                # Emma Torres (mentioned)
                SaveMemoryResult(id=203, message="ok"),  # person
                SaveMemoryResult(id=204, message="ok"),  # mention
                # Robert Whitfield (mentioned)
                SaveMemoryResult(id=205, message="ok"),  # person
                SaveMemoryResult(id=206, message="ok"),  # mention
            ],
            rel_side_effects=[501, 502, 503],  # 3 mentioned_in
        )

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_DICTATED_NOTE

            ingestor = TranscriptIngestor(data_layer=mock_dl)
            result = await ingestor.ingest(
                text=transcript_text,
                source_ref="fixture-dictated-note",
            )

        assert isinstance(result, IngestResult)
        assert result.meeting_memory_id == 100
        # 0 attendees, 3 mentioned → 3 person memories (mention-only),
        # 0 interaction memories (interactions only for attendees).
        assert set(result.person_memory_ids) == {201, 203, 205}
        assert result.interaction_memory_ids == []
        # 3 mentioned people → 3 mention memories
        assert len(result.mention_memory_ids) == 3
        assert set(result.mention_memory_ids) == {202, 204, 206}
        # 3 mentioned_in relationships
        assert len(result.relationship_ids) == 3
        # 3 follow-up candidates
        assert len(result.follow_up_candidates) == 3
        assert result.run_id


class TestFixtureLongMeeting:
    """AC9: long meeting fixture — 4 attendees + 1 mentioned."""

    async def test_ingest_fixture_long_meeting(self):
        """Ingest long meeting fixture: 4 attendees, 1 mentioned person.

        Call order:
          1. save(meeting)
          2. For each attendee: save(person), save(interaction) → 4 * 2 = 8 saves
          3. For mentioned: save(person), save(mention)          → 1 * 2 = 2 saves
          Total: 1 + 8 + 2 = 11 saves
          Relationships: 4 attended_by + 1 mentioned_in = 5
        """
        assert TRANSCRIPT_LONG_MEETING.exists(), f"Fixture missing: {TRANSCRIPT_LONG_MEETING}"
        transcript_text = TRANSCRIPT_LONG_MEETING.read_text()

        mock_dl = _make_mock_dl(
            save_side_effects=[
                SaveMemoryResult(id=100, message="ok"),  # meeting
                # Katharina Meier (attendee)
                SaveMemoryResult(id=201, message="ok"),  # person
                SaveMemoryResult(id=202, message="ok"),  # interaction
                # Stefan Wolf (attendee)
                SaveMemoryResult(id=203, message="ok"),  # person
                SaveMemoryResult(id=204, message="ok"),  # interaction
                # Annika Baum (attendee)
                SaveMemoryResult(id=205, message="ok"),  # person
                SaveMemoryResult(id=206, message="ok"),  # interaction
                # Michael Torres (attendee)
                SaveMemoryResult(id=207, message="ok"),  # person
                SaveMemoryResult(id=208, message="ok"),  # interaction
                # Jochen Jungbluth (mentioned)
                SaveMemoryResult(id=209, message="ok"),  # person
                SaveMemoryResult(id=210, message="ok"),  # mention
            ],
            rel_side_effects=[501, 502, 503, 504, 505],  # 4 attended_by + 1 mentioned_in
        )

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_LONG_MEETING

            ingestor = TranscriptIngestor(data_layer=mock_dl)
            result = await ingestor.ingest(
                text=transcript_text,
                source_ref="fixture-long-meeting",
            )

        assert isinstance(result, IngestResult)
        assert result.meeting_memory_id == 100
        # 4 attendees + 1 mentioned → 5 person memories, 4 interaction memories
        assert len(result.person_memory_ids) == 5
        assert set(result.person_memory_ids) == {201, 203, 205, 207, 209}
        assert len(result.interaction_memory_ids) == 4
        assert set(result.interaction_memory_ids) == {202, 204, 206, 208}
        # 1 mentioned person → 1 mention memory
        assert len(result.mention_memory_ids) == 1
        assert set(result.mention_memory_ids) == {210}
        # 4 attended_by + 1 mentioned_in = 5 relationships
        assert len(result.relationship_ids) == 5
        # 1 follow-up candidate
        assert len(result.follow_up_candidates) == 1
        assert result.run_id
