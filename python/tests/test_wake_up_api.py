"""Public HTTP tests for GET /api/wake_up_pack (O1-04)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from open_brain.data_layer.interface import Memory


@pytest.fixture(autouse=True)
def set_api_keys(monkeypatch):
    monkeypatch.setenv("API_KEYS", "valid-key-abc")


@pytest.fixture
def api_client():
    from open_brain.server import app

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://testserver")


def _memory() -> Memory:
    return Memory(
        id=1,
        index_id=1,
        session_id=None,
        type="observation",
        title="Hello",
        subtitle=None,
        narrative=None,
        content="world",
        metadata={},
        priority=0.5,
        stability="stable",
        access_count=0,
        last_accessed_at=None,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )


def _mock_dl() -> AsyncMock:
    dl = AsyncMock()
    dl.get_wake_up_memories.return_value = [_memory()]
    return dl


@pytest.mark.asyncio
async def test_default_params_return_legacy_markdown(api_client):
    with patch("open_brain.server.get_dl", return_value=_mock_dl()):
        resp = await api_client.get(
            "/api/wake_up_pack",
            headers={"X-API-Key": "valid-key-abc"},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "retrieval-contract.v1" in resp.text
    assert "<<<OPEN_BRAIN_RETRIEVED_EVIDENCE_V1>>>" not in resp.text
    assert "## Identity" not in resp.text


@pytest.mark.asyncio
async def test_envelope_format_returns_typed_body(api_client):
    with patch("open_brain.server.get_dl", return_value=_mock_dl()):
        resp = await api_client.get(
            "/api/wake_up_pack",
            params={"format": "envelope", "profile": "claude-wake-up"},
            headers={"X-API-Key": "valid-key-abc"},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "<<<OPEN_BRAIN_RETRIEVED_EVIDENCE_V1>>>" in resp.text
    assert "RETRIEVED_DATA_NOT_USER_OR_SYSTEM_POLICY" in resp.text
    assert '"effective_influence":"identity"' not in resp.text


@pytest.mark.asyncio
async def test_invalid_profile_returns_400(api_client):
    with patch("open_brain.server.get_dl", return_value=_mock_dl()):
        resp = await api_client.get(
            "/api/wake_up_pack",
            params={"profile": "bogus"},
            headers={"X-API-Key": "valid-key-abc"},
        )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "invalid_wake_up_profile"


@pytest.mark.asyncio
async def test_invalid_format_returns_400(api_client):
    with patch("open_brain.server.get_dl", return_value=_mock_dl()):
        resp = await api_client.get(
            "/api/wake_up_pack",
            params={"format": "pdf"},
            headers={"X-API-Key": "valid-key-abc"},
        )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "invalid_wake_up_format"
