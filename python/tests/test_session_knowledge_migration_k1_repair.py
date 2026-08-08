"""Kimi round-1 repair matrix for open-brain-ekn.8 session-knowledge migration (K1-01..K1-14)."""

from __future__ import annotations

import hashlib
import json
import os
import struct
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.session_knowledge import DERIVED_FROM_LINK_TYPE, MAX_WHAT_HAPPENED_CHARS

PROVIDER_META = {
    "embedding_model": "configured-embed-model",
    "rerank_model": "configured-rerank-model",
    "embedding_dimension": 8,
    "cost_per_1k_tokens": 0.02,
    "chars_per_token": 4.0,
}
JUDGE_POLICY = "legacy-session-knowledge-migration-judge.v1"


@dataclass
class FakeMemory:
    id: int
    type: str
    content: str
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None


@dataclass
class FakeWrite:
    kind: str
    payload: dict[str, Any]


def _vec(text: str, dim: int = 8) -> list[float]:
    d = hashlib.sha256(text.encode()).digest()
    out: list[float] = []
    for i in range(dim):
        chunk = d[i * 4 : i * 4 + 4].ljust(4, b"\0")
        out.append(struct.unpack("!I", chunk)[0] % 10_000 / 10_000.0)
    return out


class FakeEmbed:
    def __init__(self, *, dim: int = 8, fail_on: int | None = None) -> None:
        self.dim, self.fail_on, self.n = dim, fail_on, 0
        self.calls: list[list[str]] = []

    async def embed_documents(
        self, texts: list[str]
    ) -> tuple[list[list[float]], dict[str, Any]]:
        self.n += 1
        if self.fail_on is not None and self.n == self.fail_on:
            raise RuntimeError("embed adapter injected failure")
        self.calls.append(list(texts))
        return [_vec(t, self.dim) for t in texts], {
            "documents": len(texts),
            "tokens": sum(max(1, len(t) // 4) for t in texts),
            "model": "fake-hash-embed",
            "dimension": self.dim,
        }


class DeterministicControlAdapter:
    instrument = "fake-controls.v1"

    def __init__(self, scores: dict[str, float] | None = None) -> None:
        self.scores = dict(scores or {})
        self.measure_calls: list[dict[str, Any]] = []

    async def measure(self, *, control: str, query: str, documents: list[str]) -> float:
        self.measure_calls.append({"control": control, "query": query, "documents": documents})
        if control in self.scores:
            return self.scores[control]
        h = int(hashlib.sha256(f"{control}:{query}:{'|'.join(documents)}".encode()).hexdigest()[:8], 16)
        return 0.5 + (h % 500) / 1000.0


class FakeMigrationStore:
    def __init__(self, memories: list[FakeMemory] | None = None) -> None:
        self.memories = {m.id: m for m in (memories or [])}
        self.relationships: list[dict[str, Any]] = []
        self.writes: list[FakeWrite] = []
        self.operations: dict[str, dict[str, Any]] = {}
        self.mappings: dict[tuple[str, int], dict[str, Any]] = {}
        self.review_rows: list[dict[str, Any]] = []
        self.promotion_grants: set[int] = set()
        self.next_id = max(self.memories, default=0) + 1
        self.archive_log: list[int] = []

    async def list_eligible_cohort(self) -> list[FakeMemory]:
        from open_brain.session_knowledge_migration import (
            is_migration_derived_metadata,
            is_structured_session_knowledge_metadata,
        )

        return [
            m for m in sorted(self.memories.values(), key=lambda x: x.id)
            if m.type in {"session_summary", "learning"}
            and (m.metadata or {}).get("status") != "archived"
            and (m.metadata or {}).get("session_knowledge_migration", {}).get("superseded_by_operation") is None
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
        d = hashlib.sha256(json.dumps(self.review_rows, sort_keys=True, default=str).encode()).hexdigest()
        return {"count": len(self.review_rows), "digest": d}

    async def has_accepted_promotion_grant(self, memory_id: int) -> bool:
        return memory_id in self.promotion_grants

    async def save_derived(
        self, *, memory_type: str, content: str, metadata: dict[str, Any],
        title: str = "", embedding: list[float] | None = None,
    ) -> int:
        mid = self.next_id
        self.next_id += 1
        self.memories[mid] = FakeMemory(mid, memory_type, content, title, metadata, embedding)
        self.writes.append(FakeWrite("save_derived", {"id": mid, "embedding": embedding, "content": content}))
        return mid

    async def create_relationship(
        self, source_id: int, target_id: int, link_type: str, metadata: dict[str, Any] | None = None,
    ) -> int:
        rid = len(self.relationships) + 1
        self.relationships.append({"id": rid, "source_id": source_id, "target_id": target_id,
                                   "link_type": link_type, "metadata": metadata or {}})
        return rid

    async def archive_legacy(self, memory_id: int, *, operation_id: str, rollback: dict[str, Any]) -> None:
        mem = self.memories[memory_id]
        meta = dict(mem.metadata or {})
        meta["status"] = "archived"
        meta["session_knowledge_migration"] = {
            "superseded_by_operation": operation_id, "rollback": rollback,
        }
        mem.metadata = meta
        self.archive_log.append(memory_id)

    async def upsert_operation(self, record: dict[str, Any]) -> None:
        self.operations[record["operation_id"]] = dict(record)

    async def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        row = self.operations.get(operation_id)
        return dict(row) if row else None

    async def upsert_mapping(self, record: dict[str, Any]) -> None:
        self.mappings[(record["operation_id"], int(record["source_id"]))] = dict(record)

    async def get_mapping(self, operation_id: str, source_id: int) -> dict[str, Any] | None:
        row = self.mappings.get((operation_id, source_id))
        return dict(row) if row else None

    async def list_mappings(self, operation_id: str) -> list[dict[str, Any]]:
        return [dict(v) for (op, _), v in sorted(self.mappings.items()) if op == operation_id]

    async def list_mappings_for_source(self, source_id: int) -> list[dict[str, Any]]:
        return [
            dict(v)
            for (_, sid), v in sorted(self.mappings.items())
            if sid == source_id
        ]

    async def update_embedding(self, memory_id: int, embedding: list[float]) -> None:
        mem = self.memories[memory_id]
        mem.embedding = list(embedding)

    async def schema_ready(self) -> bool:
        return True


class FakeRerankAdapter:
    async def rerank(self, query: str, documents: list[str]) -> tuple[list[int], dict[str, Any]]:
        return list(range(len(documents))), {"model": "fake-rerank"}


class FakeReconcileAdapter:
    async def reconcile(self, memory_ids: list[int]) -> dict[str, Any]:
        return {"scoped_ids": list(memory_ids)}


def _summary(mid: int, content: str, **kw: Any) -> FakeMemory:
    md = {"project": "open-brain", "provenance": {"origin": {"producer": "sc", "source_ref": f"s:{mid}"}}}
    md.update(kw)
    return FakeMemory(mid, "session_summary", content, metadata=md)


def _learning(mid: int, content: str, **kw: Any) -> FakeMemory:
    prov: dict[str, Any] = {"origin": {"producer": "legacy", "source_ref": f"l:{mid}"}}
    if "source_label" in kw:
        prov["source_label"] = kw.pop("source_label")
    if "expected_use" in kw:
        prov["expected_use"] = kw.pop("expected_use")
    return FakeMemory(mid, "learning", content, metadata={"provenance": prov, **kw})


def _evidence(report: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    from open_brain.session_knowledge_migration import compute_report_digest
    digest = compute_report_digest(report)
    report["evidence_digest"] = digest
    base = {
        "decision": "ALLOW", "operation_id": report["proposed_operation_id"],
        "dry_run_report_digest": digest, "cohort_digest": report["cohort_digest"],
        "cohort_watermark": report["cohort_watermark"],
        "batch_scope": {"limit": 50, "after_id": 0},
        "backup_restore_receipt": {"verified": True},
        "retrieval_control_baseline": report.get(
            "retrieval_control_baseline", {"lexical": 0.8, "vector": 0.8, "rerank": 0.8},
        ),
        "unresolved_acknowledgement": True, "provider_metadata": PROVIDER_META,
    }
    base.update(overrides)
    return base


async def _dry(store: FakeMigrationStore, control: DeterministicControlAdapter | None = None) -> dict[str, Any]:
    from open_brain.session_knowledge_migration import dry_run_session_knowledge_migration
    kw: dict[str, Any] = {
        "provider_metadata": PROVIDER_META,
        "control_adapter": control or DeterministicControlAdapter(),
    }
    return await dry_run_session_knowledge_migration(store, **kw)


async def _apply(
    store: FakeMigrationStore, report: dict[str, Any], *,
    embed: FakeEmbed | None = None, control: DeterministicControlAdapter | None = None,
    evidence: dict[str, Any] | None = None, **kw: Any,
) -> dict[str, Any]:
    from open_brain.session_knowledge_migration import apply_session_knowledge_migration_batch
    ctrl = control or DeterministicControlAdapter({"lexical": 0.95, "vector": 0.95, "rerank": 0.95})
    return await apply_session_knowledge_migration_batch(
        store, dry_run_report=report, gate_evidence=evidence or _evidence(report),
        embed_adapter=embed or FakeEmbed(), rerank_adapter=FakeRerankAdapter(),
        reconcile_adapter=FakeReconcileAdapter(), provider_metadata=PROVIDER_META,
        control_adapter=ctrl, **kw,
    )


# K1-01
class TestK101Embeddings:
    @pytest.mark.asyncio
    async def test_save_derived_embedding_persisted(self) -> None:
        store = FakeMigrationStore([_learning(101, "embed on derived")])
        r = await _apply(store, await _dry(store))
        assert r["status"] == "completed"
        derived = [m for m in store.memories.values() if m.id != 101]
        assert derived and all(m.embedding is not None for m in derived)

    @pytest.mark.asyncio
    async def test_final_embed_uses_content_not_relink(self) -> None:
        store = FakeMigrationStore([_summary(102, "Observed: x\nKey Decisions:\n- y")])
        embed = FakeEmbed()
        await _apply(store, await _dry(store), embed=embed)
        assert len(embed.calls) >= 2 and not any(t.startswith("relink:") for t in embed.calls[-1])

    @pytest.mark.asyncio
    async def test_reconcile_missing_embeddings_not_rollback_ready(self) -> None:
        from open_brain.session_knowledge_migration import reconcile_session_knowledge_migration
        store = FakeMigrationStore([_learning(103, "missing embed")])
        applied = await _apply(store, await _dry(store))
        for oid in applied["output_ids"]:
            store.memories[oid].embedding = None
        recon = await reconcile_session_knowledge_migration(
            store, operation_id=applied["operation_id"], dry_run_report=await _dry(store),
        )
        assert recon["rollback_ready"] is False and recon["embedding_coverage"]["missing"] >= 1


# K1-02
class TestK102RetrievalControls:
    def test_measure_retrieval_controls_exists(self) -> None:
        from open_brain.session_knowledge_migration import measure_retrieval_controls
        assert callable(measure_retrieval_controls)

    @pytest.mark.asyncio
    async def test_dry_run_stores_measured_baseline(self) -> None:
        from open_brain.session_knowledge_migration import measure_retrieval_controls
        store = FakeMigrationStore([_summary(201, "Observed: m\nKey Decisions:\n- d")])
        ctrl = DeterministicControlAdapter({"lexical": 0.91, "vector": 0.92, "rerank": 0.93})
        report = await _dry(store, ctrl)
        expected = await measure_retrieval_controls(store, plans=report["plans"], control_adapter=ctrl)
        assert report["retrieval_control_baseline"] == expected

    @pytest.mark.asyncio
    async def test_apply_ignores_caller_scores_fail_closed(self) -> None:
        store = FakeMigrationStore([_learning(202, "regression")])
        ctrl = DeterministicControlAdapter({"lexical": 0.95, "vector": 0.95, "rerank": 0.95})
        report = await _dry(store, ctrl)
        ctrl.scores.update({"lexical": 0.1, "vector": 0.1, "rerank": 0.1})
        r = await _apply(store, report, control=ctrl, retrieval_controls={
            "lexical": {"baseline": 0.8, "score": 0.99}, "vector": {"baseline": 0.8, "score": 0.99},
            "rerank": {"baseline": 0.8, "score": 0.99},
        })
        assert r["status"] == "failed" and any(e["code"] == "retrieval_control_failed" for e in r["errors"])


# K1-03 / K1-07
class TestK103K107FailureTerminal:
    def test_retrieval_contract_excludes_archived_superseded(self) -> None:
        from types import SimpleNamespace

        from open_brain.retrieval_contract import (
            apply_retrieval_contract,
            profile_retrieval_contract,
        )

        active = SimpleNamespace(
            id=1,
            content="active learning",
            title="a",
            type="learning",
            metadata={"provenance": {"origin": {"producer": "t", "source_ref": "t:1"}}},
        )
        archived = SimpleNamespace(
            id=2,
            content="archived source",
            title="b",
            type="learning",
            metadata={
                "status": "archived",
                "session_knowledge_migration": {"superseded_by_operation": "op"},
                "provenance": {"origin": {"producer": "t", "source_ref": "t:2"}},
            },
        )
        result = apply_retrieval_contract(
            [active, archived],
            contract=profile_retrieval_contract(
                "compatibility",
                work_object={"kind": "project", "id": "p"},
            ),
        )
        ids = {u.memory_id for u in result.units}
        assert 1 in ids and 2 not in ids

    @pytest.mark.asyncio
    async def test_verification_failure_leaves_source_unarchived(self) -> None:
        store = FakeMigrationStore([_learning(301, "verify fail")])
        baseline = DeterministicControlAdapter(
            {"lexical": 0.9, "vector": 0.9, "rerank": 0.9}
        )
        report = await _dry(store, baseline)
        failing = DeterministicControlAdapter(
            {"lexical": 0.2, "vector": 0.2, "rerank": 0.2}
        )
        r = await _apply(store, report, control=failing)
        assert r["status"] == "failed" and 301 not in store.archive_log

    @pytest.mark.asyncio
    async def test_embed_failure_unarchived_and_terminal_failed(self) -> None:
        from open_brain.session_knowledge_migration import migration_operation_status
        store = FakeMigrationStore([_learning(302, "embed fail")])
        r = await _apply(store, await _dry(store), embed=FakeEmbed(fail_on=1))
        st = await migration_operation_status(store, r["operation_id"])
        assert r["status"] == "failed" and st["status"] == "failed" and 302 not in store.archive_log


# K1-04
class TestK104Readonly:
    @pytest.mark.asyncio
    async def test_readonly_store_skips_migrations(self) -> None:
        from open_brain.session_knowledge_migration import build_postgres_migration_store

        pool = MagicMock()
        get_pool = AsyncMock(return_value=pool)
        with patch("open_brain.data_layer.postgres.get_pool", get_pool):
            store = await build_postgres_migration_store(readonly=True)
        assert store is not None
        assert get_pool.await_args.kwargs.get("run_migrations") is False

    def test_readonly_provider_metadata_without_voyage_key(self) -> None:
        from open_brain.session_knowledge_migration import configured_provider_metadata_from_config
        env = {k: v for k, v in os.environ.items() if k != "VOYAGE_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            meta = configured_provider_metadata_from_config(readonly=True)
        assert meta["embedding_model"] and int(meta["embedding_dimension"]) > 0


# K1-05
class TestK105Baseline:
    @pytest.mark.asyncio
    async def test_operation_stores_approved_baseline(self) -> None:
        store = FakeMigrationStore([_learning(501, "baseline")])
        ctrl = DeterministicControlAdapter({"lexical": 0.9, "vector": 0.9, "rerank": 0.9})
        report = await _dry(store, ctrl)
        ev = _evidence(report)
        applied = await _apply(store, report, control=ctrl, evidence=ev)
        op = await store.get_operation(applied["operation_id"])
        assert op and op.get("approved_baseline") == ev["retrieval_control_baseline"]

    @pytest.mark.asyncio
    async def test_reconcile_without_baseline_returns_baseline_missing(self) -> None:
        from open_brain.session_knowledge_migration import reconcile_session_knowledge_migration
        store = FakeMigrationStore([])
        op_id = "00000000-0000-4000-8000-000000000502"
        await store.upsert_operation({"operation_id": op_id, "status": "completed", "parameters": {},
            "evidence_digest": "x", "cursor": "0", "counters": {}, "provider_metadata": PROVIDER_META, "error": None})
        assert (await reconcile_session_knowledge_migration(
            store, operation_id=op_id, dry_run_report={"review_ledger_before": {"count": 0, "digest": ""}},
        )).get("baseline_missing") is True

    @pytest.mark.asyncio
    async def test_reconcile_uses_stored_not_fresh_baseline(self) -> None:
        from open_brain.session_knowledge_migration import reconcile_session_knowledge_migration
        store = FakeMigrationStore([])
        op_id = "00000000-0000-4000-8000-000000000503"
        stored = {"lexical": 0.88, "vector": 0.88, "rerank": 0.88}
        await store.upsert_operation({"operation_id": op_id, "status": "completed", "approved_baseline": stored,
            "parameters": {}, "evidence_digest": "x", "cursor": "0",
            "counters": {"output_ids": [9001]}, "provider_metadata": PROVIDER_META, "error": None})
        recon = await reconcile_session_knowledge_migration(store, operation_id=op_id, dry_run_report={
            "retrieval_control_baseline": {"lexical": 0.1, "vector": 0.1, "rerank": 0.1},
            "review_ledger_before": {"count": 0, "digest": ""},
        })
        assert recon.get("baseline_used") == stored

    async def test_reconcile_catch_up_and_preservation_deltas(self) -> None:
        from open_brain.session_knowledge_migration import (
            reconcile_session_knowledge_migration,
        )

        store = FakeMigrationStore([_learning(510, "baseline learning")])
        ctrl = DeterministicControlAdapter({"lexical": 0.9, "vector": 0.9, "rerank": 0.9})
        report = await _dry(store, ctrl)
        applied = await _apply(store, report, control=ctrl, evidence=_evidence(report))
        # Post-apply catch-up: new legacy + review rows arrive after gate baseline.
        # Use an id above derived allocations so we do not clobber outputs.
        catch_id = max(store.memories) + 10
        store.memories[catch_id] = _learning(catch_id, "post-apply legacy catch-up")
        store.review_rows.append(
            {"id": 1, "review_key": "rk-new", "decision": "keep", "created_at": "t"}
        )
        recon = await reconcile_session_knowledge_migration(
            store, operation_id=applied["operation_id"]
        )
        assert recon.get("baseline_missing") is False
        assert int((recon.get("catch_up_delta") or {}).get("remaining_count") or 0) >= 1
        assert int((recon.get("review_ledger") or {}).get("delta_count") or 0) >= 1
        assert recon["lineage_closed"] is True
        assert recon["embedding_coverage"]["missing"] == 0
        assert recon["rollback_ready"] is False  # review ledger not preserved


# K1-08
class TestK108Resume:
    @pytest.mark.asyncio
    async def test_resume_uses_stored_cursor_when_after_id_omitted(self) -> None:
        store = FakeMigrationStore([_learning(801, "done"), _learning(802, "pending")])
        report = await _dry(store)
        op_id = report["proposed_operation_id"]
        await store.upsert_operation({"operation_id": op_id, "status": "running", "cursor": "801",
            "parameters": {"batch_scope": {"limit": 50, "after_id": 801}}, "evidence_digest": report["evidence_digest"],
            "counters": {}, "provider_metadata": PROVIDER_META, "error": None})
        await store.upsert_mapping({"operation_id": op_id, "source_id": 801, "source_type": "learning",
            "source_content_hash": "done", "output_ids": [8801], "routes": ["inferred_learning"], "status": "completed"})
        ev = _evidence(report)
        ev["batch_scope"] = {"limit": 50}
        r = await _apply(store, report, evidence=ev, operation_id=op_id)
        assert 802 in r.get("source_ids", []) and 801 not in r.get("source_ids", [])

    @pytest.mark.asyncio
    async def test_evidence_digest_swap_rejected(self) -> None:
        store = FakeMigrationStore([_learning(803, "digest")])
        report = await _dry(store)
        op_id = report["proposed_operation_id"]
        await store.upsert_operation({"operation_id": op_id, "status": "running", "cursor": "0",
            "parameters": {"batch_scope": {"limit": 50, "after_id": 0}}, "evidence_digest": report["evidence_digest"],
            "counters": {}, "provider_metadata": PROVIDER_META, "error": None})
        ev = _evidence(report)
        ev["dry_run_report_digest"] = "deadbeef" * 8
        r = await _apply(store, report, evidence=ev, operation_id=op_id)
        assert r["status"] in {"conflict", "blocked"}
        codes = {e.get("code") for e in r.get("errors", [])}
        reasons = " ".join((r.get("gate") or {}).get("reasons") or [])
        assert (
            "evidence_digest_conflict" in codes
            or "dry_run_report_digest_mismatch" in reasons
            or "human_decision_gate_blocked" in codes
        )


# K1-09
class TestK109Atomic:
    def test_persist_source_unit_exists(self) -> None:
        from open_brain.session_knowledge_migration import persist_source_unit
        assert callable(persist_source_unit)

    @pytest.mark.asyncio
    async def test_fault_mid_source_not_completed_and_archived(self) -> None:
        class Faulty(FakeMigrationStore):
            async def archive_legacy(self, memory_id: int, **kw: Any) -> None:
                raise RuntimeError("archive fault")
        store = Faulty([_summary(901, "Observed: a\nKey Decisions:\n- b")])
        r = await _apply(store, await _dry(store))
        m = await store.get_mapping(r["operation_id"], 901)
        assert m and m["status"] != "completed" and store.memories[901].metadata.get("status") != "archived"


# K1-10
class TestK110Parser:
    @pytest.mark.parametrize(
        ("mid", "content", "expect_routes", "forbid_substrings"),
        [
            (
                1010,
                "Key Decisions:\n- Prefer reversible supersession.",
                {"session_decision"},
                (),
            ),
            (
                1011,
                "## Key Decisions\n1. Numbered markdown decision.\n2. Second item.",
                {"session_decision"},
                (),
            ),
            (
                1012,
                "Observed: shipped cutover.\nStill pending: wire alerts.\nKey Decisions:\n- Done.",
                {"session_event", "session_decision", "unfinished_work"},
                ("Still pending",),
            ),
            (
                1013,
                "What was learned:\n- Keep lineage typed.\n\nKey Decisions:\n- Link derived_from.",
                {"inferred_learning", "session_decision"},
                (),
            ),
            (
                1014,
                "Decisions\n1) Inline heading form\nLearnings\n- Cap residue to unresolved",
                {"session_decision", "inferred_learning"},
                (),
            ),
        ],
        ids=[
            "inline_key_decisions",
            "markdown_numbered",
            "unfinished_stripped",
            "learned_and_decisions",
            "structured_forms",
        ],
    )
    def test_legacy_corpus_matrix(
        self,
        mid: int,
        content: str,
        expect_routes: set[str],
        forbid_substrings: tuple[str, ...],
    ) -> None:
        from open_brain.session_knowledge_migration import transform_legacy_memory

        plan = transform_legacy_memory(_summary(mid, content))
        routes = {o.route for o in plan.outputs}
        assert expect_routes.issubset(routes)
        joined = "\n".join(o.content for o in plan.outputs if o.persist)
        for needle in forbid_substrings:
            assert needle not in joined

    def test_markdown_key_decisions_and_numbered_lists(self) -> None:
        from open_brain.session_knowledge_migration import transform_legacy_memory
        p1 = transform_legacy_memory(_summary(1001, "## Key Decisions\n1. Reversible archival.\n\nObserved: tail."))
        assert "session_decision" in {o.route for o in p1.outputs}
        p2 = transform_legacy_memory(_summary(1002, "Key Decisions:\n1. First.\n2. Second."))
        ds = [o for o in p2.outputs if o.route == "session_decision"]
        assert len(ds) == 2 and "First" in ds[0].content

    def test_unfinished_stripped_and_overflow_unresolved(self) -> None:
        from open_brain.session_knowledge_migration import transform_legacy_memory
        p = transform_legacy_memory(_summary(1003, "Observed: ship.\nStill pending: wire.\nKey Decisions:\n- Go."))
        obs = next(o for o in p.outputs if o.route == "session_event")
        assert "Still pending" not in obs.content and any(o.route == "unfinished_work" for o in p.outputs)
        overflow = transform_legacy_memory(_summary(1004, f"Observed:\n{'x' * (MAX_WHAT_HAPPENED_CHARS + 500)}"))
        assert overflow.outcome == "unresolved"


# K1-11
class TestK111PromotionGrant:
    def test_confirmed_without_grant_downgrades(self) -> None:
        from open_brain.session_knowledge_migration import transform_legacy_memory
        mem = _learning(1101, "claimed confirmed", source_label="confirmed", expected_use="instruction")
        out = transform_legacy_memory(mem, promotion_grant_accepted=False).outputs[0]
        assert out.epistemic["source_label"] == "inferred"
        assert out.metadata.get("authority_downgrade_reason") == "missing_promotion_grant"

    def test_confirmed_with_grant_preserved(self) -> None:
        from open_brain.session_knowledge_migration import transform_legacy_memory
        mem = _learning(1102, "confirmed ok", source_label="confirmed")
        out = transform_legacy_memory(mem, promotion_grant_accepted=True).outputs[0]
        assert out.epistemic["source_label"] == "confirmed" and out.preserved_authority is True


# K1-12
class TestK112Conflicts:
    @pytest.mark.asyncio
    async def test_dry_run_reports_prior_mapping_conflict(self) -> None:
        store = FakeMigrationStore([_learning(1201, "mapped")])
        await store.upsert_mapping({"operation_id": "00000000-0000-4000-8000-000000001201",
            "source_id": 1201, "source_type": "learning", "source_content_hash": "abc",
            "output_ids": [9901], "routes": ["inferred_learning"], "status": "completed"})
        report = await _dry(store)
        assert report.get("conflicts") and report["conflicts"][0]["source_id"] == 1201


# K1-13
class TestK113Gate:
    def test_gate_requires_operation_id_match(self) -> None:
        from open_brain.session_knowledge_migration import evaluate_migration_gate
        report = {"evidence_digest": "d", "cohort_digest": "c", "cohort_watermark": {"max_id": 1, "count": 1},
            "proposed_operation_id": "11111111-1111-4111-8111-111111111111", "unresolved_count": 0, "quarantine_count": 0}
        ev = {"decision": "ALLOW", "dry_run_report_digest": "d", "cohort_digest": "c",
            "cohort_watermark": report["cohort_watermark"], "batch_scope": {"limit": 10, "after_id": 0},
            "backup_restore_receipt": {"verified": True},
            "retrieval_control_baseline": {"lexical": 0.9, "vector": 0.9, "rerank": 0.9},
            "unresolved_acknowledgement": True, "provider_metadata": PROVIDER_META,
            "operation_id": "22222222-2222-4222-8222-222222222222"}
        r = evaluate_migration_gate(decision="ALLOW", dry_run_report=report, evidence=ev,
                                    configured_provider_metadata=PROVIDER_META)
        assert r["outcome"] == "BLOCK" and "operation_id_mismatch" in r["reasons"]

    def test_gate_requires_batch_scope_in_evidence(self) -> None:
        from open_brain.session_knowledge_migration import evaluate_migration_gate
        report = {"evidence_digest": "d", "cohort_digest": "c", "cohort_watermark": {"max_id": 1, "count": 1},
            "proposed_operation_id": "11111111-1111-4111-8111-111111111111", "unresolved_count": 0, "quarantine_count": 0}
        ev = {"decision": "ALLOW", "dry_run_report_digest": "d", "cohort_digest": "c",
            "cohort_watermark": report["cohort_watermark"], "backup_restore_receipt": {"verified": True},
            "retrieval_control_baseline": {"lexical": 0.9, "vector": 0.9, "rerank": 0.9},
            "unresolved_acknowledgement": True, "provider_metadata": PROVIDER_META,
            "operation_id": report["proposed_operation_id"]}
        r = evaluate_migration_gate(decision="ALLOW", dry_run_report=report, evidence=ev,
                                    configured_provider_metadata=PROVIDER_META)
        assert r["outcome"] == "BLOCK" and "batch_scope_missing" in r["reasons"]


# Cohort exclusion + approved-source binding
class TestApprovedCohortBinding:
    @pytest.mark.asyncio
    async def test_migration_derived_learning_excluded_from_eligible_cohort(self) -> None:
        from open_brain.session_knowledge_migration import (
            is_migration_derived_metadata,
            transform_legacy_memory,
        )

        store = FakeMigrationStore([_learning(8010, "legacy source learning")])
        plan = transform_legacy_memory(store.memories[8010])
        derived_meta = plan.outputs[0].metadata
        assert is_migration_derived_metadata(derived_meta) is True
        store.memories[8011] = FakeMemory(
            8011, "learning", plan.outputs[0].content, metadata=dict(derived_meta)
        )
        cohort = await store.list_eligible_cohort()
        assert [m.id for m in cohort] == [8010]

    @pytest.mark.asyncio
    async def test_apply_binds_approved_sources_ignores_post_dry_run_catch_up(self) -> None:
        store = FakeMigrationStore([_learning(8020, "approved only")])
        ctrl = DeterministicControlAdapter({"lexical": 0.9, "vector": 0.9, "rerank": 0.9})
        report = await _dry(store, ctrl)
        # Catch-up legacy row arrives after dry-run / gate approval.
        store.memories[8021] = _learning(8021, "post-dry-run catch-up learning")
        store.next_id = 9000
        applied = await _apply(store, report, control=ctrl, evidence=_evidence(report))
        assert applied["status"] == "completed"
        assert applied["source_ids"] == [8020]
        assert 8021 not in applied["source_ids"]
        assert int((applied.get("catch_up_delta") or {}).get("remaining_count") or 0) >= 1
        assert await store.get_mapping(applied["operation_id"], 8021) is None
        # Approved source archived; catch-up remains active.
        assert 8020 in store.archive_log and 8021 not in store.archive_log

    @pytest.mark.asyncio
    async def test_resume_reuses_exact_derived_ready_outputs_not_derived_as_source(self) -> None:
        store = FakeMigrationStore([_learning(8030, "resume exact outputs")])
        ctrl_fail = DeterministicControlAdapter(
            {"lexical": 0.1, "vector": 0.1, "rerank": 0.1}
        )
        ctrl_ok = DeterministicControlAdapter(
            {"lexical": 0.9, "vector": 0.9, "rerank": 0.9}
        )
        report = await _dry(store, ctrl_ok)
        failed = await _apply(
            store, report, control=ctrl_fail, evidence=_evidence(report)
        )
        assert failed["status"] == "failed"
        mapping = await store.get_mapping(failed["operation_id"], 8030)
        assert mapping and mapping["status"] == "derived_ready"
        failed_outputs = list(mapping["output_ids"])
        assert failed_outputs
        # Derived learnings must not enlarge the live cohort for resume.
        cohort_ids = {m.id for m in await store.list_eligible_cohort()}
        assert cohort_ids == {8030}
        resumed = await _apply(
            store,
            report,
            control=ctrl_ok,
            evidence=_evidence(report),
            operation_id=failed["operation_id"],
        )
        assert resumed["status"] == "completed"
        assert resumed["output_ids"] == failed_outputs
        assert resumed["source_ids"] == [8030]
        assert (await store.get_mapping(failed["operation_id"], 8030))["output_ids"] == (
            failed_outputs
        )


# JSONB codec / resume after derived_ready fault
class TestJsonbNativeBindingAndResume:
    @pytest.mark.asyncio
    async def test_postgres_store_binds_native_json_not_dumps_strings(self) -> None:
        """Regression: asyncpg jsonb codec already dumps; pass native objects."""
        from open_brain.session_knowledge_migration import PostgresMigrationStore

        executed: list[tuple[Any, ...]] = []

        class _Conn:
            async def fetchval(self, *args: Any) -> Any:
                return None

            async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
                executed.append(("fetchrow", query, args))
                return {"id": 7}

            async def execute(self, query: str, *args: Any) -> str:
                executed.append(("execute", query, args))
                return "OK"

        class _Acquire:
            async def __aenter__(self) -> _Conn:
                return _Conn()

            async def __aexit__(self, *a: Any) -> None:
                return None

        class _Pool:
            def acquire(self) -> _Acquire:
                return _Acquire()

        store = PostgresMigrationStore(_Pool())
        await store.save_derived(
            memory_type="learning",
            content="native jsonb",
            metadata={"k": "v", "n": 1},
            title="t",
            embedding=[0.1, 0.2],
        )
        await store.create_relationship(1, 2, "derived_from", metadata={"op": "x"})
        await store.archive_legacy(
            3, operation_id="00000000-0000-4000-8000-000000000001", rollback={"r": 1}
        )
        await store.upsert_operation(
            {
                "operation_id": "00000000-0000-4000-8000-000000000001",
                "status": "running",
                "parameters": {"batch_scope": {"limit": 1}},
                "evidence_digest": "d",
                "cursor": "0",
                "counters": {"output_ids": [7]},
                "provider_metadata": {"embedding_model": "m"},
                "approved_baseline": {"lexical": 0.9},
                "error": None,
            }
        )
        await store.upsert_mapping(
            {
                "operation_id": "00000000-0000-4000-8000-000000000001",
                "source_id": 3,
                "source_type": "learning",
                "source_content_hash": "h",
                "output_ids": [7],
                "routes": ["inferred_learning"],
                "status": "derived_ready",
            }
        )

        assert executed, "expected store SQL calls"
        # Spot-check JSONB bindings are native objects/arrays (never json.dumps strings).
        # Vector params may be textual under to_pg_vector; ignore those.
        save_args = next(a for k, q, a in executed if "INSERT INTO memories" in q)
        assert isinstance(save_args[3], dict) and save_args[3]["k"] == "v"
        rel_args = next(a for k, q, a in executed if "INSERT INTO memory_relationships" in q)
        assert isinstance(rel_args[3], dict) and not isinstance(rel_args[3], str)
        arch_args = next(a for k, q, a in executed if "jsonb_set" in q)
        assert isinstance(arch_args[1], dict)
        op_args = next(
            a for k, q, a in executed if "session_knowledge_migration_operations" in q
        )
        assert isinstance(op_args[2], dict) and isinstance(op_args[5], dict)
        assert isinstance(op_args[6], dict)
        map_args = next(
            a for k, q, a in executed if "session_knowledge_migration_mappings" in q
        )
        assert isinstance(map_args[4], list) and isinstance(map_args[5], list)
        assert not isinstance(map_args[4], str) and not isinstance(map_args[5], str)

    def test_coerce_jsonb_accepts_double_encoded_string(self) -> None:
        from open_brain.session_knowledge_migration import (
            coerce_jsonb_array,
            coerce_jsonb_object,
        )

        assert coerce_jsonb_object('{"a":1}') == {"a": 1}
        assert coerce_jsonb_object({"a": 1}) == {"a": 1}
        assert coerce_jsonb_array("[1,2]") == [1, 2]
        assert coerce_jsonb_array([1, 2]) == [1, 2]

    @pytest.mark.asyncio
    async def test_resume_derived_ready_after_failed_completes_without_dupes(self) -> None:
        store = FakeMigrationStore([_learning(701, "resume after derived_ready")])
        ctrl_ok = DeterministicControlAdapter(
            {"lexical": 0.9, "vector": 0.9, "rerank": 0.9}
        )
        report = await _dry(store, ctrl_ok)
        op_id = report["proposed_operation_id"]
        # Simulate fault after persist: derived output + derived_ready mapping + failed op.
        # Derived output must not be an eligible legacy learning/summary.
        store.memories[7701] = FakeMemory(
            7701,
            "session_learning",
            "resume after derived_ready",
            metadata={"k": 1},
            embedding=[0.1],
        )
        store.next_id = 7800
        await store.upsert_mapping(
            {
                "operation_id": op_id,
                "source_id": 701,
                "source_type": "learning",
                "source_content_hash": report["plans"][0]["source_content_hash"],
                "output_ids": [7701],
                "routes": ["inferred_learning"],
                "status": "derived_ready",
            }
        )
        await store.upsert_operation(
            {
                "operation_id": op_id,
                "status": "failed",
                "parameters": {"batch_scope": {"limit": 50, "after_id": 0}},
                "evidence_digest": report["evidence_digest"],
                "cursor": "0",
                "counters": {"output_ids": [7701], "source_ids": [701], "error_count": 1},
                "provider_metadata": PROVIDER_META,
                "error": "ValueError: dictionary update sequence element #0 has length 1; 2 is required",
            }
        )
        before_rel = len(store.relationships)
        resumed = await _apply(
            store, report, control=ctrl_ok, evidence=_evidence(report), operation_id=op_id
        )
        assert resumed["status"] == "completed"
        assert resumed["output_ids"] == [7701]
        mapping = await store.get_mapping(op_id, 701)
        assert mapping and mapping["status"] == "completed"
        assert mapping["output_ids"] == [7701]
        # No duplicate derived rows for the same source resume.
        derived = [m for m in store.memories.values() if m.id not in {701}]
        assert len(derived) == 1
        op = await store.get_operation(op_id)
        assert op and op["status"] == "completed"
        prior = (op.get("parameters") or {}).get("prior_errors") or []
        assert prior and "dictionary update sequence" in str(prior[0].get("error"))
        assert len(store.relationships) >= before_rel
        assert 701 in store.archive_log


# K1-14
class TestK114JudgeLineage:
    @pytest.mark.asyncio
    async def test_migration_judge_metadata_on_derived(self) -> None:
        store = FakeMigrationStore([_learning(1401, "judge receipt")])
        applied = await _apply(store, await _dry(store))
        for oid in applied["output_ids"]:
            j = store.memories[oid].metadata.get("migration_judge") or {}
            assert j.get("receipt") and j.get("policy_version") == JUDGE_POLICY and j.get("content_hash")

    @pytest.mark.asyncio
    async def test_derived_from_links_legacy_source(self) -> None:
        store = FakeMigrationStore([_summary(1402, "Observed: x\nKey Decisions:\n- y")])
        await _apply(store, await _dry(store))
        links = [r for r in store.relationships if r["link_type"] == DERIVED_FROM_LINK_TYPE and r["target_id"] == 1402]
        assert links and all(link["source_id"] != 1402 for link in links)
