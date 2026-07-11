"""Tests for deterministic Second Brain vault import."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.interface import VALID_LINK_TYPES
from open_brain.data_layer.interface import SaveMemoryParams, SaveMemoryResult
from open_brain.data_layer.interface import paperless_reference_binary_keys
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

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
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
        return len(self.relationships)


class ContentHashCollisionDataLayer(RecordingDataLayer):
    """DataLayer double that mirrors Postgres content-hash duplicate returns."""

    def __init__(self, duplicate_refs: dict[str, int]) -> None:
        super().__init__()
        self.duplicate_refs = duplicate_refs
        self.save_attempts: list[SaveMemoryParams] = []

    async def save_memory(self, params: SaveMemoryParams) -> SaveMemoryResult:
        self.save_attempts.append(params)
        metadata = params.metadata or {}
        source_ref = metadata["source_ref"]
        if source_ref in self.duplicate_refs:
            existing_id = self.duplicate_refs[source_ref]
            return SaveMemoryResult(
                id=existing_id,
                message="Duplicate content detected",
                duplicate_of=existing_id,
            )
        return await super().save_memory(params)


class BackgroundTaskDataLayer(RecordingDataLayer):
    """DataLayer double that schedules a real background task on save."""

    def __init__(self) -> None:
        super().__init__()
        self.background_completed = False
        self.background_task: asyncio.Task[None] | None = None

    async def save_memory(self, params: SaveMemoryParams) -> SaveMemoryResult:
        result = await super().save_memory(params)

        async def mark_complete() -> None:
            await asyncio.sleep(0)
            self.background_completed = True

        self.background_task = asyncio.create_task(mark_complete())
        return result


class RaisingPaperlessClient:
    """Paperless double that raises during attachment verification."""

    async def resolve_reference(self, document_id: int) -> PaperlessResolveResult:
        raise RuntimeError(f"Paperless transport failed for {document_id}")


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

    @pytest.mark.asyncio
    async def test_unreadable_markdown_file_is_reported_as_skipped(self, tmp_path):
        """AC1: unreadable notes are skipped without aborting the vault scan."""
        from open_brain.second_brain_import import import_vault

        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Unreadable.md").write_bytes(b"\xff\xfe\xfa")

        report = await import_vault(
            vault_path=vault,
            data_layer=FakeDataLayer(),
            paperless_client=FakePaperlessClient(),
            apply=False,
        )

        assert report["summary"]["skipped"] == 1
        assert report["items"] == [
            {
                "source_ref": "Unreadable.md",
                "type": None,
                "action": "skip",
                "memory_id": None,
                "reason": "unreadable",
            }
        ]


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

    @pytest.mark.asyncio
    async def test_content_hash_collision_is_reported_as_duplicate_target(self, tmp_path):
        """AC2/AC3: content-hash duplicates are reported and remain link targets."""
        from open_brain.second_brain_import import import_vault

        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Source.md").write_text("Links to [[Target]].\n", encoding="utf-8")
        (vault / "Target.md").write_text("Same body as an existing memory.\n", encoding="utf-8")
        data_layer = ContentHashCollisionDataLayer({"Target.md": 777})

        report = await import_vault(
            vault_path=vault,
            data_layer=data_layer,
            paperless_client=FakePaperlessClient(),
            apply=True,
        )

        items = {item["source_ref"]: item for item in report["items"]}
        assert items["Target.md"] == {
            "source_ref": "Target.md",
            "type": "observation",
            "action": "duplicate",
            "memory_id": 777,
            "reason": "content_hash_collision",
        }
        assert items["Source.md"]["action"] == "import"
        assert report["summary"]["imported"] == 1
        assert report["summary"]["duplicate"] == 1
        assert [
            params.metadata["source_ref"]
            for params in data_layer.saved
            if params.metadata is not None
        ] == ["Source.md"]
        assert data_layer.relationships == [
            {
                "source_id": data_layer.existing_refs["Source.md"],
                "target_id": 777,
                "link_type": "references",
                "metadata": {
                    "source_ref": "Source.md",
                    "target_source_ref": "Target.md",
                    "wikilink": "[[Target]]",
                },
            }
        ]

    @pytest.mark.asyncio
    async def test_cli_drains_pending_background_tasks_before_return(
        self,
        tmp_path,
        capsys,
    ):
        """AC2: CLI apply waits for background save tasks before returning."""
        from open_brain.second_brain_import import _main_async

        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Note.md").write_text("Body that schedules embedding.\n", encoding="utf-8")
        data_layer = BackgroundTaskDataLayer()

        with patch(
            "open_brain.data_layer.postgres.PostgresDataLayer",
            return_value=data_layer,
        ):
            exit_code = await _main_async([str(vault), "--apply"])

        capsys.readouterr()
        assert exit_code == 0
        assert data_layer.background_task is not None
        assert data_layer.background_task.done()
        assert not data_layer.background_task.cancelled()
        assert data_layer.background_completed is True


class TestSecondBrainRelationships:
    @pytest.mark.asyncio
    async def test_resolvable_wikilinks_create_generic_reference_edges(self):
        """AC3: resolvable wikilinks become generic references relationships."""
        from open_brain.second_brain_import import import_vault

        data_layer = RecordingDataLayer()

        report = await import_vault(
            vault_path=FIXTURE_VAULT,
            paperless_mapping_path=PAPERLESS_MAPPING,
            data_layer=data_layer,
            paperless_client=FakePaperlessClient(),
            apply=True,
        )

        ids = data_layer.existing_refs
        expected_edges = {
            (ids["Projects/OpenBrain.md"], ids["People/Ada Lovelace.md"], "references"),
            (ids["Projects/OpenBrain.md"], ids["Concepts/Knowledge Graph.md"], "references"),
            (ids["People/Ada Lovelace.md"], ids["Projects/OpenBrain.md"], "references"),
            (ids["Notes/Untyped.md"], ids["Concepts/Knowledge Graph.md"], "references"),
        }
        actual_edges = {
            (row["source_id"], row["target_id"], row["link_type"])
            for row in data_layer.relationships
        }

        assert actual_edges == expected_edges
        assert all(row["metadata"]["source_ref"] for row in data_layer.relationships)
        assert all(row["metadata"]["wikilink"].startswith("[[") for row in data_layer.relationships)
        assert report["summary"]["unresolved_links"] == 2


class TestSecondBrainAttachments:
    @pytest.mark.asyncio
    async def test_only_verified_paperless_references_are_persisted(self):
        """AC4: attachments persist only as verified Paperless reference metadata."""
        from open_brain.second_brain_import import import_vault

        data_layer = RecordingDataLayer()
        paperless_client = FakePaperlessClient()

        report = await import_vault(
            vault_path=FIXTURE_VAULT,
            paperless_mapping_path=PAPERLESS_MAPPING,
            data_layer=data_layer,
            paperless_client=paperless_client,
            apply=True,
        )

        project = next(
            params for params in data_layer.saved
            if params.metadata and params.metadata["source_ref"] == "Projects/OpenBrain.md"
        )
        assert project.metadata is not None
        assert project.metadata["paperless_reference"] == {
            "document_id": 101,
            "instance": "paperless-test",
            "title": "OpenBrain migration plan",
            "added": "2026-07-11T09:30:00Z",
        }
        assert project.metadata["paperless_references"] == [
            project.metadata["paperless_reference"]
        ]
        assert "retrieval_targets" not in project.metadata["paperless_reference"]
        assert paperless_reference_binary_keys(project.metadata) == []
        assert {
            (row["attachment"], row["document_id"], row["reason"])
            for row in report["unresolved_attachments"]
        } == {
            ("missing-contract.pdf", 404, "not_found"),
            ("unmapped-scan.pdf", None, "no_mapping"),
        }

    @pytest.mark.asyncio
    async def test_attachment_transport_error_reports_exception_message(self, tmp_path):
        """AC4: attachment verification failures keep the original exception text."""
        from open_brain.second_brain_import import import_vault

        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Note.md").write_text("See ![[contract.pdf]].\n", encoding="utf-8")
        mapping = tmp_path / "paperless_mapping.json"
        mapping.write_text(
            json.dumps({"contract.pdf": {"document_id": 123}}),
            encoding="utf-8",
        )

        report = await import_vault(
            vault_path=vault,
            paperless_mapping_path=mapping,
            data_layer=FakeDataLayer(),
            paperless_client=RaisingPaperlessClient(),
            apply=False,
        )

        assert report["unresolved_attachments"] == [
            {
                "source_ref": "Note.md",
                "attachment": "contract.pdf",
                "document_id": 123,
                "reason": "transport_error",
                "error": "Paperless transport failed for 123",
            }
        ]
