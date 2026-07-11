"""Portable backup and restore helpers for Open Brain knowledge."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class RestoreTargetNotEmptyError(RuntimeError):
    """Raised when a restore target already contains portable knowledge rows."""


class PortableBackupStore(Protocol):
    """Store operations required by portable backup restore."""

    async def portable_closure_counts(self) -> dict[str, int]:
        """Return row counts for the portable knowledge closure."""

    async def restore_portable_records(
        self,
        indexes: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        *,
        regenerate_embeddings: bool,
    ) -> None:
        """Restore portable records into the backing store."""


async def restore_bundle(
    bundle_path: str | Path,
    store: PortableBackupStore,
    *,
    regenerate_embeddings: bool = True,
) -> dict[str, Any]:
    """Restore a portable bundle into a store."""
    return {"bundle_path": str(bundle_path), "regenerate_embeddings": regenerate_embeddings}
