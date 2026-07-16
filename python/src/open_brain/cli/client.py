"""Async HTTP client for MCP JSON-RPC over Streamable HTTP transport."""

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from open_brain.cli.oauth import (
    OAuthError,
    load_oauth_session,
    server_origin,
    usable_oauth_session,
)


DEFAULT_URL = "https://open-brain.sussdorff.org/mcp/mcp"
TOKEN_FILE = Path.home() / ".open-brain" / "token"
URL_TOKEN_FILE = Path.home() / ".open-brain" / "url-token"
LEGACY_CONFIG_FILE = Path.home() / ".open-brain" / "config.json"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
BATCH_ANALYSIS_TIMEOUT_SECONDS = 180.0


class MCPError(Exception):
    """Raised when the MCP server returns an error response."""


def _xdg_config_dir() -> Path:
    """Return the XDG config directory for open-brain."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "open-brain"
    return Path.home() / ".config" / "open-brain"


def _read_json_config(path: Path) -> dict[str, Any]:
    """Read a JSON object config file, returning an empty dict on absence/errors."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _load_client_config() -> dict[str, Any]:
    """Load client config, with XDG config overriding legacy config."""
    config: dict[str, Any] = {}
    config.update(_read_json_config(LEGACY_CONFIG_FILE))
    config.update(_read_json_config(_xdg_config_dir() / "config.json"))
    return config


def _config_str(config: dict[str, Any], *keys: str) -> str | None:
    """Return the first non-empty string value for the given config keys."""
    for key in keys:
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalize_mcp_url(url: str) -> str:
    """Normalize a base server URL to an MCP endpoint URL."""
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.path in ("", "/"):
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                "/mcp",
                parts.query,
                parts.fragment,
            )
        )
    return url


def _load_token() -> str | None:
    """Load OAuth bearer token from env var or token file.

    Returns:
        Token string or None if not configured.
    """
    token = os.environ.get("OB_TOKEN")
    if token:
        return token
    oauth_session = load_oauth_session()
    if oauth_session:
        return oauth_session.access_token
    config_token = _config_str(_load_client_config(), "token", "bearer_token")
    if config_token:
        return config_token
    xdg_token_file = _xdg_config_dir() / "token"
    if xdg_token_file.exists():
        return xdg_token_file.read_text(encoding="utf-8").strip()
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
    token = os.environ.get("OB_URL_TOKEN")
    if token:
        return token
    config_token = _config_str(_load_client_config(), "url_token")
    if config_token:
        return config_token
    xdg_token_file = _xdg_config_dir() / "url-token"
    if xdg_token_file.exists():
        return xdg_token_file.read_text(encoding="utf-8").strip()
    if URL_TOKEN_FILE.exists():
        return URL_TOKEN_FILE.read_text(encoding="utf-8").strip()
    return None


def _load_api_key() -> str | None:
    """Load API key from env var or config file."""
    api_key = os.environ.get("OB_API_KEY")
    if api_key:
        return api_key
    return _config_str(_load_client_config(), "api_key")


def _load_searxng_url() -> str | None:
    """Load SearXNG instance URL from env var or client config.

    Resolution order:
    1. ``OB_SEARXNG_URL`` environment variable
    2. ``searxng_url`` key in ``~/.config/open-brain/config.json``

    Returns:
        SearXNG base URL string, or None if not configured on the client side.
    """
    url = os.environ.get("OB_SEARXNG_URL")
    if url:
        return url
    return _config_str(_load_client_config(), "searxng_url")


def _get_server_url() -> str:
    """Get server URL from env var or default.

    Returns:
        Server URL string.
    """
    url = os.environ.get("OB_URL")
    if url:
        return url
    config = _load_client_config()
    mcp_url = _config_str(config, "mcp_url")
    if mcp_url:
        return mcp_url
    server_url = _config_str(config, "server_url", "url")
    if server_url:
        return _normalize_mcp_url(server_url)
    return DEFAULT_URL


def _with_url_token(url: str, token: str) -> str:
    """Append a URL token query parameter unless the URL already has one."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key == "token" for key, _value in query):
        return url
    query.append(("token", token))
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )


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
    oauth_session = load_oauth_session()
    refreshable_oauth = bool(
        oauth_session
        and not os.environ.get("OB_TOKEN")
        and token == oauth_session.access_token
    )
    if refreshable_oauth and oauth_session is not None:
        try:
            oauth_origin = server_origin(oauth_session.issuer)
            configured_origin = server_origin(url)
        except OAuthError as exc:
            raise MCPError(str(exc)) from exc
        if oauth_origin != configured_origin:
            raise MCPError(
                "Stored OAuth login belongs to a different server; "
                "run 'ob auth logout' or restore the matching OB_URL"
            )
        try:
            oauth_session = await usable_oauth_session()
        except OAuthError as exc:
            raise MCPError(str(exc)) from exc
        if oauth_session:
            token = oauth_session.access_token
    api_key = None
    if not token:
        api_key = _load_api_key()
        if not api_key:
            url_token = _load_url_token()
            if url_token:
                url = _with_url_token(url, url_token)

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif api_key:
        headers["X-API-Key"] = api_key

    try:
        timeout = (
            BATCH_ANALYSIS_TIMEOUT_SECONDS
            if tool_name == "analyze_session_learnings"
            else DEFAULT_REQUEST_TIMEOUT_SECONDS
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(2):
                try:
                    return await _call_tool_once(
                        client,
                        url=url,
                        headers=headers,
                        tool_name=tool_name,
                        arguments=arguments,
                    )
                except httpx.HTTPStatusError as exc:
                    if not (
                        attempt == 0
                        and exc.response.status_code == 401
                        and refreshable_oauth
                    ):
                        raise
                    try:
                        oauth_session = await usable_oauth_session(force_refresh=True)
                    except OAuthError as refresh_exc:
                        raise MCPError(str(refresh_exc)) from refresh_exc
                    if oauth_session is None:
                        raise
                    headers["Authorization"] = f"Bearer {oauth_session.access_token}"

            raise MCPError("OAuth retry limit reached")

    except httpx.ConnectError as e:
        raise MCPError(f"Cannot connect to server at {url}: {e}") from e
    except httpx.TimeoutException as e:
        raise MCPError(f"Request timed out connecting to {url}: {e}") from e
    except httpx.HTTPStatusError as e:
        raise MCPError(
            f"Server returned HTTP {e.response.status_code}: {e.response.text[:200]}"
        ) from e


async def _call_tool_once(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: dict[str, str],
    tool_name: str,
    arguments: dict[str, Any],
) -> Any:
    """Initialize one MCP session and invoke one tool."""
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

    request_headers = dict(headers)
    session_id = init_resp.headers.get("mcp-session-id")
    if session_id:
        request_headers["mcp-session-id"] = session_id

    notif_payload = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    await client.post(url, json=notif_payload, headers=request_headers)

    call_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }
    call_resp = await client.post(url, json=call_payload, headers=request_headers)
    call_resp.raise_for_status()

    content_type = call_resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        result = _parse_sse_response(call_resp.text)
    else:
        result = call_resp.json()

    return _extract_result(result)


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
