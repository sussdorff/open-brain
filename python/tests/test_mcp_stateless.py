"""Protocol regression tests for MCP 2026-07-28 and legacy clients."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from mcp import Client
from mcp_types.version import LATEST_HANDSHAKE_VERSION


MODERN_PROTOCOL_VERSION = "2026-07-28"


def _modern_request(
    method: str,
    *,
    request_id: int = 1,
    params: dict[str, object] | None = None,
) -> dict[str, object]:
    request_params = dict(params or {})
    request_params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {
            "name": "open-brain-test",
            "version": "1.0.0",
        },
    }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": request_params,
    }


@pytest.mark.asyncio
async def test_stateless_http_flow_preserves_auth_and_omits_session_id(monkeypatch):
    """Modern discovery, listing, and calls stay authenticated and session-free."""
    monkeypatch.setenv("API_KEYS", "stateless-test-key")

    from open_brain import config as config_module
    from open_brain.server import app

    config_module._config = None
    base_headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
        "X-API-Key": "stateless-test-key",
    }
    data_layer = AsyncMock()
    data_layer.stats.return_value = {"memories": 0, "sessions": 0}

    with patch("open_brain.server.get_dl", return_value=data_layer):
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(
                transport=transport,
                base_url="http://localhost:8091",
            ) as client:
                unauthenticated = await client.post(
                    "/mcp",
                    headers={
                        key: value
                        for key, value in base_headers.items()
                        if key != "X-API-Key"
                    }
                    | {"Mcp-Method": "server/discover"},
                    json=_modern_request("server/discover"),
                )
                discovery = await client.post(
                    "/mcp",
                    headers=base_headers | {"Mcp-Method": "server/discover"},
                    json=_modern_request("server/discover"),
                )
                tool_list = await client.post(
                    "/mcp",
                    headers=base_headers | {"Mcp-Method": "tools/list"},
                    json=_modern_request("tools/list", request_id=2),
                )
                tool_call = await client.post(
                    "/mcp",
                    headers=base_headers
                    | {"Mcp-Method": "tools/call", "Mcp-Name": "stats"},
                    json=_modern_request(
                        "tools/call",
                        request_id=3,
                        params={"name": "stats", "arguments": {}},
                    ),
                )

    assert unauthenticated.status_code == 401
    assert discovery.status_code == 200
    assert "mcp-session-id" not in discovery.headers
    result = discovery.json()["result"]
    assert result["supportedVersions"] == [MODERN_PROTOCOL_VERSION]
    assert result["resultType"] == "complete"
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "open-brain"

    assert tool_list.status_code == 200
    assert "mcp-session-id" not in tool_list.headers
    tool_names = {tool["name"] for tool in tool_list.json()["result"]["tools"]}
    assert "stats" in tool_names
    assert "analyze_session_learnings" in tool_names
    assert "promote_memory_authority" not in tool_names

    assert tool_call.status_code == 200
    assert "mcp-session-id" not in tool_call.headers
    assert tool_call.json()["result"]["resultType"] == "complete"
    data_layer.stats.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_version"),
    [
        ("auto", MODERN_PROTOCOL_VERSION),
        ("legacy", LATEST_HANDSHAKE_VERSION),
    ],
)
async def test_modern_and_legacy_clients_list_and_call_authorized_tools(
    mode: str,
    expected_version: str,
):
    """SDK v2 serves both protocol eras through one scope-filtered server."""
    from open_brain.server import _current_scopes, mcp

    data_layer = AsyncMock()
    data_layer.stats.return_value = {
        "memories": 0,
        "sessions": 0,
        "relationships": 0,
        "db_size_bytes": 0,
        "db_size_mb": 0,
        "by_type": {},
        "by_user": {},
    }
    scopes_token = _current_scopes.set(("memory",))
    try:
        with patch("open_brain.server.get_dl", return_value=data_layer):
            async with Client(mcp, mode=mode, cache=None) as client:
                assert client.protocol_version == expected_version
                tool_names = {
                    tool.name for tool in (await client.list_tools()).tools
                }
                assert "stats" in tool_names
                assert "analyze_session_learnings" not in tool_names

                result = await client.call_tool("stats", {})
                assert result.is_error is False
    finally:
        _current_scopes.reset(scopes_token)
