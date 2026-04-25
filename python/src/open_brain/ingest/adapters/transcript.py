"""TranscriptIngestor — ingests meeting transcripts into open-brain memory.

Flow per ingest call:
1. Validate input (non-empty text).
2. Compute idempotency key; check DataLayer for prior run.
3. Call Haiku extraction to get attendees, mentioned people, topics, tasks.
4. Save meeting memory with idempotency metadata.
5. For each attendee: dedup against existing person memories; save or reuse.
6. For each mentioned person: save mention memory.
7. For each attendee: save interaction memory.
8. Create relationships: meeting→person (attended_by), person→meeting (mentioned_in).
9. Return populated IngestResult.
"""

import dataclasses
import hashlib
import logging
import time
import uuid

from open_brain.data_layer.interface import (
    DataLayer,
    SaveMemoryParams,
    SearchParams,
    UpdateMemoryParams,
)
from open_brain.ingest import metrics
from open_brain.ingest.extract import extract_from_transcript
from open_brain.ingest.models import IngestResult
from open_brain.people.dedup import match_person
from open_brain.people.models import PersonMember, PersonRecord

logger = logging.getLogger(__name__)


def _compute_idempotency_key(source_ref: str, text: str) -> str:
    """Compute a stable idempotency key from source_ref + full text.

    Hashes the full transcript body (not just the first 500 chars) to avoid
    false positives where different transcripts share the same prefix.
        sha256(source_ref.encode() + sha256(text.encode()).digest())
    """
    text_hash = hashlib.sha256(text.encode()).digest()
    combined = source_ref.encode() + text_hash
    return hashlib.sha256(combined).hexdigest()


def _memory_to_person_record(memory) -> PersonRecord | None:
    """Convert a Memory object (type='person') to a PersonRecord for dedup.

    Returns None if the memory does not have a usable name.
    """
    md = memory.metadata or {}
    name = md.get("name") or memory.title
    if not name:
        return None
    member: PersonMember = {
        "name": name,
        "org": md.get("org"),
        "linkedin": md.get("linkedin"),
        "aliases": md.get("aliases") or [],
    }
    return PersonRecord(
        memory_id=memory.id,
        style="single",
        members=[member],
    )


