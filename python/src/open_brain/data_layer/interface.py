"""DataLayer interface: dataclasses + Protocol.

Importance contract
-------------------
``importance`` is an orthogonal axis from ``priority`` and ``stability``:

- **importance** (this module): caller-declared significance of the *content*
  (``critical`` > ``high`` > ``medium`` > ``low``).  Set at save time; unchanged
  by recall.  Use :func:`rank_importance` to convert to an integer for sorting.

- **priority** (float 0–1): computed recall-score updated by the DB function
  ``update_priority()`` whenever a memory is accessed.  *access_count* feeds
  into priority — it must NOT be mutated by write paths (save/update).

- **stability** (``tentative`` | ``stable`` | ``canonical``): editorial
  life-cycle state, promoted/demoted by refine actions.

``access_count`` recall-only rule
----------------------------------
``access_count`` is incremented only by the ``update_priority()`` DB trigger on
read events.  No write path (``save_memory``, ``update_memory``, etc.) may
modify it.

rank_importance mapping
-----------------------
``critical`` → 3, ``high`` → 2, ``medium`` → 1, ``low`` → 0.
Any other value raises :class:`ValueError`.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, TypedDict

# ─── Importance constants ─────────────────────────────────────────────────────

IMPORTANCE_VALUES: frozenset[str] = frozenset(["critical", "high", "medium", "low"])

_IMPORTANCE_RANK: dict[str, int] = {
    "critical": 3,
    "high": 2,
    "medium": 1,
    "low": 0,
}


def rank_importance(level: str) -> int:
    """Convert an importance level string to an integer rank.

    Returns:
        critical=3, high=2, medium=1, low=0

    Raises:
        ValueError: if *level* is not one of the four valid importance values.
    """
    try:
        return _IMPORTANCE_RANK[level]
    except (KeyError, TypeError):
        raise ValueError(
            f"Invalid importance: {level!r}. Must be one of: {sorted(IMPORTANCE_VALUES)}"
        )


# ─── Domain metadata schemas ──────────────────────────────────────────────────


class EventMetadata(TypedDict, total=False):
    """Structured metadata for type='event' memories."""

    when: str  # ISO datetime (required for events)
    who: list[str]
    where: str
    recurrence: str


class PersonMetadata(TypedDict, total=False):
    """Structured metadata for type='person' memories."""

    name: str
    org: str
    role: str
    relationship: str
    last_contact: str  # ISO datetime


class HouseholdMetadata(TypedDict, total=False):
    """Structured metadata for type='household' memories."""

    category: str
    item: str
    location: str
    details: str
    warranty_expiry: str  # ISO datetime


class DecisionMetadata(TypedDict, total=False):
    """Structured metadata for type='decision' memories."""

    what: str
    context: str
    owner: str
    alternatives: list[str]
    rationale: str


class MeetingMetadata(TypedDict, total=False):
    """Structured metadata for type='meeting' memories."""

    attendees: list[str]
    topic: str
    key_points: list[str]
    action_items: list[str]
    date: str  # ISO datetime


class MentionMetadata(TypedDict, total=False):
    """Structured metadata for type='mention' memories."""

    person_ref: str          # stable identifier pointing to a person memory
    context: str             # short snippet from source
    source_memory_ref: str   # memory id that contains the mention
    sentiment_hint: str      # positive|neutral|negative|ambiguous|unknown


class InteractionMetadata(TypedDict, total=False):
    """Structured metadata for type='interaction' memories."""

    person_ref: str
    channel: str             # meeting|call|email|chat|unknown
    direction: str           # inbound|outbound|bidirectional
    summary: str
    occurred_at: str         # ISO 8601 datetime
    follow_up_needed: bool


def _is_iso_datetime(value: str) -> bool:
    """Check if a string is a valid ISO 8601 datetime."""
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def validate_domain_metadata(memory_type: str | None, metadata: dict[str, Any] | None) -> list[str]:
    """Validate domain-specific metadata fields.

    Returns a list of human-readable warning strings.
    Unknown types and None type return no warnings (AK4).
    Does not raise exceptions — all validation results are returned as warnings.
    """
    if memory_type is None:
        return []

    md = metadata or {}
    warnings: list[str] = []

    if memory_type == "event":
        when = md.get("when")
        if when is None:
            warnings.append("event metadata missing required field 'when' (expected ISO datetime, e.g. '2026-04-15T10:00:00')")
        elif not _is_iso_datetime(str(when)):
            warnings.append(f"event metadata field 'when' is not a valid ISO datetime: {when!r}")

    elif memory_type == "person":
        last_contact = md.get("last_contact")
        if last_contact is not None and not _is_iso_datetime(str(last_contact)):
            warnings.append(f"person metadata field 'last_contact' is not a valid ISO datetime: {last_contact!r}")

    elif memory_type == "meeting":
        date = md.get("date")
        if date is not None and not _is_iso_datetime(str(date)):
            warnings.append(f"meeting metadata field 'date' is not a valid ISO datetime: {date!r}")

    elif memory_type == "household":
        warranty_expiry = md.get("warranty_expiry")
        if warranty_expiry is not None and not _is_iso_datetime(str(warranty_expiry)):
            warnings.append(f"household metadata field 'warranty_expiry' is not a valid ISO datetime: {warranty_expiry!r}")

    elif memory_type == "mention":
        person_ref = md.get("person_ref")
        if person_ref is None:
            warnings.append("mention metadata missing recommended field 'person_ref' (expected stable identifier pointing to a person memory)")

    elif memory_type == "interaction":
        person_ref = md.get("person_ref")
        if person_ref is None:
            warnings.append("interaction metadata missing recommended field 'person_ref' (expected stable identifier pointing to a person memory)")
        occurred_at = md.get("occurred_at")
        if occurred_at is not None and not _is_iso_datetime(str(occurred_at)):
            warnings.append(f"interaction metadata field 'occurred_at' is not a valid ISO datetime: {occurred_at!r}")

    # All other types pass through without validation
    return warnings


@dataclass
class SearchParams:
    """Parameters for hybrid memory search."""

    query: str | None = None
    limit: int | None = None
    offset: int | None = None
    project: str | None = None
    type: str | None = None
    obs_type: str | None = None
    date_start: str | None = None
    date_end: str | None = None
    order_by: str | None = None
    file_path: str | None = None
    metadata_filter: dict[str, str] | None = None
    author: str | None = None  # filter by user_id (contributor)


@dataclass
class TimelineParams:
    """Parameters for timeline context retrieval."""

    anchor: int | None = None
    query: str | None = None
    depth_before: int | None = None
    depth_after: int | None = None
    project: str | None = None
    date_start: str | None = None
    date_end: str | None = None


@dataclass
class SaveMemoryParams:
    """Parameters for saving a new memory.

    Field semantics:
    - text       → stored as ``content`` column. PRIMARY searchable body. Required.
                   Put the main substance here — this is what gets embedded and FTS-indexed.
                   Do NOT leave this minimal while putting all substance in ``narrative``.
    - narrative  → optional prose context: background story, reasoning, or "why".
                   Supplements ``text``; also embedded. Use when the *why* adds value beyond the fact.
    - title      → short headline / identifier (1 line).
    - subtitle   → secondary label, tags, or category hint.
    """

    text: str  # PRIMARY content → stored as `content` column; embedded + FTS-searched
    type: str | None = None
    project: str | None = None
    title: str | None = None  # short headline
    subtitle: str | None = None  # secondary label / tags
    narrative: str | None = None  # optional prose context / reasoning (supplements text)
    session_ref: str | None = None
    metadata: dict[str, Any] | None = None
    user_id: str | None = None  # authenticated user who created this memory
    upsert_mode: Literal["append", "replace"] = "append"
    importance: str = "medium"  # caller-declared significance: critical|high|medium|low
    dedup_mode: Literal["skip", "merge"] = "skip"  # auto-dedup strategy at store time
    duplicate_of: int | None = None  # caller-asserted duplicate; short-circuits dedup logic


@dataclass
class UpdateMemoryParams:
    """Parameters for updating an existing memory.

    See ``SaveMemoryParams`` for field semantics.
    Only provided (non-None) fields are applied; others are left unchanged.
    """

    id: int
    text: str | None = None  # PRIMARY content → stored as `content` column; embedded + FTS-searched
    type: str | None = None
    project: str | None = None
    title: str | None = None  # short headline
    subtitle: str | None = None  # secondary label / tags
    narrative: str | None = None  # optional prose context / reasoning (supplements text)
    metadata: dict[str, Any] | None = None  # JSONB-merged into existing metadata


@dataclass
class Memory:
    """A single memory entry.

    Field semantics:
    - content   → PRIMARY searchable body (stored from ``SaveMemoryParams.text``).
                  Embedded and FTS-indexed. Should contain the main substance.
    - narrative → optional supplementary prose context or reasoning.
                  Also embedded, but secondary to ``content``.
    - title     → short headline / identifier.
    - subtitle  → secondary label, tags, or category hint.
    """

    id: int
    index_id: int
    session_id: int | None
    type: str
    title: str | None  # short headline
    subtitle: str | None  # secondary label / tags
    narrative: str | None  # optional prose context / reasoning (supplements content)
    content: str  # PRIMARY searchable body (from SaveMemoryParams.text); embedded + FTS-indexed
    metadata: dict[str, Any]
    priority: float
    stability: str
    access_count: int
    last_accessed_at: str | None
    created_at: str
    updated_at: str
    user_id: str | None = None  # user who created this memory (NULL for pre-feature or API key auth)
    importance: str = "medium"  # caller-declared significance: critical|high|medium|low
    last_decay_at: str | None = None  # timestamp of last decay application (None = never decayed)
    project_name: str | None = None  # populated by get_wake_up_memories JOIN


@dataclass
class RefineParams:
    """Parameters for memory refinement."""

    scope: str | None = None  # "recent" | "project:<name>" | "duplicates" | "low-priority"
    limit: int | None = None
    dry_run: bool = False


@dataclass
class RefineAction:
    """A single refinement action suggested by the LLM."""

    action: str  # "merge" | "promote" | "demote" | "delete"
    memory_ids: list[int]
    reason: str
    executed: bool = False
    similarity: float | None = None  # cosine similarity (duplicates scope only)
    skip_llm_merge: bool = False  # skip LLM-powered content merge


@dataclass
class RefineResult:
    """Result of a refine_memories operation."""

    analyzed: int
    actions: list[RefineAction]
    summary: str


@dataclass
class SearchResult:
    """Result of a search operation."""

    results: list[Memory]
    total: int


@dataclass
class TimelineResult:
    """Result of a timeline operation."""

    results: list[Memory]
    anchor_id: int | None


@dataclass
class SaveMemoryResult:
    """Result of saving a memory."""

    id: int
    message: str
    duplicate_of: int | None = None


@dataclass
class DeleteParams:
    """Parameters for bulk-deleting memories.

    At least one filter must be provided (ids, or project/type/before combo).
    """

    ids: list[int] | None = None
    project: str | None = None
    type: str | None = None
    before: str | None = None  # ISO date, e.g. "2026-03-01"


@dataclass
class DeleteResult:
    """Result of a delete operation."""

    deleted: int


@dataclass
class TriageParams:
    """Parameters for memory triage."""

    scope: str | None = None  # "recent" | "project:<name>" | "type:<name>" | "session_ref:<prefix>" | None
    limit: int | None = None
    dry_run: bool = False


@dataclass
class TriageAction:
    """A single triage decision for one memory."""

    action: str  # "keep" | "merge" | "promote" | "scaffold" | "archive"
    memory_id: int
    reason: str
    memory_type: str
    memory_title: str | None
    executed: bool = False


@dataclass
class TriageResult:
    """Result of a triage_memories operation."""

    analyzed: int
    actions: list[TriageAction]
    summary: str


@dataclass
class MaterializeParams:
    """Parameters for materializing triage actions."""

    triage_actions: list[TriageAction]
    dry_run: bool = False


@dataclass
class MaterializeActionResult:
    """Result of a single materialization action."""

    memory_id: int
    action: str
    success: bool
    detail: str


@dataclass
class MaterializeResult:
    """Result of a materialize_memories operation."""

    processed: int
    results: list[MaterializeActionResult]
    summary: str


@dataclass
class DecayParams:
    """Parameters for memory decay/boost operation."""

    stale_days: int = 30          # memories not accessed in N days get decayed
    boost_days: int = 7           # recent memories (< N days) are protected
    decay_factor: float = 0.9     # priority *= decay_factor for stale memories
    boost_threshold: int = 10     # access_count >= N triggers priority boost
    boost_factor: float = 1.1     # priority *= boost_factor for frequently accessed
    dry_run: bool = False

    def __post_init__(self) -> None:
        if not (0 < self.decay_factor < 1):
            raise ValueError(f"decay_factor must be in (0, 1), got {self.decay_factor}")
        if self.boost_factor < 1.0:
            raise ValueError(f"boost_factor must be >= 1.0, got {self.boost_factor}")
        if self.stale_days <= 0:
            raise ValueError(f"stale_days must be > 0, got {self.stale_days}")
        if self.boost_days <= 0:
            raise ValueError(f"boost_days must be > 0, got {self.boost_days}")
        if self.boost_threshold < 1:
            raise ValueError(f"boost_threshold must be >= 1, got {self.boost_threshold}")


@dataclass
class DecayResult:
    """Result of a decay_memories operation."""

    decayed: int         # count of memories whose priority was reduced
    boosted: int         # count of memories whose priority was boosted
    recent_memories: int  # count of recent memories (< boost_days old); protected from decay but may still be boosted
    summary: str


@dataclass
class ClusterPlan:
    """Plan for a single cluster of near-duplicate memories."""

    cluster_id: int
    members: list[int]        # all member IDs
    canonical_id: int         # the one to keep
    to_delete: list[int]      # members minus canonical


_VALID_COMPACT_STRATEGIES = frozenset(
    ["keep_highest_access", "keep_latest", "keep_most_comprehensive"]
)


@dataclass
class CompactParams:
    """Parameters for compact_memories operation."""

    scope: str | None = None
    threshold: float = 0.87
    strategy: str = "keep_highest_access"
    dry_run: bool = True

    def __post_init__(self) -> None:
        if not (0.0 <= self.threshold <= 1.0):
            raise ValueError(
                f"threshold must be between 0.0 and 1.0, got {self.threshold}"
            )
        if self.strategy not in _VALID_COMPACT_STRATEGIES:
            raise ValueError(
                f"Unknown strategy: {self.strategy!r}. Expected one of: "
                + ", ".join(sorted(_VALID_COMPACT_STRATEGIES))
            )
        if self.scope is not None:
            if not (
                self.scope.startswith("project:")
                or self.scope.startswith("type:")
            ):
                raise ValueError(
                    f"Unknown scope format: {self.scope!r}. "
                    "Expected None, 'project:<name>', or 'type:<name>'"
                )


@dataclass
class CompactResult:
    """Result of a compact_memories operation."""

    clusters_found: int
    memories_deleted: int
    memories_kept: list[int]
    deleted_ids: list[int]
    strategy_used: str
    plan: list[ClusterPlan]   # always populated (dry_run=True: plan only; False: executed)


class DataLayer(Protocol):
    """Protocol defining the data layer interface."""

    async def search(self, params: SearchParams) -> SearchResult: ...

    async def timeline(self, params: TimelineParams) -> TimelineResult: ...

    async def get_observations(self, ids: list[int]) -> list[Memory]: ...

    async def save_memory(self, params: SaveMemoryParams) -> SaveMemoryResult: ...

    async def update_memory(self, params: UpdateMemoryParams) -> SaveMemoryResult: ...

    async def search_by_concept(
        self, query: str, limit: int | None = None, project: str | None = None
    ) -> dict[str, list[Memory]]: ...

    async def get_context(
        self, limit: int | None = None, project: str | None = None
    ) -> dict[str, list[Any]]: ...

    async def stats(self) -> dict[str, Any]: ...

    async def refine_memories(self, params: RefineParams) -> RefineResult: ...

    async def delete_memories(self, params: DeleteParams) -> DeleteResult: ...

    async def triage_memories(self, params: TriageParams) -> TriageResult: ...

    async def materialize_memories(self, params: MaterializeParams) -> MaterializeResult: ...

    async def decay_memories(self, params: DecayParams) -> DecayResult: ...

    async def compact_memories(self, params: CompactParams) -> CompactResult: ...

    async def get_wake_up_memories(self, limit: int = 500, project: str | None = None) -> list[Memory]: ...
