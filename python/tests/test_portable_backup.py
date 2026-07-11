"""Tests for portable Open Brain backup and restore."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import open_brain.portable_backup as portable_backup
from open_brain.data_layer.postgres import PostgresDataLayer
from open_brain.portable_backup import (
    BundleIntegrityError,
    ExportTargetNotEmptyError,
    ForbiddenExportContentError,
    IncompatibleBundleVersionError,
    RestoreTargetNotEmptyError,
    restore_bundle,
)


FIXED_EXPORT_TIME = datetime(2026, 7, 11, 12, 30, 0, tzinfo=UTC)


def _content_hash(content: str) -> str:
    """Return the Open Brain content hash for fixture records."""
    return hashlib.sha256(content.encode()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read fixture JSONL records."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Build a mocked asyncpg pool."""

    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


def _transaction_context():
    """Build a mocked asyncpg transaction context manager."""

    @asynccontextmanager
    async def fake_transaction():
        yield

    return fake_transaction()


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


class EmptyRestoreStore:
    """In-memory restore store that mirrors the atomic postgres restore contract.

    The emptiness/same-bundle check and the write happen inside one call (the
    real store makes this atomic with a locked transaction); a populated target
    that does not match the bundle raises, and a matching target is a no-op.
    """

    def __init__(self) -> None:
        self.indexes: dict[int, dict[str, Any]] = {}
        self.memories: dict[int, dict[str, Any]] = {}
        self.relationships: dict[tuple[int, int, str], dict[str, Any]] = {}
        self.restore_calls = 0
        self.embedded_ids: set[int] = set()
        self.embed_calls = 0

    async def export_portable_records(self) -> dict[str, list[dict[str, Any]]]:
        """Return current in-memory portable records."""
        return {
            "indexes": list(self.indexes.values()),
            "memories": list(self.memories.values()),
            "relationships": list(self.relationships.values()),
        }

    async def portable_closure_counts(self) -> dict[str, int]:
        """Return closure row counts."""
        return {
            "indexes": len(self.indexes),
            "memories": len(self.memories),
            "relationships": len(self.relationships),
        }

    async def restore_portable_records(
        self,
        indexes: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        *,
        regenerate_embeddings: bool,
    ) -> dict[str, Any]:
        """Atomically check emptiness/same-bundle, then insert (idempotent)."""
        expected = portable_backup._canonical_records(
            {"indexes": indexes, "memories": memories, "relationships": relationships}
        )
        existing = portable_backup._canonical_records(
            {
                "indexes": list(self.indexes.values()),
                "memories": list(self.memories.values()),
                "relationships": list(self.relationships.values()),
            }
        )
        populated = bool(self.indexes or self.memories or self.relationships)
        already_restored = False
        if populated:
            if existing == expected:
                already_restored = True
            else:
                raise RestoreTargetNotEmptyError(
                    "Restore target already contains portable knowledge rows "
                    "that do not match the bundle"
                )
        else:
            self.restore_calls += 1
            for index in indexes:
                self.indexes.setdefault(index["id"], dict(index))
            for memory in memories:
                self.memories.setdefault(memory["id"], dict(memory))
            for relationship in relationships:
                key = (
                    relationship["source_id"],
                    relationship["target_id"],
                    relationship["relation_type"],
                )
                self.relationships.setdefault(key, dict(relationship))

        if regenerate_embeddings:
            if already_restored:
                targets = [
                    memory["id"]
                    for memory in memories
                    if memory["id"] not in self.embedded_ids
                ]
            else:
                targets = [memory["id"] for memory in memories]
            for memory_id in targets:
                self.embedded_ids.add(memory_id)
                self.embed_calls += 1

        return {"already_restored": already_restored}


