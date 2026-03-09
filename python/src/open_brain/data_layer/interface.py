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
    """Parameters for saving a new memory."""

    text: str
    type: str | None = None
    project: str | None = None
    title: str | None = None
    subtitle: str | None = None
    narrative: str | None = None
    session_ref: str | None = None


@dataclass
class UpdateMemoryParams:
    """Parameters for updating an existing memory."""

    id: int
    text: str | None = None
    type: str | None = None
    project: str | None = None
    title: str | None = None
    subtitle: str | None = None
    narrative: str | None = None


@dataclass
class Memory:
    """A single memory entry."""

    id: int
    index_id: int
    session_id: int | None
    type: str
    title: str | None
    subtitle: str | None
    narrative: str | None
    content: str
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
