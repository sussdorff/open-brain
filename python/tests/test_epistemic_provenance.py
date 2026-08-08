"""Epistemic provenance contract, defaults, coverage, and backfill."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.cli.main import _build_parser
from open_brain.data_layer.interface import (
    ApprovedCanonicalEntityUpdateParams,
    SaveMemoryParams,
    SaveMemoryResult,
    UpdateMemoryParams,
)
from open_brain.data_layer.postgres import PostgresDataLayer


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROVENANCE_SCHEMA_DOC = PROJECT_ROOT / "docs" / "standards" / "provenance-schema.md"

ORIGIN_PROVENANCE = {
    "producer": "agent",
    "source_ref": "agent-session:codex:session-123",
}


def parse(args: list[str]) -> Any:
    return _build_parser().parse_args(args)


@pytest.fixture
def mock_dl():
    dl = AsyncMock()
    dl.save_memory.return_value = SaveMemoryResult(id=42, message="Memory saved")
    return dl


def _make_pool(conn: AsyncMock) -> MagicMock:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_acquire():
        yield conn

    @asynccontextmanager
    async def fake_transaction(*_args, **_kwargs):
        yield

    # AsyncMock(transaction) returns a coroutine, not an async CM — replace it.
    conn.transaction = MagicMock(side_effect=fake_transaction)
    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


# ─── AC1: shared validated contract + documentation ───────────────────────────


class TestEpistemicProvenanceContract:
    def test_schema_standard_documents_labels_and_origin_separation(self) -> None:
        text = PROVENANCE_SCHEMA_DOC.read_text(encoding="utf-8")
        for label in (
            "observed",
            "inferred",
            "generated",
            "confirmed",
            "disputed",
            "superseded",
        ):
            assert label in text
        assert "expected_use" in text
        assert "metadata.provenance.origin" in text
        assert "evidence" in text
        assert "instruction" in text
        assert "epistemic-provenance.v1" in text
        assert "label-only" in text or "source_label present without" in text
        assert "Use-only" in text or "use-only" in text
        assert "expected_use=evidence" in text
        assert "ambiguous_ids" in text
        assert "instruction_authorized" in text
        assert "fail closed" in text or "fail-closed" in text

    def test_shared_module_exposes_six_labels_and_version(self) -> None:
        from open_brain.epistemic_provenance import (
            EPISTEMIC_LABELS,
            EPISTEMIC_PROVENANCE_SCHEMA_VERSION,
            EXPECTED_USES,
            INSTRUCTION_GRADE_LABELS,
            EVIDENCE_ONLY_LABELS,
        )

        assert EPISTEMIC_PROVENANCE_SCHEMA_VERSION == "epistemic-provenance.v1"
        assert EPISTEMIC_LABELS == {
            "observed",
            "inferred",
            "generated",
            "confirmed",
            "disputed",
            "superseded",
        }
        assert EXPECTED_USES == {"evidence", "instruction"}
        assert INSTRUCTION_GRADE_LABELS == {"observed", "confirmed"}
        assert EVIDENCE_ONLY_LABELS == {"inferred", "generated"}

    def test_legal_expected_use_matrix(self) -> None:
        from open_brain.epistemic_provenance import is_legal_epistemic_combination

        assert is_legal_epistemic_combination("observed", "instruction")
        assert is_legal_epistemic_combination("confirmed", "instruction")
        assert is_legal_epistemic_combination("inferred", "evidence")
        assert is_legal_epistemic_combination("generated", "evidence")
        assert is_legal_epistemic_combination("disputed", "evidence")
        assert is_legal_epistemic_combination("superseded", "evidence")

        assert not is_legal_epistemic_combination("inferred", "instruction")
        assert not is_legal_epistemic_combination("generated", "instruction")
        assert not is_legal_epistemic_combination("disputed", "instruction")
        assert not is_legal_epistemic_combination("superseded", "instruction")

    def test_default_classification_is_inferred_evidence(self) -> None:
        from open_brain.epistemic_provenance import default_epistemic_classification

        default = default_epistemic_classification()
        assert default == {
            "source_label": "inferred",
            "expected_use": "evidence",
            "epistemic_version": "epistemic-provenance.v1",
        }

    def test_validate_preserves_origin_and_rejects_authority_raising(self) -> None:
        from open_brain.epistemic_provenance import (
            EpistemicProvenanceValidationError,
            validate_epistemic_provenance,
        )

        ok = validate_epistemic_provenance(
            {
                "origin": ORIGIN_PROVENANCE,
                "source_label": "observed",
                "expected_use": "instruction",
                "source_ref": "conversation://current",
            }
        )
        assert ok["origin"] == ORIGIN_PROVENANCE
        assert ok["source_label"] == "observed"
        assert ok["expected_use"] == "instruction"
        assert ok["epistemic_version"] == "epistemic-provenance.v1"

        with pytest.raises(EpistemicProvenanceValidationError) as exc:
            validate_epistemic_provenance(
                {
                    "origin": ORIGIN_PROVENANCE,
                    "source_label": "inferred",
                    "expected_use": "instruction",
                }
            )
        assert exc.value.code == "invalid_epistemic_provenance"

    def test_ensure_applies_defaults_without_implying_confirmation(self) -> None:
        from open_brain.epistemic_provenance import ensure_epistemic_provenance

        metadata = ensure_epistemic_provenance({"note": "plain write"})
        provenance = metadata["provenance"]
        assert provenance["source_label"] == "inferred"
        assert provenance["expected_use"] == "evidence"
        assert provenance["epistemic_version"] == "epistemic-provenance.v1"
        assert "origin" not in provenance
        assert provenance["source_label"] != "confirmed"

    def test_judge_reuses_shared_epistemic_labels(self) -> None:
        from open_brain import epistemic_provenance as ep
        from open_brain import memory_write_judge as judge

        assert judge.PROVENANCE_LABELS == ep.EPISTEMIC_LABELS
        assert judge.INSTRUCTION_GRADE_LABELS == ep.INSTRUCTION_GRADE_LABELS
        assert judge.EVIDENCE_ONLY_LABELS == ep.EVIDENCE_ONLY_LABELS
        assert judge.EXPECTED_USES == ep.EXPECTED_USES


# ─── AC5: negative combination matrix ─────────────────────────────────────────


class TestEpistemicNegativeMatrix:
    @pytest.mark.parametrize(
        ("label", "expected_use"),
        [
            ("inferred", "instruction"),
            ("generated", "instruction"),
            ("disputed", "instruction"),
            ("superseded", "instruction"),
            ("not-a-label", "evidence"),
            ("observed", "authority"),
        ],
    )
    def test_invalid_combinations_are_rejected(self, label: str, expected_use: str) -> None:
        from open_brain.epistemic_provenance import (
            EpistemicProvenanceValidationError,
            validate_epistemic_provenance,
        )

        with pytest.raises(EpistemicProvenanceValidationError):
            validate_epistemic_provenance(
                {"source_label": label, "expected_use": expected_use}
            )


# ─── AC2: every new write receives epistemic classification ───────────────────


class TestSaveMemoryEpistemicDefaults:
    @pytest.mark.asyncio
    async def test_save_memory_without_proposal_defaults_to_inferred_evidence(
        self, mock_dl
    ) -> None:
        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", return_value={}),
            patch("open_brain.server._extract_entities", return_value={}),
        ):
            from open_brain.server import save_memory

            result = await save_memory(
                text="Agent wrote this without a proposal",
                provenance=ORIGIN_PROVENANCE,
            )

        import json

        assert json.loads(result)["id"] == 42
        call_args = mock_dl.save_memory.call_args[0][0]
        provenance = call_args.metadata["provenance"]
        assert provenance["source_label"] == "inferred"
        assert provenance["expected_use"] == "evidence"
        assert provenance["epistemic_version"] == "epistemic-provenance.v1"

    @pytest.mark.asyncio
    async def test_save_memory_rejects_instruction_without_instruction_grade_label(
        self, mock_dl
    ) -> None:
        import json

        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import save_memory

            result = await save_memory(
                text="Trying to sneak instruction-grade memory",
                provenance=ORIGIN_PROVENANCE,
                metadata={
                    "provenance": {
                        "source_label": "generated",
                        "expected_use": "instruction",
                    }
                },
            )

        data = json.loads(result)
        assert data["error"] == "invalid_epistemic_provenance"
        mock_dl.save_memory.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_postgres_save_memory_defaults_epistemic_fields(self) -> None:
        inserted_row = {"id": 101}
        conn = AsyncMock()
        conn.fetchrow.side_effect = [None, inserted_row]
        pool = _make_pool(conn)
        dl = PostgresDataLayer()

        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            await dl.save_memory(
                SaveMemoryParams(
                    text="Direct data-layer write",
                    provenance=ORIGIN_PROVENANCE,
                )
            )

        insert_args = conn.fetchrow.call_args_list[-1][0]
        metadata_arg = next(arg for arg in insert_args if isinstance(arg, dict))
        assert metadata_arg["provenance"]["origin"] == ORIGIN_PROVENANCE
        assert metadata_arg["provenance"]["source_label"] == "inferred"
        assert metadata_arg["provenance"]["expected_use"] == "evidence"
        assert metadata_arg["provenance"]["epistemic_version"] == "epistemic-provenance.v1"

    @pytest.mark.asyncio
    async def test_allowed_proposal_metadata_includes_epistemic_version(
        self, mock_dl
    ) -> None:
        import json

        proposal = {
            "intended_memory_content": "User prefers concise status updates.",
            "category": "preference",
            "source_citation": {
                "ref": "conversation://current/preference",
                "label": "observed",
            },
            "authorization_basis": {
                "ref": "conversation://current/preference",
                "label": "observed",
                "granted_by": "user",
            },
            "expected_use": "instruction",
            "retention_scope": "personal",
            "risk_flags": [],
        }
        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", return_value={}),
            patch("open_brain.server._extract_entities", return_value={}),
        ):
            from open_brain.server import save_memory

            result = await save_memory(
                text="User prefers concise status updates.",
                proposal=proposal,
                provenance=ORIGIN_PROVENANCE,
            )

        assert json.loads(result)["id"] == 42
        call_args = mock_dl.save_memory.call_args[0][0]
        provenance = call_args.metadata["provenance"]
        assert provenance["source_label"] == "observed"
        assert provenance["expected_use"] == "instruction"
        assert provenance["epistemic_version"] == "epistemic-provenance.v1"
        # Internal non-wire authorization from the real judge, not metadata.
        assert call_args.instruction_authorized is True


# ─── AC3/AC4: coverage inventory and conservative backfill ────────────────────


class TestEpistemicCoverageAndBackfill:
    @pytest.mark.asyncio
    async def test_epistemic_coverage_report_is_read_only_and_separate_from_origin(
        self,
    ) -> None:
        conn = AsyncMock()
        conn.fetch.return_value = [
            {"cohort": "labeled", "count": 10},
            {"cohort": "unlabeled", "count": 5},
            {"cohort": "partial", "count": 2},
            {"cohort": "ambiguous", "count": 1},
        ]
        pool = _make_pool(conn)
        dl = PostgresDataLayer()

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ) as get_pool:
            result = await dl.epistemic_provenance_report()

        get_pool.assert_awaited_once_with(run_migrations=False)
        conn.execute.assert_not_awaited()
        assert result["read_only"] is True
        assert result["total"] == 18
        assert set(result["cohorts"]) == {
            "labeled",
            "unlabeled",
            "partial",
            "ambiguous",
        }
        assert "deterministic_backfill" not in result["cohorts"]

    def test_backfill_dry_run_counters_are_mutually_exclusive(self) -> None:
        from open_brain.epistemic_provenance import plan_epistemic_backfill

        rows = [
            {"id": 1, "metadata": {}},
            {
                "id": 2,
                "metadata": {
                    "provenance": {
                        "source_label": "observed",
                        "expected_use": "evidence",
                        "epistemic_version": "epistemic-provenance.v1",
                    }
                },
            },
            {
                "id": 3,
                "metadata": {
                    "provenance": {
                        "source_label": "generated",
                        "expected_use": "instruction",
                    }
                },
            },
            {
                "id": 4,
                "metadata": {"provenance": {"source_label": "observed"}},
            },
            {
                "id": 5,
                "metadata": {"provenance": {"expected_use": "evidence"}},
            },
        ]
        plan = plan_epistemic_backfill(rows)
        assert plan["mode"] == "dry_run"
        assert plan["updated"] == 0
        assert plan["already_labeled"] == 1
        assert plan["unlabeled"] == 1
        assert plan["partial"] == 1
        assert plan["ambiguous"] == 2
        assert (
            plan["already_labeled"]
            + plan["unlabeled"]
            + plan["partial"]
            + plan["ambiguous"]
            == plan["total"]
            == 5
        )
        assert plan["would_update"] == 2
        assert {item["id"] for item in plan["updates"]} == {1, 4}
        by_id = {item["id"]: item for item in plan["updates"]}
        assert by_id[1]["source_label"] == "inferred"
        assert by_id[4]["source_label"] == "observed"
        assert by_id[4]["expected_use"] == "evidence"
        assert set(plan["ambiguous_ids"]) == {3, 5}
        assert plan["ambiguous_ids_truncated"] is False
        assert plan["ambiguous_ids_cap"] == 100

    @pytest.mark.asyncio
    async def test_backfill_apply_is_idempotent_with_stateful_second_fetch(self) -> None:
        unlabeled_meta = {"note": "legacy", "provenance": {"origin": ORIGIN_PROVENANCE}}
        labeled_after = {
            "note": "legacy",
            "provenance": {
                "origin": ORIGIN_PROVENANCE,
                "source_label": "inferred",
                "expected_use": "evidence",
                "epistemic_version": "epistemic-provenance.v1",
            },
        }
        labeled_meta = {
            "provenance": {
                "origin": ORIGIN_PROVENANCE,
                "source_label": "observed",
                "expected_use": "evidence",
                "epistemic_version": "epistemic-provenance.v1",
            }
        }
        ambiguous_meta = {
            "provenance": {
                "origin": ORIGIN_PROVENANCE,
                "source_label": "inferred",
                "expected_use": "instruction",
            }
        }
        first_rows = [
            {"id": 1, "metadata": unlabeled_meta},
            {"id": 2, "metadata": labeled_meta},
            {"id": 3, "metadata": ambiguous_meta},
        ]
        second_rows = [
            {"id": 1, "metadata": labeled_after},
            {"id": 2, "metadata": labeled_meta},
            {"id": 3, "metadata": ambiguous_meta},
        ]
        conn = AsyncMock()
        conn.fetch.side_effect = [first_rows, second_rows]
        conn.fetchrow.return_value = None
        pool = _make_pool(conn)
        dl = PostgresDataLayer()

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            first = await dl.backfill_epistemic_provenance(apply=True, limit=500)
            second = await dl.backfill_epistemic_provenance(apply=True, limit=500)

        assert first["mode"] == "apply"
        assert first["updated"] == 1
        assert first["would_update"] == 1
        assert first["batch_limit"] == 500
        assert second["updated"] == 0
        assert second["would_update"] == 0
        assert second["already_labeled"] == 2
        assert second["ambiguous"] == 1
        assert (
            second["already_labeled"]
            + second["unlabeled"]
            + second["partial"]
            + second["ambiguous"]
            == second["total"]
        )

        for call in conn.execute.await_args_list:
            sql = call.args[0]
            assert "DELETE" not in sql.upper()
            assert "updated_at" not in sql.lower()
            metadata_arg = next(
                (arg for arg in call.args[1:] if isinstance(arg, dict)),
                None,
            )
            if metadata_arg is not None:
                assert metadata_arg["provenance"]["origin"] == ORIGIN_PROVENANCE
                assert metadata_arg["provenance"]["source_label"] == "inferred"
                assert metadata_arg["provenance"]["expected_use"] == "evidence"

    @pytest.mark.asyncio
    async def test_backfill_apply_is_batch_bounded_with_keyset_cursor(self) -> None:
        conn = AsyncMock()
        conn.fetch.return_value = [
            {"id": 10, "metadata": {}},
            {"id": 11, "metadata": {}},
        ]
        conn.fetchrow.return_value = {"present": 1}
        pool = _make_pool(conn)
        dl = PostgresDataLayer()

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            result = await dl.backfill_epistemic_provenance(
                apply=True,
                limit=2,
                after_id=5,
            )

        fetch_sql = conn.fetch.await_args.args[0]
        assert "id > $1" in fetch_sql
        assert "LIMIT $2" in fetch_sql
        assert conn.fetch.await_args.args[1:] == (5, 2)
        assert result["batch_limit"] == 2
        assert result["after_id"] == 5
        assert result["next_after_id"] == 11
        assert result["has_more"] is True
        assert result["scanned"] == 2

    @pytest.mark.asyncio
    async def test_backfill_apply_rejects_invalid_limit(self) -> None:
        from open_brain.epistemic_provenance import EpistemicProvenanceValidationError

        dl = PostgresDataLayer()
        with pytest.raises(EpistemicProvenanceValidationError):
            await dl.backfill_epistemic_provenance(apply=True, limit=0)


# ─── Repair: update / canonical / append / authority ───────────────────────────


class TestUpdateAndCanonicalEpistemicGuards:
    @pytest.mark.asyncio
    async def test_update_memory_rejects_instruction_and_preserves_origin(self) -> None:
        from open_brain.epistemic_provenance import EpistemicProvenanceValidationError

        existing = {
            "id": 9,
            "content": "c",
            "title": None,
            "subtitle": None,
            "narrative": None,
            "metadata": {
                "provenance": {
                    "origin": ORIGIN_PROVENANCE,
                    "source_label": "inferred",
                    "expected_use": "evidence",
                    "epistemic_version": "epistemic-provenance.v1",
                }
            },
        }
        conn = AsyncMock()
        conn.fetchrow.return_value = existing
        pool = _make_pool(conn)
        dl = PostgresDataLayer()

        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            pytest.raises(EpistemicProvenanceValidationError),
        ):
            await dl.update_memory(
                UpdateMemoryParams(
                    id=9,
                    metadata={
                        "provenance": {
                            "source_label": "observed",
                            "expected_use": "instruction",
                        }
                    },
                )
            )
        conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_memory_preserves_origin_when_provenance_replaced(self) -> None:
        existing = {
            "id": 9,
            "content": "c",
            "title": None,
            "subtitle": None,
            "narrative": None,
            "metadata": {
                "keep": True,
                "provenance": {
                    "origin": ORIGIN_PROVENANCE,
                    "source_label": "inferred",
                    "expected_use": "evidence",
                    "epistemic_version": "epistemic-provenance.v1",
                },
            },
        }
        conn = AsyncMock()
        conn.fetchrow.return_value = existing
        pool = _make_pool(conn)
        dl = PostgresDataLayer()

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            await dl.update_memory(
                UpdateMemoryParams(
                    id=9,
                    metadata={
                        "provenance": {
                            "source_label": "generated",
                            "expected_use": "evidence",
                        }
                    },
                )
            )

        sql = conn.execute.await_args.args[0]
        assert "metadata = $1::jsonb" in sql or "metadata = $" in sql
        assert "metadata ||" not in sql
        metadata_arg = next(
            arg for arg in conn.execute.await_args.args[1:] if isinstance(arg, dict)
        )
        assert metadata_arg["keep"] is True
        assert metadata_arg["provenance"]["origin"] == ORIGIN_PROVENANCE
        assert metadata_arg["provenance"]["source_label"] == "generated"
        assert metadata_arg["provenance"]["expected_use"] == "evidence"

    @pytest.mark.asyncio
    async def test_mcp_update_memory_surfaces_invalid_epistemic_error(
        self, mock_dl
    ) -> None:
        import json

        from open_brain.epistemic_provenance import EpistemicProvenanceValidationError

        mock_dl.update_memory.side_effect = EpistemicProvenanceValidationError(
            "expected_use=instruction requires an allowed memory-write judge outcome"
        )
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import update_memory

            result = json.loads(
                await update_memory(
                    id=9,
                    metadata={
                        "provenance": {
                            "source_label": "observed",
                            "expected_use": "instruction",
                        }
                    },
                )
            )
        assert result["error"] == "invalid_epistemic_provenance"

    @pytest.mark.asyncio
    async def test_canonical_update_rejects_origin_strip_and_bad_epistemic(self) -> None:
        from open_brain.epistemic_provenance import EpistemicProvenanceValidationError

        existing = {
            "id": 701,
            "content": "Original content",
            "type": "concept",
            "title": "Original",
            "subtitle": None,
            "narrative": None,
            "metadata": {
                "canonical_entity": True,
                "canonical_kind": "concept",
                "provenance": {
                    "origin": ORIGIN_PROVENANCE,
                    "source_label": "observed",
                    "expected_use": "evidence",
                    "epistemic_version": "epistemic-provenance.v1",
                },
            },
        }
        conn = AsyncMock()
        conn.fetchrow.return_value = existing
        pool = _make_pool(conn)

        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            pytest.raises(EpistemicProvenanceValidationError),
        ):
            await PostgresDataLayer().approved_update_canonical_entity(
                ApprovedCanonicalEntityUpdateParams(
                    id=701,
                    actor="test-runner",
                    note="Approved correction",
                    metadata={
                        "canonical_kind": "concept",
                        "provenance": {
                            "source_label": "generated",
                            "expected_use": "instruction",
                        },
                    },
                )
            )
        conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_canonical_update_preserves_origin_on_metadata_replace(self) -> None:
        existing = {
            "id": 701,
            "content": "Original content",
            "type": "concept",
            "title": "Original",
            "subtitle": None,
            "narrative": None,
            "metadata": {
                "canonical_entity": True,
                "canonical_kind": "concept",
                "provenance": {
                    "origin": ORIGIN_PROVENANCE,
                    "source_label": "observed",
                    "expected_use": "evidence",
                    "epistemic_version": "epistemic-provenance.v1",
                },
            },
        }
        conn = AsyncMock()
        conn.fetchrow.return_value = existing
        pool = _make_pool(conn)

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            await PostgresDataLayer().approved_update_canonical_entity(
                ApprovedCanonicalEntityUpdateParams(
                    id=701,
                    actor="test-runner",
                    note="Approved correction",
                    metadata={
                        "canonical_kind": "concept",
                        "reviewed": True,
                        "provenance": {
                            "source_label": "confirmed",
                            "expected_use": "evidence",
                        },
                    },
                )
            )

        metadata_arg = conn.execute.await_args.args[1]
        assert metadata_arg["provenance"]["origin"] == ORIGIN_PROVENANCE
        assert metadata_arg["provenance"]["source_label"] == "confirmed"
        assert metadata_arg["reviewed"] is True
        assert metadata_arg["canonical_entity"] is True


class TestAppendAndAuthorityGuards:
    def test_append_repairs_use_only_partial_and_preserves_origin(self) -> None:
        from open_brain.data_layer.postgres import _merge_append_metadata

        merged = _merge_append_metadata(
            {
                "source": "legacy",
                "provenance": {
                    "origin": ORIGIN_PROVENANCE,
                    "expected_use": "evidence",
                },
            },
            None,
            ORIGIN_PROVENANCE,
        )
        assert merged["provenance"]["origin"] == ORIGIN_PROVENANCE
        assert merged["provenance"]["source_label"] == "inferred"
        assert merged["provenance"]["expected_use"] == "evidence"
        assert "expected_use" in merged["provenance"]

    def test_k2_01_append_overwrites_with_judged_instruction(self) -> None:
        """RED/GREEN: second judged instruction append must not be gap-blocked."""
        from open_brain.data_layer.postgres import _merge_append_metadata

        merged = _merge_append_metadata(
            {
                "provenance": {
                    "origin": ORIGIN_PROVENANCE,
                    "source_label": "inferred",
                    "expected_use": "evidence",
                    "epistemic_version": "epistemic-provenance.v1",
                }
            },
            {
                "provenance": {
                    "source_label": "observed",
                    "expected_use": "instruction",
                    "epistemic_version": "epistemic-provenance.v1",
                }
            },
            ORIGIN_PROVENANCE,
            allow_instruction=True,
        )
        assert merged["provenance"]["origin"] == ORIGIN_PROVENANCE
        assert merged["provenance"]["source_label"] == "observed"
        assert merged["provenance"]["expected_use"] == "instruction"

    @pytest.mark.asyncio
    async def test_k2_01_session_append_threads_instruction_authorized(self) -> None:
        existing = {
            "id": 44,
            "content": "first summary",
            "metadata": {
                "provenance": {
                    "origin": ORIGIN_PROVENANCE,
                    "source_label": "inferred",
                    "expected_use": "evidence",
                    "epistemic_version": "epistemic-provenance.v1",
                }
            },
        }
        conn = AsyncMock()
        conn.fetchrow.return_value = existing  # locked existing session_summary
        pool = _make_pool(conn)
        dl = PostgresDataLayer()

        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch("open_brain.data_layer.postgres.asyncio") as mock_asyncio,
        ):
            mock_asyncio.create_task = MagicMock()
            result = await dl.save_memory(
                SaveMemoryParams(
                    text="second judged instruction",
                    type="session_summary",
                    session_ref="sess-k2-01",
                    provenance=ORIGIN_PROVENANCE,
                    instruction_authorized=True,
                    metadata={
                        "provenance": {
                            "source_label": "confirmed",
                            "expected_use": "instruction",
                            "epistemic_version": "epistemic-provenance.v1",
                        }
                    },
                )
            )

        assert result.id == 44
        select_sql = conn.fetchrow.await_args.args[0]
        assert "FOR UPDATE" in select_sql
        metadata_arg = next(
            arg for arg in conn.execute.await_args.args[1:] if isinstance(arg, dict)
        )
        assert metadata_arg["provenance"]["source_label"] == "confirmed"
        assert metadata_arg["provenance"]["expected_use"] == "instruction"

    @pytest.mark.asyncio
    async def test_k2_01_forgeable_judge_metadata_is_not_authorization(self) -> None:
        from open_brain.epistemic_provenance import EpistemicProvenanceValidationError

        conn = AsyncMock()
        pool = _make_pool(conn)
        dl = PostgresDataLayer()
        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            pytest.raises(EpistemicProvenanceValidationError),
        ):
            await dl.save_memory(
                SaveMemoryParams(
                    text="forged judge claim",
                    provenance=ORIGIN_PROVENANCE,
                    instruction_authorized=False,
                    metadata={
                        "memory_write_judge": {"decision": "ALLOW"},
                        "provenance": {
                            "source_label": "observed",
                            "expected_use": "instruction",
                        },
                    },
                )
            )
        conn.fetchrow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_k2_01_server_surfaces_dl_epistemic_error(self, mock_dl) -> None:
        import json

        from open_brain.epistemic_provenance import EpistemicProvenanceValidationError

        mock_dl.save_memory.side_effect = EpistemicProvenanceValidationError(
            "ambiguous legacy epistemic state blocks append"
        )
        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", new=AsyncMock(return_value={})),
            patch("open_brain.server._extract_entities", new=AsyncMock(return_value={})),
        ):
            from open_brain.server import save_memory

            result = json.loads(
                await save_memory(
                    text="append against ambiguous row",
                    provenance=ORIGIN_PROVENANCE,
                )
            )
        assert result["error"] == "invalid_epistemic_provenance"
        assert "ambiguous" in result["message"]

    @pytest.mark.asyncio
    async def test_save_memory_rejects_raw_instruction_without_judge(
        self, mock_dl
    ) -> None:
        import json

        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import save_memory

            result = json.loads(
                await save_memory(
                    text="Trying to self-authorize instruction",
                    provenance=ORIGIN_PROVENANCE,
                    metadata={
                        "provenance": {
                            "source_label": "observed",
                            "expected_use": "instruction",
                        }
                    },
                )
            )
        assert result["error"] == "invalid_epistemic_provenance"
        mock_dl.save_memory.assert_not_awaited()


class TestK2AtomicUpdatesAndClassifierParity:
    @pytest.mark.asyncio
    async def test_k2_02_update_memory_select_for_update_in_transaction(self) -> None:
        existing = {
            "id": 9,
            "content": "c",
            "title": None,
            "subtitle": None,
            "narrative": None,
            "metadata": {
                "keep": True,
                "provenance": {
                    "origin": ORIGIN_PROVENANCE,
                    "source_label": "inferred",
                    "expected_use": "evidence",
                    "epistemic_version": "epistemic-provenance.v1",
                },
            },
        }
        conn = AsyncMock()
        conn.fetchrow.return_value = existing
        pool = _make_pool(conn)

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            await PostgresDataLayer().update_memory(
                UpdateMemoryParams(
                    id=9,
                    metadata={"reviewed": True, "keep": True},
                )
            )

        select_sql = conn.fetchrow.await_args.args[0]
        assert "FOR UPDATE" in select_sql
        conn.transaction.assert_called()
        metadata_arg = next(
            arg for arg in conn.execute.await_args.args[1:] if isinstance(arg, dict)
        )
        assert metadata_arg["reviewed"] is True
        assert metadata_arg["keep"] is True
        assert metadata_arg["provenance"]["origin"] == ORIGIN_PROVENANCE

    @pytest.mark.asyncio
    async def test_k2_02_canonical_update_select_for_update_in_transaction(self) -> None:
        existing = {
            "id": 701,
            "content": "Original content",
            "type": "concept",
            "title": "Original",
            "subtitle": None,
            "narrative": None,
            "metadata": {
                "canonical_entity": True,
                "canonical_kind": "concept",
                "provenance": {
                    "origin": ORIGIN_PROVENANCE,
                    "source_label": "observed",
                    "expected_use": "evidence",
                    "epistemic_version": "epistemic-provenance.v1",
                },
            },
        }
        conn = AsyncMock()
        conn.fetchrow.return_value = existing
        pool = _make_pool(conn)

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            await PostgresDataLayer().approved_update_canonical_entity(
                ApprovedCanonicalEntityUpdateParams(
                    id=701,
                    actor="test-runner",
                    note="Approved correction",
                    metadata={"canonical_kind": "concept", "reviewed": True},
                )
            )

        select_sql = conn.fetchrow.await_args.args[0]
        assert "FOR UPDATE" in select_sql
        conn.transaction.assert_called()

    def test_k2_03_invalid_label_no_use_is_ambiguous_not_partial(self) -> None:
        from open_brain.epistemic_provenance import (
            classify_epistemic_row,
            plan_epistemic_backfill,
        )

        malformed = {"provenance": {"source_label": "not-a-real-label"}}
        assert classify_epistemic_row(malformed) == "ambiguous"
        plan = plan_epistemic_backfill(
            [
                {"id": 99, "metadata": malformed},
                {"id": 100, "metadata": {}},
                {
                    "id": 101,
                    "metadata": {"provenance": {"source_label": "observed"}},
                },
            ]
        )
        assert plan["ambiguous"] == 1
        assert plan["partial"] == 1
        assert plan["unlabeled"] == 1
        assert plan["ambiguous_ids"] == [99]
        assert plan["would_update"] == 2
        assert (
            plan["already_labeled"]
            + plan["unlabeled"]
            + plan["partial"]
            + plan["ambiguous"]
            == plan["total"]
            == 3
        )

    @pytest.mark.asyncio
    async def test_k2_03_sql_classifier_matches_python_valid_label_set(self) -> None:
        conn = AsyncMock()
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            await PostgresDataLayer().epistemic_provenance_report()
        sql = conn.fetch.await_args.args[0]
        # partial requires membership in the same six-label set as Python.
        assert "THEN 'partial'" in sql
        assert "'observed', 'inferred', 'generated'" in sql
        # Invalid labels fall through to ambiguous, not partial.
        partial_branch = sql.split("THEN 'partial'")[0].rsplit("WHEN", 1)[-1]
        assert "IN (" in partial_branch
        assert "expected_use' IS NULL" in partial_branch
        for label in (
            "observed",
            "inferred",
            "generated",
            "confirmed",
            "disputed",
            "superseded",
        ):
            assert f"'{label}'" in partial_branch

    @pytest.mark.asyncio
    async def test_k2_03_backfill_reports_capped_ambiguous_ids_and_cursor(self) -> None:
        from open_brain.epistemic_provenance import EPISTEMIC_AMBIGUOUS_IDS_CAP

        rows = [
            {
                "id": i,
                "metadata": {"provenance": {"source_label": f"bad-{i}"}},
            }
            for i in range(1, EPISTEMIC_AMBIGUOUS_IDS_CAP + 3)
        ]
        # Plus one unlabeled to prove planner continues.
        rows.append({"id": 10_000, "metadata": {}})
        conn = AsyncMock()
        conn.fetch.return_value = rows
        conn.fetchrow.return_value = {"present": 1}
        pool = _make_pool(conn)

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            plan = await PostgresDataLayer().backfill_epistemic_provenance(
                apply=False, limit=50, after_id=0
            )

        assert plan["ambiguous"] == EPISTEMIC_AMBIGUOUS_IDS_CAP + 2
        assert len(plan["ambiguous_ids"]) == EPISTEMIC_AMBIGUOUS_IDS_CAP
        assert plan["ambiguous_ids_truncated"] is True
        assert plan["batch_limit"] == 50
        assert plan["after_id"] == 0
        assert plan["next_after_id"] == 10_000
        assert plan["has_more"] is True
        assert plan["scanned"] == len(rows)
        assert plan["unlabeled"] == 1
        assert plan["would_update"] == 1


# ─── AC4: MCP/CLI epistemic coverage surface ──────────────────────────────────


class TestEpistemicCoverageSurfaces:
    @pytest.mark.asyncio
    async def test_mcp_tool_delegates_to_epistemic_report(self, mock_dl) -> None:
        import json

        fixture_path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "epistemic_coverage_report.json"
        )
        expected = json.loads(fixture_path.read_text(encoding="utf-8"))
        mock_dl.epistemic_provenance_report.return_value = expected

        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import epistemic_provenance_report

            result = json.loads(await epistemic_provenance_report())

        assert result == expected
        assert result["read_only"] is True
        assert set(result["cohorts"]) == {
            "labeled",
            "unlabeled",
            "partial",
            "ambiguous",
        }
        mock_dl.epistemic_provenance_report.assert_awaited_once_with()

    def test_cli_exposes_epistemic_report_and_bounded_backfill(self) -> None:
        report_args = parse(["provenance", "epistemic-report"])
        assert report_args.provenance_command == "epistemic-report"

        backfill_args = parse(
            ["provenance", "epistemic-backfill", "--limit", "25", "--after-id", "9"]
        )
        assert backfill_args.provenance_command == "epistemic-backfill"
        assert getattr(backfill_args, "apply", False) is False
        assert backfill_args.limit == 25
        assert backfill_args.after_id == 9
