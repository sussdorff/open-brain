"""Tests for legacy session-knowledge -> EKN migration (open-brain-ekn.8).

Seams under test (public):
- transition contract (`transform_legacy_memory`)
- side-effect-free dry run (`dry_run_session_knowledge_migration`)
- human decision gate (`evaluate_migration_gate`)
- bounded apply/resume/replay (`apply_session_knowledge_migration_batch`)
- reconciliation / rollback readiness
- CLI JSON surfaces
- disposable Postgres schema parity + concurrency (integration)
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import seam — must fail RED before production module exists
# ---------------------------------------------------------------------------


def test_red_module_import_and_contract_version() -> None:
    """RED: migration module and versioned contract must exist."""
    from open_brain.session_knowledge_migration import (
        LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_ID,
        LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_VERSION,
    )

    assert (
        LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_VERSION
        == "legacy-session-knowledge-migration.v1"
    )
    assert LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_ID.startswith("standard://")


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeMemory:
    id: int
    type: str
    content: str
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    project: str | None = None
    session_ref: str | None = None
    embedding: list[float] | None = None


@dataclass
class FakeWrite:
    kind: str
    payload: dict[str, Any]


class FakeMigrationStore:
    """In-memory store that records writes for side-effect assertions."""

    def __init__(self, memories: list[FakeMemory] | None = None) -> None:
        self.memories = {m.id: m for m in (memories or [])}
        self.relationships: list[dict[str, Any]] = []
        self.writes: list[FakeWrite] = []
        self.operations: dict[str, dict[str, Any]] = {}
        self.mappings: dict[tuple[str, int], dict[str, Any]] = {}
        self.review_rows: list[dict[str, Any]] = []
        self.next_id = max(self.memories, default=0) + 1
        self.sequences_consumed = 0
        self.embedding_calls = 0
        self.rerank_calls = 0
        self.reconcile_calls = 0
        self.run_ledger_writes = 0

    async def list_eligible_cohort(self) -> list[FakeMemory]:
        from open_brain.session_knowledge_migration import (
            is_migration_derived_metadata,
            is_structured_session_knowledge_metadata,
        )

        return [
            m
            for m in sorted(self.memories.values(), key=lambda x: x.id)
            if m.type in {"session_summary", "learning"}
            and (m.metadata or {}).get("status") != "archived"
            and (m.metadata or {}).get("session_knowledge_migration", {}).get(
                "superseded_by_operation"
            )
            is None
            and not is_migration_derived_metadata(m.metadata or {})
            and not is_structured_session_knowledge_metadata(m.metadata or {})
        ]

    async def count_already_structured(self) -> int:
        from open_brain.session_knowledge_migration import (
            is_structured_session_knowledge_metadata,
        )

        return sum(
            is_structured_session_knowledge_metadata(memory.metadata or {})
            for memory in self.memories.values()
        )

    async def get_memory(self, memory_id: int) -> FakeMemory | None:
        return self.memories.get(memory_id)

    async def count_review_ledger(self) -> dict[str, Any]:
        rows = list(self.review_rows)
        digest = hashlib.sha256(
            json.dumps(rows, sort_keys=True, default=str).encode()
        ).hexdigest()
        return {"count": len(rows), "digest": digest}

    async def save_derived(
        self,
        *,
        memory_type: str,
        content: str,
        metadata: dict[str, Any],
        title: str = "",
        embedding: list[float] | None = None,
    ) -> int:
        mid = self.next_id
        self.next_id += 1
        self.sequences_consumed += 1
        self.memories[mid] = FakeMemory(
            id=mid,
            type=memory_type,
            content=content,
            title=title,
            metadata=metadata,
            embedding=list(embedding) if embedding is not None else None,
        )
        self.writes.append(
            FakeWrite(
                "save_derived",
                {
                    "id": mid,
                    "type": memory_type,
                    "content": content,
                    "metadata": metadata,
                    "embedding": embedding,
                },
            )
        )
        return mid

    async def update_embedding(self, memory_id: int, embedding: list[float]) -> None:
        self.memories[memory_id].embedding = list(embedding)

    async def has_accepted_promotion_grant(self, memory_id: int) -> bool:
        return False

    async def list_mappings_for_source(self, source_id: int) -> list[dict[str, Any]]:
        return [
            dict(v)
            for (_, sid), v in sorted(self.mappings.items())
            if sid == source_id
        ]

    async def schema_ready(self) -> bool:
        return True

    async def create_relationship(
        self,
        source_id: int,
        target_id: int,
        link_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        rid = len(self.relationships) + 1
        self.relationships.append(
            {
                "id": rid,
                "source_id": source_id,
                "target_id": target_id,
                "link_type": link_type,
                "metadata": metadata or {},
            }
        )
        self.writes.append(
            FakeWrite(
                "relationship",
                {
                    "id": rid,
                    "source_id": source_id,
                    "target_id": target_id,
                    "link_type": link_type,
                },
            )
        )
        return rid

    async def archive_legacy(
        self, memory_id: int, *, operation_id: str, rollback: dict[str, Any]
    ) -> None:
        mem = self.memories[memory_id]
        meta = dict(mem.metadata or {})
        meta["status"] = "archived"
        skm = dict(meta.get("session_knowledge_migration") or {})
        skm["superseded_by_operation"] = operation_id
        skm["rollback"] = rollback
        meta["session_knowledge_migration"] = skm
        mem.metadata = meta
        self.writes.append(
            FakeWrite("archive", {"memory_id": memory_id, "operation_id": operation_id})
        )

    async def delete_memory(self, memory_id: int) -> None:
        raise AssertionError("hard delete is forbidden during initial cutover")

    async def upsert_operation(self, record: dict[str, Any]) -> None:
        self.operations[record["operation_id"]] = dict(record)
        self.run_ledger_writes += 1
        self.writes.append(FakeWrite("operation", record))

    async def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        row = self.operations.get(operation_id)
        return dict(row) if row else None

    async def upsert_mapping(self, record: dict[str, Any]) -> None:
        key = (record["operation_id"], int(record["source_id"]))
        self.mappings[key] = dict(record)
        self.writes.append(FakeWrite("mapping", record))

    async def get_mapping(
        self, operation_id: str, source_id: int
    ) -> dict[str, Any] | None:
        row = self.mappings.get((operation_id, source_id))
        return dict(row) if row else None

    async def list_mappings(self, operation_id: str) -> list[dict[str, Any]]:
        return [
            dict(v)
            for (op, _), v in sorted(self.mappings.items())
            if op == operation_id
        ]


class FakeEmbedAdapter:
    def __init__(self, *, model: str = "test-embed", dimension: int = 8) -> None:
        self.model = model
        self.dimension = dimension
        self.calls: list[str] = []

    async def embed_documents(
        self, texts: list[str]
    ) -> tuple[list[list[float]], dict[str, Any]]:
        self.calls.extend(texts)
        vectors = [[0.1] * self.dimension for _ in texts]
        return vectors, {
            "documents": len(texts),
            "tokens": sum(max(1, len(t) // 4) for t in texts),
            "model": self.model,
            "dimension": self.dimension,
        }


class FakeRerankAdapter:
    def __init__(self, *, model: str = "test-rerank") -> None:
        self.model = model
        self.calls = 0

    async def rerank(
        self, query: str, documents: list[str]
    ) -> tuple[list[int], dict[str, Any]]:
        self.calls += 1
        order = list(range(len(documents)))
        return order, {"model": self.model, "documents": len(documents)}


class FakeReconcileAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def reconcile(self, memory_ids: list[int]) -> dict[str, Any]:
        self.calls += 1
        return {"scoped_ids": list(memory_ids), "refined": len(memory_ids)}


class FakeControlAdapter:
    instrument = "fake-controls.v1"

    def __init__(self, scores: dict[str, float] | None = None) -> None:
        self.scores = dict(
            scores or {"lexical": 0.95, "vector": 0.95, "rerank": 0.95}
        )

    async def measure(
        self, *, control: str, query: str, documents: list[str]
    ) -> float:
        return float(self.scores.get(control, 0.95))


def _summary(
    mid: int,
    content: str,
    *,
    origin_producer: str = "session-close",
    source_ref: str = "session:abc",
) -> FakeMemory:
    return FakeMemory(
        id=mid,
        type="session_summary",
        title=f"Session {mid}",
        content=content,
        metadata={
            "project": "open-brain",
            "source": "session-close",
            "provenance": {
                "origin": {
                    "producer": origin_producer,
                    "source_ref": source_ref,
                }
            },
        },
        session_ref="session:abc",
    )


def _learning(
    mid: int,
    content: str,
    *,
    source_label: str | None = None,
    expected_use: str | None = None,
) -> FakeMemory:
    provenance: dict[str, Any] = {
        "origin": {"producer": "legacy-learning", "source_ref": f"learning:{mid}"}
    }
    if source_label is not None:
        provenance["source_label"] = source_label
    if expected_use is not None:
        provenance["expected_use"] = expected_use
    return FakeMemory(
        id=mid,
        type="learning",
        title=f"Learning {mid}",
        content=content,
        metadata={"provenance": provenance, "project": "open-brain"},
    )


PROVIDER_META = {
    "embedding_model": "configured-embed-model",
    "rerank_model": "configured-rerank-model",
    "embedding_dimension": 8,
    "cost_per_1k_tokens": 0.02,
    "chars_per_token": 4.0,
}


# ---------------------------------------------------------------------------
# Transition matrix
# ---------------------------------------------------------------------------


class TestTransitionContract:
    def test_session_summary_with_sections_routes_conservatively(self) -> None:
        from open_brain.session_knowledge_migration import transform_legacy_memory

        mem = _summary(
            1,
            "\n".join(
                [
                    "Observed: landed migration dry-run path.",
                    "Key Decisions:",
                    "- Use reversible archival instead of delete.",
                    "What was learned:",
                    "- Gate production apply behind explicit ALLOW evidence.",
                    "Still pending: wire CLI status subcommand.",
                ]
            ),
        )
        plan = transform_legacy_memory(mem)
        routes = {item.route for item in plan.outputs}
        assert plan.source_id == 1
        assert "session_event" in routes
        assert "session_decision" in routes
        assert "inferred_learning" in routes
        assert "unfinished_work" in routes
        learning = next(o for o in plan.outputs if o.route == "inferred_learning")
        assert learning.epistemic["source_label"] == "inferred"
        assert learning.epistemic["expected_use"] == "evidence"
        unfinished = next(o for o in plan.outputs if o.route == "unfinished_work")
        assert unfinished.persist is False

    def test_legacy_learning_never_promoted_by_type(self) -> None:
        from open_brain.session_knowledge_migration import transform_legacy_memory

        mem = _learning(2, "Prefer reversible supersession for cutovers.")
        plan = transform_legacy_memory(mem)
        assert len(plan.outputs) == 1
        out = plan.outputs[0]
        assert out.route == "inferred_learning"
        assert out.epistemic["source_label"] == "inferred"
        assert out.epistemic["expected_use"] == "evidence"
        assert out.persist is True

    def test_confirmed_learning_requires_existing_confirmed_label(self) -> None:
        from open_brain.session_knowledge_migration import transform_legacy_memory

        mem = _learning(
            3,
            "Confirmed lesson with prior promotion.",
            source_label="confirmed",
            expected_use="instruction",
        )
        plan = transform_legacy_memory(mem)
        out = plan.outputs[0]
        # Migration must not raise authority; confirmed stays only if already present
        # but expected_use is clamped to evidence unless promotion ledger proves it.
        assert out.epistemic["source_label"] in {"inferred", "confirmed"}
        assert out.epistemic["expected_use"] == "evidence" or (
            out.epistemic["source_label"] == "confirmed"
            and out.preserved_authority is True
        )

    def test_unsupported_shape_is_unresolved_or_quarantine(self) -> None:
        from open_brain.session_knowledge_migration import transform_legacy_memory

        mem = _summary(4, "??? garbled binary \x00\x01 without structure")
        # Use a clearly empty/unsupported payload
        mem.content = ""
        plan = transform_legacy_memory(mem)
        assert plan.outcome in {"unresolved", "quarantine"}
        assert all(not o.persist for o in plan.outputs) or plan.outcome != "ok"

    def test_preserves_canonical_origin(self) -> None:
        from open_brain.session_knowledge_migration import transform_legacy_memory

        mem = _summary(
            5,
            "Observed: kept origin intact.\nKey Decisions:\n- Keep producer/source_ref.",
            origin_producer="worktree-session-summary",
            source_ref="wt:sess-9",
        )
        plan = transform_legacy_memory(mem)
        for out in plan.outputs:
            if not out.persist:
                continue
            origin = out.metadata["provenance"]["origin"]
            assert origin["producer"] == "worktree-session-summary"
            assert origin["source_ref"] == "wt:sess-9"


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRunSideEffectFree:
    @pytest.mark.asyncio
    async def test_dry_run_reports_routes_and_writes_nothing(self) -> None:
        from open_brain.session_knowledge_migration import (
            dry_run_session_knowledge_migration,
        )

        store = FakeMigrationStore(
            [
                _summary(
                    10,
                    "Observed: dry run only.\nKey Decisions:\n- No writes.\n"
                    "What was learned:\n- Estimates use configured metadata.\n"
                    "Still pending: catch-up after watermark.",
                ),
                _learning(11, "Legacy learning stays inferred."),
            ]
        )
        store.review_rows = [
            {
                "id": 1,
                "review_key": "session-learning:10",
                "decision": "accept",
            }
        ]
        before_ops = dict(store.operations)
        before_maps = dict(store.mappings)
        before_writes = len(store.writes)
        before_seq = store.sequences_consumed
        before_reviews = list(store.review_rows)

        report = await dry_run_session_knowledge_migration(
            store,
            provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
        )

        assert report["schema_version"] == "legacy-session-knowledge-migration.v1"
        assert report["route_counts"]["session_event"] >= 1
        assert report["route_counts"]["inferred_learning"] >= 1
        assert "cohort_watermark" in report
        assert "cohort_digest" in report
        assert "proposed_operation_id" in report
        assert "evidence_digest" in report
        assert "provider_estimate" in report
        assert report["provider_estimate"]["model"] == "configured-embed-model"
        assert "catch_up_plan" in report
        assert report["review_ledger_before"]["count"] == 1
        # Side-effect free
        assert store.operations == before_ops
        assert store.mappings == before_maps
        assert len(store.writes) == before_writes
        assert store.sequences_consumed == before_seq
        assert store.embedding_calls == 0
        assert store.rerank_calls == 0
        assert store.run_ledger_writes == 0
        assert store.review_rows == before_reviews
        for mem in store.memories.values():
            assert (mem.metadata or {}).get("status") != "archived"


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class TestHumanDecisionGate:
    def test_default_is_block_without_evidence(self) -> None:
        from open_brain.session_knowledge_migration import evaluate_migration_gate

        result = evaluate_migration_gate(
            decision=None,
            dry_run_report={"evidence_digest": "abc", "cohort_digest": "def"},
            evidence={},
        )
        assert result["outcome"] == "BLOCK"
        assert result["writes_authorized"] is False

    def test_mismatched_digest_blocks(self) -> None:
        from open_brain.session_knowledge_migration import evaluate_migration_gate

        report = {
            "evidence_digest": "exact-digest",
            "cohort_digest": "cohort-1",
            "cohort_watermark": {"max_id": 11, "count": 2},
            "unresolved_count": 0,
            "quarantine_count": 0,
        }
        result = evaluate_migration_gate(
            decision="ALLOW",
            dry_run_report=report,
            evidence={
                "decision": "ALLOW",
                "dry_run_report_digest": "wrong",
                "cohort_digest": "cohort-1",
                "cohort_watermark": report["cohort_watermark"],
                "batch_scope": {"limit": 10, "after_id": 0},
                "backup_restore_receipt": {
                    "verified": True,
                    "bundle_digest": "bundle-1",
                },
                "retrieval_control_baseline": {
                    "lexical": 0.9,
                    "vector": 0.9,
                    "rerank": 0.9,
                },
                "unresolved_acknowledgement": True,
                "provider_metadata": PROVIDER_META,
            },
            configured_provider_metadata=PROVIDER_META,
        )
        assert result["outcome"] == "BLOCK"
        assert result["writes_authorized"] is False

    def test_full_allow_evidence_authorizes(self) -> None:
        from open_brain.session_knowledge_migration import (
            compute_report_digest,
            evaluate_migration_gate,
        )

        report = {
            "schema_version": "legacy-session-knowledge-migration.v1",
            "evidence_digest": "will-replace",
            "cohort_digest": "cohort-1",
            "cohort_watermark": {"max_id": 11, "count": 2},
            "unresolved_count": 0,
            "quarantine_count": 0,
            "route_counts": {},
            "provider_estimate": {"model": "configured-embed-model"},
            "proposed_operation_id": "11111111-1111-4111-8111-111111111111",
            "retrieval_control_baseline": {
                "instrument": "fake-controls.v1",
                "lexical": 0.9,
                "vector": 0.9,
                "rerank": 0.9,
            },
            "retrieval_control_source_baselines": {},
            "plans": [],
        }
        digest = compute_report_digest(report)
        report["evidence_digest"] = digest
        result = evaluate_migration_gate(
            decision="ALLOW",
            dry_run_report=report,
            evidence={
                "decision": "ALLOW",
                "operation_id": report["proposed_operation_id"],
                "dry_run_report_digest": digest,
                "cohort_digest": "cohort-1",
                "cohort_watermark": report["cohort_watermark"],
                "batch_scope": {"limit": 10, "after_id": 0},
                "backup_restore_receipt": {
                    "verified": True,
                    "bundle_digest": "bundle-1",
                },
                "retrieval_control_baseline": report["retrieval_control_baseline"],
                "unresolved_acknowledgement": True,
                "provider_metadata": PROVIDER_META,
            },
            configured_provider_metadata=PROVIDER_META,
        )
        assert result["outcome"] == "ALLOW"
        assert result["writes_authorized"] is True

    def test_revise_and_escalate_write_nothing(self) -> None:
        from open_brain.session_knowledge_migration import evaluate_migration_gate

        for decision in ("REVISE", "ESCALATE", "BLOCK"):
            result = evaluate_migration_gate(
                decision=decision,
                dry_run_report={"evidence_digest": "x", "cohort_digest": "y"},
                evidence={"decision": decision},
            )
            assert result["outcome"] == decision
            assert result["writes_authorized"] is False


# ---------------------------------------------------------------------------
# Apply / resume / conflict
# ---------------------------------------------------------------------------


class TestApplyResumeReplay:
    def _allow_evidence(self, report: dict[str, Any]) -> dict[str, Any]:
        from open_brain.session_knowledge_migration import compute_report_digest

        digest = compute_report_digest(report)
        report["evidence_digest"] = digest
        baseline = report.get("retrieval_control_baseline") or {
            "lexical": 0.8,
            "vector": 0.8,
            "rerank": 0.8,
        }
        return {
            "decision": "ALLOW",
            "operation_id": report["proposed_operation_id"],
            "dry_run_report_digest": digest,
            "cohort_digest": report["cohort_digest"],
            "cohort_watermark": report["cohort_watermark"],
            "batch_scope": {"limit": 50, "after_id": 0},
            "backup_restore_receipt": {"verified": True, "bundle_digest": "b1"},
            "retrieval_control_baseline": baseline,
            "unresolved_acknowledgement": True,
            "provider_metadata": PROVIDER_META,
        }

    def _control(self) -> FakeControlAdapter:
        return FakeControlAdapter()

    @pytest.mark.asyncio
    async def test_apply_without_gate_is_blocked_zero_writes(self) -> None:
        from open_brain.session_knowledge_migration import (
            apply_session_knowledge_migration_batch,
            dry_run_session_knowledge_migration,
        )

        store = FakeMigrationStore([_learning(20, "Do not write without gate.")])
        report = await dry_run_session_knowledge_migration(
            store, provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
        )
        result = await apply_session_knowledge_migration_batch(
            store,
            dry_run_report=report,
            gate_evidence={"decision": "ALLOW"},  # incomplete
            embed_adapter=FakeEmbedAdapter(),
            rerank_adapter=FakeRerankAdapter(),
            reconcile_adapter=FakeReconcileAdapter(),
            provider_metadata=PROVIDER_META,
        )
        assert result["status"] == "blocked"
        assert result["writes"] == 0
        assert store.writes == []
        assert store.sequences_consumed == 0

    @pytest.mark.asyncio
    async def test_apply_is_idempotent_on_replay(self) -> None:
        from open_brain.session_knowledge_migration import (
            apply_session_knowledge_migration_batch,
            dry_run_session_knowledge_migration,
        )

        store = FakeMigrationStore(
            [
                _summary(
                    30,
                    "Observed: replay safety.\nKey Decisions:\n- Stable operation IDs.\n"
                    "What was learned:\n- Replay must not duplicate outputs.",
                )
            ]
        )
        report = await dry_run_session_knowledge_migration(
            store, provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
        )
        evidence = self._allow_evidence(report)
        embed = FakeEmbedAdapter()
        first = await apply_session_knowledge_migration_batch(
            store,
            dry_run_report=report,
            gate_evidence=evidence,
            embed_adapter=embed,
            rerank_adapter=FakeRerankAdapter(),
            reconcile_adapter=FakeReconcileAdapter(),
            provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
            retrieval_controls={
                "lexical": {"query": "replay", "baseline": 0.8, "score": 0.9},
                "vector": {"query": "replay", "baseline": 0.8, "score": 0.9},
                "rerank": {"query": "replay", "baseline": 0.8, "score": 0.9},
            },
        )
        assert first["status"] in {"completed", "completed_with_errors"}
        output_ids_1 = sorted(first["output_ids"])
        mapping_count_1 = len(await store.list_mappings(first["operation_id"]))
        writes_after_first = len(store.writes)

        second = await apply_session_knowledge_migration_batch(
            store,
            dry_run_report=report,
            gate_evidence=evidence,
            embed_adapter=embed,
            rerank_adapter=FakeRerankAdapter(),
            reconcile_adapter=FakeReconcileAdapter(),
            provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
            retrieval_controls={
                "lexical": {"query": "replay", "baseline": 0.8, "score": 0.9},
                "vector": {"query": "replay", "baseline": 0.8, "score": 0.9},
                "rerank": {"query": "replay", "baseline": 0.8, "score": 0.9},
            },
            operation_id=first["operation_id"],
        )
        assert second["status"] in {"completed", "replayed"}
        assert sorted(second["output_ids"]) == output_ids_1
        assert len(await store.list_mappings(first["operation_id"])) == mapping_count_1
        # No duplicate derived rows for same source identities
        derived = [
            m
            for m in store.memories.values()
            if (m.metadata or {}).get("session_knowledge", {}).get("role")
        ]
        identities = [
            (m.metadata or {}).get("session_knowledge_record_identity") for m in derived
        ]
        assert len(identities) == len(set(identities))
        assert len(store.writes) >= writes_after_first  # status updates ok

    @pytest.mark.asyncio
    async def test_payload_change_under_same_operation_conflicts(self) -> None:
        from open_brain.session_knowledge_migration import (
            apply_session_knowledge_migration_batch,
            dry_run_session_knowledge_migration,
        )

        store = FakeMigrationStore([_learning(40, "Original learning text.")])
        report = await dry_run_session_knowledge_migration(
            store, provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
        )
        evidence = self._allow_evidence(report)
        first = await apply_session_knowledge_migration_batch(
            store,
            dry_run_report=report,
            gate_evidence=evidence,
            embed_adapter=FakeEmbedAdapter(),
            rerank_adapter=FakeRerankAdapter(),
            reconcile_adapter=FakeReconcileAdapter(),
            provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
            retrieval_controls={
                "lexical": {"query": "x", "baseline": 0.1, "score": 0.9},
                "vector": {"query": "x", "baseline": 0.1, "score": 0.9},
                "rerank": {"query": "x", "baseline": 0.1, "score": 0.9},
            },
        )
        # Mutate source content then attempt same operation/evidence
        store.memories[40].content = "Changed learning text under same op."
        conflicted = await apply_session_knowledge_migration_batch(
            store,
            dry_run_report=report,
            gate_evidence=evidence,
            embed_adapter=FakeEmbedAdapter(),
            rerank_adapter=FakeRerankAdapter(),
            reconcile_adapter=FakeReconcileAdapter(),
            provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
            retrieval_controls={
                "lexical": {"query": "x", "baseline": 0.1, "score": 0.9},
                "vector": {"query": "x", "baseline": 0.1, "score": 0.9},
                "rerank": {"query": "x", "baseline": 0.1, "score": 0.9},
            },
            operation_id=first["operation_id"],
        )
        assert conflicted["status"] == "conflict"

    @pytest.mark.asyncio
    async def test_no_hard_delete_and_legacy_archived_reversibly(self) -> None:
        from open_brain.session_knowledge_migration import (
            apply_session_knowledge_migration_batch,
            dry_run_session_knowledge_migration,
        )

        store = FakeMigrationStore(
            [
                _summary(
                    50,
                    "Observed: archive path.\nKey Decisions:\n- Keep content.\n"
                    "What was learned:\n- Rollback metadata must remain.",
                )
            ]
        )
        original_content = store.memories[50].content
        report = await dry_run_session_knowledge_migration(
            store, provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
        )
        evidence = self._allow_evidence(report)
        result = await apply_session_knowledge_migration_batch(
            store,
            dry_run_report=report,
            gate_evidence=evidence,
            embed_adapter=FakeEmbedAdapter(),
            rerank_adapter=FakeRerankAdapter(),
            reconcile_adapter=FakeReconcileAdapter(),
            provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
            retrieval_controls={
                "lexical": {"query": "archive", "baseline": 0.5, "score": 0.9},
                "vector": {"query": "archive", "baseline": 0.5, "score": 0.9},
                "rerank": {"query": "archive", "baseline": 0.5, "score": 0.9},
            },
        )
        assert result["status"] in {"completed", "completed_with_errors"}
        legacy = store.memories[50]
        assert legacy.content == original_content
        assert legacy.metadata["status"] == "archived"
        assert "rollback" in legacy.metadata["session_knowledge_migration"]
        assert 50 in store.memories  # not deleted


# ---------------------------------------------------------------------------
# Rebuild order + retrieval controls
# ---------------------------------------------------------------------------


class TestScopedRebuildAndRetrieval:
    @pytest.mark.asyncio
    async def test_rebuild_order_and_fail_closed_controls(self) -> None:
        from open_brain.session_knowledge_migration import (
            apply_session_knowledge_migration_batch,
            dry_run_session_knowledge_migration,
        )

        store = FakeMigrationStore(
            [
                _summary(
                    60,
                    "Observed: rebuild order.\nKey Decisions:\n- Inject adapters.\n"
                    "What was learned:\n- Fail closed on retrieval regression.",
                )
            ]
        )
        report = await dry_run_session_knowledge_migration(
            store, provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
        )
        from open_brain.session_knowledge_migration import compute_report_digest

        digest = compute_report_digest(report)
        report["evidence_digest"] = digest
        evidence = {
            "decision": "ALLOW",
            "operation_id": report["proposed_operation_id"],
            "dry_run_report_digest": digest,
            "cohort_digest": report["cohort_digest"],
            "cohort_watermark": report["cohort_watermark"],
            "batch_scope": {"limit": 50, "after_id": 0},
            "backup_restore_receipt": {"verified": True, "bundle_digest": "b1"},
            "retrieval_control_baseline": report["retrieval_control_baseline"],
            "unresolved_acknowledgement": True,
            "provider_metadata": PROVIDER_META,
        }
        order: list[str] = []

        class OrderedEmbed(FakeEmbedAdapter):
            async def embed_documents(
                self, texts: list[str]
            ) -> tuple[list[list[float]], dict[str, Any]]:
                order.append("embed")
                return await super().embed_documents(texts)

        class OrderedReconcile(FakeReconcileAdapter):
            async def reconcile(self, memory_ids: list[int]) -> dict[str, Any]:
                order.append("reconcile")
                return await super().reconcile(memory_ids)

        class OrderedRerank(FakeRerankAdapter):
            async def rerank(
                self, query: str, documents: list[str]
            ) -> tuple[list[int], dict[str, Any]]:
                order.append("rerank")
                return await super().rerank(query, documents)

        # First succeed to establish phase order
        ok = await apply_session_knowledge_migration_batch(
            store,
            dry_run_report=report,
            gate_evidence=evidence,
            embed_adapter=OrderedEmbed(),
            rerank_adapter=OrderedRerank(),
            reconcile_adapter=OrderedReconcile(),
            provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
            retrieval_controls={
                "lexical": {"query": "rebuild", "baseline": 0.9, "score": 0.95},
                "vector": {"query": "rebuild", "baseline": 0.9, "score": 0.95},
                "rerank": {"query": "rebuild", "baseline": 0.9, "score": 0.95},
            },
        )
        assert ok["status"] in {"completed", "completed_with_errors", "replayed"}
        # transform implied before first embed; reconcile between embeds; verify uses rerank
        assert order[0] == "embed"
        assert "reconcile" in order
        assert order.index("reconcile") > order.index("embed")

        # Fail closed when scores drop below baseline
        store2 = FakeMigrationStore(
            [
                _summary(
                    61,
                    "Observed: control failure.\nKey Decisions:\n- Fail closed.\n"
                    "What was learned:\n- Low relevance blocks completion.",
                )
            ]
        )
        report2 = await dry_run_session_knowledge_migration(
            store2, provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
        )
        dig2 = compute_report_digest(report2)
        report2["evidence_digest"] = dig2
        evidence2 = dict(evidence)
        evidence2["dry_run_report_digest"] = dig2
        evidence2["operation_id"] = report2["proposed_operation_id"]
        evidence2["cohort_digest"] = report2["cohort_digest"]
        evidence2["cohort_watermark"] = report2["cohort_watermark"]
        evidence2["retrieval_control_baseline"] = report2["retrieval_control_baseline"]
        failed = await apply_session_knowledge_migration_batch(
            store2,
            dry_run_report=report2,
            gate_evidence=evidence2,
            embed_adapter=FakeEmbedAdapter(),
            rerank_adapter=FakeRerankAdapter(),
            reconcile_adapter=FakeReconcileAdapter(),
            provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(
                {"lexical": 0.1, "vector": 0.1, "rerank": 0.1}
            ),
        )
        assert failed["status"] in {"failed", "failed_retrieval"}


class TestRetrievalControlTolerance:
    """Sub-percent provider jitter must not fail a healthy batch (K4)."""

    async def _run_apply_with_scores(self, apply_scores: dict[str, float]) -> dict:
        from open_brain.session_knowledge_migration import (
            apply_session_knowledge_migration_batch,
            compute_report_digest,
            dry_run_session_knowledge_migration,
        )

        store = FakeMigrationStore(
            [
                _summary(
                    62,
                    "Observed: tolerance check.\nKey Decisions:\n- Bound jitter.\n"
                    "What was learned:\n- Tolerance bands beat exact floors.",
                )
            ]
        )
        report = await dry_run_session_knowledge_migration(
            store,
            provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
        )
        digest = compute_report_digest(report)
        report["evidence_digest"] = digest
        evidence = {
            "decision": "ALLOW",
            "operation_id": report["proposed_operation_id"],
            "dry_run_report_digest": digest,
            "cohort_digest": report["cohort_digest"],
            "cohort_watermark": report["cohort_watermark"],
            "batch_scope": {"limit": 50, "after_id": 0},
            "backup_restore_receipt": {"verified": True, "bundle_digest": "b1"},
            "retrieval_control_baseline": report["retrieval_control_baseline"],
            "unresolved_acknowledgement": True,
            "provider_metadata": PROVIDER_META,
        }
        return await apply_session_knowledge_migration_batch(
            store,
            dry_run_report=report,
            gate_evidence=evidence,
            embed_adapter=FakeEmbedAdapter(),
            rerank_adapter=FakeRerankAdapter(),
            reconcile_adapter=FakeReconcileAdapter(),
            provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(apply_scores),
        )

    @pytest.mark.asyncio
    async def test_jitter_within_tolerance_passes(self) -> None:
        # Baseline 0.95; 0.945 is ~0.5% below — inside the 1% band.
        result = await self._run_apply_with_scores(
            {"lexical": 0.945, "vector": 0.945, "rerank": 0.945}
        )
        assert result["status"] in {"completed", "completed_with_errors", "replayed"}
        assert not result["errors"]

    @pytest.mark.asyncio
    async def test_real_regression_still_fails_closed(self) -> None:
        # 0.90 is >5% below the 0.95 baseline — outside the band.
        result = await self._run_apply_with_scores(
            {"lexical": 0.90, "vector": 0.90, "rerank": 0.90}
        )
        assert result["status"] == "failed"
        assert any(
            e.get("code") == "retrieval_control_failed" for e in result["errors"]
        )


# ---------------------------------------------------------------------------
# Reconciliation + review preservation
# ---------------------------------------------------------------------------


class TestReconciliationAndReview:
    @pytest.mark.asyncio
    async def test_reconciliation_proves_lineage_and_review_preservation(self) -> None:
        from open_brain.session_knowledge_migration import (
            apply_session_knowledge_migration_batch,
            compute_report_digest,
            dry_run_session_knowledge_migration,
            reconcile_session_knowledge_migration,
        )

        store = FakeMigrationStore(
            [
                _summary(
                    70,
                    "Observed: reconcile.\nKey Decisions:\n- Prove lineage.\n"
                    "What was learned:\n- Reviews stay auditable.",
                )
            ]
        )
        store.review_rows = [
            {"id": 9, "review_key": "session-learning:70", "decision": "accept"}
        ]
        report = await dry_run_session_knowledge_migration(
            store, provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
        )
        digest = compute_report_digest(report)
        report["evidence_digest"] = digest
        evidence = {
            "decision": "ALLOW",
            "operation_id": report["proposed_operation_id"],
            "dry_run_report_digest": digest,
            "cohort_digest": report["cohort_digest"],
            "cohort_watermark": report["cohort_watermark"],
            "batch_scope": {"limit": 50, "after_id": 0},
            "backup_restore_receipt": {"verified": True, "bundle_digest": "b1"},
            "retrieval_control_baseline": report["retrieval_control_baseline"],
            "unresolved_acknowledgement": True,
            "provider_metadata": PROVIDER_META,
        }
        applied = await apply_session_knowledge_migration_batch(
            store,
            dry_run_report=report,
            gate_evidence=evidence,
            embed_adapter=FakeEmbedAdapter(),
            rerank_adapter=FakeRerankAdapter(),
            reconcile_adapter=FakeReconcileAdapter(),
            provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
            retrieval_controls={
                "lexical": {"query": "r", "baseline": 0.5, "score": 0.9},
                "vector": {"query": "r", "baseline": 0.5, "score": 0.9},
                "rerank": {"query": "r", "baseline": 0.5, "score": 0.9},
            },
        )
        recon = await reconcile_session_knowledge_migration(
            store, operation_id=applied["operation_id"], dry_run_report=report
        )
        assert recon["lineage_closed"] is True
        assert recon["review_ledger"]["count"] == 1
        assert (
            recon["review_ledger"]["digest"] == report["review_ledger_before"]["digest"]
        )
        assert recon["rollback_ready"] is True
        assert recon["embedding_coverage"]["missing"] == 0
        assert "catch_up_delta" in recon


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCliJsonSurfaces:
    def test_cli_exposes_migration_subcommands(self) -> None:
        from open_brain.cli.main import _build_parser

        parser = _build_parser()
        # dry-run
        args = parser.parse_args(
            ["--json", "session-knowledge-migration", "dry-run"]
        )
        assert args.command == "session-knowledge-migration"
        assert args.skm_command == "dry-run"
        # apply without gate file still parses; runtime blocks
        args2 = parser.parse_args(
            ["--json", "session-knowledge-migration", "apply", "--apply"]
        )
        assert args2.skm_command == "apply"
        assert args2.apply is True
        args3 = parser.parse_args(
            ["--json", "session-knowledge-migration", "status", "--operation-id", "x"]
        )
        assert args3.skm_command == "status"
        args4 = parser.parse_args(
            [
                "--json",
                "session-knowledge-migration",
                "reconcile",
                "--operation-id",
                "x",
            ]
        )
        assert args4.skm_command == "reconcile"

    @pytest.mark.asyncio
    async def test_cli_apply_without_gate_returns_blocked_json(self) -> None:
        from open_brain.cli import main as cli_main

        store = FakeMigrationStore([_learning(80, "CLI blocked apply.")])
        with (
            patch.object(cli_main, "PostgresDataLayer", return_value=store),
            patch(
                "open_brain.session_knowledge_migration.build_postgres_migration_store",
                new=AsyncMock(return_value=store),
            ),
        ):
            ns = argparse.Namespace(
                command="session-knowledge-migration",
                skm_command="apply",
                apply=True,
                operation_id=None,
                gate_evidence_file=None,
                limit=10,
                after_id=0,
                json_output=True,
                pretty=False,
            )
            # Direct command helper if present
            if hasattr(cli_main, "_cmd_session_knowledge_migration"):
                result = await cli_main._cmd_session_knowledge_migration(ns)
                assert result["status"] == "blocked"
                assert result.get("writes", 0) == 0


# ---------------------------------------------------------------------------
# Receipt safety
# ---------------------------------------------------------------------------


class TestReceiptSafety:
    @pytest.mark.asyncio
    async def test_receipts_omit_source_contents_and_secrets(self) -> None:
        from open_brain.session_knowledge_migration import (
            dry_run_session_knowledge_migration,
        )

        store = FakeMigrationStore(
            [
                _summary(
                    90,
                    "Observed: secret sk-ant-api03-NOTREAL.\n"
                    "Key Decisions:\n- Redact receipts.\n"
                    "What was learned:\n- Never echo source bodies.",
                )
            ]
        )
        report = await dry_run_session_knowledge_migration(
            store, provider_metadata=PROVIDER_META,
            control_adapter=FakeControlAdapter(),
        )
        encoded = json.dumps(report)
        assert "sk-ant-api03-NOTREAL" not in encoded
        assert "Never echo source bodies" not in encoded
        assert "Observed: secret" not in encoded


# ---------------------------------------------------------------------------
# Integration (disposable Postgres; root provisions DB)
# ---------------------------------------------------------------------------


def test_hybrid_search_sql_excludes_archived_and_superseded() -> None:
    """K1-03: hybrid_search/browse exclude migration-superseded + default archived."""
    from pathlib import Path

    runtime = Path("src/open_brain/data_layer/postgres.py").read_text(encoding="utf-8")
    bootstrap = Path("../scripts/bootstrap_test_schema.sql").read_text(encoding="utf-8")
    marker = "session_knowledge_migration,superseded_by_operation"
    # Capture-inbox may include archived lifecycle; normal retrieval may not.
    assert marker in runtime and "p_capture_status IS NOT NULL" in runtime
    assert "m.metadata->>'status' <> 'archived'" in runtime
    assert marker in bootstrap and "p_capture_status IS NOT NULL" in bootstrap


@pytest.mark.asyncio
async def test_browse_sql_excludes_archived_unless_capture_status() -> None:
    """K1-03: browse path excludes archived by default; capture inbox keeps them."""
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock, patch

    from open_brain.data_layer.interface import SearchParams
    from open_brain.data_layer.postgres import PostgresDataLayer

    dl = PostgresDataLayer()
    conn = AsyncMock()
    conn.fetch.return_value = []
    conn.fetchrow.return_value = {"total": 0}

    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = fake_acquire

    with (
        patch("open_brain.data_layer.postgres.get_pool", new=AsyncMock(return_value=pool)),
        patch.object(dl, "_resolve_index_id", new=AsyncMock(return_value=1)),
        patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
    ):
        mock_asyncio.create_task = MagicMock()
        await dl.search(SearchParams(limit=5))
        browse_sql = conn.fetch.call_args[0][0]
        assert "superseded_by_operation" in browse_sql
        assert "archived" in browse_sql

        await dl.search(SearchParams(capture_status="inbox", limit=5))
        inbox_sql = conn.fetch.call_args[0][0]
        assert "superseded_by_operation" in inbox_sql
        # capture inbox must not add the default archived lifecycle exclusion
        assert "m.metadata->>'status' <> 'archived'" not in inbox_sql


@pytest.mark.integration
class TestMigrationSchemaIntegration:
    """K1-06: deep disposable-Postgres coverage. Root runs this class twice."""

    @pytest.mark.asyncio
    async def test_full_apply_embeddings_resume_replay_exclusion(
        self, integration_database_url: str
    ) -> None:
        import asyncio
        import asyncpg

        from open_brain.data_layer.postgres import close_pool, get_pool
        from open_brain.session_knowledge_migration import (
            PostgresMigrationStore,
            apply_session_knowledge_migration_batch,
            dry_run_session_knowledge_migration,
            reconcile_session_knowledge_migration,
        )

        with patch.dict(
            "os.environ", {"DATABASE_URL": integration_database_url}, clear=False
        ):
            try:
                await close_pool()
            except Exception:
                pass
            pool = await get_pool()
            store = PostgresMigrationStore(pool)
            assert await store.schema_ready() is True

            async with pool.acquire() as conn:
                op_type = await conn.fetchval(
                    """
                    SELECT data_type FROM information_schema.columns
                    WHERE table_name='session_knowledge_migration_operations'
                      AND column_name='parameters'
                    """
                )
                map_type = await conn.fetchval(
                    """
                    SELECT data_type FROM information_schema.columns
                    WHERE table_name='session_knowledge_migration_mappings'
                      AND column_name='output_ids'
                    """
                )
                assert op_type == "jsonb" and map_type == "jsonb"
                src_id = await conn.fetchval(
                    """
                    INSERT INTO memories (index_id, type, title, content, metadata)
                    VALUES (
                      1, 'learning', 'integ learning',
                      'Prefer reversible supersession for cutovers.',
                      '{"project":"open-brain","provenance":{"origin":{"producer":"legacy","source_ref":"l:integ"}}}'::jsonb
                    )
                    RETURNING id
                    """
                )
                src_id = int(src_id)
                structured_id = await conn.fetchval(
                    """
                    INSERT INTO memories (index_id, type, title, content, metadata)
                    VALUES (
                      1, 'learning', 'live structured learning',
                      'Already captured through the live EKN boundary.',
                      $1::jsonb
                    )
                    RETURNING id
                    """,
                    {
                        "session_knowledge_capture_identity": "live-capture:integration",
                        "session_knowledge": {
                            "schema_version": "session-knowledge-capture.v1",
                            "role": "session_learning",
                        },
                        "memory_write_judge": {"decision": "ALLOW"},
                    },
                )
                structured_id = int(structured_id)

            class HashEmbed:
                async def embed_documents(self, texts: list[str]):
                    vecs = []
                    for t in texts:
                        h = hashlib.sha256(t.encode()).digest()
                        vecs.append([(h[i % len(h)] / 255.0) for i in range(8)])
                    # pad/truncate to 1024 for pgvector column
                    vecs = [
                        (v + [0.0] * 1024)[:1024] for v in vecs
                    ]
                    return vecs, {"documents": len(texts), "tokens": 1}

            class Ctrl:
                instrument = "fake-postgres-controls.v1"

                async def measure(self, *, control: str, query: str, documents: list[str]):
                    return 0.95

            class Rerank:
                async def rerank(self, query: str, documents: list[str]):
                    return list(range(len(documents))), {"model": "t"}

            class Rec:
                async def reconcile(self, memory_ids: list[int]):
                    return {"scoped_ids": list(memory_ids)}

            report = await dry_run_session_knowledge_migration(
                store,
                provider_metadata={
                    "embedding_model": "configured",
                    "rerank_model": "configured",
                    "embedding_dimension": 1024,
                    "cost_per_1k_tokens": 0.0,
                    "chars_per_token": 4.0,
                },
                control_adapter=Ctrl(),
            )
            assert int(report["already_structured_count"]) >= 1
            assert structured_id not in {
                int(plan["source_id"]) for plan in report["plans"]
            }
            from open_brain.session_knowledge_migration import compute_report_digest

            digest = compute_report_digest(report)
            report["evidence_digest"] = digest
            evidence = {
                "decision": "ALLOW",
                "operation_id": report["proposed_operation_id"],
                "dry_run_report_digest": digest,
                "cohort_digest": report["cohort_digest"],
                "cohort_watermark": report["cohort_watermark"],
                "batch_scope": {"limit": 50, "after_id": 0},
                "backup_restore_receipt": {"verified": True, "bundle_digest": "b"},
                "retrieval_control_baseline": report["retrieval_control_baseline"],
                "unresolved_acknowledgement": True,
                "provider_metadata": {
                    "embedding_model": "configured",
                    "rerank_model": "configured",
                    "embedding_dimension": 1024,
                },
            }

            # Post-dry-run catch-up legacy row must not enter the approved operation.
            # Unique per invocation so twice-on-same-DB runs remain independent.
            catch_up_token = hashlib.sha256(
                f"{src_id}:{report['evidence_digest']}".encode()
            ).hexdigest()[:12]
            async with pool.acquire() as conn:
                catch_up_id = await conn.fetchval(
                    """
                    INSERT INTO memories (index_id, type, title, content, metadata)
                    VALUES (
                      1, 'learning', $1,
                      $2,
                      $3::jsonb
                    )
                    RETURNING id
                    """,
                    f"catch-up learning {catch_up_token}",
                    f"Arrived after dry-run approval watermark ({catch_up_token}).",
                    {
                        "project": "open-brain",
                        "provenance": {
                            "origin": {
                                "producer": "legacy",
                                "source_ref": f"l:catchup:{catch_up_token}",
                            }
                        },
                    },
                )
                catch_up_id = int(catch_up_id)

            # Fault after derived_ready: failing verification leaves mapping resumable.
            class FailCtrl:
                instrument = "fake-postgres-controls.v1"

                async def measure(self, *, control: str, query: str, documents: list[str]):
                    return 0.1

            failed = await apply_session_knowledge_migration_batch(
                store,
                dry_run_report=report,
                gate_evidence=evidence,
                embed_adapter=HashEmbed(),
                rerank_adapter=Rerank(),
                reconcile_adapter=Rec(),
                provider_metadata=evidence["provider_metadata"],
                control_adapter=FailCtrl(),
            )
            assert failed["status"] == "failed"
            failed_mapping = await store.get_mapping(failed["operation_id"], src_id)
            assert failed_mapping is not None
            assert failed_mapping["status"] == "derived_ready"
            current_source_outputs = [int(x) for x in failed_mapping["output_ids"]]
            assert current_source_outputs

            # Twice-on-same-DB: prior-run catch-up may already be in this approved cohort.
            # Resume must reuse the union of every derived_ready mapping, not only src_id.
            failed_mappings = await store.list_mappings(failed["operation_id"])
            derived_ready = [
                m for m in failed_mappings if m.get("status") == "derived_ready"
            ]
            assert derived_ready
            failed_output_union = sorted(
                {
                    int(oid)
                    for mapping in derived_ready
                    for oid in (mapping.get("output_ids") or [])
                }
            )
            approved_failed_sources = sorted(
                {int(mapping["source_id"]) for mapping in derived_ready}
            )
            assert src_id in approved_failed_sources
            assert catch_up_id not in approved_failed_sources

            # Count typed edges / identities for the current source before resume.
            async with pool.acquire() as conn:
                rel_count_before = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM memory_relationships
                     WHERE target_id = $1
                       AND source_id = ANY($2::int[])
                    """,
                    src_id,
                    current_source_outputs,
                )
                identities_before = await conn.fetch(
                    """
                    SELECT id, metadata->>'session_knowledge_record_identity' AS rid
                      FROM memories
                     WHERE id = ANY($1::int[])
                     ORDER BY id
                    """,
                    current_source_outputs,
                )

            first = await apply_session_knowledge_migration_batch(
                store,
                dry_run_report=report,
                gate_evidence=evidence,
                embed_adapter=HashEmbed(),
                rerank_adapter=Rerank(),
                reconcile_adapter=Rec(),
                provider_metadata=evidence["provider_metadata"],
                control_adapter=Ctrl(),
                operation_id=failed["operation_id"],
            )
            assert first["status"] == "completed"
            out_ids = sorted(int(x) for x in first["output_ids"])
            assert out_ids == failed_output_union
            assert sorted(int(x) for x in first["source_ids"]) == approved_failed_sources
            assert catch_up_id not in first["source_ids"]
            assert int((first.get("catch_up_delta") or {}).get("remaining_count") or 0) >= 1

            resumed_current = await store.get_mapping(first["operation_id"], src_id)
            assert resumed_current is not None
            assert resumed_current["status"] == "completed"
            assert [int(x) for x in resumed_current["output_ids"]] == current_source_outputs

            async with pool.acquire() as conn:
                rel_count_after = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM memory_relationships
                     WHERE target_id = $1
                       AND source_id = ANY($2::int[])
                    """,
                    src_id,
                    current_source_outputs,
                )
                assert int(rel_count_after) == int(rel_count_before)
                identities_after = await conn.fetch(
                    """
                    SELECT id, metadata->>'session_knowledge_record_identity' AS rid
                      FROM memories
                     WHERE id = ANY($1::int[])
                     ORDER BY id
                    """,
                    current_source_outputs,
                )
                assert [
                    (int(r["id"]), r["rid"]) for r in identities_after
                ] == [
                    (int(r["id"]), r["rid"]) for r in identities_before
                ]
                # No duplicate record identities among this operation's outputs.
                identity_rows = await conn.fetch(
                    """
                    SELECT metadata->>'session_knowledge_record_identity' AS rid, COUNT(*)::int AS n
                      FROM memories
                     WHERE id = ANY($1::int[])
                       AND metadata->>'session_knowledge_record_identity' IS NOT NULL
                     GROUP BY 1
                    """,
                    out_ids,
                )
                assert identity_rows and all(int(r["n"]) == 1 for r in identity_rows)

            for oid in out_ids:
                mem = await store.get_memory(oid)
                assert mem is not None and mem.embedding is not None
                assert isinstance(mem.metadata, dict)
                assert mem.project == "open-brain"

            # Derived learning outputs must never re-enter the eligible cohort.
            cohort_ids = {int(getattr(m, "id")) for m in await store.list_eligible_cohort()}
            assert src_id not in cohort_ids  # archived source
            assert all(oid not in cohort_ids for oid in out_ids)
            assert catch_up_id in cohort_ids
            assert structured_id not in cohort_ids
            structured = await store.get_memory(structured_id)
            assert structured is not None
            assert structured.metadata.get("status") != "archived"

            recon = await reconcile_session_knowledge_migration(
                store, operation_id=first["operation_id"]
            )
            assert int((recon.get("catch_up_delta") or {}).get("remaining_count") or 0) >= 1

            # JSONB values must be native objects/arrays (not double-encoded strings).
            # Bind per-source checks to this invocation's mapping outputs — on a
            # second populated-DB run out_ids[0] may belong to an earlier source.
            current_output_id = current_source_outputs[0]
            async with pool.acquire() as conn:
                meta_type = await conn.fetchval(
                    "SELECT jsonb_typeof(metadata) FROM memories WHERE id=$1",
                    current_output_id,
                )
                assert meta_type == "object"
                rel_type = await conn.fetchval(
                    """
                    SELECT jsonb_typeof(metadata) FROM memory_relationships
                     WHERE source_id = $1 AND target_id = $2
                     LIMIT 1
                    """,
                    current_output_id,
                    src_id,
                )
                assert rel_type == "object"
                op_types = await conn.fetchrow(
                    """
                    SELECT jsonb_typeof(parameters) AS parameters_t,
                           jsonb_typeof(counters) AS counters_t,
                           jsonb_typeof(provider_metadata) AS provider_t
                      FROM session_knowledge_migration_operations
                     WHERE operation_id = $1::uuid
                    """,
                    first["operation_id"],
                )
                assert op_types["parameters_t"] == "object"
                assert op_types["counters_t"] == "object"
                assert op_types["provider_t"] == "object"
                map_types = await conn.fetchrow(
                    """
                    SELECT jsonb_typeof(output_ids) AS output_ids_t,
                           jsonb_typeof(routes) AS routes_t
                      FROM session_knowledge_migration_mappings
                     WHERE operation_id = $1::uuid AND source_id = $2
                    """,
                    first["operation_id"],
                    src_id,
                )
                assert map_types["output_ids_t"] == "array"
                assert map_types["routes_t"] == "array"
                # Prior failure history preserved across resume.
                prior_errors = await conn.fetchval(
                    """
                    SELECT parameters->'prior_errors'
                      FROM session_knowledge_migration_operations
                     WHERE operation_id = $1::uuid
                    """,
                    first["operation_id"],
                )
                assert prior_errors and len(prior_errors) >= 1

                status = await conn.fetchval(
                    "SELECT metadata->>'status' FROM memories WHERE id=$1", src_id
                )
                assert status == "archived"
                # Archived src must not appear even when querying via its own derived vector.
                hit = await conn.fetchval(
                    """
                    SELECT id FROM hybrid_search(
                      'reversible supersession',
                      (SELECT embedding FROM memories WHERE id = $1),
                      20, 60, NULL, NULL, NULL, NULL
                    ) WHERE id = $2
                    """,
                    current_output_id,
                    src_id,
                )
                assert hit is None

            # Replay same op
            second = await apply_session_knowledge_migration_batch(
                store,
                dry_run_report=report,
                gate_evidence=evidence,
                embed_adapter=HashEmbed(),
                rerank_adapter=Rerank(),
                reconcile_adapter=Rec(),
                provider_metadata=evidence["provider_metadata"],
                control_adapter=Ctrl(),
                operation_id=first["operation_id"],
            )
            assert second["status"] == "replayed"

            # Mutated payload conflict
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE memories SET content = content || ' mutated' WHERE id=$1",
                    src_id,
                )
            conflicted = await apply_session_knowledge_migration_batch(
                store,
                dry_run_report=report,
                gate_evidence=evidence,
                embed_adapter=HashEmbed(),
                rerank_adapter=Rerank(),
                reconcile_adapter=Rec(),
                provider_metadata=evidence["provider_metadata"],
                control_adapter=Ctrl(),
                operation_id=first["operation_id"],
            )
            assert conflicted["status"] == "conflict"

            recon = await reconcile_session_knowledge_migration(
                store, operation_id=first["operation_id"]
            )
            assert recon.get("baseline_missing") is False
            assert recon["embedding_coverage"]["missing"] == 0

            # Concurrent attempts: both resume same op id → one replay/conflict, no dup
            async def _attempt():
                return await apply_session_knowledge_migration_batch(
                    store,
                    dry_run_report=report,
                    gate_evidence=evidence,
                    embed_adapter=HashEmbed(),
                    rerank_adapter=Rerank(),
                    reconcile_adapter=Rec(),
                    provider_metadata=evidence["provider_metadata"],
                    control_adapter=Ctrl(),
                    operation_id=first["operation_id"],
                )

            results = await asyncio.gather(_attempt(), _attempt())
            assert all(r["status"] in {"replayed", "conflict"} for r in results)

            # twice-rerunnable operation insert
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO session_knowledge_migration_operations (
                      operation_id, status, parameters, evidence_digest, counters
                    ) VALUES (
                      '11111111-1111-1111-1111-111111111111'::uuid,
                      'completed', '{}'::jsonb, 'd', '{}'::jsonb
                    )
                    ON CONFLICT (operation_id) DO NOTHING
                    """
                )
                await conn.execute(
                    """
                    INSERT INTO session_knowledge_migration_operations (
                      operation_id, status, parameters, evidence_digest, counters
                    ) VALUES (
                      '11111111-1111-1111-1111-111111111111'::uuid,
                      'completed', '{}'::jsonb, 'd', '{}'::jsonb
                    )
                    ON CONFLICT (operation_id) DO NOTHING
                    """
                )
                # K1-04: readonly store must not mutate schema/state
                before_ops = await conn.fetchval(
                    "SELECT COUNT(*) FROM session_knowledge_migration_operations"
                )
                before_maps = await conn.fetchval(
                    "SELECT COUNT(*) FROM session_knowledge_migration_mappings"
                )
            from open_brain.session_knowledge_migration import (
                build_postgres_migration_store,
                dry_run_session_knowledge_migration,
            )

            readonly = await build_postgres_migration_store(readonly=True)
            await dry_run_session_knowledge_migration(
                readonly,
                provider_metadata={
                    "embedding_model": "configured",
                    "rerank_model": "configured",
                    "embedding_dimension": 1024,
                    "cost_per_1k_tokens": 0.0,
                    "chars_per_token": 4.0,
                },
                control_adapter=Ctrl(),
            )
            async with pool.acquire() as conn:
                after_ops = await conn.fetchval(
                    "SELECT COUNT(*) FROM session_knowledge_migration_operations"
                )
                after_maps = await conn.fetchval(
                    "SELECT COUNT(*) FROM session_knowledge_migration_mappings"
                )
                assert int(after_ops) == int(before_ops)
                assert int(after_maps) == int(before_maps)
            await close_pool()

        conn = await asyncpg.connect(integration_database_url)
        try:
            exists = await conn.fetchval(
                """
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_name IN (
                  'session_knowledge_migration_operations',
                  'session_knowledge_migration_mappings'
                )
                """
            )
            assert int(exists) == 2
            # No hard deletes of source
            still = await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE id=$1", src_id
            )
            assert int(still) == 1
        finally:
            await conn.close()


