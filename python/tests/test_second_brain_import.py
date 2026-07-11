"""Tests for deterministic Second Brain vault import."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.interface import VALID_LINK_TYPES
from open_brain.data_layer.interface import SaveMemoryParams, SaveMemoryResult
from open_brain.data_layer.postgres import PostgresDataLayer
from open_brain.ingest.runs import get_current_run_id
from open_brain.paperless import PaperlessResolveResult


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "second_brain"
FIXTURE_VAULT = FIXTURE_DIR / "vault"
PAPERLESS_MAPPING = FIXTURE_DIR / "paperless_mapping.json"
EXPECTED_DRY_RUN = FIXTURE_DIR / "expected_dry_run_manifest.json"


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Build a properly structured asyncpg pool mock."""

    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


class TestSecondBrainSharedContracts:
    def test_references_link_type_is_available_for_wikilinks(self):
        """AC3: wikilinks use the generic references relationship type."""
        assert "references" in VALID_LINK_TYPES

    @pytest.mark.asyncio
    async def test_source_ref_status_can_disable_memory_type_filter(self):
        """AC2: importer can check idempotency across all canonical memory types."""
        row = MagicMock()
        row.__getitem__ = lambda self, key: {
            "source_ref": "Projects/OpenBrain.md",
            "memory_id": 42,
            "run_id": "run-123",
            "ingested_at": "2026-07-11T12:00:00",
            "title": "Open Brain Migration",
        }[key]

        conn = AsyncMock()
        conn.fetch.return_value = [row]
        pool = _make_pool(conn)

        with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await PostgresDataLayer().ingest_status_by_source_refs(
                ["Projects/OpenBrain.md"],
                memory_type=None,
            )

        sql = conn.fetch.call_args[0][0]
        assert "type = 'meeting'" not in sql
        assert result["Projects/OpenBrain.md"]["ingested"] is True
        assert result["Projects/OpenBrain.md"]["memory_id"] == 42

    def test_importer_module_exists(self):
        """AC5: importer is a package module that can emit reconciliation reports."""
        assert importlib.util.find_spec("open_brain.second_brain_import") is not None


