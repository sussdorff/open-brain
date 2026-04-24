"""Ingest domain models."""

from dataclasses import dataclass, field


@dataclass
class IngestResult:
    """Result of a transcript ingest operation.

    Attributes:
        meeting_memory_id: The memory ID for the saved meeting memory.
        person_memory_ids: Memory IDs for all person records (attendees + deduped).
        mention_memory_ids: Memory IDs for mention records (mentioned-but-absent people).
        interaction_memory_ids: Memory IDs for interaction records (attendee interactions).
        relationship_ids: Relationship edge IDs created (attended_by, mentioned_in).
        follow_up_candidates: List of follow-up task dicts — never auto-created as bd issues.
        run_id: UUID4 string generated per ingest call; stored in each memory's metadata.
    """

    meeting_memory_id: int
    person_memory_ids: list[int] = field(default_factory=list)
    mention_memory_ids: list[int] = field(default_factory=list)
    interaction_memory_ids: list[int] = field(default_factory=list)
    relationship_ids: list[int] = field(default_factory=list)
    follow_up_candidates: list[dict] = field(default_factory=list)
    run_id: str = ""
