"""Base IngestAdapter Protocol — per cr3.5 ADR."""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from open_brain.ingest.models import IngestResult


class IngestAdapter(Protocol):
    """Protocol for all ingest adapters.

    Any adapter that can ingest content into open-brain must implement this
    interface. The ``source_ref`` uniquely identifies the source (e.g. a file
    path, URL, or meeting ID) and drives idempotency.
    """

    async def ingest(
        self,
        text: str,
        source_ref: str,
        medium_hint: str | None = None,
    ) -> "IngestResult": ...
