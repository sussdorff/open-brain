"""Tests for portable Open Brain backup and restore."""

from __future__ import annotations

from pathlib import Path

import pytest

from open_brain.portable_backup import RestoreTargetNotEmptyError, restore_bundle


class PopulatedRestoreStore:
    """Fake restore store that starts with existing portable closure rows."""

    def __init__(self) -> None:
        self.restore_called = False

    async def portable_closure_counts(self) -> dict[str, int]:
        """Return non-empty closure counts."""
        return {"indexes": 1, "memories": 0, "relationships": 0}

    async def restore_portable_records(
        self,
        indexes: list[dict],
        memories: list[dict],
        relationships: list[dict],
        *,
        regenerate_embeddings: bool,
    ) -> None:
        """Record that an unsafe restore was attempted."""
        self.restore_called = True


@pytest.mark.asyncio
async def test_restore_refuses_populated_target_before_writing(tmp_path: Path) -> None:
    """Restore fails closed when any portable closure table already has rows."""
    bundle_path = tmp_path / "bundle"
    bundle_path.mkdir()
    (bundle_path / "manifest.json").write_text(
        """
        {
          "bundle_format_version": "1.0.0",
          "record_counts": {"indexes": 0, "memories": 0, "relationships": 0},
          "files": {}
        }
        """,
        encoding="utf-8",
    )
    (bundle_path / "indexes.jsonl").write_text("", encoding="utf-8")
    (bundle_path / "memories.jsonl").write_text("", encoding="utf-8")
    (bundle_path / "relationships.jsonl").write_text("", encoding="utf-8")
    store = PopulatedRestoreStore()

    with pytest.raises(RestoreTargetNotEmptyError):
        await restore_bundle(bundle_path, store)

    assert store.restore_called is False
