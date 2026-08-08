"""Opus round-2 repair matrix for open-brain-ekn.4 (O2-01 .. O2-05)."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from open_brain.data_layer.interface import Memory
from open_brain.retrieval_contract import (
    PROVENANCE_FLOOR_EXCLUDE_LABELS,
    PROVENANCE_FLOOR_MIN_LABELS,
    RetrievalContractValidationError,
    apply_retrieval_contract,
    parse_retrieval_contract,
    profile_retrieval_contract,
    retrieval_contract_schema_is_valid,
)

HOOKS = Path(__file__).resolve().parents[2] / "hooks" / "scripts"
sys.path.insert(0, str(HOOKS))
from context_inject import (  # noqa: E402
    build_output,
    session_start_preamble,
    token_estimate as hook_token_estimate,
)


def _memory(**kwargs: Any) -> Memory:
    defaults: dict[str, Any] = dict(
        id=1,
        index_id=1,
        session_id=None,
        type="observation",
        title="KNOWN_WAKE_TITLE",
        subtitle=None,
        narrative=None,
        content="ordinary wake-up memory body for fixed budget",
        metadata={},
        priority=0.5,
        stability="stable",
        access_count=0,
        last_accessed_at=None,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    defaults.update(kwargs)
    return Memory(**defaults)


def _canonical_contract(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": "retrieval-contract.v1",
        "work_object": {"kind": "project", "id": "open-brain"},
        "retrieval_units": ["memory"],
        "authoritative_sources": [
            {"unit_kind": "memory", "source": "open-brain.memories"}
        ],
        "permissions": {
            "read": True,
            "write_back": False,
            "allow_high_authority": False,
        },
        "provenance_requirements": {
            "min_labels_for_high_authority": ["observed", "confirmed"],
            "require_instruction_expected_use": True,
            "require_promotion_audit_reason": True,
            "exclude_labels": [
                "disputed",
                "superseded",
                "inferred",
                "generated",
            ],
        },
        "compiled_context_candidates": [
            {
                "section": "evidence",
                "max_influence": "evidence",
                "require_promotion": False,
            }
        ],
        "write_back": {
            "allowed": False,
            "requires_memory_write_proposal": True,
            "allowed_expected_uses": ["evidence"],
        },
    }
    base.update(overrides)
    return base


def _schema_ok(raw: dict[str, Any]) -> bool:
    return retrieval_contract_schema_is_valid(raw)


def _parser_ok(raw: dict[str, Any]) -> bool:
    try:
        parse_retrieval_contract(raw)
        return True
    except RetrievalContractValidationError:
        return False


# ─── O2-01 fixed-budget SessionStart usefulness ──────────────────────────────


class TestO201FixedBudgetEnvelope:
    def test_claude_wake_up_emits_unit_at_production_budget_500(self) -> None:
        from open_brain.wake_up import build_wake_up_pack

        memories = [
            _memory(id=i, title=f"KNOWN_WAKE_TITLE_{i}", content=f"body-{i}-pad")
            for i in range(1, 9)
        ]
        envelope = build_wake_up_pack(
            memories,
            token_budget=500,
            retrieval_contract={
                "profile": "claude-wake-up",
                "work_object": {"kind": "project", "id": "proj"},
            },
            as_envelope=True,
        )
        assert envelope, "production budget must not fail-closed to empty"
        assert "KNOWN_WAKE_TITLE_" in envelope
        payload = json.loads(envelope.split("\n")[1])
        assert isinstance(payload.get("units"), list)
        assert len(payload["units"]) >= 1
        # Fixed header must be by reference, not a full inline contract dump.
        assert "contract" not in payload or payload.get("contract") is None
        assert payload.get("contract_ref")
        assert payload.get("profile") == "claude-wake-up"

    @pytest.mark.asyncio
    async def test_http_to_hook_injects_known_title_at_default_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("API_KEYS", "valid-key-abc")
        from open_brain.server import app
        from open_brain.wake_up import token_estimate

        memories = [
            _memory(id=1, title="KNOWN_E2E_TITLE", content="e2e body content"),
            _memory(id=2, title="Other", content="second"),
        ]
        with patch(
            "open_brain.server.get_dl",
            return_value=AsyncMock(
                get_wake_up_memories=AsyncMock(return_value=memories)
            ),
        ):
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                # Mirror the shipped hook: subtract preamble, request envelope.
                preamble = session_start_preamble()
                server_budget = 500 - hook_token_estimate(preamble)
                resp = await client.get(
                    "/api/wake_up_pack",
                    params={
                        "token_budget": server_budget,
                        "project": "proj",
                        "format": "envelope",
                        "profile": "claude-wake-up",
                    },
                    headers={"X-API-Key": "valid-key-abc"},
                )
        assert resp.status_code == 200
        body = resp.text
        assert "KNOWN_E2E_TITLE" in body
        output = build_output(body, "claude", token_budget=500)
        assert "systemMessage" in output
        assert "KNOWN_E2E_TITLE" in output["systemMessage"]
        assert token_estimate(output["systemMessage"]) <= 500


# ─── O2-02 schema/parser parity corpus ───────────────────────────────────────


def _mutation_corpus() -> list[dict[str, Any]]:
    """Generated mutations over every nested object and field class."""
    cases: list[dict[str, Any]] = [_canonical_contract()]

    # Missing required top-level keys
    for key in (
        "version",
        "work_object",
        "retrieval_units",
        "authoritative_sources",
        "permissions",
        "provenance_requirements",
        "compiled_context_candidates",
        "write_back",
    ):
        raw = _canonical_contract()
        del raw[key]
        cases.append(raw)

    # Null / wrong type / empty
    cases.append(_canonical_contract(retrieval_units=None))
    cases.append(_canonical_contract(retrieval_units=[]))
    cases.append(_canonical_contract(retrieval_units="memory"))
    cases.append(_canonical_contract(authoritative_sources=None))
    cases.append(_canonical_contract(authoritative_sources=[]))
    cases.append(_canonical_contract(compiled_context_candidates=[]))
    cases.append(_canonical_contract(permissions=None))
    cases.append(_canonical_contract(write_back=[]))

    # Unknown nested keys
    for path in (
        ("permissions", "surprise"),
        ("provenance_requirements", "excluded_labels"),
        ("write_back", "extra"),
    ):
        raw = _canonical_contract()
        raw[path[0]][path[1]] = True
        cases.append(raw)

    raw = _canonical_contract()
    raw["authoritative_sources"][0]["extra"] = True
    cases.append(raw)
    raw = _canonical_contract()
    raw["compiled_context_candidates"][0]["extra"] = True
    cases.append(raw)
    cases.append(_canonical_contract(extra_top=1))
    raw = _canonical_contract()
    raw["work_object"]["surprise"] = True
    cases.append(raw)

    # Duplicates
    cases.append(_canonical_contract(retrieval_units=["memory", "memory"]))
    cases.append(
        _canonical_contract(
            authoritative_sources=[
                {"unit_kind": "memory", "source": "open-brain.memories"},
                {"unit_kind": "memory", "source": "open-brain.other"},
            ]
        )
    )
    cases.append(
        _canonical_contract(
            compiled_context_candidates=[
                {
                    "section": "evidence",
                    "max_influence": "evidence",
                    "require_promotion": False,
                },
                {
                    "section": "evidence",
                    "max_influence": "context",
                    "require_promotion": False,
                },
            ]
        )
    )

    # Empty / whitespace / booleans
    cases.append(_canonical_contract(work_object={"kind": "project", "id": " p "}))
    cases.append(
        _canonical_contract(
            permissions={
                "read": False,
                "write_back": False,
                "allow_high_authority": False,
            }
        )
    )
    cases.append(
        _canonical_contract(
            permissions={
                "read": True,
                "write_back": False,
                "allow_high_authority": "yes",
            }
        )
    )

    # Provenance floor relaxations
    cases.append(
        _canonical_contract(
            provenance_requirements={
                "min_labels_for_high_authority": ["observed", "confirmed", "inferred"],
                "require_instruction_expected_use": True,
                "require_promotion_audit_reason": True,
                "exclude_labels": [
                    "disputed",
                    "superseded",
                    "inferred",
                    "generated",
                ],
            }
        )
    )
    cases.append(
        _canonical_contract(
            provenance_requirements={
                "min_labels_for_high_authority": ["observed", "confirmed"],
                "require_instruction_expected_use": True,
                "require_promotion_audit_reason": True,
                "exclude_labels": [],
            }
        )
    )
    cases.append(
        _canonical_contract(
            provenance_requirements={
                "min_labels_for_high_authority": ["observed", "confirmed"],
                "require_instruction_expected_use": True,
                "require_promotion_audit_reason": True,
                "exclude_labels": ["disputed", "superseded", "inferred"],
            }
        )
    )

    # Cross-field authority rules
    cases.append(
        _canonical_contract(
            permissions={
                "read": True,
                "write_back": False,
                "allow_high_authority": True,
            }
        )
    )
    cases.append(
        _canonical_contract(
            permissions={
                "read": True,
                "write_back": False,
                "allow_high_authority": False,
            },
            compiled_context_candidates=[
                {
                    "section": "identity",
                    "max_influence": "identity",
                    "require_promotion": True,
                }
            ],
        )
    )

    # Reserved authoritative source namespaces
    cases.append(
        _canonical_contract(
            authoritative_sources=[
                {
                    "unit_kind": "memory",
                    "source": "user-confirmed-open-brain-identity",
                }
            ]
        )
    )
    cases.append(
        _canonical_contract(
            authoritative_sources=[
                {
                    "unit_kind": "memory",
                    "source": "server-issued-promotion-ledger.v1",
                }
            ]
        )
    )

    # Profile security mismatches (when profile is carried on a full contract)
    for profile, flip in (
        ("compatibility", {"write_back": {"allowed": True,
                                          "requires_memory_write_proposal": True,
                                          "allowed_expected_uses": ["evidence"]}}),
        ("ios-mobile-readonly", {"permissions": {
            "read": True, "write_back": False, "allow_high_authority": True
        }}),
        ("bead-orchestrator", {"write_back": {
            "allowed": False,
            "requires_memory_write_proposal": True,
            "allowed_expected_uses": ["evidence"],
        }}),
        ("claude-wake-up", {"permissions": {
            "read": True, "write_back": False, "allow_high_authority": False
        }}),
    ):
        expected = profile_retrieval_contract(
            profile, work_object={"kind": "project", "id": "open-brain"}
        ).to_dict()
        mutated = copy.deepcopy(expected)
        mutated.update(flip)
        cases.append(mutated)

    # Valid profile-expanded full contracts (no profile key after expand)
    for profile in (
        "compatibility",
        "ios-mobile-readonly",
        "bead-orchestrator",
        "claude-wake-up",
    ):
        expanded = profile_retrieval_contract(
            profile, work_object={"kind": "project", "id": "open-brain"}
        ).to_dict()
        # Caller parser path: strip reserved profile-only sources for claude
        # when re-validating as caller-authored; keep as profile-built shape
        # for schema of full contracts without reserved prefixes when possible.
        cases.append(expanded)

    return cases


class TestO202SchemaParserParity:
    def test_mutation_corpus_schema_equals_parser(self) -> None:
        corpus = _mutation_corpus()
        assert len(corpus) >= 40
        divergences: list[str] = []
        for idx, raw in enumerate(corpus):
            schema_ok = _schema_ok(raw)
            parser_ok = _parser_ok(raw)
            if schema_ok != parser_ok:
                divergences.append(
                    f"case[{idx}] schema_ok={schema_ok} parser_ok={parser_ok} "
                    f"keys={sorted(raw.keys())}"
                )
        assert divergences == [], (
            f"{len(divergences)} schema/parser divergences:\n"
            + "\n".join(divergences[:20])
        )

    def test_nested_unknown_keys_rejected_by_parser(self) -> None:
        for mutator in (
            lambda d: d["permissions"].__setitem__("surprise", True),
            lambda d: d["provenance_requirements"].__setitem__("excluded_labels", []),
            lambda d: d["write_back"].__setitem__("extra", 1),
            lambda d: d["authoritative_sources"][0].__setitem__("extra", 1),
            lambda d: d["compiled_context_candidates"][0].__setitem__("extra", 1),
        ):
            raw = _canonical_contract()
            mutator(raw)
            with pytest.raises(RetrievalContractValidationError):
                parse_retrieval_contract(raw)


# ─── O2-03 reserved authoritative source namespaces ──────────────────────────


class TestO203AuthoritativeSourceTrust:
    def test_parser_rejects_reserved_source_namespaces(self) -> None:
        for source in (
            "user-confirmed-open-brain-identity",
            "server-issued-promotion-ledger.v1",
        ):
            raw = _canonical_contract(
                authoritative_sources=[{"unit_kind": "memory", "source": source}]
            )
            with pytest.raises(RetrievalContractValidationError) as exc:
                parse_retrieval_contract(raw)
            assert "authoritative_sources" in str(exc.value).lower() or (
                exc.value.field and "authoritative_sources" in exc.value.field
            )

    def test_parser_rejects_duplicate_unit_kinds(self) -> None:
        raw = _canonical_contract(
            authoritative_sources=[
                {"unit_kind": "memory", "source": "open-brain.memories"},
                {"unit_kind": "memory", "source": "open-brain.other"},
            ]
        )
        with pytest.raises(RetrievalContractValidationError):
            parse_retrieval_contract(raw)

    def test_schema_rejects_reserved_source_namespaces(self) -> None:
        for source in (
            "user-confirmed-open-brain-identity",
            "server-issued-promotion-ledger.v1",
        ):
            raw = _canonical_contract(
                authoritative_sources=[{"unit_kind": "memory", "source": source}]
            )
            assert _schema_ok(raw) is False

    def test_profile_may_use_server_constructed_sources_without_caller_parser(
        self,
    ) -> None:
        # Profiles are server-constructed and never pass through the caller
        # parser for their own source table. Reserved trust prefixes remain
        # forbidden on explicit caller contracts (covered above).
        contract = profile_retrieval_contract(
            "claude-wake-up",
            work_object={"kind": "project", "id": "proj"},
        )
        sources = {s.unit_kind: s.source for s in contract.authoritative_sources}
        assert sources["promoted_identity"] == "open-brain.promoted-identity"
        assert sources["promoted_constraint"] == "open-brain.promoted-constraints"
        unit = apply_retrieval_contract(
            [_memory(type="observation")],
            contract=contract,
        ).units[0]
        assert unit.authoritative_source == "open-brain.memories"

    @pytest.mark.asyncio
    async def test_public_search_seam_rejects_reserved_source(self) -> None:
        from open_brain.data_layer.interface import SearchResult

        mock_dl = AsyncMock()
        mock_dl.search = AsyncMock(
            return_value=SearchResult(results=[], total=0)
        )
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import search

            result = json.loads(
                await search(
                    query="x",
                    retrieval_contract=_canonical_contract(
                        authoritative_sources=[
                            {
                                "unit_kind": "memory",
                                "source": "user-confirmed-open-brain-identity",
                            }
                        ]
                    ),
                )
            )
        assert result["error"] == "invalid_retrieval_contract"


# ─── O2-04 get_context typed failures + binding ──────────────────────────────


class TestO204GetContextContract:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "contract",
        [
            {"version": "v0"},
            "{not json",
            [1, 2],
            {"version": "retrieval-contract.v1"},
        ],
    )
    async def test_invalid_contract_returns_typed_error(
        self, contract: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_dl = AsyncMock()
        mock_dl.get_context = AsyncMock(return_value={"sessions": [{"id": 1}]})
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import get_context

            result = json.loads(
                await get_context(retrieval_contract=contract)
            )
        assert result["error"] == "invalid_retrieval_contract"
        assert "message" in result

    @pytest.mark.asyncio
    async def test_project_mismatch_rejected(self) -> None:
        mock_dl = AsyncMock()
        mock_dl.get_context = AsyncMock(return_value={"sessions": [{"id": 1}]})
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import get_context

            result = json.loads(
                await get_context(
                    project="a",
                    retrieval_contract={
                        "profile": "claude-wake-up",
                        "work_object": {"kind": "project", "id": "b"},
                    },
                )
            )
        assert result["error"] == "invalid_retrieval_contract"
        assert "message" in result

    @pytest.mark.asyncio
    async def test_always_emits_empty_high_authority_units(self) -> None:
        mock_dl = AsyncMock()
        mock_dl.get_context = AsyncMock(return_value={"sessions": [{"id": 1}]})
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import get_context

            data = json.loads(
                await get_context(
                    project="proj",
                    retrieval_contract={
                        "profile": "claude-wake-up",
                        "work_object": {"kind": "project", "id": "proj"},
                    },
                )
            )
        assert data["high_authority_units"] == []
        assert data["retrieval_contract"]["permissions"]["allow_high_authority"] is True


# ─── O2-05 tautology / floor pins / budget rewrite helpers ───────────────────


class TestO205TestQuality:
    def test_preamble_length_divisible_by_four(self) -> None:
        # Floor-division token accounting requires len(preamble) % 4 == 0 so
        # token_estimate(preamble) + token_estimate(body) == token_estimate(preamble+body).
        preamble = session_start_preamble()
        assert len(preamble) % 4 == 0, (
            "SessionStart preamble length must stay divisible by 4; "
            f"got len={len(preamble)}. Changing this reintroduces a one-token "
            "overrun that fail-closes injection at the default budget."
        )

    def test_floor_constants_pinned_to_literals(self) -> None:
        assert set(PROVENANCE_FLOOR_MIN_LABELS) == {"observed", "confirmed"}
        assert set(PROVENANCE_FLOOR_EXCLUDE_LABELS) == {
            "disputed",
            "superseded",
            "inferred",
            "generated",
        }