class TranscriptIngestor:
    """Ingests meeting transcripts into open-brain memory.

    Idempotent: repeated calls with the same (source_ref, text) return
    the same IDs without creating duplicate memories.

    Args:
        data_layer: DataLayer implementation to use for persistence.
    """

    def __init__(self, data_layer: DataLayer) -> None:
        self._dl = data_layer

    async def ingest(
        self,
        text: str,
        source_ref: str,
        medium_hint: str | None = None,
    ) -> IngestResult:
        """Ingest a meeting transcript and return an IngestResult.

        Args:
            text: The full transcript text.
            source_ref: Unique identifier for this transcript source.
            medium_hint: Optional hint about the medium (e.g. 'macwhisper').

        Returns:
            Populated IngestResult with all created memory IDs.

        Raises:
            ValueError: If text is empty or whitespace-only.
        """
        if not text or not text.strip():
            raise ValueError("text must not be empty or whitespace-only")

        _ingest_start = time.monotonic()
        metrics.record_ingest("transcript")

        run_id = str(uuid.uuid4())
        idempotency_key = _compute_idempotency_key(source_ref, text)

        # --- Idempotency check ---
        prior = await self._find_prior_run(idempotency_key, source_ref)
        if prior is not None:
            return prior

        # --- LLM extraction ---
        metrics.record_llm_call("extract")
        extracted = await extract_from_transcript(text)
        attendees: list[str] = extracted.get("attendees") or []
        mentioned: list[str] = extracted.get("mentioned_people") or []
        topics: list[str] = extracted.get("topics") or []
        follow_up_tasks: list[str] = extracted.get("follow_up_tasks") or []

        # Build follow_up_candidates list (never auto-created as bd issues)
        follow_up_candidates: list[dict] = [
            {"task": task} for task in follow_up_tasks
        ]

        # --- Load existing person memories for dedup ---
        existing_records = await self._load_existing_persons()

        # --- Save meeting memory (placeholder metadata; updated with full result below) ---
        transcript_excerpt = text[:4000]
        meeting_result = await self._dl.save_memory(
            SaveMemoryParams(
                text=transcript_excerpt,
                type="meeting",
                project="people",
                title=f"Meeting: {source_ref}",
                metadata={
                    "source_ref": source_ref,
                    "idempotency_key": idempotency_key,
                    "run_id": run_id,
                    "topics": topics,
                    "medium_hint": medium_hint,
                },
            )
        )
        metrics.record_memory_written("meeting")
        meeting_id = meeting_result.id

        # --- Process attendees (present people) ---
        person_memory_ids: list[int] = []
        interaction_memory_ids: list[int] = []

        for name in attendees:
            person_id = await self._resolve_person(
                name=name,
                existing_records=existing_records,
                run_id=run_id,
            )
            person_memory_ids.append(person_id)

            # Create interaction memory for this attendee
            interaction_result = await self._dl.save_memory(
                SaveMemoryParams(
                    text=f"{name} attended meeting: {source_ref}",
                    type="interaction",
                    project="people",
                    title=f"Interaction: {name} @ {source_ref}",
                    metadata={
                        "person_ref": str(person_id),
                        "channel": "meeting",
                        "direction": "bidirectional",
                        "summary": f"{name} attended this meeting",
                        "run_id": run_id,
                    },
                )
            )
            interaction_memory_ids.append(interaction_result.id)

        # --- Process mentioned people (absent from meeting) ---
        mention_memory_ids: list[int] = []
        mentioned_person_ids: list[int] = []  # cached to avoid double-resolve

        for name in mentioned:
            person_id = await self._resolve_person(
                name=name,
                existing_records=existing_records,
                run_id=run_id,
            )
            mentioned_person_ids.append(person_id)

            # Create mention memory
            mention_result = await self._dl.save_memory(
                SaveMemoryParams(
                    text=f"{name} was mentioned in meeting: {source_ref}",
                    type="mention",
                    project="people",
                    title=f"Mention: {name} in {source_ref}",
                    metadata={
                        "person_ref": str(person_id),
                        "source_memory_ref": str(meeting_id),
                        "context": f"Mentioned in {source_ref}",
                        "run_id": run_id,
                    },
                )
            )
            mention_memory_ids.append(mention_result.id)

        # --- Create relationships ---
        relationship_ids: list[int] = []

        # meeting → person: attended_by (for each attendee)
        for person_id in person_memory_ids:
            rel_id = await self._dl.create_relationship(
                source_id=meeting_id,
                target_id=person_id,
                link_type="attended_by",
                metadata={"run_id": run_id},
            )
            relationship_ids.append(rel_id)
            metrics.record_relationship_written("attended_by")

        # person → meeting: mentioned_in (for each mentioned person, cached)
        for person_id in mentioned_person_ids:
            rel_id = await self._dl.create_relationship(
                source_id=person_id,
                target_id=meeting_id,
                link_type="mentioned_in",
                metadata={"run_id": run_id},
            )
            relationship_ids.append(rel_id)
            metrics.record_relationship_written("mentioned_in")

        # Surface mentioned people in person_memory_ids so mention-only
        # transcripts return the person IDs they created. Done AFTER the
        # attended_by relationship loop to avoid miscategorising mentions
        # as attendees.
        for person_id in mentioned_person_ids:
            if person_id not in person_memory_ids:
                person_memory_ids.append(person_id)

        # --- Build final IngestResult ---
        result = IngestResult(
            meeting_memory_id=meeting_id,
            person_memory_ids=person_memory_ids,
            mention_memory_ids=mention_memory_ids,
            interaction_memory_ids=interaction_memory_ids,
            relationship_ids=relationship_ids,
            follow_up_candidates=follow_up_candidates,
            run_id=run_id,
        )

        # --- Persist IngestResult for idempotency on future calls ---
        await self._dl.update_memory(
            UpdateMemoryParams(
                id=meeting_id,
                metadata={"ingest_result": dataclasses.asdict(result)},
            )
        )

        metrics.record_ingest_duration("transcript", time.monotonic() - _ingest_start)
        return result

    async def _find_prior_run(
        self, idempotency_key: str, source_ref: str
    ) -> IngestResult | None:
        """Check for a prior ingest run with the same idempotency key.

        Returns reconstructed IngestResult if found, None otherwise.
        """
        try:
            search_result = await self._dl.search(
                SearchParams(
                    type="meeting",
                    project="people",
                    metadata_filter={"idempotency_key": idempotency_key},
                )
            )
        except Exception as exc:
            logger.warning("Idempotency check failed: %s — proceeding with fresh ingest", exc)
            return None

        if not search_result.results:
            return None

        meeting_memory = search_result.results[0]
        stored = meeting_memory.metadata.get("ingest_result")
        if not stored:
            return None

        logger.info("Idempotency hit for source_ref=%r — returning prior run", source_ref)
        return IngestResult(
            meeting_memory_id=stored.get("meeting_memory_id", meeting_memory.id),
            person_memory_ids=stored.get("person_memory_ids", []),
            mention_memory_ids=stored.get("mention_memory_ids", []),
            interaction_memory_ids=stored.get("interaction_memory_ids", []),
            relationship_ids=stored.get("relationship_ids", []),
            follow_up_candidates=stored.get("follow_up_candidates", []),
            run_id=stored.get("run_id", ""),
        )

    async def _load_existing_persons(self) -> list[PersonRecord]:
        """Load existing person memories from the DataLayer for dedup matching."""
        try:
            search_result = await self._dl.search(
                SearchParams(
                    type="person",
                    project="people",
                    limit=200,
                )
            )
        except Exception as exc:
            logger.warning("Failed to load existing persons for dedup: %s", exc)
            return []

        records: list[PersonRecord] = []
        for memory in search_result.results:
            record = _memory_to_person_record(memory)
            if record is not None:
                records.append(record)
        return records

    async def _resolve_person(
        self,
        name: str,
        existing_records: list[PersonRecord],
        run_id: str,
    ) -> int:
        """Resolve a person name to a memory ID, deduplicating against existing records.

        Args:
            name: Person's name as extracted from the transcript.
            existing_records: Pre-loaded list of PersonRecord for matching.
            run_id: Current ingest run UUID.

        Returns:
            memory_id — either an existing one (auto_merge) or newly created.
        """
        decision = match_person(
            new_name=name,
            new_org=None,
            new_linkedin=None,
            existing=existing_records,
        )

        metrics.record_dedup_decision(decision.action)

        if decision.action == "auto_merge" and decision.target is not None:
            return decision.target.memory_id

        # For llm_confirm, ambiguous, or new: create a new person memory (conservative)
        save_result = await self._dl.save_memory(
            SaveMemoryParams(
                text=f"Person: {name}",
                type="person",
                project="people",
                title=name,
                metadata={
                    "name": name,
                    "run_id": run_id,
                },
            )
        )
        metrics.record_memory_written("person")
        # Refresh dedup snapshot so subsequent loop iterations can match this new person
        existing_records.append(
            PersonRecord(
                memory_id=save_result.id,
                style="single",
                members=[{"name": name, "org": None, "linkedin": None, "aliases": []}],
            )
        )
        return save_result.id
