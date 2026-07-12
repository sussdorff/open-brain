"""Tests for the Second Brain cutover verifier."""

from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from open_brain.data_layer.interface import Memory, SaveMemoryParams, SaveMemoryResult, SearchResult
from open_brain.paperless import PaperlessResolveResult


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify_second_brain_cutover.py"
ALL_CAPABILITIES = [
    "open-brain-ccd",
    "open-brain-hws",
    "open-brain-slu",
    "open-brain-5qo",
    "open-brain-amq",
    "open-brain-brt",
    "open-brain-jhg",
]

SENSITIVE_NOTE_TITLE = "Private Estate Plan"
SENSITIVE_WIKILINK = "Secret Family Trust"
SENSITIVE_PAPERLESS_URL = "https://paperless.example/api/documents/101/download/?token=do-not-commit"
SENSITIVE_PAPERLESS_ERROR = "transport failed for https://paperless.example?token=do-not-commit"
SENSITIVE_MEMORY_CONTENT = "My private health insurance claim narrative"


def load_verifier() -> Any:
    """Load the repo-root verifier script as a test module."""
    spec = importlib.util.spec_from_file_location("verify_second_brain_cutover", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load verifier script")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _content_hash(content: str) -> str:
    """Return the Open Brain content hash for fixture records."""
    return hashlib.sha256(content.encode()).hexdigest()


def _memory(
    *,
    memory_id: int,
    title: str,
    memory_type: str = "project",
    metadata: dict[str, Any] | None = None,
    content: str = SENSITIVE_MEMORY_CONTENT,
) -> Memory:
    """Build a minimal Memory dataclass for digest smoke tests."""
    return Memory(
        id=memory_id,
        index_id=1,
        session_id=None,
        type=memory_type,
        title=title,
        subtitle=None,
        narrative=None,
        content=content,
        metadata=metadata or {},
        priority=1.0,
        stability="stable",
        access_count=1,
        last_accessed_at=None,
        created_at="2026-07-12T09:00:00+00:00",
        updated_at="2026-07-12T09:00:00+00:00",
        user_id="fixture-user",
        importance="high",
        last_decay_at=None,
    )


class FixtureDataLayer:
    """DataLayer test double for cutover gate evaluation."""

    def __init__(
        self,
        *,
        stats: dict[str, Any] | None = None,
        existing_refs: dict[str, int] | None = None,
    ) -> None:
        self._stats = stats if stats is not None else {
            "memories": 12,
            "sessions": 2,
            "relationships": 4,
            "types": {
                "person": 3,
                "project": 2,
                "resource": 2,
                "journal": 1,
            },
            "canonical_entities": 3,
        }
        self.existing_refs = existing_refs or {}
        self.search_calls: list[Any] = []
        self.saved: list[SaveMemoryParams] = []

    async def stats(self) -> dict[str, Any]:
        """Return aggregate stats for the capability gate."""
        return self._stats

    async def search(self, params: Any) -> SearchResult:
        """Return deterministic memories for daily and weekly digest smoke tests."""
        self.search_calls.append(params)
        if getattr(params, "metadata_filter", None) == {"canonical_entity": True}:
            return SearchResult(
                results=[
                    _memory(
                        memory_id=10,
                        title="Ada Sensitive",
                        memory_type="person",
                        metadata={
                            "canonical_entity": True,
                            "canonical_kind": "person",
                            "name": "Ada Sensitive",
                        },
                    )
                ],
                total=1,
            )
        if getattr(params, "capture_status", None) == "inbox":
            return SearchResult(
                results=[_memory(memory_id=30, title="Inbox Capture")],
                total=1,
            )
        return SearchResult(
            results=[
                _memory(
                    memory_id=20,
                    title="Open Brain cutover",
                    metadata={"entities": {"people": ["Ada Sensitive"]}},
                )
            ],
            total=1,
        )

    async def ingest_status_by_source_refs(
        self,
        source_refs: list[str],
        memory_type: str | None = "meeting",
    ) -> dict[str, dict[str, Any]]:
        """Return pre-existing status for every requested source ref."""
        assert memory_type is None
        return {
            source_ref: {
                "source_ref": source_ref,
                "ingested": source_ref in self.existing_refs,
                "memory_id": self.existing_refs.get(source_ref),
                "run_id": "prior-run" if source_ref in self.existing_refs else None,
                "ingested_at": "2026-07-12T09:00:00+00:00"
                if source_ref in self.existing_refs
                else None,
                "title": "Existing note" if source_ref in self.existing_refs else None,
            }
            for source_ref in source_refs
        }

    async def save_memory(self, params: SaveMemoryParams) -> SaveMemoryResult:
        """Reject writes so migration reconciliation stays dry-run only."""
        self.saved.append(params)
        raise AssertionError("cutover verifier must not write imported memories")

    async def create_relationship(
        self,
        source_id: int,
        target_id: int,
        link_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Reject writes so migration reconciliation stays dry-run only."""
        raise AssertionError("cutover verifier must not write relationships")


class FixturePaperlessClient:
    """Paperless client double that can return any resolver status."""

    def __init__(self, status: str = "found") -> None:
        self.status = status
        self.resolved_ids: list[int] = []

    async def resolve_reference(self, document_id: int) -> PaperlessResolveResult:
        """Return a deterministic resolver result."""
        self.resolved_ids.append(document_id)
        if self.status == "found":
            return PaperlessResolveResult(
                status="found",
                document_id=document_id,
                title="Sensitive Paperless Title",
                mime_type="application/pdf",
                added="2026-07-12T09:30:00+00:00",
                retrieval_targets={
                    "download": SENSITIVE_PAPERLESS_URL,
                    "preview": "https://paperless.example/api/documents/101/preview/",
                },
            )
        return PaperlessResolveResult(
            status=self.status,
            document_id=document_id,
            error=SENSITIVE_PAPERLESS_ERROR,
        )


class FixturePortableStore:
    """PortableBackupStore test double for isolated backup round trips."""

    def __init__(self, *, populated: bool = False) -> None:
        self.records = _portable_records() if populated else {
            "indexes": [],
            "memories": [],
            "relationships": [],
        }

    async def export_portable_records(self) -> dict[str, list[dict[str, Any]]]:
        """Return portable records."""
        return self.records

    async def portable_closure_counts(self) -> dict[str, int]:
        """Return row counts for the portable closure."""
        return {
            "indexes": len(self.records["indexes"]),
            "memories": len(self.records["memories"]),
            "relationships": len(self.records["relationships"]),
        }

    async def restore_portable_records(
        self,
        indexes: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        *,
        regenerate_embeddings: bool,
    ) -> dict[str, Any]:
        """Restore records into this in-memory store."""
        self.records = {
            "indexes": [dict(record) for record in indexes],
            "memories": [dict(record) for record in memories],
            "relationships": [dict(record) for record in relationships],
        }
        return {"already_restored": False}


def _portable_records() -> dict[str, list[dict[str, Any]]]:
    """Return a non-empty portable closure with no binary or credential fields."""
    content = "Portable backup fixture content"
    return {
        "indexes": [{"id": 1, "name": "default"}],
        "memories": [
            {
                "id": 10,
                "index_id": 1,
                "session_id": None,
                "type": "person",
                "title": "Ada Sensitive",
                "subtitle": None,
                "narrative": None,
                "content": content,
                "metadata": {
                    "content_hash": _content_hash(content),
                    "canonical_entity": True,
                    "canonical_kind": "person",
                },
                "priority": 1.0,
                "stability": "canonical",
                "access_count": 1,
                "last_accessed_at": None,
                "created_at": "2026-07-12T09:00:00+00:00",
                "updated_at": "2026-07-12T09:00:00+00:00",
                "user_id": "fixture-user",
                "importance": "critical",
                "last_decay_at": None,
                "session_ref": None,
            }
        ],
        "relationships": [],
    }


def _backup_store_factory(*, populated_source: bool = True) -> Callable[[], FixturePortableStore]:
    """Return source then restore-target stores for a verifier run."""
    stores = [
        FixturePortableStore(populated=populated_source),
        FixturePortableStore(populated=False),
    ]

    def factory() -> FixturePortableStore:
        if stores:
            return stores.pop(0)
        return FixturePortableStore(populated=False)

    return factory


@asynccontextmanager
async def complete_cutover_fixture(
    tmp_path: Path,
    *,
    data_layer: FixtureDataLayer | None = None,
    paperless_client: FixturePaperlessClient | None = None,
    backup_store_factory: Callable[[], FixturePortableStore] | None = None,
    vault_has_importable_note: bool = False,
):
    """Yield a complete verifier fixture with targeted override points."""
    vault = tmp_path / "vault"
    vault.mkdir()
    if vault_has_importable_note:
        (vault / f"{SENSITIVE_NOTE_TITLE}.md").write_text(
            f"# {SENSITIVE_NOTE_TITLE}\n\n[[{SENSITIVE_WIKILINK}]]\n",
            encoding="utf-8",
        )
    report_path = tmp_path / "cutover-report.json"
    verifier = load_verifier()
    report = await verifier.run_cutover(
        data_layer=data_layer or FixtureDataLayer(),
        paperless_client=paperless_client or FixturePaperlessClient(),
        backup_store_factory=backup_store_factory or _backup_store_factory(),
        vault_path=vault,
        paperless_mapping_path=None,
        paperless_probe_ids=[101],
        required_capabilities=list(ALL_CAPABILITIES),
        report_path=report_path,
    )
    yield verifier, report, json.loads(report_path.read_text(encoding="utf-8")), report_path


def _gate_statuses(payload: dict[str, Any]) -> dict[str, str]:
    """Return gate statuses keyed by gate id."""
    return {gate["id"]: gate["status"] for gate in payload["gates"]}


def _assert_single_red_gate(payload: dict[str, Any], gate_id: str) -> None:
    """Assert exactly one named gate is red."""
    statuses = _gate_statuses(payload)
    assert statuses[gate_id] == "red"
    assert {key for key, status in statuses.items() if status == "red"} == {gate_id}
    assert payload["overall_status"] == "red"


@pytest.mark.asyncio
async def test_complete_cutover_run_writes_redacted_machine_readable_report(
    tmp_path: Path,
) -> None:
    """A green run writes the durable redacted report schema."""
    async with complete_cutover_fixture(tmp_path) as (_verifier, report, payload, report_path):
        assert report_path.exists()
        assert payload["schema_version"] == "cutover-report.v1"
        assert payload["overall_status"] == "green"
        assert _gate_statuses(payload) == {
            "open_brain_capabilities": "green",
            "paperless_references": "green",
            "migration_reconciliation": "green",
            "backup_round_trip": "green",
        }
        assert payload == report.to_payload()


@pytest.mark.asyncio
async def test_missing_open_brain_capability_fails_named_gate_only(tmp_path: Path) -> None:
    """Missing Open Brain prerequisites fail only the capability gate."""
    incomplete_stats = {
        "memories": 0,
        "sessions": 0,
        "relationships": 0,
        "types": {},
    }
    async with complete_cutover_fixture(
        tmp_path,
        data_layer=FixtureDataLayer(stats=incomplete_stats),
    ) as (_verifier, _report, payload, _report_path):
        _assert_single_red_gate(payload, "open_brain_capabilities")


@pytest.mark.asyncio
async def test_missing_paperless_reference_fails_named_gate_only(tmp_path: Path) -> None:
    """Paperless probes fail closed on every non-found resolver status."""
    async with complete_cutover_fixture(
        tmp_path,
        paperless_client=FixturePaperlessClient(status="not_configured"),
    ) as (_verifier, _report, payload, _report_path):
        _assert_single_red_gate(payload, "paperless_references")


@pytest.mark.asyncio
async def test_migration_reconciliation_failure_fails_named_gate_only(tmp_path: Path) -> None:
    """Any remaining importable note or unresolved link keeps migration red."""
    async with complete_cutover_fixture(
        tmp_path,
        vault_has_importable_note=True,
    ) as (_verifier, _report, payload, _report_path):
        _assert_single_red_gate(payload, "migration_reconciliation")
        migration_gate = next(
            gate for gate in payload["gates"] if gate["id"] == "migration_reconciliation"
        )
        assert migration_gate["counts"]["importable"] == 1
        assert migration_gate["counts"]["unresolved_links"] == 1


@pytest.mark.asyncio
async def test_backup_round_trip_failure_fails_named_gate_only(tmp_path: Path) -> None:
    """An empty portable closure cannot make the backup round-trip gate green."""
    async with complete_cutover_fixture(
        tmp_path,
        backup_store_factory=_backup_store_factory(populated_source=False),
    ) as (_verifier, _report, payload, _report_path):
        _assert_single_red_gate(payload, "backup_round_trip")


@pytest.mark.asyncio
async def test_report_schema_rejects_sensitive_fixture_content(tmp_path: Path) -> None:
    """Report JSON must stay aggregate-only and never include primitive raw fields."""
    async with complete_cutover_fixture(
        tmp_path,
        vault_has_importable_note=True,
        paperless_client=FixturePaperlessClient(status="transport_error"),
    ) as (_verifier, _report, payload, _report_path):
        assert set(payload) == {"schema_version", "overall_status", "gates", "meta"}
        for gate in payload["gates"]:
            assert set(gate) == {"id", "status", "counts", "detail"}
            assert all(isinstance(value, int) for value in gate["counts"].values())

        report_json = json.dumps(payload, sort_keys=True)
        for forbidden in [
            SENSITIVE_NOTE_TITLE,
            SENSITIVE_WIKILINK,
            SENSITIVE_PAPERLESS_URL,
            SENSITIVE_PAPERLESS_ERROR,
            SENSITIVE_MEMORY_CONTENT,
            "items",
            "unresolved_attachments",
            "retrieval_targets",
            "download",
            "token=do-not-commit",
        ]:
            assert forbidden not in report_json


@pytest.mark.asyncio
async def test_cli_exit_codes_follow_cutover_status(tmp_path: Path) -> None:
    """CLI returns 0 for green reports, 1 for red reports, and 2 for bad args."""
    verifier = load_verifier()
    gate = verifier.GateResult(
        id="open_brain_capabilities",
        status="green",
        counts={},
        detail="capabilities verified",
    )
    green_report = verifier.CutoverReport(
        schema_version="cutover-report.v1",
        overall_status="green",
        gates=[gate],
        meta={"generated_at": "2026-07-12T09:00:00+00:00"},
    )
    red_report = verifier.CutoverReport(
        schema_version="cutover-report.v1",
        overall_status="red",
        gates=[asdict(gate) | {"status": "red"}],
        meta={"generated_at": "2026-07-12T09:00:00+00:00"},
    )
    argv = [
        "--vault-path",
        str(tmp_path),
        "--paperless-probe-id",
        "101",
        "--report-path",
        str(tmp_path / "report.json"),
    ]

    with patch.object(verifier, "run_cutover", new=AsyncMock(return_value=green_report)):
        assert await verifier.main(argv) == 0
    with patch.object(verifier, "run_cutover", new=AsyncMock(return_value=red_report)):
        assert await verifier.main(argv) == 1
    assert await verifier.main([]) == 2
