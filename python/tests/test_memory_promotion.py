"""Memory promotion ledger, grant verification, and retrieval elevation."""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest

from open_brain.data_layer.interface import Memory
from open_brain.retrieval_contract import (
    HIGH_AUTHORITY_INFLUENCES,
    apply_retrieval_contract,
    memory_to_retrieval_unit,
    profile_retrieval_contract,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_SCHEMA_PATH = PROJECT_ROOT / "python/src/open_brain/data_layer/postgres.py"
BOOTSTRAP_SCHEMA_PATH = PROJECT_ROOT / "scripts/bootstrap_test_schema.sql"
PROMOTION_DOC = PROJECT_ROOT / "docs/standards/memory-promotion.md"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

PROMOTION_SECRET = "promotion-grant-test-secret-at-least-32-chars"
OTHER_SECRET = "different-promotion-secret-also-32chars!!"
POLICY_VERSION = "memory-promotion.v1"
GRANT_AUD = "open-brain.promotion-grant"
GRANT_ISS = "open-brain.promotion-grant.v1"

ALL_LABELS = (
    "observed",
    "inferred",
    "generated",
    "confirmed",
    "disputed",
    "superseded",
)


def _make_pool(conn: AsyncMock) -> MagicMock:
    @asynccontextmanager
    async def fake_acquire():
        yield conn

    @asynccontextmanager
    async def fake_transaction(*_args: Any, **_kwargs: Any):
        yield

    conn.transaction = MagicMock(side_effect=fake_transaction)
    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


def _memory(
    *,
    memory_id: int = 42,
    memory_type: str = "identity",
    source_label: str = "inferred",
    expected_use: str = "evidence",
    ingestion_route: str = "mcp_save_memory",
    producer: str = "agent",
    source_ref: str = "agent-session:test:1",
    extra_metadata: dict[str, Any] | None = None,
) -> Memory:
    metadata: dict[str, Any] = {
        "ingestion_route": ingestion_route,
        "provenance": {
            "origin": {"producer": producer, "source_ref": source_ref},
            "source_label": source_label,
            "expected_use": expected_use,
            "epistemic_version": "epistemic-provenance.v1",
        },
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return Memory(
        id=memory_id,
        index_id=1,
        session_id=None,
        type=memory_type,
        title="title",
        subtitle=None,
        narrative=None,
        content="body",
        metadata=metadata,
        priority=0.5,
        stability="stable",
        access_count=0,
        last_accessed_at=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _attestation(
    memory_id: int = 42,
    producer: str = "agent",
    source_ref: str = "agent-session:test:1",
    ingestion_route: str = "mcp_save_memory",
) -> str:
    from open_brain.memory_promotion import compute_origin_attestation_digest

    return compute_origin_attestation_digest(
        memory_id=memory_id,
        producer=producer,
        source_ref=source_ref,
        ingestion_route=ingestion_route,
    )


def _mint_grant(
    *,
    memory_id: int = 42,
    from_label: str = "inferred",
    to_label: str = "confirmed",
    sub: str = "admin-user",
    actor: str = "admin-user",
    reason: str = "operator confirmed after review",
    evidence_refs: list[str] | None = None,
    policy_version: str = POLICY_VERSION,
    secret: str = PROMOTION_SECRET,
    algorithm: str = "HS256",
    aud: str = GRANT_AUD,
    iss: str = GRANT_ISS,
    jti: str | None = None,
    iat: datetime | None = None,
    exp: datetime | None = None,
    successor_memory_id: int | None = None,
    origin_attestation_digest: str | None = None,
    omit: set[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    from open_brain.memory_promotion import (
        compute_evidence_digest,
        compute_reason_digest,
    )

    now = datetime.now(UTC)
    refs = evidence_refs if evidence_refs is not None else ["evidence:note:1"]
    digest = origin_attestation_digest
    if digest is None and "origin_attestation_digest" not in (omit or set()):
        digest = _attestation(memory_id=memory_id)
    payload: dict[str, Any] = {
        "memory_id": memory_id,
        "from_label": from_label,
        "to_label": to_label,
        "sub": sub,
        "actor": actor,
        "reason_digest": compute_reason_digest(reason),
        "evidence_refs": refs,
        "evidence_digest": compute_evidence_digest(refs),
        "policy_version": policy_version,
        "successor_memory_id": successor_memory_id,
        "origin_attestation_digest": digest,
        "jti": jti or f"jti-{time.time_ns()}",
        "iat": iat or now,
        "exp": exp or (now + timedelta(minutes=5)),
        "aud": aud,
        "iss": iss,
    }
    if omit:
        for key in omit:
            payload.pop(key, None)
    if extra:
        payload.update(extra)
    return jwt.encode(payload, secret, algorithm=algorithm)


# ─── AC1: transition matrix + signed grant path ───────────────────────────────


class TestPromotionContractAndTransitions:
    def test_versioned_contract_and_docs_exist(self) -> None:
        from open_brain.memory_promotion import (
            MEMORY_PROMOTION_SCHEMA_VERSION,
            PROMOTION_GRANT_AUDIENCE,
            PROMOTION_GRANT_ISSUER,
            is_transition_allowed,
        )

        assert MEMORY_PROMOTION_SCHEMA_VERSION == "memory-promotion.v1"
        assert PROMOTION_GRANT_AUDIENCE == GRANT_AUD
        assert PROMOTION_GRANT_ISSUER == GRANT_ISS
        text = PROMOTION_DOC.read_text(encoding="utf-8")
        assert "memory-promotion.v1" in text
        assert "PROMOTION_GRANT_SECRET" in text
        assert "JWT_SECRET" in text
        assert "disputed" in text and "superseded" in text
        assert "automatic_rule_disabled" in text
        assert "ob --json provenance history" in text
        env = ENV_EXAMPLE.read_text(encoding="utf-8")
        assert "PROMOTION_GRANT_SECRET" in env
        assert is_transition_allowed("inferred", "confirmed")

    def test_complete_transition_allow_deny_matrix(self) -> None:
        from open_brain.memory_promotion import (
            ALLOWED_TRANSITIONS,
            is_transition_allowed,
            requires_signed_grant,
        )

        allowed = {
            ("inferred", "confirmed"),
            ("generated", "confirmed"),
            ("observed", "confirmed"),
            ("disputed", "confirmed"),
            ("inferred", "disputed"),
            ("generated", "disputed"),
            ("observed", "disputed"),
            ("confirmed", "disputed"),
            ("inferred", "superseded"),
            ("generated", "superseded"),
            ("observed", "superseded"),
            ("confirmed", "superseded"),
            ("disputed", "superseded"),
        }
        assert ALLOWED_TRANSITIONS == frozenset(allowed)
        for source in ALL_LABELS:
            for target in ALL_LABELS:
                expected = (source, target) in allowed
                assert is_transition_allowed(source, target) is expected, (
                    f"{source}->{target}"
                )
                if expected:
                    assert requires_signed_grant(source, target) is True

    def test_env_secret_fail_closed_and_no_jwt_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from open_brain import config as config_module
        from open_brain.memory_promotion import (
            PromotionGrantError,
            get_promotion_grant_secret,
            verify_promotion_grant,
        )

        monkeypatch.setenv("JWT_SECRET", "jwt-secret-must-not-be-used-for-promotion!!")
        monkeypatch.delenv("PROMOTION_GRANT_SECRET", raising=False)
        config_module._config = None
        with pytest.raises(PromotionGrantError) as missing:
            get_promotion_grant_secret()
        assert missing.value.code == "missing_grant_secret"

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", "too-short")
        config_module._config = None
        with pytest.raises(PromotionGrantError) as short:
            get_promotion_grant_secret()
        assert short.value.code == "missing_grant_secret"

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
        config_module._config = None
        assert get_promotion_grant_secret() == PROMOTION_SECRET

        grant = _mint_grant()
        # Wrong secret must fail closed even when JWT_SECRET would decode it.
        monkeypatch.setenv("PROMOTION_GRANT_SECRET", OTHER_SECRET)
        config_module._config = None
        with pytest.raises(PromotionGrantError):
            verify_promotion_grant(
                grant,
                memory_id=42,
                from_label="inferred",
                to_label="confirmed",
                actor="admin-user",
                reason="operator confirmed after review",
                evidence_refs=["evidence:note:1"],
            )


class TestPromotionGrantVerification:
    def test_valid_signed_grant_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from open_brain import config as config_module
        from open_brain.memory_promotion import verify_promotion_grant

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
        config_module._config = None
        grant = _mint_grant()
        claims = verify_promotion_grant(
            grant,
            memory_id=42,
            from_label="inferred",
            to_label="confirmed",
            actor="admin-user",
            reason="operator confirmed after review",
            evidence_refs=["evidence:note:1"],
        )
        assert claims.jti
        assert claims.memory_id == 42
        assert claims.from_label == "inferred"
        assert claims.to_label == "confirmed"

    @pytest.mark.parametrize(
        "mutate,code",
        [
            (lambda: _mint_grant(secret=OTHER_SECRET), "grant_invalid"),
            (lambda: _mint_grant(algorithm="HS384"), "grant_invalid"),
            (lambda: _mint_grant(aud="wrong-audience"), "grant_invalid"),
            (lambda: _mint_grant(iss="wrong-issuer"), "grant_invalid"),
            (lambda: _mint_grant(sub="other-user"), "subject_mismatch"),
            (lambda: _mint_grant(actor="other-actor"), "actor_mismatch"),
            (lambda: _mint_grant(from_label="generated"), "transition_mismatch"),
            (
                lambda: _mint_grant(
                    iat=datetime.now(UTC) - timedelta(minutes=5),
                    exp=datetime.now(UTC) - timedelta(minutes=1),
                ),
                "grant_expired",
            ),
            (
                lambda: _mint_grant(
                    iat=datetime.now(UTC) + timedelta(minutes=10),
                    exp=datetime.now(UTC) + timedelta(minutes=20),
                ),
                "grant_future_iat",
            ),
            (lambda: _mint_grant(omit={"jti"}), "grant_invalid"),
            (lambda: _mint_grant(omit={"memory_id"}), "grant_invalid"),
            (
                lambda: _mint_grant(extra={"evidence_refs": "not-a-list"}),
                "malformed_evidence",
            ),
        ],
    )
    def test_grant_rejection_matrix(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mutate: Any,
        code: str,
    ) -> None:
        from open_brain import config as config_module
        from open_brain.memory_promotion import (
            PromotionGrantError,
            verify_promotion_grant,
        )

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
        config_module._config = None
        token = mutate()
        with pytest.raises(PromotionGrantError) as exc:
            verify_promotion_grant(
                token,
                memory_id=42,
                from_label="inferred",
                to_label="confirmed",
                actor="admin-user",
                reason="operator confirmed after review",
                evidence_refs=["evidence:note:1"],
            )
        assert exc.value.code == code
        assert PROMOTION_SECRET not in str(exc.value)
        assert "eyJ" not in str(exc.value)

    def test_tampered_payload_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from open_brain import config as config_module
        from open_brain.memory_promotion import (
            PromotionGrantError,
            verify_promotion_grant,
        )

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
        config_module._config = None
        token = _mint_grant()
        head, payload, sig = token.split(".")
        # Flip one character in the payload segment.
        flipped = ("A" if payload[0] != "A" else "B") + payload[1:]
        tampered = f"{head}.{flipped}.{sig}"
        with pytest.raises(PromotionGrantError) as exc:
            verify_promotion_grant(
                tampered,
                memory_id=42,
                from_label="inferred",
                to_label="confirmed",
                actor="admin-user",
                reason="operator confirmed after review",
                evidence_refs=["evidence:note:1"],
            )
        assert exc.value.code == "grant_invalid"


# ─── AC2/AC3: ledger, dispute/supersession, atomicity ─────────────────────────


class TestPromotionLedgerAndGraph:
    @pytest.mark.asyncio
    async def test_accepted_and_rejected_attempts_append_immutable_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from open_brain import config as config_module
        from open_brain.memory_promotion import (
            PromotionAttemptParams,
            attempt_memory_promotion,
        )

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
        config_module._config = None

        conn = AsyncMock()
        memory_row = {
            "id": 42,
            "metadata": {
                "ingestion_route": "mcp_save_memory",
                "provenance": {
                    "origin": {"producer": "agent", "source_ref": "agent-session:test:1"},
                    "source_label": "inferred",
                    "expected_use": "evidence",
                    "epistemic_version": "epistemic-provenance.v1",
                },
            },
        }
        conn.fetchrow = AsyncMock(
            side_effect=[
                memory_row,
                {
                    "id": 1,
                    "memory_id": 42,
                    "actor": "admin-user",
                    "source_state": "inferred",
                    "target_state": "confirmed",
                    "reason": "operator confirmed after review",
                    "evidence_refs": ["evidence:note:1"],
                    "policy_version": POLICY_VERSION,
                    "rule_version": None,
                    "grant_jti": "jti-1",
                    "grant_digest": "abc",
                    "decision": "accepted",
                    "outcome": "promoted",
                    "rejection_code": None,
                    "relationship_id": None,
                    "created_at": datetime.now(UTC),
                },
                memory_row,
                {
                    "id": 2,
                    "memory_id": 42,
                    "actor": "admin-user",
                    "source_state": "inferred",
                    "target_state": "confirmed",
                    "reason": "bad grant",
                    "evidence_refs": [],
                    "policy_version": POLICY_VERSION,
                    "rule_version": None,
                    "grant_jti": None,
                    "grant_digest": None,
                    "decision": "rejected",
                    "outcome": "rejected",
                    "rejection_code": "grant_invalid",
                    "relationship_id": None,
                    "created_at": datetime.now(UTC),
                },
            ]
        )
        conn.execute = AsyncMock()
        conn.fetchval = AsyncMock(return_value=None)
        pool = _make_pool(conn)

        with patch("open_brain.memory_promotion.get_pool", new_callable=AsyncMock, return_value=pool):
            accepted = await attempt_memory_promotion(
                PromotionAttemptParams(
                    memory_id=42,
                    target_state="confirmed",
                    reason="operator confirmed after review",
                    evidence_refs=["evidence:note:1"],
                    actor="admin-user",
                    promotion_grant=_mint_grant(jti="jti-1"),
                )
            )
            assert accepted.decision == "accepted"
            assert accepted.event.grant_jti == "jti-1"
            assert accepted.event.grant_digest
            assert PROMOTION_SECRET not in json.dumps(accepted.to_dict())

            rejected = await attempt_memory_promotion(
                PromotionAttemptParams(
                    memory_id=42,
                    target_state="confirmed",
                    reason="bad grant",
                    evidence_refs=[],
                    actor="admin-user",
                    promotion_grant="not-a-jwt",
                )
            )
            assert rejected.decision == "rejected"
            assert rejected.rejection_code == "grant_invalid"
            assert rejected.event is not None

        insert_sql = " ".join(
            str(call.args[0]) for call in conn.fetchrow.await_args_list if call.args
        )
        assert "INSERT INTO memory_promotion_events" in insert_sql or any(
            "INSERT INTO memory_promotion_events" in str(c.args[0])
            for c in conn.fetchrow.await_args_list
            if c.args
        )

    @pytest.mark.asyncio
    async def test_replay_rejected_and_logged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from open_brain import config as config_module
        from open_brain.memory_promotion import (
            PromotionAttemptParams,
            attempt_memory_promotion,
        )

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
        config_module._config = None
        grant = _mint_grant(jti="replay-jti")

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            side_effect=[
                {
                    "id": 42,
                    "metadata": {
                        "ingestion_route": "mcp_save_memory",
                        "provenance": {
                            "origin": {
                                "producer": "agent",
                                "source_ref": "agent-session:test:1",
                            },
                            "source_label": "inferred",
                            "expected_use": "evidence",
                            "epistemic_version": "epistemic-provenance.v1",
                        },
                    },
                },
                {
                    "id": 9,
                    "memory_id": 42,
                    "actor": "admin-user",
                    "source_state": "inferred",
                    "target_state": "confirmed",
                    "reason": "operator confirmed after review",
                    "evidence_refs": ["evidence:note:1"],
                    "policy_version": POLICY_VERSION,
                    "rule_version": None,
                    "grant_jti": "replay-jti",
                    "grant_digest": "x",
                    "decision": "rejected",
                    "outcome": "rejected",
                    "rejection_code": "grant_replay",
                    "relationship_id": None,
                    "created_at": datetime.now(UTC),
                },
            ]
        )
        conn.fetchval = AsyncMock(return_value=1)  # existing jti
        pool = _make_pool(conn)
        with patch("open_brain.memory_promotion.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await attempt_memory_promotion(
                PromotionAttemptParams(
                    memory_id=42,
                    target_state="confirmed",
                    reason="operator confirmed after review",
                    evidence_refs=["evidence:note:1"],
                    actor="admin-user",
                    promotion_grant=grant,
                )
            )
        assert result.decision == "rejected"
        assert result.rejection_code == "grant_replay"

    @pytest.mark.asyncio
    async def test_automatic_rule_disabled_by_default(self) -> None:
        from open_brain.memory_promotion import (
            PromotionAttemptParams,
            attempt_memory_promotion,
        )

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            side_effect=[
                {
                    "id": 7,
                    "metadata": {
                        "provenance": {
                            "origin": {
                                "producer": "agent",
                                "source_ref": "agent-session:test:1",
                            },
                            "source_label": "inferred",
                            "expected_use": "evidence",
                            "epistemic_version": "epistemic-provenance.v1",
                        }
                    },
                },
                {
                    "id": 3,
                    "memory_id": 7,
                    "actor": "admin-user",
                    "source_state": "inferred",
                    "target_state": "confirmed",
                    "reason": "rule path",
                    "evidence_refs": ["evidence:1"],
                    "policy_version": POLICY_VERSION,
                    "rule_version": "repetition.v0",
                    "grant_jti": None,
                    "grant_digest": None,
                    "decision": "rejected",
                    "outcome": "rejected",
                    "rejection_code": "automatic_rule_disabled",
                    "relationship_id": None,
                    "created_at": datetime.now(UTC),
                },
            ]
        )
        conn.fetchval = AsyncMock(return_value=None)
        pool = _make_pool(conn)
        with patch("open_brain.memory_promotion.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await attempt_memory_promotion(
                PromotionAttemptParams(
                    memory_id=7,
                    target_state="confirmed",
                    reason="rule path",
                    evidence_refs=["evidence:1"],
                    actor="admin-user",
                    authorization_mode="automatic_rule",
                    rule_version="repetition.v0",
                )
            )
        assert result.rejection_code == "automatic_rule_disabled"

    @pytest.mark.asyncio
    async def test_supersession_graph_guards(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from open_brain import config as config_module
        from open_brain.memory_promotion import (
            PromotionAttemptParams,
            attempt_memory_promotion,
        )

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
        config_module._config = None

        cases = [
            (
                PromotionAttemptParams(
                    memory_id=10,
                    target_state="superseded",
                    reason="self",
                    evidence_refs=["e"],
                    actor="admin-user",
                    successor_memory_id=10,
                    promotion_grant=_mint_grant(
                        memory_id=10,
                        from_label="confirmed",
                        to_label="superseded",
                        successor_memory_id=10,
                        reason="self",
                        evidence_refs=["e"],
                        origin_attestation_digest=_attestation(memory_id=10),
                    ),
                ),
                "self_supersession",
            ),
            (
                PromotionAttemptParams(
                    memory_id=10,
                    target_state="superseded",
                    reason="missing",
                    evidence_refs=["e"],
                    actor="admin-user",
                    successor_memory_id=None,
                    promotion_grant=_mint_grant(
                        memory_id=10,
                        from_label="confirmed",
                        to_label="superseded",
                        successor_memory_id=None,
                        reason="missing",
                        evidence_refs=["e"],
                        origin_attestation_digest=_attestation(memory_id=10),
                    ),
                ),
                "missing_successor",
            ),
        ]
        for params, code in cases:
            conn = AsyncMock()
            conn.fetchrow = AsyncMock(
                side_effect=[
                    {
                        "id": params.memory_id,
                        "metadata": {
                            "ingestion_route": "mcp_save_memory",
                            "provenance": {
                                "origin": {
                                    "producer": "agent",
                                    "source_ref": "agent-session:test:1",
                                },
                                "source_label": "confirmed",
                                "expected_use": "instruction",
                                "epistemic_version": "epistemic-provenance.v1",
                            }
                        },
                    },
                    {
                        "id": 1,
                        "memory_id": params.memory_id,
                        "actor": "admin-user",
                        "source_state": "confirmed",
                        "target_state": "superseded",
                        "reason": params.reason,
                        "evidence_refs": params.evidence_refs,
                        "policy_version": POLICY_VERSION,
                        "rule_version": None,
                        "grant_jti": "x",
                        "grant_digest": "y",
                        "origin_attestation_digest": _attestation(memory_id=10),
                        "decision": "rejected",
                        "outcome": "rejected",
                        "rejection_code": code,
                        "relationship_id": None,
                        "created_at": datetime.now(UTC),
                    },
                ]
            )
            conn.fetchval = AsyncMock(return_value=None)
            pool = _make_pool(conn)
            with patch(
                "open_brain.memory_promotion.get_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ):
                result = await attempt_memory_promotion(params)
            assert result.rejection_code == code

    @pytest.mark.asyncio
    async def test_failed_transaction_does_not_partially_elevate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from open_brain import config as config_module
        from open_brain.memory_promotion import (
            PromotionAttemptParams,
            attempt_memory_promotion,
        )

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
        config_module._config = None

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": 42,
                "metadata": {
                    "ingestion_route": "mcp_save_memory",
                    "provenance": {
                        "origin": {"producer": "agent", "source_ref": "agent-session:test:1"},
                        "source_label": "inferred",
                        "expected_use": "evidence",
                        "epistemic_version": "epistemic-provenance.v1",
                    },
                },
            }
        )
        conn.execute = AsyncMock(side_effect=RuntimeError("boom"))
        conn.fetchval = AsyncMock(return_value=None)
        pool = _make_pool(conn)
        with patch("open_brain.memory_promotion.get_pool", new_callable=AsyncMock, return_value=pool):
            with pytest.raises(RuntimeError, match="boom"):
                await attempt_memory_promotion(
                    PromotionAttemptParams(
                        memory_id=42,
                        target_state="confirmed",
                        reason="operator confirmed after review",
                        evidence_refs=["evidence:note:1"],
                        actor="admin-user",
                        promotion_grant=_mint_grant(),
                    )
                )


# ─── AC5: no silent escalation ────────────────────────────────────────────────


class TestNoSilentEscalation:
    def test_no_silent_escalation_matrix(self) -> None:
        from open_brain.retrieval_contract import (
            inspect_promotion,
            memory_to_retrieval_unit,
            profile_retrieval_contract,
        )

        cases = [
            {"type": "learning"},
            {"session_learning_review": {"decision": "accept"}},
            {"repetition_count": 100},
            {"ingestion_route": "migration", "migration_origin": "legacy"},
            {"memory_write_judge": {"decision": "ALLOW"}},
            {
                "retrieval_promotion": {
                    "state": "promoted",
                    "audit_reason": "forged",
                }
            },
            {"model_generated": True, "category": "identity"},
        ]
        contract = profile_retrieval_contract(
            "claude-wake-up", work_object={"kind": "project", "id": "p"}
        )
        for extra in cases:
            memory = _memory(
                source_label="confirmed",
                expected_use="instruction",
                memory_type="identity",
                extra_metadata=extra,
            )
            state, reason = inspect_promotion(memory.metadata)
            assert state != "promoted"
            assert reason
            unit = memory_to_retrieval_unit(
                memory, contract, requested_influence="identity"
            )
            assert unit.effective_influence == "evidence"

    def test_secrets_never_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        from open_brain.memory_promotion import redact_secrets

        raw = f"token={_mint_grant()} secret={PROMOTION_SECRET}"
        with caplog.at_level(logging.INFO):
            logging.getLogger("open_brain.memory_promotion").info(
                "attempt %s", redact_secrets(raw)
            )
        joined = " ".join(record.getMessage() for record in caplog.records)
        assert PROMOTION_SECRET not in joined
        assert "eyJ" not in joined


# ─── Retrieval elevation via ledger projection ────────────────────────────────


class TestRetrievalPromotionProjection:
    def _projection(
        self,
        *,
        memory_id: int = 42,
        target_state: str = "confirmed",
        outcome: str = "promoted",
        decision: str = "accepted",
        source_state: str = "inferred",
        origin_attestation_digest: str | None = None,
    ) -> Any:
        from open_brain.memory_promotion import PromotionProjection

        return PromotionProjection(
            memory_id=memory_id,
            event_id=11,
            decision=decision,  # type: ignore[arg-type]
            source_state=source_state,
            target_state=target_state,
            outcome=outcome,
            is_current=True,
            policy_version=POLICY_VERSION,
            grant_jti_digest="digest",
            audit_reason="ledger_promotion_grant",
            audit_source="memory_promotion_events",
            origin_attestation_digest=(
                origin_attestation_digest
                if origin_attestation_digest is not None
                else _attestation(memory_id=memory_id)
            ),
        )

    def test_direct_call_without_projection_fail_closed(self) -> None:
        memory = _memory(
            source_label="confirmed",
            expected_use="instruction",
            memory_type="identity",
        )
        contract = profile_retrieval_contract(
            "claude-wake-up", work_object={"kind": "project", "id": "p"}
        )
        unit = memory_to_retrieval_unit(
            memory, contract, requested_influence="identity"
        )
        assert unit.effective_influence == "evidence"
        assert unit.promotion_state != "promoted"

    def test_valid_ledger_projection_elevates_identity(self) -> None:
        memory = _memory(
            source_label="confirmed",
            expected_use="instruction",
            memory_type="identity",
        )
        contract = profile_retrieval_contract(
            "claude-wake-up", work_object={"kind": "project", "id": "p"}
        )
        unit = memory_to_retrieval_unit(
            memory,
            contract,
            requested_influence="identity",
            promotion_projection={42: self._projection()},
        )
        assert unit.effective_influence == "identity"
        assert unit.promotion_state == "promoted"
        assert unit.section == "identity"
        assert "ledger" in unit.audit_reason or unit.audit_reason == "ledger_promotion_grant"
        assert unit.authoritative_source == "open-brain.promoted-identity"

    def test_missing_attestation_stays_evidence(self) -> None:
        memory = _memory(
            source_label="confirmed",
            expected_use="instruction",
            memory_type="identity",
            ingestion_route="",
            producer="unknown",
        )
        # Force empty route/unknown producer
        memory.metadata["ingestion_route"] = ""
        memory.metadata["provenance"]["origin"]["producer"] = "unknown"
        contract = profile_retrieval_contract(
            "claude-wake-up", work_object={"kind": "project", "id": "p"}
        )
        unit = memory_to_retrieval_unit(
            memory,
            contract,
            requested_influence="identity",
            promotion_projection={42: self._projection()},
        )
        assert unit.effective_influence == "evidence"

    def test_current_grant_metadata_mismatch_fail_closed(self) -> None:
        memory = _memory(
            source_label="inferred",  # metadata not confirmed
            expected_use="evidence",
            memory_type="identity",
        )
        contract = profile_retrieval_contract(
            "claude-wake-up", work_object={"kind": "project", "id": "p"}
        )
        unit = memory_to_retrieval_unit(
            memory,
            contract,
            requested_influence="identity",
            promotion_projection={42: self._projection()},
        )
        assert unit.effective_influence == "evidence"
        assert "mismatch" in unit.audit_reason

    def test_dispute_and_supersession_win(self) -> None:
        memory = _memory(
            source_label="disputed",
            expected_use="evidence",
            memory_type="identity",
        )
        contract = profile_retrieval_contract(
            "claude-wake-up", work_object={"kind": "project", "id": "p"}
        )
        for outcome, label in (("disputed", "disputed"), ("superseded", "superseded")):
            memory.metadata["provenance"]["source_label"] = label
            unit = memory_to_retrieval_unit(
                memory,
                contract,
                requested_influence="identity",
                promotion_projection={
                    42: self._projection(target_state=label, outcome=outcome)
                },
            )
            assert unit.effective_influence == "evidence"
            assert unit.promotion_state == label

    def test_policy_section_absent_from_wake_up_never_elevates(self) -> None:
        memory = _memory(
            source_label="confirmed",
            expected_use="instruction",
            memory_type="policy",
        )
        contract = profile_retrieval_contract(
            "claude-wake-up", work_object={"kind": "project", "id": "p"}
        )
        unit = memory_to_retrieval_unit(
            memory,
            contract,
            requested_influence="policy",
            promotion_projection={42: self._projection()},
        )
        assert unit.effective_influence == "evidence"
        assert unit.section != "system_instruction"

    def test_apply_with_projection_allows_ha_units(self) -> None:
        memory = _memory(
            source_label="confirmed",
            expected_use="instruction",
            memory_type="constraint",
        )
        result = apply_retrieval_contract(
            [memory],
            contract={"profile": "claude-wake-up", "work_object": {"kind": "project", "id": "p"}},
            promotion_projection={42: self._projection()},
        )
        assert any(u.effective_influence in HIGH_AUTHORITY_INFLUENCES for u in result.units)

    def test_forged_metadata_still_untrusted_with_projection_absent(self) -> None:
        memory = _memory(
            source_label="confirmed",
            expected_use="instruction",
            memory_type="identity",
            extra_metadata={
                "retrieval_promotion": {
                    "state": "promoted",
                    "audit_reason": "forged",
                },
                "memory_write_judge": {"decision": "ALLOW"},
            },
        )
        contract = profile_retrieval_contract(
            "claude-wake-up", work_object={"kind": "project", "id": "p"}
        )
        unit = memory_to_retrieval_unit(
            memory, contract, requested_influence="identity"
        )
        assert unit.effective_influence == "evidence"


# ─── MCP / CLI / schema ───────────────────────────────────────────────────────


class TestPromotionMcpCliSchema:
    @pytest.mark.asyncio
    async def test_admin_tool_gated_and_api_key_denied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from open_brain import config as config_module
        from open_brain.server import (
            _ADMIN_TOOLS,
            _current_scopes,
            _current_user_id,
            _is_api_key_auth,
            ScopeDeniedError,
            mcp,
            promote_memory_authority,
        )

        monkeypatch.setenv("PROMOTION_ADMIN_USERS", "admin-user")
        config_module._config = None

        assert "promote_memory_authority" in _ADMIN_TOOLS
        assert "get_memory_promotion_history" not in _ADMIN_TOOLS

        memory_token = _current_scopes.set(("memory",))
        try:
            names = {tool.name for tool in await mcp.list_tools()}
            assert "promote_memory_authority" not in names
            assert "get_memory_promotion_history" in names
        finally:
            _current_scopes.reset(memory_token)

        admin_token = _current_scopes.set(("memory", "admin"))
        user_token = _current_user_id.set("admin-user")
        api_false = _is_api_key_auth.set(False)
        try:
            names = {tool.name for tool in await mcp.list_tools()}
            assert "promote_memory_authority" in names
        finally:
            _current_scopes.reset(admin_token)
            _current_user_id.reset(user_token)
            _is_api_key_auth.reset(api_false)

        api_token = _is_api_key_auth.set(True)
        scope_token = _current_scopes.set(("memory", "evolution"))
        try:
            with pytest.raises((ScopeDeniedError, PermissionError, ValueError)) as exc:
                await promote_memory_authority(
                    memory_id=1,
                    target_state="disputed",
                    reason="no",
                    evidence_refs=["e"],
                )
            assert "admin" in str(exc.value).lower() or "api" in str(exc.value).lower()
        finally:
            _current_scopes.reset(scope_token)
            _is_api_key_auth.reset(api_token)

    @pytest.mark.asyncio
    async def test_history_tool_readonly_under_memory_scope(self) -> None:
        from open_brain.server import (
            _current_scopes,
            get_memory_promotion_history,
        )

        with patch(
            "open_brain.server.list_memory_promotion_history",
            new_callable=AsyncMock,
            return_value=[{"id": 1, "memory_id": 9, "decision": "rejected"}],
        ):
            token = _current_scopes.set(("memory",))
            try:
                payload = json.loads(await get_memory_promotion_history(memory_id=9))
            finally:
                _current_scopes.reset(token)
        assert payload["memory_id"] == 9
        assert payload["events"][0]["decision"] == "rejected"

    def test_cli_provenance_history_smoke(self) -> None:
        from open_brain.cli.main import _build_parser

        parser = _build_parser()
        args = parser.parse_args(
            ["--json", "provenance", "history", "123"]
        )
        assert args.provenance_command == "history"
        assert args.memory_id == 123

    def test_schema_parity_ddl_present(self) -> None:
        runtime = RUNTIME_SCHEMA_PATH.read_text(encoding="utf-8")
        bootstrap = BOOTSTRAP_SCHEMA_PATH.read_text(encoding="utf-8")
        for blob in (runtime, bootstrap):
            assert "CREATE TABLE IF NOT EXISTS memory_promotion_events" in blob
            assert "grant_jti" in blob
            assert "memory_promotion_events_no_update" in blob or "append-only" in blob.lower()
            assert "UNIQUE" in blob and "grant_jti" in blob


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_db_append_only_and_replay_unique(
    integration_database_url: str,
) -> None:
    """Real-DB evidence: immutability triggers + grant_jti uniqueness.

    Uses a per-run UUID jti so consecutive runs on the same append-only
    disposable DB do not collide. Duplicate accepted jti still fails in-test.
    """
    import asyncpg

    from open_brain.data_layer import postgres as pg_module

    run_jti = f"integ-jti-{uuid.uuid4().hex}"
    await pg_module.close_pool()
    pool = await pg_module.get_pool()
    try:
        async with pool.acquire() as conn:
            memory_id = await conn.fetchval(
                """
                INSERT INTO memories (index_id, type, content, metadata)
                SELECT id, 'fact', $1, '{}'::jsonb
                FROM memory_indexes ORDER BY id LIMIT 1
                RETURNING id
                """,
                f"promotion integ {run_jti}",
            )
            await conn.execute(
                """
                INSERT INTO memory_promotion_events (
                    memory_id, actor, source_state, target_state, reason,
                    evidence_refs, policy_version, grant_jti, grant_digest,
                    decision, outcome
                ) VALUES (
                    $1, 'admin', 'inferred', 'confirmed', 'ok',
                    '[]'::jsonb, $2, $3, 'digest',
                    'accepted', 'promoted'
                )
                """,
                memory_id,
                POLICY_VERSION,
                run_jti,
            )
            with pytest.raises(Exception):
                await conn.execute(
                    "UPDATE memory_promotion_events SET reason = 'x' WHERE grant_jti = $1",
                    run_jti,
                )
            with pytest.raises(Exception):
                await conn.execute(
                    "DELETE FROM memory_promotion_events WHERE grant_jti = $1",
                    run_jti,
                )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    """
                    INSERT INTO memory_promotion_events (
                        memory_id, actor, source_state, target_state, reason,
                        evidence_refs, policy_version, grant_jti, grant_digest,
                        decision, outcome
                    ) VALUES (
                        $1, 'admin', 'inferred', 'confirmed', 'replay',
                        '[]'::jsonb, $2, $3, 'digest',
                        'accepted', 'promoted'
                    )
                    """,
                    memory_id,
                    POLICY_VERSION,
                    run_jti,
                )
    finally:
        await pg_module.close_pool()
