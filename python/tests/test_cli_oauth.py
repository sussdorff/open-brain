"""OAuth login, persistence, refresh, and CLI routing tests."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import open_brain.cli.client as cli_client
import open_brain.cli.main as cli_main
from open_brain.cli.main import _build_parser
from open_brain.cli.oauth import (
    OAuthError,
    OAuthSession,
    _pkce_pair,
    delete_oauth_session,
    load_oauth_session,
    oauth_status,
    save_oauth_session,
    server_origin,
)


def _jwt(subject: str = "malte") -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": subject}).encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )
    return f"header.{payload}.signature"


def _session(**overrides: object) -> OAuthSession:
    values: dict[str, object] = {
        "issuer": "https://brain.example.com",
        "token_endpoint": "https://brain.example.com/token",
        "revocation_endpoint": "https://brain.example.com/revoke",
        "client_id": "client-1",
        "access_token": _jwt(),
        "refresh_token": "refresh-secret",
        "expires_at": time.time() + 3600,
        "scope": "memory evolution",
    }
    values.update(overrides)
    return OAuthSession(**values)  # type: ignore[arg-type]


class TestOAuthPersistence:
    def test_round_trip_is_owner_only_and_atomic(self, tmp_path: Path) -> None:
        path = tmp_path / "open-brain" / "oauth.json"
        session = _session()

        save_oauth_session(session, path)

        assert load_oauth_session(path) == session
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert list(path.parent.glob("*.tmp")) == []

    def test_delete_removes_only_requested_oauth_file(self, tmp_path: Path) -> None:
        oauth_path = tmp_path / "oauth.json"
        api_config = tmp_path / "config.json"
        save_oauth_session(_session(), oauth_path)
        api_config.write_text('{"api_key":"legacy"}', encoding="utf-8")

        assert delete_oauth_session(oauth_path) is True
        assert delete_oauth_session(oauth_path) is False
        assert api_config.exists()

    def test_status_never_contains_token_material(self) -> None:
        session = _session()

        result = oauth_status(session)
        serialized = json.dumps(result)

        assert result["reviewer"] == "malte"
        assert session.access_token not in serialized
        assert session.refresh_token not in serialized
        assert "access_token" not in result
        assert "refresh_token" not in result


class TestOAuthProtocolHelpers:
    def test_pkce_pair_is_s256(self) -> None:
        verifier, challenge = _pkce_pair()

        assert 43 <= len(verifier) <= 128
        assert len(challenge) == 43
        assert "=" not in challenge

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://brain.example.com/mcp/mcp", "https://brain.example.com"),
            ("http://127.0.0.1:8091/mcp", "http://127.0.0.1:8091"),
        ],
    )
    def test_server_origin(self, url: str, expected: str) -> None:
        assert server_origin(url) == expected

    def test_server_origin_rejects_non_http_url(self) -> None:
        with pytest.raises(OAuthError):
            server_origin("file:///tmp/open-brain")


class TestOAuthCredentialPrecedence:
    def test_saved_oauth_precedes_legacy_api_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OB_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        config_dir = tmp_path / "open-brain"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps({"token": "legacy-bearer", "api_key": "legacy-api-key"}),
            encoding="utf-8",
        )
        save_oauth_session(_session(), config_dir / "oauth.json")

        assert cli_client._load_token() == _session().access_token

    @pytest.mark.asyncio
    async def test_saved_oauth_is_never_sent_to_another_server(self) -> None:
        session = _session()
        with (
            patch.object(
                cli_client,
                "_get_server_url",
                return_value="https://other.example.com/mcp",
            ),
            patch.object(cli_client, "_load_token", return_value=session.access_token),
            patch.object(cli_client, "load_oauth_session", return_value=session),
        ):
            with pytest.raises(cli_client.MCPError, match="different server"):
                await cli_client.call_tool("stats", {})

    @pytest.mark.asyncio
    async def test_http_401_refreshes_and_retries_exactly_once(self) -> None:
        old = _session(access_token="old-access")
        refreshed = _session(access_token="new-access", refresh_token="new-refresh")
        response = httpx.Response(
            401,
            request=httpx.Request("POST", "https://brain.example.com/mcp"),
        )
        unauthorized = httpx.HTTPStatusError(
            "unauthorized",
            request=response.request,
            response=response,
        )
        context_client = MagicMock()
        context_client.__aenter__ = AsyncMock(return_value=context_client)
        context_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch.object(
                cli_client,
                "_get_server_url",
                return_value="https://brain.example.com/mcp",
            ),
            patch.object(cli_client, "_load_token", return_value=old.access_token),
            patch.object(cli_client, "load_oauth_session", return_value=old),
            patch.object(
                cli_client,
                "usable_oauth_session",
                new_callable=AsyncMock,
                side_effect=[old, refreshed],
            ) as usable,
            patch.object(cli_client.httpx, "AsyncClient", return_value=context_client),
            patch.object(
                cli_client,
                "_call_tool_once",
                new_callable=AsyncMock,
                side_effect=[unauthorized, {"ok": True}],
            ) as invoke,
        ):
            result = await cli_client.call_tool("stats", {})

        assert result == {"ok": True}
        assert invoke.await_count == 2
        assert usable.await_count == 2
        assert usable.await_args_list[1].kwargs == {"force_refresh": True}
        assert (
            invoke.await_args_list[1].kwargs["headers"]["Authorization"]
            == "Bearer new-access"
        )


class TestAuthCommands:
    def test_parser_exposes_login_status_and_logout(self) -> None:
        login_args = _build_parser().parse_args(["auth", "login", "--no-browser"])
        status_args = _build_parser().parse_args(["auth", "status"])
        logout_args = _build_parser().parse_args(["auth", "logout"])

        assert login_args.auth_command == "login"
        assert login_args.no_browser is True
        assert status_args.auth_command == "status"
        assert logout_args.auth_command == "logout"

    @pytest.mark.asyncio
    async def test_login_handler_uses_configured_server(self) -> None:
        args = _build_parser().parse_args(["auth", "login", "--timeout", "15"])
        expected = {"authenticated": True, "reviewer": "malte"}

        with (
            patch.object(
                cli_main,
                "_get_server_url",
                return_value="https://brain.example.com/mcp",
            ),
            patch.object(
                cli_main, "login", new_callable=AsyncMock, return_value=expected
            ) as login,
        ):
            result = await cli_main._cmd_auth(args)

        assert result == expected
        login.assert_awaited_once_with(
            "https://brain.example.com/mcp",
            open_browser=True,
            timeout=15.0,
        )


def test_source_contains_no_process_environment_token_output() -> None:
    """Keep the security assertion explicit: token names are never printed."""
    source = Path(cli_main.__file__).with_name("oauth.py").read_text(encoding="utf-8")
    assert "print(session.access_token)" not in source
    assert "print(session.refresh_token)" not in source
