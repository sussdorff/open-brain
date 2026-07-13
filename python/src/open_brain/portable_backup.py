"""Portable backup and restore helpers for Open Brain knowledge."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

BUNDLE_FORMAT_VERSION = "1.1.0"
OPEN_BRAIN_SCHEMA_VERSION = "postgres-portable-v1"

INDEX_FIELDS = ("id", "name")
SESSION_FIELDS = (
    "id",
    "session_id",
    "index_id",
    "project",
    "started_at",
    "ended_at",
    "metadata",
    "status",
    "prompt_counter",
)
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
    "sessions": "sessions.jsonl",
    "memories": "memories.jsonl",
    "relationships": "relationships.jsonl",
}
LEGACY_JSONL_FILES = {
    "indexes": "indexes.jsonl",
    "memories": "memories.jsonl",
    "relationships": "relationships.jsonl",
}
PORTABLE_DATABASE_TABLES = {
    "indexes": "memory_indexes",
    "sessions": "sessions",
    "memories": "memories",
    "relationships": "memory_relationships",
}
PORTABLE_FOREIGN_KEY_PARENTS = {
    "memory_indexes": frozenset(),
    "sessions": frozenset({"memory_indexes"}),
    "memories": frozenset({"memory_indexes", "sessions"}),
    "memory_relationships": frozenset({"memories"}),
}

# Credential-shaped key markers that must never appear anywhere in an exported
# record (including nested inside the metadata jsonb blob). The export asserts
# ``contains_credentials``/``contains_binaries`` are false, so we fail closed
# (raise) rather than silently shipping a bundle that contradicts that manifest.
# Extends the fail-closed precedent of ``paperless_reference_binary_keys()`` in
# ``data_layer/interface.py`` (which rejects binary payload keys on write). Match
# is a case-insensitive substring test on the key name; the markers are compound
# and unambiguous so legitimate structured keys (``content_hash``, ``token_count``)
# are not flagged.
FORBIDDEN_METADATA_KEY_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "credential",
    "token_hash",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "private_key",
    "client_secret",
    "auth_token",
    "bearer_token",
    "session_token",
)


class RestoreTargetNotEmptyError(RuntimeError):
    """Raised when a restore target already contains portable knowledge rows."""


class ExportTargetNotEmptyError(RuntimeError):
    """Raised when an export target directory already exists and is non-empty."""


class ForbiddenExportContentError(RuntimeError):
    """Raised when an exported record contains a credential-shaped key."""


class BundleIntegrityError(RuntimeError):
    """Raised when a bundle fails manifest hash/count integrity validation."""


class IncompatibleBundleVersionError(RuntimeError):
    """Raised when a bundle declares an incompatible bundle_format_version."""


class PortableBackupStore(Protocol):
    """Store operations required by portable backup and restore."""

    async def export_portable_records(self) -> dict[str, list[dict[str, Any]]]:
        """Return records from the portable knowledge closure."""

    async def portable_closure_counts(self) -> dict[str, int]:
        """Return row counts for the portable knowledge closure."""

    async def restore_portable_records(
        self,
        indexes: list[dict[str, Any]],
        sessions: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        *,
        regenerate_embeddings: bool,
    ) -> dict[str, Any]:
        """Atomically restore portable records into the backing store.

        The emptiness/same-bundle check MUST run inside the same transaction as
        the write so it is race-free (see postgres implementation). Returns a
        result dict with at least ``already_restored`` (True when the target
        already matched the bundle exactly and no rows were written). Raises
        ``RestoreTargetNotEmptyError`` when the target is populated with rows
        that do not match the bundle.
        """


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
    return {
        field: _json_ready(record.get(field)) for field in fields if field in record
    }


def _canonical_records(
    records: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Return sorted records projected onto the portable bundle contract."""
    indexes = [
        _project_fields(record, INDEX_FIELDS)
        for record in sorted(records.get("indexes", []), key=lambda item: item["id"])
    ]
    sessions = [
        _project_fields(record, SESSION_FIELDS)
        for record in sorted(records.get("sessions", []), key=lambda item: item["id"])
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
        "sessions": sessions,
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


def _sha256_text(value: str) -> str:
    """Return the SHA-256 hash for a text value."""
    return hashlib.sha256(value.encode()).hexdigest()


def _memory_record_hash(memory: dict[str, Any]) -> str:
    """Return a full-record hash covering every round-tripped memory field.

    Covers the complete export allowlist (``MEMORY_FIELDS``) except ``id`` (the
    per-record comparison key), so corruption in any field that is supposed to
    round-trip — including ``type``, ``index_id``, ``priority``, ``importance``,
    and session-related fields — is caught, not just content/title/metadata.
    """
    payload = {field: memory.get(field) for field in MEMORY_FIELDS if field != "id"}
    return _sha256_text(_canonical_json(payload))


def _session_record_hash(session: dict[str, Any]) -> str:
    """Return a full-record hash covering every round-tripped session field."""
    payload = {field: session.get(field) for field in SESSION_FIELDS if field != "id"}
    return _sha256_text(_canonical_json(payload))


def _relationship_edges(records: list[dict[str, Any]]) -> set[tuple[int, int, str]]:
    """Return the canonical semantic relationship edge set."""
    return {
        (
            int(record["source_id"]),
            int(record["target_id"]),
            str(record["relation_type"]),
        )
        for record in records
    }


def _relationship_field_map(
    records: list[dict[str, Any]],
) -> dict[tuple[int, int, str], str]:
    """Map each relationship edge to a hash of its non-key round-tripped fields."""
    result: dict[tuple[int, int, str], str] = {}
    for record in records:
        key = (
            int(record["source_id"]),
            int(record["target_id"]),
            str(record["relation_type"]),
        )
        payload = {
            field: record.get(field)
            for field in RELATIONSHIP_FIELDS
            if field not in ("id", "source_id", "target_id", "relation_type")
        }
        result[key] = _sha256_text(_canonical_json(payload))
    return result


def _index_identity_set(records: list[dict[str, Any]]) -> set[tuple[int, str]]:
    """Return the (id, name) identity set for indexes."""
    return {(int(record["id"]), str(record["name"])) for record in records}


def _canonical_entity_ids(records: list[dict[str, Any]]) -> set[int]:
    """Return ids for memories marked as canonical entities."""
    ids: set[int] = set()
    for record in records:
        metadata = record.get("metadata") or {}
        if isinstance(metadata, dict) and metadata.get("canonical_entity") is True:
            ids.add(int(record["id"]))
    return ids


def _scan_forbidden_keys(value: Any, path: str = "") -> list[str]:
    """Recursively collect dotted paths of credential-shaped keys in a value.

    Traverses dicts and lists so credential-shaped keys nested inside the
    ``metadata`` jsonb blob (e.g. ``token_hash``, ``password``) are caught, not
    just top-level columns. Returns an empty list when nothing is forbidden.
    """
    findings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_str = str(key)
            key_path = f"{path}.{key_str}" if path else key_str
            lowered = key_str.lower()
            if any(marker in lowered for marker in FORBIDDEN_METADATA_KEY_MARKERS):
                findings.append(key_path)
            findings.extend(_scan_forbidden_keys(item, key_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(_scan_forbidden_keys(item, f"{path}[{index}]"))
    return findings


def _assert_no_forbidden_content(
    records: dict[str, list[dict[str, Any]]],
) -> None:
    """Fail closed if any exported record carries a credential-shaped key.

    The manifest hardcodes ``contains_credentials``/``contains_binaries`` = false,
    so rather than silently stripping (which could mask a leak) we RAISE, per the
    fail-closed, reject-malformed-input security default.
    """
    offenders: list[str] = []
    for record_type in ("indexes", "sessions", "memories", "relationships"):
        for record in records.get(record_type, []):
            record_id = record.get("id")
            for hit in _scan_forbidden_keys(record):
                offenders.append(f"{record_type}[id={record_id}].{hit}")
    if offenders:
        raise ForbiddenExportContentError(
            "Refusing to export records containing credential-shaped keys: "
            + ", ".join(sorted(offenders))
        )


def _is_compatible_bundle_version(version: Any) -> bool:
    """Return True when the bundle major version matches this reader."""
    if not isinstance(version, str):
        return False
    head = version.split(".", 1)[0]
    try:
        major = int(head)
    except ValueError:
        return False
    current_major = int(BUNDLE_FORMAT_VERSION.split(".", 1)[0])
    return major == current_major


def _load_manifest(path: Path) -> dict[str, Any]:
    """Read and parse a bundle manifest, failing closed on absence/corruption."""
    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        raise BundleIntegrityError(f"Bundle manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BundleIntegrityError(f"Bundle manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BundleIntegrityError("Bundle manifest must be a JSON object")
    return manifest


def _verify_bundle_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Validate version, per-file SHA-256, and record counts before any write.

    Raises before any database mutation so a corrupted, truncated, or
    incompatible bundle is never silently written to the store.
    """
    version = manifest.get("bundle_format_version")
    if not _is_compatible_bundle_version(version):
        raise IncompatibleBundleVersionError(
            f"Unsupported bundle_format_version {version!r}; "
            f"this reader supports major version "
            f"{BUNDLE_FORMAT_VERSION.split('.', 1)[0]}.x"
        )

    declared_files = manifest.get("files")
    if not isinstance(declared_files, dict):
        raise BundleIntegrityError("Bundle manifest is missing the 'files' section")
    declared_counts = manifest.get("record_counts")
    if not isinstance(declared_counts, dict):
        raise BundleIntegrityError(
            "Bundle manifest is missing the 'record_counts' section"
        )

    version = manifest.get("bundle_format_version")
    bundle_files = LEGACY_JSONL_FILES if version == "1.0.0" else JSONL_FILES
    for key, filename in bundle_files.items():
        file_path = path / filename
        if not file_path.exists():
            raise BundleIntegrityError(f"Bundle file missing: {filename}")

        declared = declared_files.get(filename)
        if not isinstance(declared, dict) or "sha256" not in declared:
            raise BundleIntegrityError(f"Bundle manifest missing sha256 for {filename}")
        actual_sha = _sha256_file(file_path)
        if actual_sha != declared["sha256"]:
            raise BundleIntegrityError(
                f"SHA-256 mismatch for {filename}: manifest declares "
                f"{declared['sha256']}, computed {actual_sha}"
            )

        actual_count = len(_read_jsonl(file_path))
        declared_count = declared_counts.get(key)
        if declared_count != actual_count:
            raise BundleIntegrityError(
                f"Record count mismatch for {filename}: manifest declares "
                f"{declared_count}, file contains {actual_count}"
            )


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
        raise ValueError(
            "source_label must be non-identifying and must not contain credentials"
        )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a deterministic JSONL file into record dictionaries."""
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"Portable backup record must be an object: {path}")
        records.append(value)
    return records


def _bundle_records(
    path: Path,
    manifest: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Read and canonicalize all authoritative JSONL records from a bundle."""
    resolved_manifest = manifest or _load_manifest(path)
    bundle_files = (
        LEGACY_JSONL_FILES
        if resolved_manifest.get("bundle_format_version") == "1.0.0"
        else JSONL_FILES
    )
    return _canonical_records(
        {key: _read_jsonl(path / filename) for key, filename in bundle_files.items()}
    )


async def verify_round_trip(
    bundle_path: str | Path,
    store: PortableBackupStore,
) -> dict[str, Any]:
    """Verify that a restored store matches a portable bundle."""
    path = Path(bundle_path)
    manifest = _load_manifest(path)
    expected = _bundle_records(path, manifest)
    restored = _canonical_records(await store.export_portable_records())

    expected_memories = {record["id"]: record for record in expected["memories"]}
    restored_memories = {record["id"]: record for record in restored["memories"]}
    content_hash_matches = 0
    content_hash_mismatches: list[int] = []
    record_hash_mismatches: list[int] = []
    for memory_id, expected_memory in expected_memories.items():
        restored_memory = restored_memories.get(memory_id)
        if restored_memory is None:
            content_hash_mismatches.append(memory_id)
            record_hash_mismatches.append(memory_id)
            continue

        expected_metadata = expected_memory.get("metadata") or {}
        expected_content_hash = expected_metadata.get("content_hash") or _sha256_text(
            str(expected_memory.get("content") or "")
        )
        restored_content_hash = _sha256_text(str(restored_memory.get("content") or ""))
        if restored_content_hash == expected_content_hash:
            content_hash_matches += 1
        else:
            content_hash_mismatches.append(memory_id)

        if _memory_record_hash(restored_memory) != _memory_record_hash(expected_memory):
            record_hash_mismatches.append(memory_id)

    expected_sessions = {record["id"]: record for record in expected["sessions"]}
    restored_sessions = {record["id"]: record for record in restored["sessions"]}
    missing_sessions = sorted(set(expected_sessions) - set(restored_sessions))
    extra_sessions = sorted(set(restored_sessions) - set(expected_sessions))
    session_record_mismatches = sorted(
        session_id
        for session_id in set(expected_sessions) & set(restored_sessions)
        if _session_record_hash(expected_sessions[session_id])
        != _session_record_hash(restored_sessions[session_id])
    )

    expected_edges = _relationship_edges(expected["relationships"])
    restored_edges = _relationship_edges(restored["relationships"])
    missing_edges = sorted(expected_edges - restored_edges)
    extra_edges = sorted(restored_edges - expected_edges)

    # Full-record comparison for edges present in both sides, so corruption of
    # link_type / confidence / metadata is caught (edge presence alone would miss it).
    expected_rel_fields = _relationship_field_map(expected["relationships"])
    restored_rel_fields = _relationship_field_map(restored["relationships"])
    relationship_record_mismatches = sorted(
        edge
        for edge in (expected_edges & restored_edges)
        if expected_rel_fields.get(edge) != restored_rel_fields.get(edge)
    )

    # Id/name set comparison for indexes (was count-only, which missed renames).
    expected_index_set = _index_identity_set(expected["indexes"])
    restored_index_set = _index_identity_set(restored["indexes"])
    missing_indexes = sorted(expected_index_set - restored_index_set)
    extra_indexes = sorted(restored_index_set - expected_index_set)

    expected_canonical_ids = _canonical_entity_ids(expected["memories"])
    restored_canonical_ids = _canonical_entity_ids(restored["memories"])
    preserved_ids = sorted(expected_canonical_ids & restored_canonical_ids)

    report = {
        "bundle_format_version": manifest["bundle_format_version"],
        "ok": False,
        "memories": {
            "expected": len(expected["memories"]),
            "restored": len(restored["memories"]),
            "content_hash_matches": content_hash_matches,
            "content_hash_mismatches": sorted(content_hash_mismatches),
            "record_hash_mismatches": sorted(record_hash_mismatches),
        },
        "sessions": {
            "expected": len(expected["sessions"]),
            "restored": len(restored["sessions"]),
            "missing": missing_sessions,
            "extra": extra_sessions,
            "record_mismatches": session_record_mismatches,
        },
        "relationships": {
            "expected": len(expected["relationships"]),
            "restored": len(restored["relationships"]),
            "missing": missing_edges,
            "extra": extra_edges,
            "record_mismatches": relationship_record_mismatches,
        },
        "indexes": {
            "expected": len(expected["indexes"]),
            "restored": len(restored["indexes"]),
            "missing": missing_indexes,
            "extra": extra_indexes,
        },
        "canonical_entities": {
            "expected": len(expected_canonical_ids),
            "restored": len(restored_canonical_ids),
            "preserved_ids": preserved_ids,
        },
    }
    report["ok"] = (
        report["memories"]["expected"] == report["memories"]["restored"]
        and report["memories"]["content_hash_matches"] == report["memories"]["expected"]
        and not report["memories"]["content_hash_mismatches"]
        and not report["memories"]["record_hash_mismatches"]
        and report["sessions"]["expected"] == report["sessions"]["restored"]
        and not report["sessions"]["missing"]
        and not report["sessions"]["extra"]
        and not report["sessions"]["record_mismatches"]
        and report["relationships"]["expected"] == report["relationships"]["restored"]
        and not report["relationships"]["missing"]
        and not report["relationships"]["extra"]
        and not report["relationships"]["record_mismatches"]
        and report["indexes"]["expected"] == report["indexes"]["restored"]
        and not report["indexes"]["missing"]
        and not report["indexes"]["extra"]
        and report["canonical_entities"]["expected"] == len(preserved_ids)
    )
    return report


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
    # Fail closed on a non-empty target directory (mirrors the restore-side
    # fail-closed pattern): reusing a prior export dir could leave stale
    # credentials/binaries alongside a manifest that asserts it contains none.
    # We refuse rather than silently deleting operator data.
    if path.exists():
        if not path.is_dir():
            raise ExportTargetNotEmptyError(
                f"Export target exists and is not a directory: {path}"
            )
        if any(path.iterdir()):
            raise ExportTargetNotEmptyError(
                f"Export target directory already exists and is non-empty: {path}"
            )
    path.mkdir(parents=True, exist_ok=True)

    records = _canonical_records(await store.export_portable_records())
    # Fail closed if any exported record (including nested metadata) carries a
    # credential-shaped key, so the manifest's contains_credentials=false holds.
    _assert_no_forbidden_content(records)
    for key, filename in JSONL_FILES.items():
        _write_jsonl(path / filename, records[key])

    manifest: dict[str, Any] = {
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "open_brain_schema_version": OPEN_BRAIN_SCHEMA_VERSION,
        "created_at": _iso_utc(created_at or datetime.now(UTC)),
        "record_counts": {
            "indexes": len(records["indexes"]),
            "sessions": len(records["sessions"]),
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

    # Validate the bundle (version, per-file SHA-256, record counts) BEFORE any
    # database write, so a corrupted/truncated/incompatible bundle is never
    # partially written.
    manifest = _load_manifest(path)
    _verify_bundle_manifest(path, manifest)

    records = _bundle_records(path, manifest)
    if manifest.get("bundle_format_version") == "1.0.0" and any(
        memory.get("session_id") is not None for memory in records["memories"]
    ):
        raise IncompatibleBundleVersionError(
            "Legacy bundle format 1.0.0 omits sessions.jsonl but contains "
            "memories with session_id references; restore cannot preserve those "
            "foreign keys. Re-export the source with bundle format 1.1 or newer."
        )

    # The emptiness/same-bundle check and the write are performed atomically by
    # the store (inside one locked transaction) to avoid a TOCTOU race — see
    # PortableBackupStore.restore_portable_records. The store raises
    # RestoreTargetNotEmptyError when the target holds non-matching rows.
    result = await store.restore_portable_records(
        records["indexes"],
        records["sessions"],
        records["memories"],
        records["relationships"],
        regenerate_embeddings=regenerate_embeddings,
    )
    already_restored = (
        bool(result.get("already_restored")) if isinstance(result, dict) else False
    )

    report = {
        "bundle_path": str(path),
        "restored": {
            "indexes": len(records["indexes"]),
            "sessions": len(records["sessions"]),
            "memories": len(records["memories"]),
            "relationships": len(records["relationships"]),
        },
        "regenerate_embeddings": regenerate_embeddings,
    }
    if already_restored:
        report["already_restored"] = True
    return report