class FakeDataLayer:
    """DataLayer test double that rejects writes in dry-run tests."""

    def __init__(self, existing_refs: dict[str, int] | None = None) -> None:
        self.existing_refs = existing_refs or {}
        self.saved: list[SaveMemoryParams] = []
        self.relationships: list[dict[str, Any]] = []

    async def ingest_status_by_source_refs(
        self,
        source_refs: list[str],
        memory_type: str | None = "meeting",
    ) -> dict[str, dict[str, Any]]:
        assert memory_type is None
        return {
            source_ref: {
                "source_ref": source_ref,
                "ingested": source_ref in self.existing_refs,
                "memory_id": self.existing_refs.get(source_ref),
                "run_id": "prior-run" if source_ref in self.existing_refs else None,
                "ingested_at": "2026-07-11T12:00:00Z"
                if source_ref in self.existing_refs
                else None,
                "title": "Existing note" if source_ref in self.existing_refs else None,
            }
            for source_ref in source_refs
        }

    async def save_memory(self, params: SaveMemoryParams) -> SaveMemoryResult:
        self.saved.append(params)
        raise AssertionError("dry run must not write memories")

    async def create_relationship(
        self,
        source_id: int,
        target_id: int,
        link_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        self.relationships.append({
            "source_id": source_id,
            "target_id": target_id,
            "link_type": link_type,
            "metadata": metadata,
        })
        raise AssertionError("dry run must not write relationships")


class FakePaperlessClient:
    """Paperless test double for fixture document ids."""

    def __init__(self) -> None:
        self.resolved_ids: list[int] = []

    async def resolve_reference(self, document_id: int) -> PaperlessResolveResult:
        self.resolved_ids.append(document_id)
        if document_id == 101:
            return PaperlessResolveResult(
                status="found",
                document_id=101,
                title="OpenBrain migration plan",
                mime_type="application/pdf",
                added="2026-07-11T09:30:00Z",
                retrieval_targets={
                    "download": "https://paperless.test/api/documents/101/download/",
                    "preview": "https://paperless.test/api/documents/101/preview/",
                    "thumb": "https://paperless.test/api/documents/101/thumb/",
                },
            )
        if document_id == 404:
            return PaperlessResolveResult(
                status="not_found",
                document_id=404,
                error="Paperless document 404 was not found",
            )
        raise AssertionError(f"unexpected document id {document_id}")


class RecordingDataLayer(FakeDataLayer):
    """DataLayer test double that records importer writes."""

    def __init__(self) -> None:
        super().__init__()
        self.next_id = 1000
        self.saved_run_ids: list[str | None] = []

    async def save_memory(self, params: SaveMemoryParams) -> SaveMemoryResult:
        run_id = get_current_run_id()
        self.saved_run_ids.append(run_id)
        stored_metadata = dict(params.metadata or {})
        if run_id is not None:
            stored_metadata["run_id"] = run_id
        stored = replace(params, metadata=stored_metadata)
        self.saved.append(stored)
        memory_id = self.next_id
        self.next_id += 1
        source_ref = stored_metadata["source_ref"]
        self.existing_refs[source_ref] = memory_id
        return SaveMemoryResult(id=memory_id, message="Memory saved")


def _normalize_report(report: dict[str, Any]) -> dict[str, Any]:
    """Normalize environment-specific fields before fixture comparison."""
    normalized = json.loads(json.dumps(report, default=str))
    normalized["vault_path"] = "<vault>"
    return normalized


class TestSecondBrainDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_reports_all_dispositions_without_store_writes(self):
        """AC1/AC5: dry-run report is complete, machine-readable, and write-free."""
        from open_brain.second_brain_import import import_vault

        data_layer = FakeDataLayer(existing_refs={"People/Ada Lovelace.md": 900})
        paperless_client = FakePaperlessClient()

        report = await import_vault(
            vault_path=FIXTURE_VAULT,
            paperless_mapping_path=PAPERLESS_MAPPING,
            data_layer=data_layer,
            paperless_client=paperless_client,
            apply=False,
        )

        assert data_layer.saved == []
        assert data_layer.relationships == []
        assert paperless_client.resolved_ids == [101, 404]
        assert _normalize_report(report) == json.loads(EXPECTED_DRY_RUN.read_text())


class TestSecondBrainApply:
    @pytest.mark.asyncio
    async def test_apply_twice_imports_supported_notes_once_with_provenance(self):
        """AC2: apply writes supported notes idempotently with preserved content."""
        from open_brain.second_brain_import import import_vault

        data_layer = RecordingDataLayer()
        paperless_client = FakePaperlessClient()

        first = await import_vault(
            vault_path=FIXTURE_VAULT,
            paperless_mapping_path=PAPERLESS_MAPPING,
            data_layer=data_layer,
            paperless_client=paperless_client,
            apply=True,
        )

        assert first["mode"] == "apply"
        assert first["run_id"] is not None
        assert first["summary"]["imported"] == 6
        assert first["summary"]["duplicate"] == 0
        assert first["summary"]["skipped"] == 1
        assert len(data_layer.saved) == 6
        assert set(data_layer.saved_run_ids) == {first["run_id"]}

        project = next(
            params for params in data_layer.saved
            if params.metadata and params.metadata["source_ref"] == "Projects/OpenBrain.md"
        )
        assert project.type == "project"
        assert project.title == "Open Brain Migration"
        assert "Project body references" in project.text
        assert project.metadata is not None
        assert project.metadata["source"] == "second_brain"
        assert project.metadata["source_ref"] == "Projects/OpenBrain.md"
        assert project.metadata["source_path"].endswith("Projects/OpenBrain.md")
        assert project.metadata["frontmatter"]["status"] == "active"
        assert project.metadata["owner"] == "Malte"

        second = await import_vault(
            vault_path=FIXTURE_VAULT,
            paperless_mapping_path=PAPERLESS_MAPPING,
            data_layer=data_layer,
            paperless_client=paperless_client,
            apply=True,
        )

        assert len(data_layer.saved) == 6
        assert second["mode"] == "apply"
        assert second["run_id"] is not None
        assert second["summary"]["imported"] == 0
        assert second["summary"]["duplicate"] == 6
        assert second["summary"]["skipped"] == 1
        assert all(
            item["action"] in {"duplicate", "skip"}
            for item in second["items"]
        )
