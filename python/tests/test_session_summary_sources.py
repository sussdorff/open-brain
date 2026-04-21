"""Tests for session-end-hook endpoint and session_summary helper.

AK8: test_session_end_writes_source_marker
      test_session_summary_source_allowlist
      test_dedup_second_call_same_session_id
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "plugin" / "scripts"))
from session_end_summary import _filter_turns

import pytest
from httpx import ASGITransport, AsyncClient

from open_brain.data_layer.interface import SaveMemoryResult

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# ─── Shared helpers ────────────────────────────────────────────────────────────

SAMPLE_TURNS = [
    {"type": "user", "content": "Please add the new endpoint.", "isMeta": False},
    {"type": "assistant", "content": "Sure, here is the implementation.", "isMeta": False},
]

SAMPLE_SESSION_ID = "test-session-abc123"
SAMPLE_PROJECT = "open-brain"


@pytest.fixture(autouse=True)
def patch_api_keys(monkeypatch):
    """Ensure a known API key is available for all tests in this module."""
    monkeypatch.setenv("API_KEYS", "test-hook-key")


@pytest.fixture
def mock_dl():
    dl = AsyncMock()
    dl.save_memory.return_value = SaveMemoryResult(id=7, message="Memory saved")
    return dl


@pytest.fixture
def api_client():
    from open_brain.server import app
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://testserver")


def _api_headers() -> dict:
    return {"X-API-Key": "test-hook-key"}


# ─── POST /api/session-end ─────────────────────────────────────────────────────

class TestApiSessionEnd:
    @pytest.mark.asyncio
    async def test_valid_request_returns_202(self, api_client, mock_dl):
        """Valid payload returns 202 Accepted."""
        body = {
            "session_id": SAMPLE_SESSION_ID,
            "project": SAMPLE_PROJECT,
            "turns": SAMPLE_TURNS,
            "reason": "stop",
        }
        with patch("open_brain.server.asyncio.create_task"):
            resp = await api_client.post(
                "/api/session-end",
                json=body,
                headers=_api_headers(),
            )
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_401(self, api_client):
        """Request without auth returns 401."""
        resp = await api_client.post(
            "/api/session-end",
            json={"session_id": "x", "project": "p", "turns": SAMPLE_TURNS},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_turns_returns_202_no_op(self, api_client):
        """Empty turns list returns 202 but performs no work."""
        with patch("open_brain.server.asyncio.create_task") as mock_task:
            resp = await api_client.post(
                "/api/session-end",
                json={"session_id": "s1", "project": "p1", "turns": []},
                headers=_api_headers(),
            )
        assert resp.status_code == 202
        mock_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_list_turns_returns_202_no_op(self, api_client):
        """Non-list turns returns 202 but no background task."""
        with patch("open_brain.server.asyncio.create_task") as mock_task:
            resp = await api_client.post(
                "/api/session-end",
                json={"session_id": "s1", "project": "p1", "turns": "not-a-list"},
                headers=_api_headers(),
            )
        assert resp.status_code == 202
        mock_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_background_task_spawned_for_valid_payload(self, api_client):
        """Background task is created when payload has turns."""
        with patch("open_brain.server.asyncio.create_task") as mock_task:
            resp = await api_client.post(
                "/api/session-end",
                json={
                    "session_id": SAMPLE_SESSION_ID,
                    "project": SAMPLE_PROJECT,
                    "turns": SAMPLE_TURNS,
                    "reason": "stop",
                },
                headers=_api_headers(),
            )
        assert resp.status_code == 202
        mock_task.assert_called_once()


# ─── AK8: source marker + dedup ───────────────────────────────────────────────

class TestSessionEndSourceMarker:
    """AK8: summarize_transcript_turns writes metadata.source == 'session-end-hook'."""

    @pytest.mark.asyncio
    async def test_session_end_writes_source_marker(self, mock_dl):
        """summarize_transcript_turns sets metadata.source='session-end-hook'."""
        mock_summary = {"title": "Test session", "content": "Did stuff.", "narrative": "Learned things."}

        # Patch the pool dedup check to return None (no existing summary)
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(
            return_value=_async_ctx_manager(mock_conn)
        )

        with patch("open_brain.session_summary.get_pool", return_value=mock_pool):
            with patch("open_brain.session_summary.get_dl", return_value=mock_dl):
                with patch("open_brain.session_summary.llm_complete", new_callable=AsyncMock, return_value="{}"):
                    with patch("open_brain.session_summary.parse_llm_json", return_value=mock_summary):
                        from open_brain.session_summary import summarize_transcript_turns
                        result = await summarize_transcript_turns(
                            project=SAMPLE_PROJECT,
                            session_id=SAMPLE_SESSION_ID,
                            turns=SAMPLE_TURNS,
                            source="session-end-hook",
                            reason="stop",
                        )

        assert result is not None
        mock_dl.save_memory.assert_called_once()
        call_params = mock_dl.save_memory.call_args[0][0]
        assert call_params.metadata["source"] == "session-end-hook"
        assert call_params.metadata["reason"] == "stop"
        assert call_params.metadata["turn_count"] == len(SAMPLE_TURNS)
        assert call_params.type == "session_summary"
        assert call_params.session_ref == SAMPLE_SESSION_ID

    @pytest.mark.asyncio
    async def test_session_summary_source_allowlist(self, mock_dl):
        """All source values used in tests are in the expected allowlist."""
        # This tests that valid sources can flow through without error
        allowed_sources = {"session-close", "session-end-hook", "transcript-backfill", None}
        mock_summary = {"title": "T", "content": "C", "narrative": None}

        for source in allowed_sources:
            mock_dl.reset_mock()
            mock_conn = AsyncMock()
            mock_conn.fetchrow = AsyncMock(return_value=None)
            mock_pool = MagicMock()
            mock_pool.acquire = MagicMock(return_value=_async_ctx_manager(mock_conn))

            with patch("open_brain.session_summary.get_pool", return_value=mock_pool):
                with patch("open_brain.session_summary.get_dl", return_value=mock_dl):
                    with patch("open_brain.session_summary.llm_complete", new_callable=AsyncMock, return_value="{}"):
                        with patch("open_brain.session_summary.parse_llm_json", return_value=mock_summary):
                            from open_brain.session_summary import summarize_transcript_turns
                            result = await summarize_transcript_turns(
                                project=SAMPLE_PROJECT,
                                session_id=f"sess-{source or 'none'}",
                                turns=SAMPLE_TURNS,
                                source=source,
                                reason=None,
                            )

            assert result is not None, f"Expected non-None result for source={source!r}"
            call_params = mock_dl.save_memory.call_args[0][0]
            assert call_params.metadata["source"] == source

    @pytest.mark.asyncio
    async def test_dedup_second_call_same_session_id(self, mock_dl):
        """Second call with same session_id returns None (dedup skip)."""
        mock_summary = {"title": "T", "content": "C", "narrative": None}

        # Simulate existing row found in DB
        existing_row = {"id": 1, "metadata": {"source": "session-end-hook"}}
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=existing_row)
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=_async_ctx_manager(mock_conn))

        with patch("open_brain.session_summary.get_pool", return_value=mock_pool):
            with patch("open_brain.session_summary.get_dl", return_value=mock_dl):
                with patch("open_brain.session_summary.llm_complete", new_callable=AsyncMock, return_value="{}"):
                    with patch("open_brain.session_summary.parse_llm_json", return_value=mock_summary):
                        from open_brain.session_summary import summarize_transcript_turns
                        result = await summarize_transcript_turns(
                            project=SAMPLE_PROJECT,
                            session_id=SAMPLE_SESSION_ID,
                            turns=SAMPLE_TURNS,
                            source="session-end-hook",
                            reason="stop",
                        )

        assert result is None, "Expected None when existing session_summary found"
        mock_dl.save_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_turns_returns_none_without_post(self, mock_dl):
        """Empty turns list returns None without calling save_memory."""
        mock_pool = MagicMock()

        with patch("open_brain.session_summary.get_pool", return_value=mock_pool):
            with patch("open_brain.session_summary.get_dl", return_value=mock_dl):
                from open_brain.session_summary import summarize_transcript_turns
                result = await summarize_transcript_turns(
                    project=SAMPLE_PROJECT,
                    session_id=SAMPLE_SESSION_ID,
                    turns=[],
                    source="session-end-hook",
                    reason="stop",
                )

        assert result is None
        mock_dl.save_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_turns_text_truncated_for_large_input(self, mock_dl):
        """Large turns list triggers head+tail truncation."""
        # Build 2000 turns to exceed 8000 chars
        large_turns = [
            {"type": "user" if i % 2 == 0 else "assistant",
             "content": f"Message {i:04d} " + "x" * 50,
             "isMeta": False}
            for i in range(200)
        ]

        captured_prompts = []
        async def mock_llm(messages, **kwargs):
            captured_prompts.append(messages[0].content)
            return "{}"

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=_async_ctx_manager(mock_conn))

        with patch("open_brain.session_summary.get_pool", return_value=mock_pool):
            with patch("open_brain.session_summary.get_dl", return_value=mock_dl):
                with patch("open_brain.session_summary.llm_complete", side_effect=mock_llm):
                    with patch("open_brain.session_summary.parse_llm_json", return_value={"title": "T", "content": "C", "narrative": None}):
                        from open_brain.session_summary import summarize_transcript_turns
                        await summarize_transcript_turns(
                            project=SAMPLE_PROJECT,
                            session_id=SAMPLE_SESSION_ID,
                            turns=large_turns,
                            source="session-end-hook",
                        )

        assert captured_prompts, "llm_complete should have been called"
        assert "[...truncated...]" in captured_prompts[0]


# ─── AK9: fixture files ────────────────────────────────────────────────────────

class TestFixtureFiles:
    """Verify fixture files exist and have the expected properties."""

    def test_fixture_small_exists(self):
        assert (FIXTURES_DIR / "transcript_small.jsonl").exists()

    def test_fixture_large_exists(self):
        assert (FIXTURES_DIR / "transcript_large.jsonl").exists()

    def test_fixture_empty_exists(self):
        assert (FIXTURES_DIR / "transcript_empty.jsonl").exists()

    def test_small_has_42_lines(self):
        lines = (FIXTURES_DIR / "transcript_small.jsonl").read_text().strip().splitlines()
        assert len(lines) == 42

    def test_small_has_filtered_lines(self):
        """small fixture has isMeta=true and type=summary lines to filter."""
        lines = (FIXTURES_DIR / "transcript_small.jsonl").read_text().strip().splitlines()
        parsed = [json.loads(l) for l in lines]
        meta_count = sum(1 for t in parsed if t.get("isMeta") is True)
        summary_count = sum(1 for t in parsed if t.get("type") == "summary")
        assert meta_count == 2
        assert summary_count == 1

    def test_large_has_2000_lines(self):
        lines = (FIXTURES_DIR / "transcript_large.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2000

    def test_large_triggers_truncation(self):
        """large fixture total char count > 8000."""
        lines = (FIXTURES_DIR / "transcript_large.jsonl").read_text().strip().splitlines()
        parsed = [json.loads(l) for l in lines]
        valid = [t for t in parsed if t.get("type") in ("user", "assistant") and not t.get("isMeta") and isinstance(t.get("content"), str)]
        total_chars = sum(len(t["content"]) for t in valid)
        assert total_chars > 8000

    def test_empty_has_zero_valid_turns(self):
        """empty fixture has 0 valid turns after filter."""
        lines = (FIXTURES_DIR / "transcript_empty.jsonl").read_text().strip().splitlines()
        parsed = [json.loads(l) for l in lines]
        valid = [
            t for t in parsed
            if t.get("type") in ("user", "assistant")
            and not t.get("isMeta")
            and isinstance(t.get("content"), str)
            and t["content"]
        ]
        assert len(valid) == 0


# ─── AK10: _filter_turns unit tests ──────────────────────────────────────────

class TestFilterTurns:
    """Unit tests for _filter_turns in the session_end_summary hook script."""

    def test_filter_turns_small(self):
        """42 raw lines → skip 2 isMeta + 1 type=summary → 39 valid turns."""
        lines = (FIXTURES_DIR / "transcript_small.jsonl").read_text().splitlines()
        turns = _filter_turns(lines)
        assert len(turns) == 39

    def test_filter_turns_empty(self):
        """Empty fixture → no valid turns returned."""
        lines = (FIXTURES_DIR / "transcript_empty.jsonl").read_text().splitlines()
        turns = _filter_turns(lines)
        assert turns == []

    def test_filter_turns_skips_isMeta(self):
        """isMeta=true entries are excluded from results."""
        lines = [
            '{"type": "user", "content": "hello", "isMeta": false}',
            '{"type": "assistant", "content": "system note", "isMeta": true}',
            '{"type": "assistant", "content": "world", "isMeta": false}',
            '{"type": "user", "content": "meta too", "isMeta": true}',
        ]
        turns = _filter_turns(lines)
        assert len(turns) == 2
        assert all(not t.get("isMeta") for t in turns)
        assert turns[0]["content"] == "hello"
        assert turns[1]["content"] == "world"

    def test_filter_turns_skips_non_user_assistant_types(self):
        """Entries with type other than user/assistant are excluded."""
        lines = [
            '{"type": "user", "content": "keep", "isMeta": false}',
            '{"type": "summary", "content": "skip this", "isMeta": false}',
            '{"type": "tool_result", "content": "skip too", "isMeta": false}',
            '{"type": "assistant", "content": "keep too", "isMeta": false}',
        ]
        turns = _filter_turns(lines)
        assert len(turns) == 2
        assert turns[0]["content"] == "keep"
        assert turns[1]["content"] == "keep too"

    def test_filter_turns_skips_non_string_content(self):
        """Entries with non-string content are excluded."""
        lines = [
            '{"type": "user", "content": ["list", "not", "string"], "isMeta": false}',
            '{"type": "assistant", "content": null, "isMeta": false}',
            '{"type": "user", "content": "valid", "isMeta": false}',
        ]
        turns = _filter_turns(lines)
        assert len(turns) == 1
        assert turns[0]["content"] == "valid"

    def test_filter_turns_skips_invalid_json(self):
        """Lines that are not valid JSON are silently skipped."""
        lines = [
            "not json at all",
            '{"type": "user", "content": "good", "isMeta": false}',
            "{broken",
        ]
        turns = _filter_turns(lines)
        assert len(turns) == 1
        assert turns[0]["content"] == "good"

    def test_filter_turns_empty_lines_skipped(self):
        """Blank lines are silently skipped."""
        lines = [
            "",
            '{"type": "user", "content": "hi", "isMeta": false}',
            "   ",
        ]
        turns = _filter_turns(lines)
        assert len(turns) == 1

    def test_filter_turns_preserves_type_and_content(self):
        """Returned dicts have normalized type and content fields."""
        lines = [
            '{"type": "user", "content": "question", "isMeta": false}',
            '{"type": "assistant", "content": "answer", "isMeta": false}',
        ]
        turns = _filter_turns(lines)
        assert turns[0]["type"] == "user"
        assert turns[0]["content"] == "question"
        assert turns[1]["type"] == "assistant"
        assert turns[1]["content"] == "answer"


# ─── Helper ───────────────────────────────────────────────────────────────────

class _async_ctx_manager:
    """Minimal async context manager that yields a fixed value."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        pass
