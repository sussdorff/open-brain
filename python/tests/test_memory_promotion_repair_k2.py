"""Kimi round-2 final repairs for open-brain-ekn.5 (K2-01..K2-04)."""

from __future__ import annotations

import math
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest

from open_brain.data_layer.interface import Memory
from open_brain.retrieval_contract import (
    memory_to_retrieval_unit,
    profile_retrieval_contract,
)


PROMOTION_SECRET = "promotion-grant-test-secret-at-least-32-chars"
POLICY_VERSION = "memory-promotion.v1"
GRANT_AUD = "open-brain.promotion-grant"
GRANT_ISS = "open-brain.promotion-grant.v1"
REASON = "operator confirmed after review"
EVIDENCE = ["evidence:note:1"]


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


def _run_id() -> str:
    return uuid.uuid4().hex


def _mint(
    *,
    memory_id: int = 42,
    from_label: str = "inferred",
    to_label: str = "confirmed",
    sub: str = "admin-user",
    actor: str = "admin-user",
    reason: str = REASON,
    evidence_refs: list[str] | None = None,
    successor_memory_id: int | None = None,
    origin_attestation_digest: str | None = None,
    iat: datetime | None = None,
    exp: datetime | None = None,
    jti: str | None = None,
    secret: str = PROMOTION_SECRET,
    extra: dict[str, Any] | None = None,
    omit: set[str] | None = None,
) -> str:
    from open_brain.memory_promotion import (
        compute_evidence_digest,
        compute_origin_attestation_digest,
        compute_reason_digest,
    )

    now = datetime.now(UTC)
    refs = evidence_refs if evidence_refs is not None else list(EVIDENCE)
    digest = origin_attestation_digest
    if digest is None and "origin_attestation_digest" not in (omit or set()):
        digest = compute_origin_attestation_digest(
            memory_id=memory_id,
            producer="agent",
            source_ref="agent-session:test:1",
            ingestion_route="mcp_save_memory",
        )
    payload: dict[str, Any] = {
        "memory_id": memory_id,
        "from_label": from_label,
        "to_label": to_label,
        "sub": sub,
        "actor": actor,
        "reason_digest": compute_reason_digest(reason),
        "evidence_refs": refs,
        "evidence_digest": compute_evidence_digest(refs),
        "policy_version": POLICY_VERSION,
        "successor_memory_id": successor_memory_id,
        "origin_attestation_digest": digest,
        "jti": jti or f"jti-{_run_id()}",
        "iat": iat or now,
        "exp": exp or (now + timedelta(minutes=5)),
        "aud": GRANT_AUD,
        "iss": GRANT_ISS,
    }
    if omit:
        for key in omit:
            payload.pop(key, None)
    if extra:
        payload.update(extra)
    return jwt.encode(payload, secret, algorithm="HS256")


def _memory_meta(
    *,
    source_label: str = "inferred",
    source_ref: str = "agent-session:test:1",
) -> dict[str, Any]:
    return {
        "ingestion_route": "mcp_save_memory",
        "provenance": {
            "origin": {"producer": "agent", "source_ref": source_ref},
            "source_label": source_label,
            "expected_use": "evidence" if source_label != "confirmed" else "instruction",
            "epistemic_version": "epistemic-provenance.v1",
        },
    }


# ─── K2-01 native JSONB arguments ─────────────────────────────────────────────


