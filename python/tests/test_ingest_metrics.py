"""Tests for ingest observability metrics — cr3.16.

Acceptance criteria:
1. Counters increment correctly across a fixture ingest
2. MCP tool people_ingest_stats returns structured dict with all six metric families
3. Reset functionality for test isolation
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from open_brain.data_layer.interface import SaveMemoryResult, SearchResult


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_mock_dl() -> AsyncMock:
    """Build a minimal mock DataLayer."""
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


# ─── Criterion 1: Counter increments ─────────────────────────────────────────


class TestCounterIncrements:
    """AC1: Counters increment correctly across a fixture ingest."""

    def setup_method(self):
        """Reset metrics before each test for isolation."""
        from open_brain.ingest import metrics
        metrics.reset_all()

    async def test_ingests_total_increments_on_ingest(self):
        """ingests_total[adapter=transcript] should be 1 after one ingest."""
        from open_brain.ingest import metrics
        from open_brain.ingest.adapters.transcript import TranscriptIngestor

        LLM_RESPONSE = json.dumps({
            "attendees": [],
            "mentioned_people": [],
            "topics": [],
            "follow_up_tasks": [],
        })
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_RESPONSE
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            await ingestor.ingest(
                text="Meeting transcript with Alice and Bob discussing the deadline.",
                source_ref="metrics-test-001",
            )

        stats = metrics.get_stats()
        assert stats["ingests_total"]["transcript"] == 1

    async def test_llm_calls_total_increments_on_extract(self):
        """llm_calls_total[purpose=extract] should be 1 after one ingest."""
        from open_brain.ingest import metrics
        from open_brain.ingest.adapters.transcript import TranscriptIngestor

        LLM_RESPONSE = json.dumps({
            "attendees": [],
            "mentioned_people": [],
            "topics": [],
            "follow_up_tasks": [],
        })
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_RESPONSE
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            await ingestor.ingest(
                text="Meeting transcript with Alice and Bob discussing the deadline.",
                source_ref="metrics-test-llm-001",
            )

        stats = metrics.get_stats()
        assert stats["llm_calls_total"]["extract"] == 1

    async def test_memories_written_meeting_increments(self):
        """memories_written_total[type=meeting] increments once per ingest."""
        from open_brain.ingest import metrics
        from open_brain.ingest.adapters.transcript import TranscriptIngestor

        LLM_RESPONSE = json.dumps({
            "attendees": [],
            "mentioned_people": [],
            "topics": [],
            "follow_up_tasks": [],
        })
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_RESPONSE
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            await ingestor.ingest(
                text="A short note.",
                source_ref="metrics-test-meeting",
            )

        stats = metrics.get_stats()
        assert stats["memories_written_total"]["meeting"] == 1

    async def test_memories_written_person_increments_per_person(self):
        """memories_written_total[type=person] increments once per new person."""
        from open_brain.ingest import metrics
        from open_brain.ingest.adapters.transcript import TranscriptIngestor

        LLM_RESPONSE = json.dumps({
            "attendees": ["Alice Smith", "Bob Jones"],
            "mentioned_people": [],
            "topics": [],
            "follow_up_tasks": [],
        })
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_RESPONSE
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            await ingestor.ingest(
                text="Meeting with Alice and Bob.",
                source_ref="metrics-test-persons",
            )

        stats = metrics.get_stats()
        assert stats["memories_written_total"]["person"] == 2

    async def test_dedup_decisions_auto_merge_increments(self):
        """dedup_decisions_total[action=auto_merge] increments for exact name match."""
        from open_brain.data_layer.interface import Memory
        from open_brain.ingest import metrics
        from open_brain.ingest.adapters.transcript import TranscriptIngestor

        # Pre-existing person "Alice Smith"
        alice_memory = Memory(
            id=50,
            index_id=1,
            session_id=None,
            type="person",
            title="Alice Smith",
            subtitle=None,
            narrative=None,
            content="Alice Smith.",
            metadata={"name": "Alice Smith"},
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
            SaveMemoryResult(id=10, message="ok"),  # meeting
            SaveMemoryResult(id=21, message="ok"),  # interaction for Alice
        ]
        mock_dl.create_relationship.return_value = 101
        mock_dl.get_relationships.return_value = []

        LLM_RESPONSE = json.dumps({
            "attendees": ["Alice Smith"],
            "mentioned_people": [],
            "topics": [],
            "follow_up_tasks": [],
        })

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_RESPONSE
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            await ingestor.ingest(
                text="Alice Smith was at the meeting.",
                source_ref="metrics-test-dedup",
            )

        stats = metrics.get_stats()
        assert stats["dedup_decisions_total"]["auto_merge"] == 1

    async def test_dedup_decisions_new_increments(self):
        """dedup_decisions_total[action=new] increments when no existing person found."""
        from open_brain.ingest import metrics
        from open_brain.ingest.adapters.transcript import TranscriptIngestor

        LLM_RESPONSE = json.dumps({
            "attendees": ["Charlie New"],
            "mentioned_people": [],
            "topics": [],
            "follow_up_tasks": [],
        })
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_RESPONSE
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            await ingestor.ingest(
                text="Charlie New was at the meeting.",
                source_ref="metrics-test-new-person",
            )

        stats = metrics.get_stats()
        assert stats["dedup_decisions_total"]["new"] == 1

    async def test_relationships_written_increments(self):
        """relationships_written_total[link_type] increments per relationship."""
        from open_brain.ingest import metrics
        from open_brain.ingest.adapters.transcript import TranscriptIngestor

        LLM_RESPONSE = json.dumps({
            "attendees": ["Alice Smith"],
            "mentioned_people": ["Bob Mentioned"],
            "topics": [],
            "follow_up_tasks": [],
        })
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_RESPONSE
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            await ingestor.ingest(
                text="Alice met, Bob was mentioned.",
                source_ref="metrics-test-rels",
            )

        stats = metrics.get_stats()
        # 1 attended_by, 1 mentioned_in
        assert stats["relationships_written_total"]["attended_by"] == 1
        assert stats["relationships_written_total"]["mentioned_in"] == 1

    async def test_ingest_duration_recorded(self):
        """ingest_duration_seconds[adapter=transcript] contains one entry after one ingest."""
        from open_brain.ingest import metrics
        from open_brain.ingest.adapters.transcript import TranscriptIngestor

        LLM_RESPONSE = json.dumps({
            "attendees": [],
            "mentioned_people": [],
            "topics": [],
            "follow_up_tasks": [],
        })
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_RESPONSE
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            await ingestor.ingest(
                text="Quick note.",
                source_ref="metrics-test-duration",
            )

        stats = metrics.get_stats()
        durations = stats["ingest_duration_seconds"]["transcript"]
        assert isinstance(durations, list)
        assert len(durations) == 1
        assert durations[0] >= 0.0

    async def test_fixture_ingest_all_counters(self):
        """Fixture ingest: 1 transcript, 2 attendees, 2 relationships, 3 memories written."""
        from open_brain.ingest import metrics
        from open_brain.ingest.adapters.transcript import TranscriptIngestor

        LLM_RESPONSE = json.dumps({
            "attendees": ["Alice Smith", "Bob Jones"],
            "mentioned_people": [],
            "topics": ["deadline"],
            "follow_up_tasks": [],
        })
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_RESPONSE
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            await ingestor.ingest(
                text="Meeting with Alice and Bob. Bob mentioned the deadline.",
                source_ref="fixture-metrics-test",
            )

        stats = metrics.get_stats()

        # 1 ingest (label=transcript)
        assert stats["ingests_total"]["transcript"] == 1
        # 1 LLM call (label=extract)
        assert stats["llm_calls_total"]["extract"] == 1
        # 2 attendees → 2 dedup decisions (both "new" since empty search)
        assert stats["dedup_decisions_total"]["new"] == 2
        # 2 relationships (attended_by)
        assert stats["relationships_written_total"]["attended_by"] == 2
        # 3 memories: 1 meeting + 2 persons
        assert stats["memories_written_total"]["meeting"] == 1
        assert stats["memories_written_total"]["person"] == 2
        # 1 duration entry
        assert len(stats["ingest_duration_seconds"]["transcript"]) == 1


# ─── Criterion 2: people_ingest_stats MCP tool ───────────────────────────────


class TestPeopleIngestStatsTool:
    """AC2: people_ingest_stats returns structured dict with all six metric families."""

    def setup_method(self):
        from open_brain.ingest import metrics
        metrics.reset_all()

    async def test_get_stats_returns_all_six_families(self):
        """get_stats() returns dict with all 6 required metric families."""
        from open_brain.ingest import metrics

        result = metrics.get_stats()

        assert "ingests_total" in result
        assert "llm_calls_total" in result
        assert "dedup_decisions_total" in result
        assert "relationships_written_total" in result
        assert "memories_written_total" in result
        assert "ingest_duration_seconds" in result

    async def test_get_stats_returns_empty_dicts_when_no_ingests(self):
        """get_stats() returns empty dicts/lists for all families when counters are 0."""
        from open_brain.ingest import metrics

        result = metrics.get_stats()

        assert result["ingests_total"] == {}
        assert result["llm_calls_total"] == {}
        assert result["dedup_decisions_total"] == {}
        assert result["relationships_written_total"] == {}
        assert result["memories_written_total"] == {}
        assert result["ingest_duration_seconds"] == {}

    async def test_people_ingest_stats_mcp_tool_returns_json(self):
        """people_ingest_stats() MCP tool returns valid JSON string with all 6 families."""
        from open_brain.server import people_ingest_stats

        result_str = await people_ingest_stats()
        result = json.loads(result_str)

        assert "ingests_total" in result
        assert "llm_calls_total" in result
        assert "dedup_decisions_total" in result
        assert "relationships_written_total" in result
        assert "memories_written_total" in result
        assert "ingest_duration_seconds" in result

    async def test_people_ingest_stats_consistent_schema_after_ingest(self):
        """people_ingest_stats() returns consistent schema regardless of which counters are set."""
        from open_brain.ingest import metrics
        from open_brain.ingest.adapters.transcript import TranscriptIngestor
        from open_brain.server import people_ingest_stats

        LLM_RESPONSE = json.dumps({
            "attendees": ["Alice Smith"],
            "mentioned_people": [],
            "topics": [],
            "follow_up_tasks": [],
        })
        mock_dl = _make_mock_dl()

        with patch("open_brain.ingest.extract.llm_complete") as mock_llm:
            mock_llm.return_value = LLM_RESPONSE
            ingestor = TranscriptIngestor(data_layer=mock_dl)
            await ingestor.ingest(
                text="Meeting with Alice.",
                source_ref="mcp-tool-test",
            )

        result_str = await people_ingest_stats()
        result = json.loads(result_str)

        # All six families present regardless of which ones have data
        assert set(result.keys()) == {
            "ingests_total",
            "llm_calls_total",
            "dedup_decisions_total",
            "relationships_written_total",
            "memories_written_total",
            "ingest_duration_seconds",
        }
        # ingests_total has data
        assert result["ingests_total"]["transcript"] == 1

    async def test_multiple_adapters_tracked_separately(self):
        """Counters for adapter=transcript and adapter=email tracked separately."""
        from open_brain.ingest import metrics

        metrics.record_ingest("transcript")
        metrics.record_ingest("transcript")
        metrics.record_ingest("email")

        stats = metrics.get_stats()
        assert stats["ingests_total"]["transcript"] == 2
        assert stats["ingests_total"]["email"] == 1


# ─── Criterion 3: Reset functionality ────────────────────────────────────────


class TestResetFunctionality:
    """AC3: Reset functionality clears all counters to 0."""

    def setup_method(self):
        """Reset metrics before each test for isolation."""
        from open_brain.ingest import metrics
        metrics.reset_all()

    async def test_reset_clears_ingests_total(self):
        """reset_all() zeroes ingests_total."""
        from open_brain.ingest import metrics

        metrics.record_ingest("transcript")
        assert metrics.get_stats()["ingests_total"]["transcript"] == 1

        metrics.reset_all()
        assert metrics.get_stats()["ingests_total"] == {}

    async def test_reset_clears_all_families(self):
        """reset_all() clears all six metric families."""
        from open_brain.ingest import metrics

        # Populate all families
        metrics.record_ingest("transcript")
        metrics.record_llm_call("extract")
        metrics.record_dedup_decision("auto_merge")
        metrics.record_relationship_written("attended_by")
        metrics.record_memory_written("meeting")
        metrics.record_ingest_duration("transcript", 1.23)

        # Verify populated
        stats_before = metrics.get_stats()
        assert stats_before["ingests_total"]["transcript"] == 1
        assert stats_before["llm_calls_total"]["extract"] == 1

        # Reset
        metrics.reset_all()

        # Verify all empty
        stats_after = metrics.get_stats()
        assert stats_after["ingests_total"] == {}
        assert stats_after["llm_calls_total"] == {}
        assert stats_after["dedup_decisions_total"] == {}
        assert stats_after["relationships_written_total"] == {}
        assert stats_after["memories_written_total"] == {}
        assert stats_after["ingest_duration_seconds"] == {}

    async def test_reset_allows_fresh_accumulation(self):
        """After reset_all(), counters accumulate fresh values."""
        from open_brain.ingest import metrics

        metrics.record_ingest("transcript")
        metrics.reset_all()

        metrics.record_ingest("transcript")
        metrics.record_ingest("transcript")

        stats = metrics.get_stats()
        assert stats["ingests_total"]["transcript"] == 2

    async def test_reset_provides_test_isolation(self):
        """Each test method gets a clean counter slate via reset_all()."""
        from open_brain.ingest import metrics

        # Simulate a different counter state
        metrics.record_llm_call("dedup_confirm")
        metrics.record_llm_call("relationship_classify")
        metrics.reset_all()

        # After reset, all families are empty
        stats = metrics.get_stats()
        assert not any(stats[key] for key in stats)
