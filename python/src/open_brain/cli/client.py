"""Async HTTP client for MCP JSON-RPC over Streamable HTTP transport."""

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx


DEFAULT_URL = "https://open-brain.sussdorff.org/mcp/mcp"
TOKEN_FILE = Path.home() / ".open-brain" / "token"
URL_TOKEN_FILE = Path.home() / ".open-brain" / "url-token"


class MCPError(Exception):
    """Raised when the MCP server returns an error response."""


def _load_token() -> str | None:
    """Load OAuth bearer token from env var or token file.

    Returns:
        Token string or None if not configured.
    """
    import os

    token = os.environ.get("OB_TOKEN")
    if token:
        return token
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    return None


def _load_url_token() -> str | None:
    """Load URL token from env var or token file.

    URL tokens are issued by the server's /token/url endpoint and must be sent
    as a `?token=` query parameter, not as an OAuth Bearer token.

    Returns:
        URL token string or None if not configured.
    """
    import os

    token = os.environ.get("OB_URL_TOKEN")
    if token:
        return token
    if URL_TOKEN_FILE.exists():
        return URL_TOKEN_FILE.read_text(encoding="utf-8").strip()
    return None


def _get_server_url() -> str:
    """Get server URL from env var or default.

    Returns:
        Server URL string.
    """
    import os

    return os.environ.get("OB_URL", DEFAULT_URL)


def _with_url_token(url: str, token: str) -> str:
    """Append a URL token query parameter unless the URL already has one."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key == "token" for key, _value in query):
        return url
    query.append(("token", token))
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(query),
        parts.fragment,
    ))


async def call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """Call an MCP tool via JSON-RPC over Streamable HTTP.

    Initializes an MCP session, then calls the specified tool.

    Args:
        tool_name: Name of the MCP tool to call.
        arguments: Arguments to pass to the tool.

    Returns:
        Parsed response data from the tool.

    Raises:
        MCPError: If the server returns an error or is unreachable.
    """
    url = _get_server_url()
    token = _load_token()
    if not token:
        url_token = _load_url_token()
        if url_token:
            url = _with_url_token(url, url_token)

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: Initialize session
            init_payload = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "ob-cli", "version": "1.0.0"},
                },
            }
            init_resp = await client.post(url, json=init_payload, headers=headers)
            init_resp.raise_for_status()

            # Extract session ID from response headers
            session_id = init_resp.headers.get("mcp-session-id")
            if session_id:
                headers["mcp-session-id"] = session_id

            # Step 2: Send initialized notification
            notif_payload = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
            await client.post(url, json=notif_payload, headers=headers)

            # Step 3: Call the tool
            call_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments,
                },
            }
            call_resp = await client.post(url, json=call_payload, headers=headers)
            call_resp.raise_for_status()

            # Parse SSE or JSON response
            content_type = call_resp.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                result = _parse_sse_response(call_resp.text)
            else:
                result = call_resp.json()

            return _extract_result(result)

    except httpx.ConnectError as e:
        raise MCPError(f"Cannot connect to server at {url}: {e}") from e
    except httpx.TimeoutException as e:
        raise MCPError(f"Request timed out connecting to {url}: {e}") from e
    except httpx.HTTPStatusError as e:
        raise MCPError(
            f"Server returned HTTP {e.response.status_code}: {e.response.text[:200]}"
        ) from e


def _parse_sse_response(text: str) -> dict[str, Any]:
    """Parse Server-Sent Events response to extract JSON-RPC result.

    Args:
        text: Raw SSE response text.

    Returns:
        Parsed JSON-RPC response dict.

    Raises:
        MCPError: If no valid JSON-RPC result found in SSE stream.
    """
    for line in text.splitlines():
        if line.startswith("data: "):
            data = line[6:].strip()
            if data and data != "[DONE]":
                try:
                    return json.loads(data)
                except json.JSONDecodeError:
                    continue
    raise MCPError("No valid JSON-RPC result found in SSE response")


def _extract_result(response: dict[str, Any]) -> Any:
    """Extract the actual result payload from a JSON-RPC response.

    Args:
        response: Full JSON-RPC response dict.

    Returns:
        Parsed result data.

    Raises:
        MCPError: If the response contains an error.
    """
    if "error" in response:
        err = response["error"]
        msg = err.get("message", str(err))
        raise MCPError(f"MCP error: {msg}")

    result = response.get("result", {})

    # MCP tool responses have content as a list of content items
    content = result.get("content", [])
    if content and isinstance(content, list):
        for item in content:
            if item.get("type") == "text":
                text = item.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text

    return result
