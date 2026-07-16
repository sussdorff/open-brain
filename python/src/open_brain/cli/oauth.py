"""OAuth 2.1 login and token persistence for the open-brain CLI."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx


OAUTH_SESSION_FILENAME = "oauth.json"
TOKEN_EXPIRY_SKEW_SECONDS = 60
DEFAULT_LOGIN_TIMEOUT_SECONDS = 300.0


class OAuthError(Exception):
    """Raised when the CLI OAuth flow cannot complete safely."""


@dataclass(frozen=True)
class OAuthSession:
    """Persisted OAuth client registration and tokens."""

    issuer: str
    token_endpoint: str
    revocation_endpoint: str
    client_id: str
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str


def _xdg_config_dir() -> Path:
    """Return the open-brain XDG configuration directory."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "open-brain"
    return Path.home() / ".config" / "open-brain"


def oauth_session_path() -> Path:
    """Return the path used for the persisted OAuth session."""
    return _xdg_config_dir() / OAUTH_SESSION_FILENAME


def load_oauth_session(path: Path | None = None) -> OAuthSession | None:
    """Load a valid-shaped OAuth session without exposing token values."""
    target = path or oauth_session_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return OAuthSession(
            issuer=str(data["issuer"]),
            token_endpoint=str(data["token_endpoint"]),
            revocation_endpoint=str(data["revocation_endpoint"]),
            client_id=str(data["client_id"]),
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            expires_at=float(data["expires_at"]),
            scope=str(data.get("scope", "")),
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def save_oauth_session(session: OAuthSession, path: Path | None = None) -> None:
    """Atomically persist an OAuth session with owner-only permissions."""
    target = path or oauth_session_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(asdict(session), handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def delete_oauth_session(path: Path | None = None) -> bool:
    """Delete only the OAuth session file and report whether it existed."""
    target = path or oauth_session_path()
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False


def server_origin(mcp_url: str) -> str:
    """Reduce an MCP endpoint URL to its HTTP origin."""
    parts = urlsplit(mcp_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise OAuthError("The configured open-brain URL must be an HTTP(S) URL")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


async def discover_oauth_metadata(
    mcp_url: str,
    client: httpx.AsyncClient,
) -> dict[str, str]:
    """Discover and validate the server OAuth metadata."""
    origin = server_origin(mcp_url)
    response = await client.get(f"{origin}/.well-known/oauth-authorization-server")
    response.raise_for_status()
    body = response.json()
    required = (
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
        "revocation_endpoint",
    )
    if not isinstance(body, dict) or any(
        not isinstance(body.get(key), str) for key in required
    ):
        raise OAuthError("The server returned incomplete OAuth metadata")
    for key in required:
        endpoint = str(body[key])
        if server_origin(endpoint) != origin:
            raise OAuthError(f"OAuth metadata {key} points to a different origin")
    return {key: str(body[key]) for key in required}


def _pkce_pair() -> tuple[str, str]:
    """Create an RFC 7636 verifier and S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


async def _receive_callback(
    future: asyncio.Future[dict[str, str]],
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Receive one loopback redirect without logging its authorization code."""
    status = "200 OK"
    message = "Authorization received. You can close this window."
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5)
        parts = request_line.decode("ascii", errors="replace").strip().split(" ")
        if len(parts) != 3 or parts[0] != "GET":
            raise ValueError("Invalid callback request")
        query = parse_qs(urlsplit(parts[1]).query)
        payload = {key: values[0] for key, values in query.items() if values}
        if not future.done():
            future.set_result(payload)
    except Exception as exc:
        status = "400 Bad Request"
        message = "The authorization callback was invalid. Return to the terminal."
        if not future.done():
            future.set_exception(OAuthError(str(exc)))
    body = (f"<!doctype html><html><body><p>{message}</p></body></html>").encode(
        "utf-8"
    )
    writer.write(
        f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
        + body
    )
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def login(
    mcp_url: str,
    *,
    open_browser: bool = True,
    timeout: float = DEFAULT_LOGIN_TIMEOUT_SECONDS,
    scope: str = "memory evolution",
) -> dict[str, Any]:
    """Run dynamic registration plus loopback authorization-code login."""
    loop = asyncio.get_running_loop()
    callback: asyncio.Future[dict[str, str]] = loop.create_future()

    async def callback_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await _receive_callback(callback, reader, writer)

    server = await asyncio.start_server(
        callback_handler,
        host="127.0.0.1",
        port=0,
    )
    socket = server.sockets[0]
    redirect_uri = f"http://127.0.0.1:{socket.getsockname()[1]}/callback"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            metadata = await discover_oauth_metadata(mcp_url, client)
            registration = await client.post(
                metadata["registration_endpoint"],
                json={
                    "client_name": "open-brain CLI",
                    "redirect_uris": [redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "scope": scope,
                    "token_endpoint_auth_method": "none",
                },
            )
            registration.raise_for_status()
            client_id = registration.json().get("client_id")
            if not isinstance(client_id, str) or not client_id:
                raise OAuthError("Dynamic client registration returned no client_id")

            state = secrets.token_urlsafe(32)
            verifier, challenge = _pkce_pair()
            authorization_url = (
                metadata["authorization_endpoint"]
                + "?"
                + urlencode(
                    {
                        "client_id": client_id,
                        "redirect_uri": redirect_uri,
                        "response_type": "code",
                        "scope": scope,
                        "state": state,
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                    }
                )
            )
            if open_browser:
                if not webbrowser.open(authorization_url):
                    raise OAuthError("Could not open a browser for OAuth login")
            else:
                print(f"Open this URL in a browser:\n{authorization_url}")

            parameters = await asyncio.wait_for(callback, timeout=timeout)
            if not secrets.compare_digest(parameters.get("state", ""), state):
                raise OAuthError("OAuth callback state did not match")
            if parameters.get("error"):
                raise OAuthError(f"Authorization failed: {parameters['error']}")
            code = parameters.get("code")
            if not code:
                raise OAuthError("OAuth callback contained no authorization code")

            token_response = await client.post(
                metadata["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                },
            )
            token_response.raise_for_status()
            session = _session_from_token_response(
                token_response.json(),
                metadata=metadata,
                client_id=client_id,
                scope=scope,
            )
            save_oauth_session(session)
            return oauth_status(session)
    except asyncio.TimeoutError as exc:
        raise OAuthError("Timed out waiting for OAuth authorization") from exc
    except httpx.HTTPStatusError as exc:
        raise OAuthError(
            f"OAuth server request failed with HTTP {exc.response.status_code}"
        ) from exc
    except httpx.RequestError as exc:
        raise OAuthError("Could not reach the OAuth server") from exc
    finally:
        server.close()
        await server.wait_closed()


def _session_from_token_response(
    body: Any,
    *,
    metadata: dict[str, str],
    client_id: str,
    scope: str,
) -> OAuthSession:
    """Validate a token response and build its persisted representation."""
    if not isinstance(body, dict):
        raise OAuthError("OAuth token endpoint returned an invalid response")
    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    expires_in = body.get("expires_in")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise OAuthError("OAuth token endpoint returned incomplete tokens")
    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
        raise OAuthError("OAuth token endpoint returned an invalid expiry")
    return OAuthSession(
        issuer=metadata["issuer"],
        token_endpoint=metadata["token_endpoint"],
        revocation_endpoint=metadata["revocation_endpoint"],
        client_id=client_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=time.time() + float(expires_in),
        scope=scope,
    )


async def refresh_oauth_session(session: OAuthSession) -> OAuthSession:
    """Refresh and atomically persist a rotated OAuth token pair."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                session.token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "client_id": session.client_id,
                    "refresh_token": session.refresh_token,
                    "scope": session.scope,
                },
            )
            response.raise_for_status()
            refreshed = _session_from_token_response(
                response.json(),
                metadata={
                    "issuer": session.issuer,
                    "token_endpoint": session.token_endpoint,
                    "revocation_endpoint": session.revocation_endpoint,
                },
                client_id=session.client_id,
                scope=session.scope,
            )
            save_oauth_session(refreshed)
            return refreshed
    except httpx.HTTPError as exc:
        raise OAuthError(
            "OAuth token refresh failed; run 'ob auth login' again"
        ) from exc


async def usable_oauth_session(*, force_refresh: bool = False) -> OAuthSession | None:
    """Return a usable saved session, refreshing it when needed."""
    session = load_oauth_session()
    if session is None:
        return None
    if force_refresh or session.expires_at <= time.time() + TOKEN_EXPIRY_SKEW_SECONDS:
        return await refresh_oauth_session(session)
    return session


def _jwt_subject(token: str) -> str | None:
    """Read an unverified JWT subject for redacted local status display only."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        subject = decoded.get("sub")
        return str(subject) if subject else None
    except (IndexError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def oauth_status(session: OAuthSession | None = None) -> dict[str, Any]:
    """Return redacted OAuth status suitable for terminal or JSON output."""
    current = session or load_oauth_session()
    if current is None:
        return {"authenticated": False, "message": "No OAuth login is stored."}
    return {
        "authenticated": True,
        "reviewer": _jwt_subject(current.access_token),
        "issuer": current.issuer,
        "client_id": current.client_id,
        "scope": current.scope,
        "expires_at": current.expires_at,
        "expired": current.expires_at <= time.time(),
    }


async def logout() -> dict[str, Any]:
    """Revoke saved OAuth tokens and remove only OAuth state."""
    session = load_oauth_session()
    if session is None:
        return {
            "authenticated": False,
            "removed": False,
            "message": "No OAuth login was stored.",
        }
    warnings: list[str] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for token, hint in (
            (session.access_token, "access_token"),
            (session.refresh_token, "refresh_token"),
        ):
            try:
                response = await client.post(
                    session.revocation_endpoint,
                    data={"token": token, "token_type_hint": hint},
                )
                response.raise_for_status()
            except httpx.HTTPError:
                warnings.append(f"Server revocation failed for {hint}")
    removed = delete_oauth_session()
    return {
        "authenticated": False,
        "removed": removed,
        "warnings": warnings,
        "message": "OAuth login removed.",
    }
