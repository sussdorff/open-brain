"""DataLayer interface: dataclasses + Protocol."""

from dataclasses import dataclass, field
from typing import Any, Protocol


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

    scope: str | None = None  # "recent" | "project:<name>" | "type:<name>" | None
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
