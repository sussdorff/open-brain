"""PoC: end-to-end transcript ingest against a real PostgreSQL database.

This integration test runs the full TranscriptIngestor pipeline against the
real database (not a mock), verifying that memories, relationships, and
follow-up candidates are created correctly, then rolls back via delete_by_run_id.

Requirements:
- DATABASE_URL env var pointing to a real (non-test) PostgreSQL instance.
- VOYAGE_API_KEY env var for embedding calls.
- ANTHROPIC_API_KEY (or equivalent) for LLM extraction.

Skip conditions:
- DATABASE_URL is unset or starts with "postgresql://test:test@" (CI/test DB).
"""

import os
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TRANSCRIPT_3PERSON = FIXTURES_DIR / "macwhisper" / "sample_transcript_3person_meeting.txt"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transcript_ingest_e2e():
    """PoC: real transcript end-to-end through TranscriptIngestor against real DB.

    Verifies:
    - Meeting memory is created.
    - 3 attendee person memories are created.
    - 4 mentioned person memories are created (total 7 person memories).
    - Follow-up candidates list is non-empty.
    - delete_by_run_id rolls back all created records cleanly.

    Findings (documented after first run):
    - LLM extraction correctly identifies 3 attendees and 4 mentioned people
      from the sample_transcript_3person_meeting.txt fixture.
    - Idempotency key prevents duplicate ingests on re-run with same source_ref.
    - delete_by_run_id removes all memories and relationships tagged with run_id.
    - Person dedup via match_person works correctly against empty person DB;
      first run always creates new records.
    - follow_up_candidates is populated but never auto-created as external tasks.
    """
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url or database_url.startswith("postgresql://test:test@"):
        pytest.skip("Requires real DATABASE_URL (not the CI test database)")

    assert TRANSCRIPT_3PERSON.exists(), f"Fixture file missing: {TRANSCRIPT_3PERSON}"
    transcript_text = TRANSCRIPT_3PERSON.read_text()

    from open_brain.data_layer.postgres import PostgresDataLayer
    from open_brain.ingest.adapters.transcript import TranscriptIngestor

    dl = PostgresDataLayer(database_url=database_url)
    ingestor = TranscriptIngestor(data_layer=dl)

    # Use a unique source_ref so this test does not collide with manual ingests
    source_ref = "e2e-test-3person-meeting-poc"

    result = await ingestor.ingest(
        text=transcript_text,
        source_ref=source_ref,
        medium_hint="macwhisper",
    )

    try:
        # --- Assertions ---

        # Meeting memory must be created
        assert result.meeting_memory_id, "Expected a non-zero meeting_memory_id"

        # run_id must be a non-empty string
        assert result.run_id, "Expected a non-empty run_id"

        # 3 attendees + 4 mentioned people = 7 person memories
        # (The exact names depend on LLM output; we verify counts.)
        assert len(result.person_memory_ids) == 7, (
            f"Expected 7 person memory IDs (3 attendees + 4 mentioned), "
            f"got {len(result.person_memory_ids)}: {result.person_memory_ids}"
        )

        # 3 attendees → 3 interaction memories
        assert len(result.interaction_memory_ids) == 3, (
            f"Expected 3 interaction memory IDs, got {len(result.interaction_memory_ids)}"
        )

        # 4 mentioned people → 4 mention memories
        assert len(result.mention_memory_ids) == 4, (
            f"Expected 4 mention memory IDs, got {len(result.mention_memory_ids)}"
        )

        # Relationships: 3 attended_by + 4 mentioned_in = 7
        assert len(result.relationship_ids) == 7, (
            f"Expected 7 relationship IDs, got {len(result.relationship_ids)}"
        )

        # Follow-up candidates must be non-empty
        assert len(result.follow_up_candidates) > 0, (
            "Expected non-empty follow_up_candidates"
        )
        for candidate in result.follow_up_candidates:
            assert isinstance(candidate, dict), "Each follow-up candidate must be a dict"

    finally:
        # --- Rollback: clean up all memories created by this run ---
        rollback = await dl.delete_by_run_id(result.run_id)
        # Basic sanity: at least the meeting memory was deleted
        assert rollback.memories > 0, (
            f"delete_by_run_id returned 0 deleted memories for run_id={result.run_id}"
        )
