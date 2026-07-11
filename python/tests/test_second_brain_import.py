"""Tests for deterministic Second Brain vault import."""

from __future__ import annotations

import importlib.util
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.interface import VALID_LINK_TYPES
from open_brain.data_layer.postgres import PostgresDataLayer


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