@pytest.mark.asyncio
async def test_restore_refuses_populated_target_before_writing(tmp_path: Path) -> None:
    """Restore fails closed when the target holds rows that do not match the bundle."""
    bundle = tmp_path / "bundle"
    await portable_backup.export_bundle(
        bundle,
        FixturePortableStore(),
        source_label="fixture",
        created_at=FIXED_EXPORT_TIME,
    )

    # Target already holds an unrelated index row -> populated and non-matching.
    store = EmptyRestoreStore()
    store.indexes[99] = {"id": 99, "name": "unrelated"}

    with pytest.raises(RestoreTargetNotEmptyError):
        await restore_bundle(bundle, store)

    # No bundle rows were written and the pre-existing row is untouched.
    assert store.restore_calls == 0
    assert store.memories == {}
    assert store.relationships == {}
    assert store.indexes == {99: {"id": 99, "name": "unrelated"}}


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


@pytest.mark.asyncio
async def test_export_refuses_non_empty_target_directory(tmp_path: Path) -> None:
    """Export fails closed on a pre-existing non-empty target dir (finding 1)."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    # A stale file from a prior export must not silently ride along.
    (bundle / "stale-credentials.txt").write_text("LEAKED", encoding="utf-8")

    with pytest.raises(ExportTargetNotEmptyError):
        await portable_backup.export_bundle(
            bundle,
            FixturePortableStore(),
            source_label="fixture",
            created_at=FIXED_EXPORT_TIME,
        )

    # The stale file was left untouched (no silent deletion), and no bundle
    # files were written.
    assert (bundle / "stale-credentials.txt").read_text(encoding="utf-8") == "LEAKED"
    assert not (bundle / "manifest.json").exists()

    # An existing but empty directory is acceptable.
    empty = tmp_path / "empty"
    empty.mkdir()
    manifest = await portable_backup.export_bundle(
        empty,
        FixturePortableStore(),
        source_label="fixture",
        created_at=FIXED_EXPORT_TIME,
    )
    assert (empty / "manifest.json").exists()
    assert manifest["contains_credentials"] is False


@pytest.mark.asyncio
async def test_export_raises_on_nested_forbidden_metadata_key(tmp_path: Path) -> None:
    """A credential-shaped key nested in metadata makes export fail closed (finding 2)."""
    bundle = tmp_path / "bundle"
    store = FixturePortableStore()
    # token_hash nested INSIDE the metadata jsonb blob would survive field
    # projection (metadata is exported verbatim) and leak into the bundle.
    store.records["memories"][0]["metadata"]["token_hash"] = "LEAKED_CREDENTIAL"

    with pytest.raises(ForbiddenExportContentError, match="token_hash"):
        await portable_backup.export_bundle(
            bundle,
            store,
            source_label="fixture",
            created_at=FIXED_EXPORT_TIME,
        )

    # No bundle files were written (export raised before writing).
    assert not (bundle / "manifest.json").exists()
    assert not (bundle / "memories.jsonl").exists()


@pytest.mark.asyncio
async def test_export_raises_on_deeply_nested_forbidden_key(tmp_path: Path) -> None:
    """Forbidden keys nested inside lists/dicts in metadata are also caught."""
    bundle = tmp_path / "bundle"
    store = FixturePortableStore()
    store.records["memories"][1]["metadata"]["audit"][0]["api_key"] = "sk-leaked"

    with pytest.raises(ForbiddenExportContentError, match="api_key"):
        await portable_backup.export_bundle(
            bundle,
            store,
            source_label="fixture",
            created_at=FIXED_EXPORT_TIME,
        )


@pytest.mark.asyncio
async def test_restore_rejects_bundle_with_tampered_file_hash(tmp_path: Path) -> None:
    """Restore verifies per-file SHA-256 before any write (finding 3)."""
    bundle = tmp_path / "bundle"
    await portable_backup.export_bundle(
        bundle,
        FixturePortableStore(),
        source_label="fixture",
        created_at=FIXED_EXPORT_TIME,
    )
    # Tamper with a JSONL file after the manifest hash was computed.
    memories_path = bundle / "memories.jsonl"
    memories_path.write_text(
        memories_path.read_text(encoding="utf-8").replace("Ada", "Eve"),
        encoding="utf-8",
    )

    target = EmptyRestoreStore()
    with pytest.raises(BundleIntegrityError, match="SHA-256 mismatch"):
        await portable_backup.restore_bundle(bundle, target, regenerate_embeddings=False)

    # Nothing was written to the target.
    assert target.restore_calls == 0
    assert await target.portable_closure_counts() == {
        "indexes": 0,
        "memories": 0,
        "relationships": 0,
    }


@pytest.mark.asyncio
async def test_restore_rejects_bundle_with_record_count_mismatch(tmp_path: Path) -> None:
    """Restore verifies declared record counts before any write (finding 3)."""
    bundle = tmp_path / "bundle"
    await portable_backup.export_bundle(
        bundle,
        FixturePortableStore(),
        source_label="fixture",
        created_at=FIXED_EXPORT_TIME,
    )
    # Overstate the memory count in the manifest and recompute the file hash so
    # only the count check (not the hash check) can catch it.
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Truncate one memory line from the file, then re-hash so hash matches but
    # the declared count no longer does.
    memories_path = bundle / "memories.jsonl"
    lines = memories_path.read_text(encoding="utf-8").splitlines()
    memories_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    manifest["files"]["memories.jsonl"]["sha256"] = hashlib.sha256(
        memories_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    target = EmptyRestoreStore()
    with pytest.raises(BundleIntegrityError, match="Record count mismatch"):
        await portable_backup.restore_bundle(bundle, target, regenerate_embeddings=False)
    assert target.restore_calls == 0


@pytest.mark.asyncio
async def test_restore_rejects_incompatible_bundle_version(tmp_path: Path) -> None:
    """Restore rejects a bundle_format_version it does not understand (finding 3)."""
    bundle = tmp_path / "bundle"
    await portable_backup.export_bundle(
        bundle,
        FixturePortableStore(),
        source_label="fixture",
        created_at=FIXED_EXPORT_TIME,
    )
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["bundle_format_version"] = "2.0.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    target = EmptyRestoreStore()
    with pytest.raises(IncompatibleBundleVersionError):
        await portable_backup.restore_bundle(bundle, target, regenerate_embeddings=False)
    assert target.restore_calls == 0


@pytest.mark.asyncio
async def test_matching_rerun_regenerates_missing_embeddings(tmp_path: Path) -> None:
    """A same-bundle rerun still regenerates embeddings for missing memories (finding 6)."""
    bundle = tmp_path / "bundle"
    await portable_backup.export_bundle(
        bundle,
        FixturePortableStore(),
        source_label="fixture",
        created_at=FIXED_EXPORT_TIME,
    )
    target = EmptyRestoreStore()

    # First restore skips embeddings entirely.
    first = await portable_backup.restore_bundle(
        bundle, target, regenerate_embeddings=False
    )
    assert first.get("already_restored") is None
    assert target.embed_calls == 0
    assert target.embedded_ids == set()

    # Rerun with the same bundle (no-op for record data) but asking for
    # embeddings must still generate the missing ones instead of reporting
    # success with embeddings absent.
    second = await portable_backup.restore_bundle(
        bundle, target, regenerate_embeddings=True
    )
    assert second["already_restored"] is True
    assert target.embed_calls == 2
    assert target.embedded_ids == {10, 20}

    # A further rerun does not re-embed already-embedded memories.
    third = await portable_backup.restore_bundle(
        bundle, target, regenerate_embeddings=True
    )
    assert third["already_restored"] is True
    assert target.embed_calls == 2


@pytest.mark.asyncio
async def test_restore_recreates_graph_and_same_bundle_rerun_creates_no_duplicates(
    tmp_path: Path,
) -> None:
    """Restore is id-preserving and idempotent for the same portable bundle."""
    bundle = tmp_path / "bundle"
    await portable_backup.export_bundle(
        bundle,
        FixturePortableStore(),
        source_label="fixture",
        created_at=FIXED_EXPORT_TIME,
    )
    target = EmptyRestoreStore()

    first_result = await portable_backup.restore_bundle(
        bundle,
        target,
        regenerate_embeddings=False,
    )
    assert first_result["restored"] == {
        "indexes": 2,
        "memories": 2,
        "relationships": 2,
    }
    assert await target.portable_closure_counts() == {
        "indexes": 2,
        "memories": 2,
        "relationships": 2,
    }
    assert target.memories[10]["metadata"]["canonical_entity"] is True
    assert target.memories[20]["metadata"]["paperless_reference"]["document_id"] == 101

    second_result = await portable_backup.restore_bundle(
        bundle,
        target,
        regenerate_embeddings=False,
    )

    assert second_result["restored"] == {
        "indexes": 2,
        "memories": 2,
        "relationships": 2,
    }
    assert await target.portable_closure_counts() == {
        "indexes": 2,
        "memories": 2,
        "relationships": 2,
    }


@pytest.mark.asyncio
async def test_verify_round_trip_reports_hashes_edges_and_canonical_ids(
    tmp_path: Path,
) -> None:
    """Round-trip verification reports record, edge, and content integrity."""
    bundle = tmp_path / "bundle"
    await portable_backup.export_bundle(
        bundle,
        FixturePortableStore(),
        source_label="fixture",
        created_at=FIXED_EXPORT_TIME,
    )
    target = EmptyRestoreStore()
    await portable_backup.restore_bundle(
        bundle,
        target,
        regenerate_embeddings=False,
    )

    report = await portable_backup.verify_round_trip(bundle, target)

    assert report == {
        "bundle_format_version": "1.0.0",
        "ok": True,
        "memories": {
            "expected": 2,
            "restored": 2,
            "content_hash_matches": 2,
            "content_hash_mismatches": [],
            "record_hash_mismatches": [],
        },
        "relationships": {
            "expected": 2,
            "restored": 2,
            "missing": [],
            "extra": [],
            "record_mismatches": [],
        },
        "indexes": {
            "expected": 2,
            "restored": 2,
            "missing": [],
            "extra": [],
        },
        "canonical_entities": {
            "expected": 1,
            "restored": 1,
            "preserved_ids": [10],
        },
    }

    target.memories[10]["metadata"] = {
        **target.memories[10]["metadata"],
        "status": "corrupted",
    }
    corrupted_report = await portable_backup.verify_round_trip(bundle, target)

    assert corrupted_report["ok"] is False
    assert corrupted_report["memories"]["content_hash_matches"] == 2
    assert corrupted_report["memories"]["record_hash_mismatches"] == [10]


@pytest.mark.asyncio
async def test_verify_round_trip_catches_previously_uncovered_field_corruption(
    tmp_path: Path,
) -> None:
    """Corruption of type, link_type, or an index name is now caught (finding 5)."""
    bundle = tmp_path / "bundle"
    await portable_backup.export_bundle(
        bundle,
        FixturePortableStore(),
        source_label="fixture",
        created_at=FIXED_EXPORT_TIME,
    )
    target = EmptyRestoreStore()
    await portable_backup.restore_bundle(bundle, target, regenerate_embeddings=False)

    assert (await portable_backup.verify_round_trip(bundle, target))["ok"] is True

    # 1. memory.type (content_hash unchanged -> only the full-record hash catches it).
    target.memories[10]["type"] = "observation"
    type_report = await portable_backup.verify_round_trip(bundle, target)
    assert type_report["ok"] is False
    assert type_report["memories"]["content_hash_matches"] == 2
    assert type_report["memories"]["record_hash_mismatches"] == [10]
    target.memories[10]["type"] = "person"  # restore

    # 2. relationship.link_type (edge set unchanged -> only record comparison catches it).
    edge_key = (10, 20, "mentioned_in")
    target.relationships[edge_key]["link_type"] = "tampered"
    link_report = await portable_backup.verify_round_trip(bundle, target)
    assert link_report["ok"] is False
    assert link_report["relationships"]["missing"] == []
    assert link_report["relationships"]["extra"] == []
    assert link_report["relationships"]["record_mismatches"] == [edge_key]
    target.relationships[edge_key]["link_type"] = "mentioned_in"  # restore

    # 3. index name (was count-only before -> now an id/name set comparison).
    target.indexes[1]["name"] = "renamed"
    index_report = await portable_backup.verify_round_trip(bundle, target)
    assert index_report["ok"] is False
    assert index_report["indexes"]["expected"] == index_report["indexes"]["restored"]
    assert index_report["indexes"]["missing"] == [(1, "alpha")]
    assert index_report["indexes"]["extra"] == [(1, "renamed")]


@pytest.mark.asyncio
async def test_postgres_export_reads_only_portable_closure_tables() -> None:
    """Postgres export reads indexes, memories without embeddings, and relationships."""
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_transaction_context())
    conn.fetch.side_effect = [
        [{"id": 1, "name": "alpha"}],
        [
            {
                "id": 10,
                "index_id": 1,
                "session_id": None,
                "type": "observation",
                "title": "T",
                "subtitle": None,
                "narrative": None,
                "content": "C",
                "metadata": {"content_hash": _content_hash("C")},
                "priority": 0.5,
                "stability": "stable",
                "access_count": 0,
                "last_accessed_at": None,
                "created_at": "2026-07-11T09:00:00+00:00",
                "updated_at": "2026-07-11T09:00:00+00:00",
                "user_id": None,
                "importance": "medium",
                "last_decay_at": None,
                "session_ref": None,
            }
        ],
        [
            {
                "id": 7,
                "source_id": 10,
                "target_id": 20,
                "relation_type": "references",
                "link_type": "references",
                "confidence": 1.0,
                "metadata": {"source": "fixture"},
            }
        ],
    ]
    pool = _make_pool(conn)

    with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
        records = await PostgresDataLayer().export_portable_records()

    assert records["indexes"] == [{"id": 1, "name": "alpha"}]
    assert records["memories"][0]["id"] == 10
    assert "embedding" not in records["memories"][0]
    assert records["relationships"][0]["relation_type"] == "references"

    # Finding 7: the three reads share one REPEATABLE READ snapshot.
    conn.transaction.assert_called_once_with(isolation="repeatable_read")

    export_sql = "\n".join(call.args[0] for call in conn.fetch.call_args_list).lower()
    for forbidden_table in (
        "url_tokens",
        "memory_usage_log",
        "embedding_token_log",
        "sessions",
        "session_summaries",
    ):
        assert forbidden_table not in export_sql
    assert "embedding" not in export_sql


@pytest.mark.asyncio
async def test_postgres_restore_uses_explicit_ids_conflicts_and_sequence_repair() -> None:
    """Postgres restore bypasses save_memory and writes explicit ids transactionally."""
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_transaction_context())
    # Empty closure so the in-transaction emptiness check proceeds to insert.
    conn.fetch.side_effect = [[], [], []]
    pool = _make_pool(conn)
    fixture = FixturePortableStore().records

    with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
        result = await PostgresDataLayer().restore_portable_records(
            fixture["indexes"],
            fixture["memories"],
            fixture["relationships"],
            regenerate_embeddings=False,
        )

    assert result == {"already_restored": False}

    restore_sql = "\n".join(call.args[0] for call in conn.execute.call_args_list)
    # Findings 4 & 8: the closure tables are locked before the check-then-write.
    assert (
        "LOCK TABLE memory_indexes, memories, memory_relationships IN EXCLUSIVE MODE"
        in restore_sql
    )
    assert "INSERT INTO memory_indexes (id, name)" in restore_sql
    assert "INSERT INTO memories (" in restore_sql
    assert "ON CONFLICT (id) DO NOTHING" in restore_sql
    assert "INSERT INTO memory_relationships" in restore_sql
    assert "ON CONFLICT (source_id, target_id, relation_type) DO NOTHING" in restore_sql
    assert "pg_get_serial_sequence('memories', 'id')" in restore_sql
    assert "pg_get_serial_sequence('memory_relationships', 'id')" in restore_sql
    # The lock and inserts all happen inside a single transaction.
    conn.transaction.assert_called_once_with()
