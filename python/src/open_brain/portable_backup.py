"""Portable backup and restore helpers for Open Brain knowledge."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

BUNDLE_FORMAT_VERSION = "1.0.0"
OPEN_BRAIN_SCHEMA_VERSION = "postgres-portable-v1"

INDEX_FIELDS = ("id", "name")
MEMORY_FIELDS = (
    "id",
    "index_id",
    "session_id",
    "type",
    "title",
    "subtitle",
    "narrative",
    "content",
    "metadata",
    "priority",
    "stability",
    "access_count",
    "last_accessed_at",
    "created_at",
    "updated_at",
    "user_id",
    "importance",
    "last_decay_at",
    "session_ref",
)
RELATIONSHIP_FIELDS = (
    "id",
    "source_id",
    "target_id",
    "relation_type",
    "link_type",
    "confidence",
    "metadata",
)
JSONL_FILES = {
    "indexes": "indexes.jsonl",
    "memories": "memories.jsonl",
    "relationships": "relationships.jsonl",
}


class RestoreTargetNotEmptyError(RuntimeError):
    """Raised when a restore target already contains portable knowledge rows."""


class PortableBackupStore(Protocol):
    """Store operations required by portable backup and restore."""

    async def export_portable_records(self) -> dict[str, list[dict[str, Any]]]:
        """Return records from the portable knowledge closure."""

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


def _iso_utc(value: datetime) -> str:
    """Return a UTC ISO-8601 timestamp string."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _json_ready(value: Any) -> Any:
    """Normalize values into deterministic JSON-compatible data."""
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, datetime):
        return _iso_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _canonical_json(value: Any, *, pretty: bool = False) -> str:
    """Serialize a value as deterministic JSON."""
    if pretty:
        return json.dumps(
            _json_ready(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _project_fields(record: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Return a record restricted to the portable field contract."""
    return {field: _json_ready(record.get(field)) for field in fields if field in record}


def _canonical_records(
    records: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Return sorted records projected onto the portable bundle contract."""
    indexes = [
        _project_fields(record, INDEX_FIELDS)
        for record in sorted(records.get("indexes", []), key=lambda item: item["id"])
    ]
    memories = [
        _project_fields(record, MEMORY_FIELDS)
        for record in sorted(records.get("memories", []), key=lambda item: item["id"])
    ]
    relationships = [
        _project_fields(record, RELATIONSHIP_FIELDS)
        for record in sorted(
            records.get("relationships", []),
            key=lambda item: (
                item["source_id"],
                item["target_id"],
                item["relation_type"],
            ),
        )
    ]
    return {
        "indexes": indexes,
        "memories": memories,
        "relationships": relationships,
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """Write records as canonical JSONL."""
    payload = "".join(_canonical_json(record) + "\n" for record in records)
    path.write_text(payload, encoding="utf-8")


def _sha256_file(path: Path) -> str:
    """Return the SHA-256 hash for a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_source_label(source_label: str | None) -> None:
    """Reject source labels that look like credentials or connection strings."""
    if source_label is None:
        return
    lowered = source_label.lower()
    forbidden_markers = (
        "://",
        "@",
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "database_url",
        "jwt",
    )
    if any(marker in lowered for marker in forbidden_markers):
        raise ValueError("source_label must be non-identifying and must not contain credentials")


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


def _bundle_records(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read and canonicalize all authoritative JSONL records from a bundle."""
    return _canonical_records({
        key: _read_jsonl(path / filename)
        for key, filename in JSONL_FILES.items()
    })


async def _target_matches_bundle(
    store: PortableBackupStore,
    records: dict[str, list[dict[str, Any]]],
) -> bool:
    """Return True when a populated target already contains exactly the bundle."""
    existing = _canonical_records(await store.export_portable_records())
    return existing == records


async def export_bundle(
    bundle_path: str | Path,
    store: PortableBackupStore,
    *,
    source_label: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Export a portable Open Brain knowledge bundle."""
    _validate_source_label(source_label)
    path = Path(bundle_path)
    path.mkdir(parents=True, exist_ok=True)

    records = _canonical_records(await store.export_portable_records())
    for key, filename in JSONL_FILES.items():
        _write_jsonl(path / filename, records[key])

    manifest: dict[str, Any] = {
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "open_brain_schema_version": OPEN_BRAIN_SCHEMA_VERSION,
        "created_at": _iso_utc(created_at or datetime.now(UTC)),
        "record_counts": {
            "indexes": len(records["indexes"]),
            "memories": len(records["memories"]),
            "relationships": len(records["relationships"]),
        },
        "files": {
            filename: {"sha256": _sha256_file(path / filename)}
            for filename in JSONL_FILES.values()
        },
        "embedding_model": {"name": "voyage-4", "dim": 1024},
        "contains_binaries": False,
        "contains_credentials": False,
    }
    if source_label is not None:
        manifest["source_label"] = source_label

    (path / "manifest.json").write_text(
        _canonical_json(manifest, pretty=True) + "\n",
        encoding="utf-8",
    )
    return manifest


async def restore_bundle(
    bundle_path: str | Path,
    store: PortableBackupStore,
    *,
    regenerate_embeddings: bool = True,
) -> dict[str, Any]:
    """Restore a portable bundle into a store."""
    path = Path(bundle_path)
    records = _bundle_records(path)
    counts = await store.portable_closure_counts()
    populated = {name: count for name, count in counts.items() if count > 0}
    if populated:
        try:
            if await _target_matches_bundle(store, records):
                return {
                    "bundle_path": str(path),
                    "restored": {
                        "indexes": len(records["indexes"]),
                        "memories": len(records["memories"]),
                        "relationships": len(records["relationships"]),
                    },
                    "regenerate_embeddings": regenerate_embeddings,
                    "already_restored": True,
                }
        except (AttributeError, TypeError, ValueError):
            pass
        raise RestoreTargetNotEmptyError(
            "Restore target already contains portable knowledge rows: "
            + ", ".join(f"{name}={count}" for name, count in sorted(populated.items()))
        )

    await store.restore_portable_records(
        records["indexes"],
        records["memories"],
        records["relationships"],
        regenerate_embeddings=regenerate_embeddings,
    )
    return {
        "bundle_path": str(path),
        "restored": {
            "indexes": len(records["indexes"]),
            "memories": len(records["memories"]),
            "relationships": len(records["relationships"]),
        },
        "regenerate_embeddings": regenerate_embeddings,
    }