class TestK201NativeJsonbArguments:
    @pytest.mark.asyncio
    async def test_promotion_passes_native_jsonb_python_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from open_brain import config as config_module
        from open_brain.memory_promotion import (
            PromotionAttemptParams,
            attempt_memory_promotion,
        )

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
        config_module._config = None

        captured: dict[str, Any] = {}
        grant = _mint(jti=f"native-{_run_id()}")

        async def fetchrow(query: str, *args: Any) -> Any:
            if "FROM memories" in query and "FOR UPDATE" in query:
                return {"id": 42, "metadata": _memory_meta()}
            if "INSERT INTO memory_promotion_events" in query:
                captured["evidence_refs"] = args[5]
                return {
                    "id": 1,
                    "memory_id": 42,
                    "actor": "admin-user",
                    "source_state": "inferred",
                    "target_state": "confirmed",
                    "reason": REASON,
                    "evidence_refs": EVIDENCE,
                    "policy_version": POLICY_VERSION,
                    "rule_version": None,
                    "grant_jti": "x",
                    "grant_digest": "y",
                    "origin_attestation_digest": "0" * 64,
                    "decision": "accepted",
                    "outcome": "promoted",
                    "rejection_code": None,
                    "relationship_id": None,
                    "created_at": datetime.now(UTC),
                }
            return None

        async def execute(query: str, *args: Any) -> str:
            if "UPDATE memories" in query:
                captured["metadata"] = args[1]
            return "UPDATE 1"

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=fetchrow)
        conn.execute = AsyncMock(side_effect=execute)
        conn.fetchval = AsyncMock(return_value=None)
        with patch(
            "open_brain.memory_promotion.get_pool",
            new_callable=AsyncMock,
            return_value=_make_pool(conn),
        ):
            result = await attempt_memory_promotion(
                PromotionAttemptParams(
                    memory_id=42,
                    target_state="confirmed",
                    reason=REASON,
                    evidence_refs=EVIDENCE,
                    actor="admin-user",
                    promotion_grant=grant,
                )
            )
        assert result.decision == "accepted"
        assert isinstance(captured["metadata"], dict)
        assert not isinstance(captured["metadata"], str)
        assert isinstance(captured["evidence_refs"], list)
        assert not isinstance(captured["evidence_refs"], str)
        assert all(isinstance(item, str) for item in captured["evidence_refs"])

    @pytest.mark.asyncio
    async def test_supersession_relationship_metadata_is_native_mapping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from open_brain import config as config_module
        from open_brain.memory_promotion import (
            PromotionAttemptParams,
            attempt_memory_promotion,
            compute_origin_attestation_digest,
        )

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
        config_module._config = None
        digest = compute_origin_attestation_digest(
            memory_id=10,
            producer="agent",
            source_ref="agent-session:test:1",
            ingestion_route="mcp_save_memory",
        )
        grant = _mint(
            memory_id=10,
            from_label="confirmed",
            to_label="superseded",
            successor_memory_id=99,
            origin_attestation_digest=digest,
        )
        captured: dict[str, Any] = {}

        async def fetchrow(query: str, *args: Any) -> Any:
            if "FROM memories" in query and "FOR UPDATE" in query:
                return {"id": 10, "metadata": _memory_meta(source_label="confirmed")}
            if "FROM memories" in query:
                return {"id": 99, "metadata": {"provenance": {"source_label": "observed"}}}
            if "INSERT INTO memory_promotion_events" in query:
                return {
                    "id": 2,
                    "memory_id": 10,
                    "actor": "admin-user",
                    "source_state": "confirmed",
                    "target_state": "superseded",
                    "reason": REASON,
                    "evidence_refs": EVIDENCE,
                    "policy_version": POLICY_VERSION,
                    "rule_version": None,
                    "grant_jti": "s",
                    "grant_digest": "d",
                    "origin_attestation_digest": digest,
                    "decision": "accepted",
                    "outcome": "superseded",
                    "rejection_code": None,
                    "relationship_id": 7,
                    "created_at": datetime.now(UTC),
                }
            return None

        async def fetchval(query: str, *args: Any) -> Any:
            if "INSERT INTO memory_relationships" in query:
                captured["relationship_metadata"] = args[3]
                return 7
            return None

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=fetchrow)
        conn.fetchval = AsyncMock(side_effect=fetchval)
        conn.execute = AsyncMock(return_value="UPDATE 1")
        with patch(
            "open_brain.memory_promotion.get_pool",
            new_callable=AsyncMock,
            return_value=_make_pool(conn),
        ):
            result = await attempt_memory_promotion(
                PromotionAttemptParams(
                    memory_id=10,
                    target_state="superseded",
                    reason=REASON,
                    evidence_refs=EVIDENCE,
                    actor="admin-user",
                    promotion_grant=grant,
                    successor_memory_id=99,
                )
            )
        assert result.decision == "accepted"
        assert isinstance(captured["relationship_metadata"], dict)
        assert not isinstance(captured["relationship_metadata"], str)


