"""Tests for portable Open Brain backup and restore."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import open_brain.portable_backup as portable_backup
from open_brain.portable_backup import RestoreTargetNotEmptyError, restore_bundle


FIXED_EXPORT_TIME = datetime(2026, 7, 11, 12, 30, 0, tzinfo=UTC)


def _content_hash(content: str) -> str:
    """Return the Open Brain content hash for fixture records."""
    return hashlib.sha256(content.encode()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read fixture JSONL records."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class FixturePortableStore:
    """Fake portable store with deterministic knowledge graph records."""

    def __init__(self) -> None:
        self.records = {
            "indexes": [
                {"id": 2, "name": "zeta"},
                {"id": 1, "name": "alpha"},
            ],
            "memories": [
                {
                    "id": 20,
                    "index_id": 2,
                    "session_id": None,
                    "type": "resource",
                    "title": "Paperless reference",
                    "subtitle": None,
                    "narrative": "A referenced document without binary payloads.",
                    "content": "Document 101 is the authoritative source.",
                    "metadata": {
                        "content_hash": _content_hash("Document 101 is the authoritative source."),
                        "paperless_reference": {
                            "document_id": 101,
                            "instance": "paperless-local",
                            "title": "Invoice 2026",
                            "added": "2026-07-11T09:30:00+00:00",
                        },
                    },
                    "priority": 0.7,
                    "stability": "stable",
                    "access_count": 3,
                    "last_accessed_at": None,
                    "created_at": "2026-07-11T09:30:00+00:00",
                    "updated_at": "2026-07-11T09:31:00+00:00",
                    "user_id": "user-1",
                    "importance": "high",
                    "last_decay_at": "2026-07-11T09:32:00+00:00",
                    "session_ref": None,
                    "embedding": [0.1, 0.2],
                },
                {
                    "id": 10,
                    "index_id": 1,
                    "session_id": None,
                    "type": "person",
                    "title": "Ada Lovelace",
                    "subtitle": "Mathematician",
                    "narrative": "Canonical person entity.",
                    "content": "Ada is a protected canonical person entity.",
                    "metadata": {
                        "content_hash": _content_hash("Ada is a protected canonical person entity."),
                        "canonical_entity": True,
                        "canonical_kind": "person",
                        "status": "active",
                        "audit": [
                            {
                                "op": "create",
                                "at": "2026-07-11T09:00:00+00:00",
                                "actor": "test",
                                "note": "fixture",
                            }
                        ],
                    },
                    "priority": 1.0,
                    "stability": "canonical",
                    "access_count": 5,
                    "last_accessed_at": "2026-07-11T09:10:00+00:00",
                    "created_at": "2026-07-11T09:00:00+00:00",
                    "updated_at": "2026-07-11T09:01:00+00:00",
                    "user_id": "user-1",
                    "importance": "critical",
                    "last_decay_at": "2026-07-11T09:02:00+00:00",
                    "session_ref": "session-1",
                    "embedding": [0.3, 0.4],
                },
            ],
            "relationships": [
                {
                    "id": 2,
                    "source_id": 20,
                    "target_id": 10,
                    "relation_type": "references",
                    "link_type": "references",
                    "confidence": 0.8,
                    "metadata": {"source": "fixture-b"},
                },
                {
                    "id": 1,
                    "source_id": 10,
                    "target_id": 20,
                    "relation_type": "mentioned_in",
                    "link_type": "mentioned_in",
                    "confidence": 1.0,
                    "metadata": {"source": "fixture-a"},
                },
            ],
        }

    async def export_portable_records(self) -> dict[str, list[dict[str, Any]]]:
        """Return unsorted records to prove export canonicalizes them."""
        return self.records


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


@pytest.mark.asyncio
async def test_export_writes_versioned_deterministic_bundle_with_metadata_and_paperless_refs(
    tmp_path: Path,
) -> None:
    """Export writes a stable manifest and canonical JSONL records."""
    first_bundle = tmp_path / "first"
    second_bundle = tmp_path / "second"
    store = FixturePortableStore()

    await portable_backup.export_bundle(
        first_bundle,
        store,
        source_label="fixture",
        created_at=FIXED_EXPORT_TIME,
    )
    await portable_backup.export_bundle(
        second_bundle,
        store,
        source_label="fixture",
        created_at=FIXED_EXPORT_TIME,
    )

    for relative_path in (
        "manifest.json",
        "indexes.jsonl",
        "memories.jsonl",
        "relationships.jsonl",
    ):
        assert (first_bundle / relative_path).read_bytes() == (
            second_bundle / relative_path
        ).read_bytes()

    manifest = json.loads((first_bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["bundle_format_version"] == "1.0.0"
    assert manifest["record_counts"] == {
        "indexes": 2,
        "memories": 2,
        "relationships": 2,
    }
    assert manifest["contains_binaries"] is False
    assert manifest["contains_credentials"] is False
    assert manifest["embedding_model"] == {"name": "voyage-4", "dim": 1024}
    for filename in ("indexes.jsonl", "memories.jsonl", "relationships.jsonl"):
        expected_hash = hashlib.sha256((first_bundle / filename).read_bytes()).hexdigest()
        assert manifest["files"][filename]["sha256"] == expected_hash

    indexes = _read_jsonl(first_bundle / "indexes.jsonl")
    assert [record["id"] for record in indexes] == [1, 2]

    memories = _read_jsonl(first_bundle / "memories.jsonl")
    assert [record["id"] for record in memories] == [10, 20]
    assert all("embedding" not in record for record in memories)
    assert memories[0]["metadata"]["canonical_entity"] is True
    assert memories[1]["metadata"]["paperless_reference"] == {
        "document_id": 101,
        "instance": "paperless-local",
        "title": "Invoice 2026",
        "added": "2026-07-11T09:30:00+00:00",
    }

    relationships = _read_jsonl(first_bundle / "relationships.jsonl")
    assert [
        (record["source_id"], record["target_id"], record["relation_type"])
        for record in relationships
    ] == [(10, 20, "mentioned_in"), (20, 10, "references")]


@pytest.mark.asyncio
async def test_export_rejects_credential_source_label_and_bundle_scan_excludes_binaries(
    tmp_path: Path,
) -> None:
    """Export refuses credential-like labels and keeps binary columns out of files."""
    unsafe_bundle = tmp_path / "unsafe"
    secret_source_label = "postgresql://user:secret-password@localhost/open-brain"

    with pytest.raises(ValueError, match="source_label"):
        await portable_backup.export_bundle(
            unsafe_bundle,
            FixturePortableStore(),
            source_label=secret_source_label,
            created_at=FIXED_EXPORT_TIME,
        )

    assert not unsafe_bundle.exists()

    safe_bundle = tmp_path / "safe"
    store = FixturePortableStore()
    store.records["memories"][0]["embedding"] = "DOCUMENT_BINARY_SENTINEL"
    store.records["memories"][1]["token_hash"] = "CREDENTIAL_SENTINEL"
    await portable_backup.export_bundle(
        safe_bundle,
        store,
        source_label="fixture",
        created_at=FIXED_EXPORT_TIME,
    )

    bundle_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(safe_bundle.rglob("*"))
        if path.is_file()
    )
    forbidden_terms = [
        "DOCUMENT_BINARY_SENTINEL",
        "CREDENTIAL_SENTINEL",
        "secret-password",
        "token_hash",
    ]
    assert all(term not in bundle_text for term in forbidden_terms)
