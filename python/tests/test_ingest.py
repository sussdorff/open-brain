"""Tests for REST API endpoints: /api/ingest, /api/context, /api/summarize."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from open_brain.auth.tokens import TokenClaims, issue_access_token
from open_brain.data_layer.interface import (
    Memory,
    SaveMemoryResult,
    SearchResult,
)


def _make_memory(id: int = 1, **kwargs) -> Memory:
    defaults = dict(
        index_id=1,
        session_id=None,
        type="observation",
        title="Test Memory",
        subtitle=None,
        narrative=None,
        content="Test content",
        metadata={},
        priority=0.5,
        stability="stable",
        access_count=0,
        last_accessed_at=None,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    defaults.update(kwargs)
    return Memory(id=id, **defaults)


@pytest.fixture
def mock_dl():
    dl = AsyncMock()
    dl.search.return_value = SearchResult(results=[_make_memory()], total=1)
    dl.save_memory.return_value = SaveMemoryResult(id=99, message="Memory saved")
    dl.get_context.return_value = {"sessions": []}
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


def _bearer_headers() -> str:
    claims = TokenClaims(sub="testuser", client_id="test-client", scopes=[])
    return f"Bearer {issue_access_token(claims)}"


# ─── POST /api/ingest ─────────────────────────────────────────────────────────

class TestApiIngest:
    @pytest.mark.asyncio
    async def test_valid_api_key_returns_202(self, api_client, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            with patch("open_brain.server.asyncio.create_task"):
                resp = await api_client.post(
                    "/api/ingest",
                    json={
                        "tool_name": "Edit",
                        "tool_input": "some code change",
                        "tool_response": "success",
                        "project": "open-brain",
                        "session_id": "sess-123",
                    },
                    headers=_api_headers(),
                )
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_skipped_tool_returns_202_with_status_skipped(self, api_client):
        """Tools in the server-side skip list return 202 with status=skipped."""
        resp = await api_client.post(
            "/api/ingest",
            json={"tool_name": "Read"},
            headers=_api_headers(),
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_glob_tool_is_skipped(self, api_client):
        resp = await api_client.post(
            "/api/ingest",
            json={"tool_name": "Glob"},
            headers=_api_headers(),
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_grep_tool_is_skipped(self, api_client):
        resp = await api_client.post(
            "/api/ingest",
            json={"tool_name": "Grep"},
            headers=_api_headers(),
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_edit_tool_is_not_skipped(self, api_client, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            with patch("open_brain.server.asyncio.create_task"):
                resp = await api_client.post(
                    "/api/ingest",
                    json={"tool_name": "Edit", "tool_input": "x", "tool_response": "y"},
                    headers=_api_headers(),
                )
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_401(self, api_client):
        resp = await api_client.post(
            "/api/ingest",
            json={"tool_name": "Edit"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_api_key_returns_401(self, api_client):
        resp = await api_client.post(
            "/api/ingest",
            json={"tool_name": "Edit"},
            headers={"X-API-Key": "wrong-key"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_bearer_token_also_accepted(self, api_client, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            with patch("open_brain.server.asyncio.create_task"):
                resp = await api_client.post(
                    "/api/ingest",
                    json={"tool_name": "Write"},
                    headers={"Authorization": _bearer_headers()},
                )
        assert resp.status_code == 202

    @pytest.mark.asyncio
    async def test_create_task_is_called_for_non_skipped_tool(self, api_client, mock_dl):
        """Background processing task is spawned for non-skipped tools."""
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            with patch("open_brain.server.asyncio.create_task") as mock_task:
                await api_client.post(
                    "/api/ingest",
                    json={"tool_name": "Bash", "tool_input": "npm test", "tool_response": "ok"},
                    headers=_api_headers(),
                )
        mock_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_task_not_called_for_skipped_tool(self, api_client):
        """Background task is NOT spawned when the tool is in the skip list."""
        with patch("open_brain.server.asyncio.create_task") as mock_task:
            await api_client.post(
                "/api/ingest",
                json={"tool_name": "Read"},
                headers=_api_headers(),
            )
        mock_task.assert_not_called()


# ─── GET /api/context ─────────────────────────────────────────────────────────

class TestApiContext:
    @pytest.mark.asyncio
    async def test_returns_markdown_content_type(self, api_client, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            resp = await api_client.get("/api/context", headers=_api_headers())
        assert resp.status_code == 200
        assert "text/markdown" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_includes_observations_in_response(self, api_client, mock_dl):
        mock_dl.search.return_value = SearchResult(
            results=[_make_memory(id=1, title="Fixed auth bug", type="bugfix")],
            total=1,
        )
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            resp = await api_client.get("/api/context", headers=_api_headers())
        assert "Fixed auth bug" in resp.text

    @pytest.mark.asyncio
    async def test_includes_session_summaries_when_present(self, api_client, mock_dl):
        session_mem = _make_memory(
            id=99, title="Big refactor session", type="session_summary"
        )
        mock_dl.search.return_value = SearchResult(results=[session_mem], total=1)
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            resp = await api_client.get("/api/context", headers=_api_headers())
        assert "Big refactor session" in resp.text

    @pytest.mark.asyncio
    async def test_returns_empty_string_when_no_data(self, api_client, mock_dl):
        mock_dl.search.return_value = SearchResult(results=[], total=0)
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            resp = await api_client.get("/api/context", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.text == ""

    @pytest.mark.asyncio
    async def test_project_param_is_passed_to_data_layer(self, api_client, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            await api_client.get(
                "/api/context?project=my-repo",
                headers=_api_headers(),
            )
        search_call = mock_dl.search.call_args[0][0]
        assert search_call.project == "my-repo"

    @pytest.mark.asyncio
    async def test_limit_param_is_passed_to_data_layer(self, api_client, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            await api_client.get(
                "/api/context?limit=10",
                headers=_api_headers(),
            )
        search_call = mock_dl.search.call_args[0][0]
        assert search_call.limit == 10

    @pytest.mark.asyncio
    async def test_unauthorized_returns_401(self, api_client):
        resp = await api_client.get("/api/context")
        assert resp.status_code == 401


# ─── POST /api/summarize ──────────────────────────────────────────────────────

class TestApiSummarize:
    @pytest.mark.asyncio
    async def test_returns_202_accepted(self, api_client, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            with patch("open_brain.server.asyncio.create_task"):
                resp = await api_client.post(
                    "/api/summarize",
                    json={"session_id": "sess-456", "project": "open-brain", "hook_type": "Stop"},
                    headers=_api_headers(),
                )
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_create_task_is_called(self, api_client, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            with patch("open_brain.server.asyncio.create_task") as mock_task:
                await api_client.post(
                    "/api/summarize",
                    json={"session_id": "sess-789", "project": "proj"},
                    headers=_api_headers(),
                )
        mock_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_401(self, api_client):
        resp = await api_client.post(
            "/api/summarize",
            json={"session_id": "sess-123"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_bearer_token_also_accepted(self, api_client, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            with patch("open_brain.server.asyncio.create_task"):
                resp = await api_client.post(
                    "/api/summarize",
                    json={"session_id": "s1", "project": "p1"},
                    headers={"Authorization": _bearer_headers()},
                )
        assert resp.status_code == 202