# ─── K2-02 replay before transition validation ────────────────────────────────


class TestK202ReplayBeforeTransition:
    def test_authenticate_phase_exposes_verified_jti(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from open_brain import config as config_module
        from open_brain.memory_promotion import (
            PromotionGrantError,
            authenticate_promotion_grant,
        )

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
        config_module._config = None
        jti = f"auth-{_run_id()}"
        token = _mint(jti=jti)
        authed = authenticate_promotion_grant(token)
        assert authed.jti == jti
        assert authed.origin_attestation_digest
        assert len(authed.jti) <= 128

        with pytest.raises(PromotionGrantError):
            authenticate_promotion_grant(token[:-4] + "dead")

    @pytest.mark.asyncio
    async def test_replay_wins_before_invalid_transition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Accepted jti must record grant_replay even when transition is now illegal."""
        from open_brain import config as config_module
        from open_brain.memory_promotion import (
            PromotionAttemptParams,
            attempt_memory_promotion,
        )

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
        config_module._config = None
        jti = f"replay-first-{_run_id()}"
        # Grant claims inferred->confirmed, but memory is already confirmed.
        grant = _mint(jti=jti, from_label="inferred", to_label="confirmed")

        async def fetchrow(query: str, *args: Any) -> Any:
            if "FROM memories" in query:
                return {"id": 42, "metadata": _memory_meta(source_label="confirmed")}
            if "INSERT INTO memory_promotion_events" in query:
                return {
                    "id": 9,
                    "memory_id": 42,
                    "actor": "admin-user",
                    "source_state": "confirmed",
                    "target_state": "confirmed",
                    "reason": REASON,
                    "evidence_refs": EVIDENCE,
                    "policy_version": POLICY_VERSION,
                    "rule_version": None,
                    "grant_jti": jti,
                    "grant_digest": "digest",
                    "origin_attestation_digest": "a" * 64,
                    "decision": "rejected",
                    "outcome": "rejected",
                    "rejection_code": "grant_replay",
                    "relationship_id": None,
                    "created_at": datetime.now(UTC),
                }
            return None

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=fetchrow)
        conn.fetchval = AsyncMock(return_value=1)  # accepted jti exists
        with patch(
            "open_brain.memory_promotion.get_pool",
            new_callable=AsyncMock,
            return_value=_make_pool(conn),
        ):
            result = await attempt_memory_promotion(
                PromotionAttemptParams(
                    memory_id=42,
                    target_state="confirmed",
                    reason=REASON,
                    evidence_refs=EVIDENCE,
                    actor="admin-user",
                    promotion_grant=grant,
                )
            )
        assert result.rejection_code == "grant_replay"
        assert result.event is not None
        assert result.event.grant_jti == jti
        assert result.event.grant_digest
        assert result.event.origin_attestation_digest

    @pytest.mark.asyncio
    async def test_tampered_token_never_stores_unverified_jti(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from open_brain import config as config_module
        from open_brain.memory_promotion import (
            PromotionAttemptParams,
            attempt_memory_promotion,
        )

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
        config_module._config = None
        token = _mint(jti=f"tamper-{_run_id()}")
        head, payload, sig = token.split(".")
        flipped = ("A" if payload[0] != "A" else "B") + payload[1:]
        tampered = f"{head}.{flipped}.{sig}"
        inserted: dict[str, Any] = {}

        async def fetchrow(query: str, *args: Any) -> Any:
            if "FROM memories" in query:
                return {"id": 42, "metadata": _memory_meta()}
            if "INSERT INTO memory_promotion_events" in query:
                inserted["grant_jti"] = args[8]
                return {
                    "id": 3,
                    "memory_id": 42,
                    "actor": "admin-user",
                    "source_state": "inferred",
                    "target_state": "confirmed",
                    "reason": REASON,
                    "evidence_refs": EVIDENCE,
                    "policy_version": POLICY_VERSION,
                    "rule_version": None,
                    "grant_jti": None,
                    "grant_digest": "digest-only",
                    "origin_attestation_digest": None,
                    "decision": "rejected",
                    "outcome": "rejected",
                    "rejection_code": "grant_invalid",
                    "relationship_id": None,
                    "created_at": datetime.now(UTC),
                }
            return None

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=fetchrow)
        conn.fetchval = AsyncMock(return_value=None)
        with patch(
            "open_brain.memory_promotion.get_pool",
            new_callable=AsyncMock,
            return_value=_make_pool(conn),
        ):
            result = await attempt_memory_promotion(
                PromotionAttemptParams(
                    memory_id=42,
                    target_state="confirmed",
                    reason=REASON,
                    evidence_refs=EVIDENCE,
                    actor="admin-user",
                    promotion_grant=tampered,
                )
            )
        assert result.rejection_code == "grant_invalid"
        assert inserted.get("grant_jti") is None


# ─── K2-03 rerunnable integ identifiers ───────────────────────────────────────


class TestK203RerunnableIdentifiers:
    def test_k1_integ_helpers_use_per_run_ids(self) -> None:
        from pathlib import Path

        text = Path(__file__).with_name("test_memory_promotion_repair_k1.py").read_text(
            encoding="utf-8"
        )
        # Per-run UUID helpers required for append-only DB reruns.
        assert "def _run_id" in text
        assert "uuid.uuid4" in text
        assert "jti=f\"k1-atomic-{run}\"" in text or "k1-atomic-{run}" in text
        assert "Rerunnable on the same append-only disposable DB" in text
        # Trigger probe may mention DELETE inside pytest.raises; no cleanup helper.
        assert "def cleanup" not in text.lower()
        assert "TRUNCATE memory_promotion_events" not in text


# ─── K2-04 signer helpers + non-finite time ───────────────────────────────────


class TestK204SignerHelpersAndTimeClaims:
    def test_public_digest_helpers_match_verify(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from open_brain import config as config_module
        from open_brain.memory_promotion import (
            compute_evidence_digest,
            compute_origin_attestation_digest,
            compute_reason_digest,
            verify_promotion_grant,
        )

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
        config_module._config = None
        reason = REASON
        refs = list(EVIDENCE)
        attestation = compute_origin_attestation_digest(
            memory_id=42,
            producer="agent",
            source_ref="agent-session:test:1",
            ingestion_route="mcp_save_memory",
        )
        token = _mint(
            reason=reason,
            evidence_refs=refs,
            origin_attestation_digest=attestation,
        )
        claims = verify_promotion_grant(
            token,
            memory_id=42,
            from_label="inferred",
            to_label="confirmed",
            actor="admin-user",
            reason=reason,
            evidence_refs=refs,
            successor_memory_id=None,
            origin_attestation_digest=attestation,
        )
        assert claims.reason_digest == compute_reason_digest(reason)
        assert claims.evidence_digest == compute_evidence_digest(refs)
        assert claims.origin_attestation_digest == attestation

    def test_docs_document_helpers_and_manual_expiry_order(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        text = (root / "docs/standards/memory-promotion.md").read_text(encoding="utf-8")
        assert "compute_reason_digest" in text
        assert "compute_evidence_digest" in text
        assert "compute_origin_attestation_digest" in text
        assert "sort_keys=True" in text
        assert 'separators=(",", ":")' in text or "separators=(',', ':')" in text
        # Expiry is module-ordered manual check, not attributed solely to PyJWT.
        assert "manual" in text.lower() or "ordered" in text.lower()
        assert "grant_expired" in text

    @pytest.mark.parametrize(
        "bad",
        [float("nan"), float("inf"), float("-inf"), 1e309, -(10**20)],
    )
    def test_non_finite_time_claims_are_typed_errors(
        self, monkeypatch: pytest.MonkeyPatch, bad: float
    ) -> None:
        from open_brain import config as config_module
        from open_brain.memory_promotion import (
            PromotionGrantError,
            verify_promotion_grant,
        )

        monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
        config_module._config = None
        now = datetime.now(UTC)
        # Build a signature-valid token then force a bad numeric claim via extra.
        # For iat/exp, jwt.encode may coerce; inject after decode path by
        # minting with a valid window then patching payload via extra on encode.
        if math.isnan(bad) or math.isinf(bad):
            # PyJWT may reject encode of nan/inf; construct manually if needed.
            token = _mint(extra={"iat": bad, "exp": now.timestamp() + 60})
        else:
            token = _mint(extra={"iat": bad, "exp": bad + 60})
        with pytest.raises(PromotionGrantError) as exc:
            verify_promotion_grant(
                token,
                memory_id=42,
                from_label="inferred",
                to_label="confirmed",
                actor="admin-user",
                reason=REASON,
                evidence_refs=EVIDENCE,
                successor_memory_id=None,
            )
        assert exc.value.code == "grant_time_invalid"
        assert not isinstance(exc.value, ValueError) or isinstance(
            exc.value, PromotionGrantError
        )


# ─── K2 integ: native JSONB + replay + rerun (real Postgres) ──────────────────


async def _ensure_index(conn: Any) -> int:
    index_id = await conn.fetchval("SELECT id FROM memory_indexes ORDER BY id LIMIT 1")
    if index_id is None:
        index_id = await conn.fetchval(
            "INSERT INTO memory_indexes (name) VALUES ($1) RETURNING id",
            f"k2-promo-{_run_id()}",
        )
    return int(index_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_k201_k202_real_db_native_jsonb_and_replay(
    integration_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import open_brain.config as config_module
    from open_brain.data_layer import postgres as pg_module
    from open_brain.memory_promotion import (
        PromotionAttemptParams,
        attempt_memory_promotion,
        compute_origin_attestation_digest,
        fetch_promotion_projections,
    )

    run = _run_id()
    monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
    monkeypatch.setenv("PROMOTION_ADMIN_USERS", "admin-user")
    config_module._config = None
    await pg_module.close_pool()
    pool = await pg_module.get_pool()
    try:
        async with pool.acquire() as conn:
            index_id = await _ensure_index(conn)
            source_ref = f"agent-session:k2:{run}"
            mid = await conn.fetchval(
                """
                INSERT INTO memories (index_id, type, content, metadata)
                VALUES ($1, 'identity', $2, $3::jsonb)
                RETURNING id
                """,
                index_id,
                f"k2 native {run}",
                {
                    "ingestion_route": "mcp_save_memory",
                    "provenance": {
                        "origin": {
                            "producer": "agent",
                            "source_ref": source_ref,
                        },
                        "source_label": "inferred",
                        "expected_use": "evidence",
                        "epistemic_version": "epistemic-provenance.v1",
                    },
                },
            )
        mid_i = int(mid)
        digest = compute_origin_attestation_digest(
            memory_id=mid_i,
            producer="agent",
            source_ref=source_ref,
            ingestion_route="mcp_save_memory",
        )
        jti = f"k2-native-{run}"
        grant = _mint(
            memory_id=mid_i,
            jti=jti,
            origin_attestation_digest=digest,
        )
        accepted = await attempt_memory_promotion(
            PromotionAttemptParams(
                memory_id=mid_i,
                target_state="confirmed",
                reason=REASON,
                evidence_refs=EVIDENCE,
                actor="admin-user",
                promotion_grant=grant,
            )
        )
        assert accepted.decision == "accepted"
        async with pool.acquire() as conn:
            meta_type = await conn.fetchval(
                "SELECT jsonb_typeof(metadata) FROM memories WHERE id=$1", mid_i
            )
            refs_type = await conn.fetchval(
                """
                SELECT jsonb_typeof(evidence_refs) FROM memory_promotion_events
                WHERE memory_id=$1 AND decision='accepted'
                ORDER BY id DESC LIMIT 1
                """,
                mid_i,
            )
            label = await conn.fetchval(
                "SELECT metadata->'provenance'->>'source_label' FROM memories WHERE id=$1",
                mid_i,
            )
            assert meta_type == "object"
            assert refs_type == "array"
            assert label == "confirmed"

        projections = await fetch_promotion_projections([mid_i])
        mem = Memory(
            id=mid_i,
            index_id=1,
            session_id=None,
            type="identity",
            title="t",
            subtitle=None,
            narrative=None,
            content="c",
            metadata={
                "ingestion_route": "mcp_save_memory",
                "provenance": {
                    "origin": {"producer": "agent", "source_ref": source_ref},
                    "source_label": "confirmed",
                    "expected_use": "instruction",
                    "epistemic_version": "epistemic-provenance.v1",
                },
            },
            priority=0.5,
            stability="stable",
            access_count=0,
            last_accessed_at=None,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        contract = profile_retrieval_contract(
            "claude-wake-up", work_object={"kind": "project", "id": "p"}
        )
        unit = memory_to_retrieval_unit(
            mem,
            contract,
            requested_influence="identity",
            promotion_projection=projections,
        )
        assert unit.effective_influence == "identity"

        # Replay after state is confirmed must still be grant_replay, not invalid_transition.
        replay = await attempt_memory_promotion(
            PromotionAttemptParams(
                memory_id=mid_i,
                target_state="confirmed",
                reason=REASON,
                evidence_refs=EVIDENCE,
                actor="admin-user",
                promotion_grant=grant,
            )
        )
        assert replay.rejection_code == "grant_replay"
        assert replay.event is not None
        assert replay.event.grant_jti == jti
    finally:
        await pg_module.close_pool()
        config_module._config = None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_k203_integ_rerunnable_same_db(
    integration_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-run UUIDs allow consecutive runs without jti/content collisions.

    Disposable DBs accumulate accepted ledger rows; tests never DELETE them.
    """
    import open_brain.config as config_module
    from open_brain.data_layer import postgres as pg_module
    from open_brain.memory_promotion import (
        PromotionAttemptParams,
        attempt_memory_promotion,
        compute_origin_attestation_digest,
    )

    monkeypatch.setenv("PROMOTION_GRANT_SECRET", PROMOTION_SECRET)
    config_module._config = None
    await pg_module.close_pool()
    pool = await pg_module.get_pool()
    try:
        for _ in range(2):
            run = _run_id()
            async with pool.acquire() as conn:
                index_id = await _ensure_index(conn)
                source_ref = f"agent-session:rerun:{run}"
                mid = await conn.fetchval(
                    """
                    INSERT INTO memories (index_id, type, content, metadata)
                    VALUES ($1, 'identity', $2, $3::jsonb) RETURNING id
                    """,
                    index_id,
                    f"k2 rerun {run}",
                    {
                        "ingestion_route": "mcp_save_memory",
                        "provenance": {
                            "origin": {
                                "producer": "agent",
                                "source_ref": source_ref,
                            },
                            "source_label": "inferred",
                            "expected_use": "evidence",
                            "epistemic_version": "epistemic-provenance.v1",
                        },
                    },
                )
            mid_i = int(mid)
            digest = compute_origin_attestation_digest(
                memory_id=mid_i,
                producer="agent",
                source_ref=source_ref,
                ingestion_route="mcp_save_memory",
            )
            result = await attempt_memory_promotion(
                PromotionAttemptParams(
                    memory_id=mid_i,
                    target_state="confirmed",
                    reason=REASON,
                    evidence_refs=EVIDENCE,
                    actor="admin-user",
                    promotion_grant=_mint(
                        memory_id=mid_i,
                        jti=f"k2-rerun-{run}",
                        origin_attestation_digest=digest,
                    ),
                )
            )
            assert result.decision == "accepted"
    finally:
        await pg_module.close_pool()
        config_module._config = None
