"""Opus round-2 repair matrix for open-brain-ekn.9 (O2-01 .. O2-05).

Exhaustive regression evidence — final model-review round.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.interface import (
    Memory,
    SaveMemoryParams,
    SaveMemoryResult,
    SearchResult,
)

TEST_ACTOR = "test-actor"
SECRET_TOKEN = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX"


def _valid_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "session-knowledge-capture.v1",
        "session_id": "sess-ekn9-o2",
        "producer": "session-knowledge-capture",
        "source_ref": "agent-session:codex:sess-ekn9-o2",
        "project": "open-brain",
        "what_happened": (
            "Focused O2 repair tests passed. Verified capacity, auth, and "
            "classification repairs."
        ),
        "decisions": [
            {
                "text": "Separate rate capacity from daily row capacity for capture.",
                "rationale": "Large Session Close payloads must remain admissible.",
            }
        ],
        "what_was_learned": [
            {
                "text": (
                    "Session-knowledge capture must reserve one rate op and "
                    "daily row slots only when a write will occur."
                ),
                "evidence": "conversation://session/sess-ekn9-o2/learning/0",
            }
        ],
        "unfinished_work": [
            {"text": "Still pending: external producer adapter verification."}
        ],
    }
    payload.update(overrides)
    return payload


def _mock_dl() -> MagicMock:
    dl = MagicMock()
    dl.search = AsyncMock(return_value=SearchResult(results=[], total=0))
    counter = {"n": 800}

    async def _save(params: SaveMemoryParams) -> SaveMemoryResult:
        counter["n"] += 1
        return SaveMemoryResult(id=counter["n"], message="saved")

    dl.save_memory = AsyncMock(side_effect=_save)
    dl.create_relationship = AsyncMock(side_effect=lambda *a, **k: 900 + counter["n"])
    dl.update_memory = AsyncMock(return_value=None)
    return dl


def _memory(
    memory_id: int,
    *,
    memory_type: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> Memory:
    return Memory(
        id=memory_id,
        index_id=1,
        session_id=None,
        type=memory_type,
        title=None,
        subtitle=None,
        narrative=None,
        content=content,
        metadata=metadata or {},
        priority=0.5,
        stability="stable",
        access_count=0,
        last_accessed_at=None,
        created_at="2026-08-08T00:00:00Z",
        updated_at="2026-08-08T00:00:00Z",
    )


# ─── O2-01 ────────────────────────────────────────────────────────────────────


class TestO201AuthenticatedActorOnAllPaths:
    @pytest.mark.asyncio
    async def test_api_key_path_sets_stable_non_secret_actor(self) -> None:
        from open_brain.server import (
            BearerAuthMiddleware,
            _current_scopes,
            _current_user_id,
            _is_api_key_auth,
        )

        middleware = BearerAuthMiddleware(app=MagicMock())
        api_key = "test-hook-key-not-for-actor"
        request = MagicMock()
        request.url.path = "/mcp"
        request.headers = {"x-api-key": api_key}
        request.query_params = {}

        captured: dict[str, Any] = {}

        async def call_next(_req: Any) -> Any:
            captured["user_id"] = _current_user_id.get()
            captured["scopes"] = _current_scopes.get()
            captured["api_key_auth"] = _is_api_key_auth.get()
            return MagicMock(status_code=200)

        with patch.object(
            middleware, "_get_api_keys", return_value=frozenset({api_key})
        ):
            await middleware.dispatch(request, call_next)

        actor = captured["user_id"]
        assert actor == "api-key:configured"
        assert api_key not in actor
        # Must not contain a digest or other credential-derived value.
        key_digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        assert key_digest not in actor
        assert key_digest[:16] not in actor
        assert captured["api_key_auth"] is True
        assert "memory" in captured["scopes"]

    @pytest.mark.asyncio
    async def test_url_token_path_sets_named_actor(self) -> None:
        from open_brain.server import (
            BearerAuthMiddleware,
            _current_scopes,
            _current_user_id,
        )

        middleware = BearerAuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/mcp"
        request.headers = {}
        request.query_params = {"token": "raw-secret-url-token-value"}
        request.scope = {"query_string": b"token=raw-secret-url-token-value"}

        pool = MagicMock()
        pool.fetchrow = AsyncMock(
            return_value={
                "name": "cli-session-close",
                "scopes": ["memory", "evolution"],
                "expires_at": "2099-01-01T00:00:00Z",
            }
        )

        captured: dict[str, Any] = {}

        async def call_next(_req: Any) -> Any:
            captured["user_id"] = _current_user_id.get()
            captured["scopes"] = _current_scopes.get()
            return MagicMock(status_code=200)

        with patch("open_brain.server.get_pool", new=AsyncMock(return_value=pool)):
            await middleware.dispatch(request, call_next)

        assert captured["user_id"] == "url-token:cli-session-close"
        assert "raw-secret-url-token-value" not in str(captured["user_id"])
        assert "memory" in captured["scopes"]

    @pytest.mark.asyncio
    async def test_mcp_capture_works_with_api_key_actor_context(self) -> None:
        from open_brain.server import (
            _current_scopes,
            _current_user_id,
            _is_api_key_auth,
            capture_session_knowledge as mcp_capture,
        )

        dl = _mock_dl()
        actor = "api-key:configured"
        scope = _current_scopes.set(("memory", "evolution"))
        user = _current_user_id.set(actor)
        api = _is_api_key_auth.set(True)
        try:
            with (
                patch("open_brain.server.get_dl", return_value=dl),
                patch(
                    "open_brain.server.reserve_capture_capacity",
                    new_callable=AsyncMock,
                    return_value=None,
                ) as reserve,
            ):
                raw = await mcp_capture(capture=_valid_payload())
        finally:
            _current_scopes.reset(scope)
            _current_user_id.reset(user)
            _is_api_key_auth.reset(api)
        data = json.loads(raw)
        assert data["status"] == "captured"
        assert data.get("error") != "missing_actor"
        for call in dl.save_memory.await_args_list:
            assert call.args[0].user_id == actor
        reserve.assert_awaited()

    @pytest.mark.asyncio
    async def test_promotion_still_rejects_api_key_auth(self) -> None:
        from open_brain.server import (
            ScopeDeniedError,
            _current_scopes,
            _current_user_id,
            _is_api_key_auth,
            promote_memory_authority,
        )

        scope = _current_scopes.set(("memory", "evolution", "admin"))
        user = _current_user_id.set("api-key:configured")
        api = _is_api_key_auth.set(True)
        try:
            with pytest.raises(ScopeDeniedError, match="API keys cannot obtain"):
                await promote_memory_authority(
                    memory_id=1,
                    target_state="promoted",
                    reason="must remain blocked for API keys",
                )
        finally:
            _current_scopes.reset(scope)
            _current_user_id.reset(user)
            _is_api_key_auth.reset(api)

    def test_skill_documents_auth_and_memory_scope(self) -> None:
        from pathlib import Path

        skill = (
            Path(__file__).resolve().parents[2]
            / "skills"
            / "session-knowledge-capture"
            / "SKILL.md"
        ).read_text(encoding="utf-8")
        lowered = skill.lower()
        assert "memory" in lowered and "scope" in lowered
        assert "api key" in lowered or "api-key" in lowered or "bearer" in lowered
        assert "url token" in lowered or "url-token" in lowered


# ─── O2-02 ────────────────────────────────────────────────────────────────────


class TestO202RateVsDailyCapacity:
    @pytest.mark.asyncio
    async def test_large_in_bounds_capture_is_rate_admissible(self) -> None:
        from open_brain import server as server_mod
        from open_brain.session_knowledge import (
            MAX_DECISIONS,
            MAX_LEARNINGS,
            estimate_capture_write_slots,
            parse_session_knowledge_capture_request,
        )

        payload = _valid_payload(
            decisions=[
                {"text": f"Decision {i} must stay within structural bounds."}
                for i in range(MAX_DECISIONS)
            ],
            what_was_learned=[
                {
                    "text": (
                        f"Learning {i}: callers must hold the row lock before "
                        "merging metadata."
                    ),
                    "evidence": f"conversation://session/sess-ekn9-o2/learning/{i}",
                }
                for i in range(MAX_LEARNINGS)
            ],
        )
        request, issues = parse_session_knowledge_capture_request(payload)
        assert issues == [] or all(
            issue.code
            not in {"too_many_items", "what_happened_too_long", "item_too_long"}
            for issue in issues
        )
        assert request is not None
        slots = estimate_capture_write_slots(request)
        assert slots == 1 + MAX_DECISIONS + MAX_LEARNINGS

        server_mod._save_timestamps.clear()
        with patch("open_brain.server.get_config") as cfg:
            cfg.return_value.MAX_MEMORIES_PER_DAY = 10_000
            err = await server_mod.reserve_capture_capacity(
                daily_slots=slots, user_key=TEST_ACTOR
            )
        assert err is None
        # One rate op claimed, not one per row.
        assert len(server_mod._save_timestamps[TEST_ACTOR]) == 1

    @pytest.mark.asyncio
    async def test_structural_oversize_is_typed_not_rate_limit_loop(self) -> None:
        from open_brain.session_knowledge import (
            MAX_DECISIONS,
            parse_session_knowledge_capture_request,
        )

        _, issues = parse_session_knowledge_capture_request(
            _valid_payload(
                decisions=[
                    {"text": f"Decision {i} exceeds the structural bound."}
                    for i in range(MAX_DECISIONS + 1)
                ],
                what_was_learned=[],
            )
        )
        assert any(issue.code == "too_many_items" for issue in issues)
        joined = json.dumps([issue.to_dict() for issue in issues])
        assert "retry" not in joined.lower()
        assert "61" not in joined


# ─── O2-05 ────────────────────────────────────────────────────────────────────


class TestO205NoCapacityOnZeroWritePaths:
    @pytest.mark.asyncio
    async def test_replay_does_not_consume_capacity(self) -> None:
        from open_brain.server import (
            _current_scopes,
            _current_user_id,
            _save_timestamps,
            capture_session_knowledge as mcp_capture,
        )
        from open_brain.session_knowledge import (
            capture_identity,
            compute_capture_fingerprint,
            parse_session_knowledge_capture_request,
        )

        payload = _valid_payload()
        request, issues = parse_session_knowledge_capture_request(payload)
        assert request is not None and not [
            i for i in issues if i.code.startswith("invalid")
        ]
        # Accept classification-only issues; fingerprint must still work.
        fingerprint = compute_capture_fingerprint(request)
        identity = capture_identity(
            TEST_ACTOR,
            request.producer,
            request.source_ref,
            request.schema_version,
        )
        prior = _memory(
            55,
            memory_type="session_event",
            content=str(payload["what_happened"]),
            metadata={
                "session_knowledge_capture_identity": identity,
                "session_knowledge": {
                    "role": "session_event",
                    "capture_identity": identity,
                    "payload_fingerprint": fingerprint,
                    "capture_status": "complete",
                    "capture_result": {
                        "session_event_id": 55,
                        "decision_ids": [56],
                        "learning_ids": [57],
                        "relationship_ids": [301],
                        "unfinished_work": payload["unfinished_work"],
                        "judge_outcomes": [],
                        "issues": [i.to_dict() for i in issues],
                    },
                },
            },
        )
        dl = _mock_dl()
        dl.search = AsyncMock(return_value=SearchResult(results=[prior], total=1))
        _save_timestamps.clear()
        scope = _current_scopes.set(("memory",))
        user = _current_user_id.set(TEST_ACTOR)
        try:
            with (
                patch("open_brain.server.get_dl", return_value=dl),
                patch(
                    "open_brain.server.reserve_capture_capacity",
                    new_callable=AsyncMock,
                    return_value=None,
                ) as reserve,
            ):
                raw = await mcp_capture(capture=payload)
        finally:
            _current_scopes.reset(scope)
            _current_user_id.reset(user)
        data = json.loads(raw)
        assert data["status"] == "replayed"
        reserve.assert_not_awaited()
        assert _save_timestamps.get(TEST_ACTOR, []) == []

    @pytest.mark.asyncio
    async def test_empty_and_rejected_do_not_reserve(self) -> None:
        from open_brain.server import (
            _current_scopes,
            _current_user_id,
            capture_session_knowledge as mcp_capture,
        )

        dl = _mock_dl()
        scope = _current_scopes.set(("memory",))
        user = _current_user_id.set(TEST_ACTOR)
        try:
            with (
                patch("open_brain.server.get_dl", return_value=dl),
                patch(
                    "open_brain.server.reserve_capture_capacity",
                    new_callable=AsyncMock,
                    return_value=None,
                ) as reserve,
            ):
                empty = await mcp_capture(
                    capture=_valid_payload(
                        what_happened="",
                        decisions=[],
                        what_was_learned=[],
                        unfinished_work=[],
                    )
                )
                rejected = await mcp_capture(
                    capture=_valid_payload(schema_version="session-knowledge-capture.v0")
                )
        finally:
            _current_scopes.reset(scope)
            _current_user_id.reset(user)
        assert json.loads(empty)["status"] == "captured"
        assert json.loads(rejected)["status"] == "rejected"
        reserve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_capacity_callback_runs_only_before_first_write(self) -> None:
        from open_brain.session_knowledge import capture_session_knowledge

        dl = _mock_dl()
        calls: list[int] = []

        async def reserve(*, daily_slots: int) -> str | None:
            calls.append(daily_slots)
            return None

        result = await capture_session_knowledge(
            _valid_payload(),
            data_layer=dl,
            actor=TEST_ACTOR,
            capacity_reserve=reserve,
        )
        assert result.status == "captured"
        assert calls == [3]
        assert dl.save_memory.await_count >= 1

    @pytest.mark.asyncio
    async def test_judge_blocked_zero_write_leaves_rate_bucket_unchanged(self) -> None:
        """Event judge BLOCK with zero rows must not retain a rate-op timestamp."""
        from open_brain import server as server_mod
        from open_brain.session_knowledge import capture_session_knowledge

        actor = "api-key:configured"
        dl = _mock_dl()
        server_mod._save_timestamps.clear()
        before = list(server_mod._save_timestamps.get(actor, []))

        async def reserve(*, daily_slots: int) -> str | None:
            return await server_mod.reserve_capture_capacity(
                daily_slots=daily_slots, user_key=actor
            )

        def release() -> None:
            server_mod.release_capture_rate_reservation(user_key=actor)

        with patch("open_brain.server.get_config") as cfg:
            cfg.return_value.MAX_MEMORIES_PER_DAY = 10_000
            result = await capture_session_knowledge(
                _valid_payload(
                    what_happened=(
                        f"Deployed with ANTHROPIC_API_KEY={SECRET_TOKEN} "
                        "and password=hunter2correct."
                    ),
                    decisions=[],
                    what_was_learned=[],
                ),
                data_layer=dl,
                actor=actor,
                capacity_reserve=reserve,
                capacity_release=release,
            )

        assert result.status == "judged"
        assert result.session_event_id is None
        assert result.decision_ids == ()
        assert result.learning_ids == ()
        assert any(
            outcome.get("decision") == "BLOCK" for outcome in result.judge_outcomes
        )
        assert dl.save_memory.await_count == 0
        assert list(server_mod._save_timestamps.get(actor, [])) == before


# ─── O2-03 ────────────────────────────────────────────────────────────────────


class TestO203SecretScanAndSafeJudgeReceipt:
    @pytest.mark.asyncio
    async def test_secret_in_source_ref_never_persists(self) -> None:
        from open_brain.session_knowledge import capture_session_knowledge

        dl = _mock_dl()
        result = await capture_session_knowledge(
            _valid_payload(
                source_ref=f"https://ci:{SECRET_TOKEN}@build.example.com/run/9"
            ),
            data_layer=dl,
            actor=TEST_ACTOR,
        )
        assert result.session_event_id is None or result.status in {
            "rejected",
            "judged",
        }
        blob = json.dumps(result.to_dict())
        assert SECRET_TOKEN not in blob
        for call in dl.save_memory.await_args_list:
            params = call.args[0]
            assert SECRET_TOKEN not in json.dumps(params.metadata or {})
            assert SECRET_TOKEN not in str(params.provenance)
            assert SECRET_TOKEN not in params.text

    @pytest.mark.asyncio
    async def test_secret_in_learning_evidence_blocked_and_not_in_receipt(self) -> None:
        from open_brain.session_knowledge import capture_session_knowledge

        dl = _mock_dl()
        result = await capture_session_knowledge(
            _valid_payload(
                what_was_learned=[
                    {
                        "text": (
                            "Never embed credentials in evidence refs; "
                            "treat them as blocked material."
                        ),
                        "evidence": (
                            f"https://svc:{SECRET_TOKEN}@logs.example.com/y"
                        ),
                    }
                ]
            ),
            data_layer=dl,
            actor=TEST_ACTOR,
        )
        assert result.learning_ids == ()
        assert result.judge_outcomes
        for outcome in result.judge_outcomes:
            assert outcome.get("decision") == "BLOCK"
            assert "provenance_refs" not in outcome
            assert "revised_proposal" not in outcome
            assert SECRET_TOKEN not in json.dumps(outcome)
        # Event/decision may still persist; blocked learning must not.
        learning_saves = [
            c.args[0]
            for c in dl.save_memory.await_args_list
            if c.args[0].type == "learning"
        ]
        assert learning_saves == []
        # Finalize metadata must not contain the secret either.
        if dl.update_memory.await_count:
            meta = dl.update_memory.await_args.args[0].metadata
            assert SECRET_TOKEN not in json.dumps(meta)

    @pytest.mark.asyncio
    async def test_scans_producer_project_and_rationale(self) -> None:
        from open_brain.session_knowledge import detect_secret_risk_flags

        assert detect_secret_risk_flags(
            f"password={SECRET_TOKEN}",
            f"project-with-{SECRET_TOKEN}",
            f"Rationale mentions {SECRET_TOKEN}",
        ) == ("secret", "credential")


# ─── O2-04 ────────────────────────────────────────────────────────────────────


class TestO204CompletedNarrationClassifier:
    @pytest.mark.parametrize(
        "text",
        [
            "Migrations must be applied before a partial index is added.",
            "Never cache a resolved symlink path; always re-stat it.",
            "Signatures must be verified before the grant is trusted.",
            "Postgres blocks a duplicate insert until the first transaction commits.",
            "Voyage rerank drops candidates below the fetch limit.",
        ],
    )
    def test_normative_and_mechanism_learnings_accepted(self, text: str) -> None:
        from open_brain.session_knowledge import parse_session_knowledge_capture_request

        request, issues = parse_session_knowledge_capture_request(
            _valid_payload(
                what_was_learned=[
                    {
                        "text": text,
                        "evidence": "conversation://session/sess-ekn9-o2/learning/0",
                    }
                ]
            )
        )
        assert request is not None
        assert len(request.what_was_learned) == 1
        assert not any(
            issue.code in {"completed_work_as_learning", "non_reusable_learning"}
            for issue in issues
        )

    @pytest.mark.parametrize(
        "text",
        [
            (
                "Implemented the retry loop in server.py and fixed the flake "
                "because Postgres timed out."
            ),
            (
                "Deployed the new index when the migration finished; "
                "verified 2048 tests pass."
            ),
            "Fixed the bug; the reviewer must be credited.",
        ],
    )
    def test_round1_completed_narration_still_rejected(self, text: str) -> None:
        from open_brain.session_knowledge import parse_session_knowledge_capture_request

        request, issues = parse_session_knowledge_capture_request(
            _valid_payload(
                what_was_learned=[{"text": text, "evidence": "probe"}]
            )
        )
        # Nonfatal: request may exist without the bad learning.
        assert any(issue.code == "completed_work_as_learning" for issue in issues)
        if request is not None:
            assert all(item.text != text for item in request.what_was_learned)


# ─── Per-learning nonfatal + fingerprint (O2-04 / user item 6) ────────────────


class TestO2NonfatalLearningIssuesAndFingerprint:
    @pytest.mark.asyncio
    async def test_bad_learning_dropped_event_and_decisions_persist(self) -> None:
        from open_brain.session_knowledge import capture_session_knowledge

        dl = _mock_dl()
        result = await capture_session_knowledge(
            _valid_payload(
                what_was_learned=[
                    {
                        "text": "Fixed the bug; the reviewer must be credited.",
                        "evidence": "probe",
                    },
                    {
                        "text": (
                            "Adapter rollout must be gated behind an explicit "
                            "feature flag."
                        ),
                        "evidence": "conversation://session/sess-ekn9-o2/learning/1",
                    },
                ]
            ),
            data_layer=dl,
            actor=TEST_ACTOR,
        )
        assert result.status == "captured"
        assert result.session_event_id is not None
        assert len(result.decision_ids) == 1
        assert len(result.learning_ids) == 1
        assert any(
            issue.code == "completed_work_as_learning" for issue in result.issues
        )

    @pytest.mark.asyncio
    async def test_issues_preserved_on_replay(self) -> None:
        from open_brain.session_knowledge import (
            capture_identity,
            capture_session_knowledge,
            compute_capture_fingerprint,
            parse_session_knowledge_capture_request,
        )

        payload = _valid_payload(
            what_was_learned=[
                {
                    "text": "Fixed the bug; the reviewer must be credited.",
                    "evidence": "probe",
                },
                {
                    "text": (
                        "Signatures must be verified before the grant is trusted."
                    ),
                    "evidence": "conversation://session/sess-ekn9-o2/learning/1",
                },
            ]
        )
        request, issues = parse_session_knowledge_capture_request(payload)
        assert request is not None
        assert any(i.code == "completed_work_as_learning" for i in issues)
        fingerprint = compute_capture_fingerprint(request)
        identity = capture_identity(
            TEST_ACTOR,
            request.producer,
            request.source_ref,
            request.schema_version,
        )
        stored_issues = [i.to_dict() for i in issues]
        prior = _memory(
            70,
            memory_type="session_event",
            content=str(payload["what_happened"]),
            metadata={
                "session_knowledge_capture_identity": identity,
                "session_knowledge": {
                    "role": "session_event",
                    "capture_identity": identity,
                    "payload_fingerprint": fingerprint,
                    "capture_status": "complete",
                    "capture_result": {
                        "session_event_id": 70,
                        "decision_ids": [71],
                        "learning_ids": [72],
                        "relationship_ids": [301],
                        "unfinished_work": [],
                        "judge_outcomes": [],
                        "issues": stored_issues,
                    },
                },
            },
        )
        dl = _mock_dl()
        dl.search = AsyncMock(return_value=SearchResult(results=[prior], total=1))
        result = await capture_session_knowledge(
            payload, data_layer=dl, actor=TEST_ACTOR
        )
        assert result.status == "replayed"
        assert any(
            issue.code == "completed_work_as_learning" for issue in result.issues
        )

    @pytest.mark.asyncio
    async def test_different_rejected_learning_conflicts_not_silent_replay(
        self,
    ) -> None:
        from open_brain.session_knowledge import (
            capture_identity,
            capture_session_knowledge,
            compute_capture_fingerprint,
            parse_session_knowledge_capture_request,
        )

        first = _valid_payload(
            what_was_learned=[
                {
                    "text": "Fixed the bug; the reviewer must be credited.",
                    "evidence": "probe-a",
                }
            ]
        )
        second = _valid_payload(
            what_was_learned=[
                {
                    "text": (
                        "Implemented the retry loop in server.py and fixed the "
                        "flake because Postgres timed out."
                    ),
                    "evidence": "probe-b",
                }
            ]
        )
        req_a, _ = parse_session_knowledge_capture_request(first)
        req_b, _ = parse_session_knowledge_capture_request(second)
        assert req_a is not None and req_b is not None
        assert compute_capture_fingerprint(req_a) != compute_capture_fingerprint(req_b)

        identity = capture_identity(
            TEST_ACTOR,
            req_a.producer,
            req_a.source_ref,
            req_a.schema_version,
        )
        prior = _memory(
            80,
            memory_type="session_event",
            content=str(first["what_happened"]),
            metadata={
                "session_knowledge_capture_identity": identity,
                "session_knowledge": {
                    "role": "session_event",
                    "capture_identity": identity,
                    "payload_fingerprint": compute_capture_fingerprint(req_a),
                    "capture_status": "complete",
                    "capture_result": {
                        "session_event_id": 80,
                        "decision_ids": [81],
                        "learning_ids": [],
                        "relationship_ids": [301],
                        "unfinished_work": [],
                        "judge_outcomes": [],
                        "issues": [
                            {
                                "code": "completed_work_as_learning",
                                "field": "what_was_learned[0]",
                                "message": "x",
                            }
                        ],
                    },
                },
            },
        )
        dl = _mock_dl()
        dl.search = AsyncMock(return_value=SearchResult(results=[prior], total=1))
        result = await capture_session_knowledge(
            second, data_layer=dl, actor=TEST_ACTOR
        )
        assert result.status == "conflict"
        # Rejected content itself must not be persisted on conflict.
        assert all(
            SECRET_TOKEN not in (c.args[0].text or "")
            for c in dl.save_memory.await_args_list
        )
