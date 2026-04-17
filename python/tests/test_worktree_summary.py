"""Tests for POST /api/worktree-session-summary endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from open_brain.data_layer.interface import SaveMemoryResult


SAMPLE_TURNS = [
    {
        "ts": "2026-04-17T10:23:45Z",
        "agent": "claude-code",
        "session_id": "9f3a1b22-5c7e-4a10-8e44-2d1f6b0c9e01",
        "parent_session_id": None,
        "hook_type": "Stop",
        "user_input_excerpt": "Add /api/worktree-session-summary endpoint",
        "assistant_summary_excerpt": "Scaffolded route + pydantic model",
        "tool_calls": [{"name": "Edit", "target": "python/src/open_brain/server.py"}],
    },
    {
        "ts": "2026-04-17T10:31:02Z",
        "agent": "claude-code",
        "session_id": "9f3a1b22-5c7e-4a10-8e44-2d1f6b0c9e01",
        "parent_session_id": None,
        "hook_type": "SubagentStop",
        "user_input_excerpt": "Write pytest fixtures",
        "assistant_summary_excerpt": "Added 3 tests, mocked llm_complete",
        "tool_calls": [{"name": "Write", "target": "python/tests/test_worktree_summary.py"}],
    },
    {
        "ts": "2026-04-17T10:42:18Z",
        "agent": "claude-code",
        "session_id": "c2b88d14-7f00-42ab-9a6d-8e5411d73c77",
        "parent_session_id": "9f3a1b22-5c7e-4a10-8e44-2d1f6b0c9e01",
        "hook_type": "Stop",
        "user_input_excerpt": "Run tests",
        "assistant_summary_excerpt": "All green, 3 passed",
        "tool_calls": [{"name": "Bash", "target": "uv run pytest"}],
    },
]

SAMPLE_BODY = {
    "worktree": ".claude/worktrees/bead-open-brain-krx",
    "bead_id": "open-brain-krx",
    "project": "open-brain",
    "turns": SAMPLE_TURNS,
}


@pytest.fixture
def mock_dl():
    dl = AsyncMock()
    dl.save_memory.return_value = SaveMemoryResult(id=42, message="Memory saved")
    return dl


@pytest.fixture(autouse=True)
def patch_api_keys(monkeypatch):
    """Ensure a known API key is available for all tests in this module."""
    monkeypatch.setenv("API_KEYS", "test-hook-key")


@pytest.fixture
def api_client():
    from open_brain.server import app
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://testserver")


def _api_headers() -> dict:
    return {"X-API-Key": "test-hook-key"}


# ─── POST /api/worktree-session-summary ───────────────────────────────────────

class TestApiWorktreeSessionSummary:
    @pytest.mark.asyncio
    async def test_valid_request_returns_202(self, api_client, mock_dl):
        """Valid payload with required fields returns 202 Accepted."""
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            with patch("open_brain.server.asyncio.create_task"):
                resp = await api_client.post(
                    "/api/worktree-session-summary",
                    json=SAMPLE_BODY,
                    headers=_api_headers(),
                )
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_401(self, api_client):
        """Request without X-API-Key header returns 401 Unauthorized."""
        resp = await api_client.post(
            "/api/worktree-session-summary",
            json=SAMPLE_BODY,
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_turns_returns_400(self, api_client):
        """Payload with empty turns array returns 400 Bad Request."""
        body = {**SAMPLE_BODY, "turns": []}
        resp = await api_client.post(
            "/api/worktree-session-summary",
            json=body,
            headers=_api_headers(),
        )
        assert resp.status_code == 400
        assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_missing_project_returns_400(self, api_client):
        """Payload without project field returns 400 Bad Request."""
        body = {k: v for k, v in SAMPLE_BODY.items() if k != "project"}
        resp = await api_client.post(
            "/api/worktree-session-summary",
            json=body,
            headers=_api_headers(),
        )
        assert resp.status_code == 400
        assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_create_task_is_called_for_valid_payload(self, api_client, mock_dl):
        """Background task is spawned when payload is valid."""
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            with patch("open_brain.server.asyncio.create_task") as mock_task:
                await api_client.post(
                    "/api/worktree-session-summary",
                    json=SAMPLE_BODY,
                    headers=_api_headers(),
                )
        mock_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_background_task_saves_memory_with_correct_type(self, api_client, mock_dl):
        """Background task calls dl.save_memory with type='session_summary'."""
        mock_llm_response = '{"title": "Test session", "content": "Did stuff.", "narrative": "Learned things."}'
        mock_summary = {"title": "Test session", "content": "Did stuff.", "narrative": "Learned things."}

        with patch("open_brain.server.get_dl", return_value=mock_dl):
            with patch("open_brain.server.llm_complete", new_callable=AsyncMock, return_value=mock_llm_response):
                with patch("open_brain.server.parse_llm_json", return_value=mock_summary):
                    from open_brain.server import _process_worktree_session_summary
                    await _process_worktree_session_summary(SAMPLE_BODY)

        mock_dl.save_memory.assert_called_once()
        call_args = mock_dl.save_memory.call_args[0][0]
        assert call_args.type == "session_summary"

    @pytest.mark.asyncio
    async def test_background_task_session_ref_combines_unique_session_ids(self, api_client, mock_dl):
        """session_ref is sorted unique session_ids joined by comma."""
        mock_summary = {"title": "T", "content": "C", "narrative": "N"}

        with patch("open_brain.server.get_dl", return_value=mock_dl):
            with patch("open_brain.server.llm_complete", new_callable=AsyncMock, return_value="{}"):
                with patch("open_brain.server.parse_llm_json", return_value=mock_summary):
                    from open_brain.server import _process_worktree_session_summary
                    await _process_worktree_session_summary(SAMPLE_BODY)

        call_args = mock_dl.save_memory.call_args[0][0]
        # SAMPLE_TURNS has 2 unique session_ids; sorted alphabetically
        expected_ids = sorted({
            "9f3a1b22-5c7e-4a10-8e44-2d1f6b0c9e01",
            "c2b88d14-7f00-42ab-9a6d-8e5411d73c77",
        })
        assert call_args.session_ref == ",".join(expected_ids)

    @pytest.mark.asyncio
    async def test_background_task_metadata_contains_expected_fields(self, api_client, mock_dl):
        """Background task stores worktree, bead_id, agent, turn_count, last_ts in metadata."""
        mock_summary = {"title": "T", "content": "C", "narrative": "N"}

        with patch("open_brain.server.get_dl", return_value=mock_dl):
            with patch("open_brain.server.llm_complete", new_callable=AsyncMock, return_value="{}"):
                with patch("open_brain.server.parse_llm_json", return_value=mock_summary):
                    from open_brain.server import _process_worktree_session_summary
                    await _process_worktree_session_summary(SAMPLE_BODY)

        call_args = mock_dl.save_memory.call_args[0][0]
        meta = call_args.metadata
        assert meta["worktree"] == ".claude/worktrees/bead-open-brain-krx"
        assert meta["bead_id"] == "open-brain-krx"
        assert meta["agent"] == "claude-code"
        assert meta["turn_count"] == 3
        assert meta["last_ts"] == "2026-04-17T10:42:18Z"
