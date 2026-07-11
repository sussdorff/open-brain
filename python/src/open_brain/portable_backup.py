"""Portable backup and restore helpers for Open Brain knowledge."""

from __future__ import annotations

import json
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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a deterministic JSONL file into record dictionaries."""
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"Portable backup record must be an object: {path}")
        records.append(value)
    return records


async def restore_bundle(
    bundle_path: str | Path,
    store: PortableBackupStore,
    *,
    regenerate_embeddings: bool = True,
) -> dict[str, Any]:
    """Restore a portable bundle into a store."""
    path = Path(bundle_path)
    counts = await store.portable_closure_counts()
    populated = {name: count for name, count in counts.items() if count > 0}
    if populated:
        raise RestoreTargetNotEmptyError(
            "Restore target already contains portable knowledge rows: "
            + ", ".join(f"{name}={count}" for name, count in sorted(populated.items()))
        )

    indexes = _read_jsonl(path / "indexes.jsonl")
    memories = _read_jsonl(path / "memories.jsonl")
    relationships = _read_jsonl(path / "relationships.jsonl")
    await store.restore_portable_records(
        indexes,
        memories,
        relationships,
        regenerate_embeddings=regenerate_embeddings,
    )
    return {
        "bundle_path": str(path),
        "restored": {
            "indexes": len(indexes),
            "memories": len(memories),
            "relationships": len(relationships),
        },
        "regenerate_embeddings": regenerate_embeddings,
    }