class TestConfiguredRerankAdapterBatching:
    """Provider rerank calls must respect the per-request document cap (K3)."""

    async def test_full_cohort_rerank_is_chunked_with_global_indices(self) -> None:
        from open_brain import session_knowledge_migration as skm
        from open_brain.data_layer.reranker import RerankResult

        calls: list[int] = []

        async def fake_rerank(
            query: str, documents: list[str], model: str, top_k: int | None = None
        ) -> list[RerankResult]:
            calls.append(len(documents))
            # highest score for the last document of each chunk
            return [
                RerankResult(index=i, relevance_score=float(i) / len(documents))
                for i in range(len(documents))
            ]

        adapter = skm.ConfiguredRerankAdapter("rerank-2.5")
        documents = [f"doc-{i}" for i in range(2350)]
        with patch("open_brain.data_layer.reranker.rerank", new=fake_rerank):
            indices, metrics = await adapter.rerank("query", documents)

        assert calls == [1000, 1000, 350]
        assert all(size <= skm.RERANK_MAX_BATCH_DOCUMENTS for size in calls)
        assert len(indices) == 2350
        assert sorted(indices) == list(range(2350))
        # best global index of each chunk ranks first (score 999/1000)
        assert indices[0] in {999, 1999}
        assert metrics["documents"] == 2350
        assert metrics["max_relevance"] == pytest.approx(999.0 / 1000.0)

    async def test_empty_documents_need_no_provider_call(self) -> None:
        from open_brain import session_knowledge_migration as skm

        async def fail_rerank(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("provider must not be called for empty input")

        adapter = skm.ConfiguredRerankAdapter("rerank-2.5")
        with patch("open_brain.data_layer.reranker.rerank", new=fail_rerank):
            indices, metrics = await adapter.rerank("query", [])
        assert indices == []
        assert metrics["max_relevance"] == 0.0
