"""Final Kimi round-2 regression coverage for open-brain-ekn.8."""

from __future__ import annotations

import argparse
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_session_knowledge_migration_k1_repair import (
    PROVIDER_META,
    FakeEmbed,
    FakeMigrationStore,
    _apply,
    _dry,
    _evidence,
    _learning,
)


@pytest.mark.asyncio
async def test_live_session_knowledge_is_excluded_and_counted() -> None:
    structured = _learning(2001, "Already structured live learning.")
    structured.metadata.update(
        {
            "session_knowledge_capture_identity": "session-knowledge-capture:2001",
            "session_knowledge": {
                "schema_version": "session-knowledge-capture.v1",
                "role": "session_learning",
            },
            "memory_write_judge": {"decision": "ALLOW"},
        }
    )
    legacy = _learning(2002, "Historical learning.")
    store = FakeMigrationStore([structured, legacy])

    report = await _dry(store)

    assert report["already_structured_count"] == 1
    assert [plan["source_id"] for plan in report["plans"]] == [2002]


def test_gate_blocks_unmeasured_and_instrument_mismatch() -> None:
    from open_brain.session_knowledge_migration import evaluate_migration_gate

    report = {
        "cohort_digest": "cohort",
        "cohort_watermark": {"count": 0, "max_id": None},
        "proposed_operation_id": "00000000-0000-4000-8000-000000002002",
        "retrieval_control_baseline": {
            "instrument": None,
            "lexical": 0.0,
            "vector": 0.0,
            "rerank": 0.0,
            "unmeasured": True,
        },
    }
    evidence = {
        "decision": "ALLOW",
        "operation_id": report["proposed_operation_id"],
        "dry_run_report_digest": "invalid-until-replaced",
        "cohort_digest": "cohort",
        "cohort_watermark": report["cohort_watermark"],
        "batch_scope": {"limit": 10, "after_id": 0},
        "backup_restore_receipt": {"verified": True},
        "retrieval_control_baseline": report["retrieval_control_baseline"],
        "unresolved_acknowledgement": True,
        "provider_metadata": PROVIDER_META,
    }
    from open_brain.session_knowledge_migration import compute_report_digest

    evidence["dry_run_report_digest"] = compute_report_digest(report)
    blocked = evaluate_migration_gate(
        decision="ALLOW",
        dry_run_report=report,
        evidence=evidence,
        configured_provider_metadata=PROVIDER_META,
    )
    assert "retrieval_control_baseline_unmeasured" in blocked["reasons"]

    report["retrieval_control_baseline"] = {
        "instrument": "instrument-a",
        "lexical": 0.8,
        "vector": 0.8,
        "rerank": 0.8,
    }
    evidence["retrieval_control_baseline"] = {
        **report["retrieval_control_baseline"],
        "instrument": "instrument-b",
    }
    evidence["dry_run_report_digest"] = compute_report_digest(report)
    mismatch = evaluate_migration_gate(
        decision="ALLOW",
        dry_run_report=report,
        evidence=evidence,
        configured_provider_metadata=PROVIDER_META,
    )
    assert "retrieval_control_instrument_mismatch" in mismatch["reasons"]


@pytest.mark.asyncio
async def test_final_embedding_arity_failure_is_terminal_and_does_not_archive() -> None:
    class ShortFinalEmbed(FakeEmbed):
        async def embed_documents(self, texts: list[str]):
            vectors, usage = await super().embed_documents(texts)
            return (vectors if self.n == 1 else vectors[:-1]), usage

    store = FakeMigrationStore([_learning(2003, "Final arity must match.")])
    result = await _apply(store, await _dry(store), embed=ShortFinalEmbed())

    assert result["status"] == "failed"
    assert store.memories[2003].metadata.get("status") != "archived"


def test_migrated_metadata_uses_capture_status_vocabulary() -> None:
    from open_brain.data_layer.interface import CAPTURE_STATUS_VALUES
    from open_brain.session_knowledge_migration import transform_legacy_memory

    plan = transform_legacy_memory(_learning(2004, "Vocabulary-safe metadata."))
    for output in plan.outputs:
        if output.persist:
            status = output.metadata.get("capture_status")
            assert status is None or status in CAPTURE_STATUS_VALUES


@pytest.mark.asyncio
async def test_cli_full_allow_dispatches_operational_adapters(tmp_path) -> None:
    from open_brain.cli import main as cli_main

    store = FakeMigrationStore([_learning(2005, "Operational CLI apply.")])
    store.pool = MagicMock()
    report = await _dry(store)
    evidence = _evidence(report)
    report_path = tmp_path / "dry-run.json"
    evidence_path = tmp_path / "gate.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    applied = AsyncMock(return_value={"status": "completed", "writes": 1})

    with (
        patch(
            "open_brain.session_knowledge_migration.build_postgres_migration_store",
            new=AsyncMock(return_value=store),
        ),
        patch(
            "open_brain.session_knowledge_migration.configured_provider_metadata_from_config",
            return_value=PROVIDER_META,
        ),
        patch(
            "open_brain.session_knowledge_migration.ConfiguredRetrievalControlAdapter",
            return_value=MagicMock(instrument="fake-controls.v1"),
        ),
        patch(
            "open_brain.session_knowledge_migration.apply_session_knowledge_migration_batch",
            new=applied,
        ),
        patch.dict("os.environ", {"VOYAGE_API_KEY": "test-provider-key"}),
    ):
        args = argparse.Namespace(
            command="session-knowledge-migration",
            skm_command="apply",
            apply=True,
            operation_id=evidence["operation_id"],
            gate_evidence_file=str(evidence_path),
            dry_run_report_file=str(report_path),
        )
        result = await cli_main._cmd_session_knowledge_migration(args)

    assert result["status"] == "completed"
    applied.assert_awaited_once()


@pytest.mark.asyncio
async def test_configured_control_uses_real_instrument_seams() -> None:
    from open_brain.session_knowledge_migration import (
        ConfiguredRetrievalControlAdapter,
    )

    conn = AsyncMock()
    conn.fetchval.return_value = 0.4

    @asynccontextmanager
    async def acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = acquire
    adapter = ConfiguredRetrievalControlAdapter(pool, PROVIDER_META)
    rerank_results = [SimpleNamespace(index=0, relevance_score=0.9)]

    with (
        patch(
            "open_brain.data_layer.embedding.embed_query_with_usage",
            new=AsyncMock(return_value=([1.0, 0.0], 1)),
        ),
        patch(
            "open_brain.data_layer.embedding.embed_batch_with_usage",
            new=AsyncMock(return_value=([[1.0, 0.0]], 1)),
        ),
        patch(
            "open_brain.data_layer.reranker.rerank",
            new=AsyncMock(return_value=rerank_results),
        ),
    ):
        lexical = await adapter.measure(
            control="lexical", query="query", documents=["document"]
        )
        vector = await adapter.measure(
            control="vector", query="query", documents=["document"]
        )
        rerank = await adapter.measure(
            control="rerank", query="query", documents=["document"]
        )

    assert lexical == 0.4
    assert vector == 1.0
    assert rerank == 0.9
    assert adapter.instrument.startswith("configured-retrieval-controls.v1:")
