"""AK 2: JWT auth tests and OAuth flow logic tests."""

from __future__ import annotations

import time

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from open_brain.auth.tokens import (
    TokenClaims,
    VerifiedToken,
    issue_access_token,
    issue_refresh_token,
    verify_token,
)
from open_brain.auth.provider import OAuthProvider


# ─── Token Generation Tests ────────────────────────────────────────────────────

class TestIssueAccessToken:
    def test_returns_string(self):
        claims = TokenClaims(sub="user1", client_id="client1", scopes=["read"])
        token = issue_access_token(claims)
        assert isinstance(token, str)
        assert len(token) > 0

    def test_jwt_header_alg_hs256(self):
        claims = TokenClaims(sub="user1", client_id="client1", scopes=["read"])
        token = issue_access_token(claims)
        header = jwt.get_unverified_header(token)
        assert header["alg"] == "HS256"

    def test_contains_correct_claims(self):
        claims = TokenClaims(sub="testuser", client_id="my-client", scopes=["read", "write"])
        token = issue_access_token(claims)
        # Decode without verification for payload inspection
        payload = jwt.decode(token, options={"verify_signature": False})
        assert payload["sub"] == "testuser"
        assert payload["clientId"] == "my-client"
        assert payload["scopes"] == ["read", "write"]

    def test_expires_in_one_hour(self):
        claims = TokenClaims(sub="user1", client_id="c1", scopes=[])
        before = int(time.time())
        token = issue_access_token(claims)
        payload = jwt.decode(token, options={"verify_signature": False})
        # exp should be roughly now + 3600 seconds
        assert payload["exp"] >= before + 3590
        assert payload["exp"] <= before + 3610

    def test_does_not_have_refresh_type(self):
        claims = TokenClaims(sub="u", client_id="c", scopes=[])
        token = issue_access_token(claims)
        payload = jwt.decode(token, options={"verify_signature": False})
        assert "type" not in payload or payload.get("type") != "refresh"

    def test_issuer_matches_config(self, config):
        claims = TokenClaims(sub="u", client_id="c", scopes=[])
        token = issue_access_token(claims)
        payload = jwt.decode(token, options={"verify_signature": False})
        assert payload["iss"] == config.MCP_SERVER_URL


class TestIssueRefreshToken:
    def test_returns_string(self):
        claims = TokenClaims(sub="user1", client_id="client1", scopes=["read"])
        token = issue_refresh_token(claims)
        assert isinstance(token, str)

    def test_has_refresh_type(self):
        claims = TokenClaims(sub="u", client_id="c", scopes=[])
        token = issue_refresh_token(claims)
        payload = jwt.decode(token, options={"verify_signature": False})
        assert payload.get("type") == "refresh"

    def test_expires_in_30_days(self):
        claims = TokenClaims(sub="u", client_id="c", scopes=[])
        before = int(time.time())
        token = issue_refresh_token(claims)
        payload = jwt.decode(token, options={"verify_signature": False})
        expected = before + 30 * 24 * 3600
        assert payload["exp"] >= expected - 10
        assert payload["exp"] <= expected + 10


# ─── Token Verification Tests ──────────────────────────────────────────────────

class TestVerifyToken:
    def test_verifies_access_token(self):
        claims = TokenClaims(sub="testuser", client_id="client1", scopes=["read"])
        token = issue_access_token(claims)
        verified = verify_token(token)
        assert isinstance(verified, VerifiedToken)
        assert verified.sub == "testuser"
        assert verified.client_id == "client1"
        assert verified.scopes == ["read"]

    def test_verifies_refresh_token(self):
        claims = TokenClaims(sub="u", client_id="c", scopes=["read", "write"])
        token = issue_refresh_token(claims)
        verified = verify_token(token)
        assert verified.token_type == "refresh"
        assert verified.scopes == ["read", "write"]

    def test_raises_for_invalid_token(self):
        with pytest.raises(Exception):
            verify_token("not-a-valid-jwt")

    def test_raises_for_wrong_secret(self, config):
        import jwt as pyjwt
        # Sign with a different secret
        payload = {"sub": "u", "iss": config.MCP_SERVER_URL, "exp": int(time.time()) + 3600}
        bad_token = pyjwt.encode(payload, "wrong-secret-that-is-long-enough-32chars", algorithm="HS256")
        with pytest.raises(Exception):
            verify_token(bad_token)

    def test_raises_for_expired_token(self, config):
        import jwt as pyjwt
        payload = {
            "sub": "u",
            "clientId": "c",
            "scopes": [],
            "iss": config.MCP_SERVER_URL,
            "exp": int(time.time()) - 1,  # expired
            "iat": int(time.time()) - 3700,
        }
        expired_token = pyjwt.encode(payload, config.JWT_SECRET, algorithm="HS256")
        with pytest.raises(Exception):
            verify_token(expired_token)

    def test_raises_for_wrong_issuer(self, config):
        import jwt as pyjwt
        payload = {
            "sub": "u",
            "clientId": "c",
            "scopes": [],
            "iss": "http://wrong-issuer.example.com",
            "exp": int(time.time()) + 3600,
        }
        token = pyjwt.encode(payload, config.JWT_SECRET, algorithm="HS256")
        with pytest.raises(Exception):
            verify_token(token)


# ─── OAuth Provider Flow Tests ─────────────────────────────────────────────────

class TestOAuthProvider:
    def test_build_login_form_contains_client_name(self, oauth_provider):
        html = oauth_provider.build_login_form(
            client_name="My MCP Client",
            client_id="client123",
            redirect_uri="http://localhost/callback",
            code_challenge="abc123",
            state="mystate",
            scopes=["read"],
        )
        assert "My MCP Client" in html
        assert "client123" in html
        assert "mystate" in html

    def test_handle_login_submit_success(self, oauth_provider):
        success, redirect_url = oauth_provider.handle_login_submit(
            username="testuser",
            password="testpassword123",
            client_id="client1",
            redirect_uri="http://localhost/callback",
            code_challenge="challenge",
            state="state1",
            scopes_str="read write",
        )
        assert success is True
        assert "code=" in redirect_url
        assert "state=state1" in redirect_url

    def test_handle_login_submit_wrong_password(self, oauth_provider):
        success, redirect_url = oauth_provider.handle_login_submit(
            username="testuser",
            password="wrongpassword",
            client_id="client1",
            redirect_uri="http://localhost/callback",
            code_challenge="challenge",
            state="",
            scopes_str="",
        )
        assert success is False
        assert "error=access_denied" in redirect_url

    def test_handle_login_submit_wrong_username(self, oauth_provider):
        success, redirect_url = oauth_provider.handle_login_submit(
            username="wronguser",
            password="testpassword123",
            client_id="client1",
            redirect_uri="http://localhost/callback",
            code_challenge="challenge",
            state="",
            scopes_str="",
        )
        assert success is False
        assert "error=access_denied" in redirect_url

    def test_get_code_challenge_success(self, oauth_provider):
        success, redirect_url = oauth_provider.handle_login_submit(
            username="testuser",
            password="testpassword123",
            client_id="client1",
            redirect_uri="http://localhost/callback",
            code_challenge="my-challenge",
            state="",
            scopes_str="",
        )
        assert success is True
        # Extract code from redirect URL
        code = redirect_url.split("code=")[1].split("&")[0]
        challenge = oauth_provider.get_code_challenge(code)
        assert challenge == "my-challenge"

    def test_get_code_challenge_invalid_code(self, oauth_provider):
        with pytest.raises(ValueError, match="Invalid or expired"):
            oauth_provider.get_code_challenge("nonexistent-code")

    def test_exchange_authorization_code_success(self, oauth_provider):
        success, redirect_url = oauth_provider.handle_login_submit(
            username="testuser",
            password="testpassword123",
            client_id="client1",
            redirect_uri="http://localhost/callback",
            code_challenge="challenge",
            state="",
            scopes_str="read",
        )
        assert success is True
        code = redirect_url.split("code=")[1].split("&")[0]
        tokens = oauth_provider.exchange_authorization_code("client1", code)
        assert tokens.token_type == "Bearer"
        assert tokens.expires_in == 3600
        assert tokens.access_token
        assert tokens.refresh_token

    def test_exchange_authorization_code_consumes_code(self, oauth_provider):
        """Authorization code can only be used once."""
        success, redirect_url = oauth_provider.handle_login_submit(
            username="testuser",
            password="testpassword123",
            client_id="client1",
            redirect_uri="http://localhost/callback",
            code_challenge="challenge",
            state="",
            scopes_str="",
        )
        code = redirect_url.split("code=")[1]
        oauth_provider.exchange_authorization_code("client1", code)
        with pytest.raises(ValueError):
            oauth_provider.exchange_authorization_code("client1", code)

    def test_exchange_authorization_code_client_mismatch(self, oauth_provider):
        success, redirect_url = oauth_provider.handle_login_submit(
            username="testuser",
            password="testpassword123",
            client_id="client1",
            redirect_uri="http://localhost/callback",
            code_challenge="challenge",
            state="",
            scopes_str="",
        )
        code = redirect_url.split("code=")[1]
        with pytest.raises(ValueError, match="mismatch"):
            oauth_provider.exchange_authorization_code("other-client", code)

    def test_exchange_refresh_token_success(self, oauth_provider):
        claims = TokenClaims(sub="testuser", client_id="client1", scopes=["read"])
        refresh_token = issue_refresh_token(claims)
        tokens = oauth_provider.exchange_refresh_token("client1", refresh_token)
        assert tokens.access_token
        assert tokens.refresh_token

    def test_exchange_refresh_token_rejects_access_token(self, oauth_provider):
        claims = TokenClaims(sub="testuser", client_id="client1", scopes=[])
        access_token = issue_access_token(claims)
        with pytest.raises(ValueError, match="Not a refresh token"):
            oauth_provider.exchange_refresh_token("client1", access_token)

    def test_verify_access_token_success(self, oauth_provider):
        claims = TokenClaims(sub="testuser", client_id="client1", scopes=["read"])
        access_token = issue_access_token(claims)
        auth_info = oauth_provider.verify_access_token(access_token)
        assert auth_info.client_id == "client1"
        assert auth_info.scopes == ["read"]

    def test_verify_access_token_rejects_refresh_token(self, oauth_provider):
        claims = TokenClaims(sub="u", client_id="c", scopes=[])
        refresh_token = issue_refresh_token(claims)
        with pytest.raises(ValueError, match="Cannot use refresh token"):
            oauth_provider.verify_access_token(refresh_token)

    def test_revoke_token(self, oauth_provider):
        claims = TokenClaims(sub="u", client_id="c", scopes=[])
        token = issue_access_token(claims)
        oauth_provider.revoke_token(token)
        with pytest.raises(ValueError, match="revoked"):
            oauth_provider.verify_access_token(token)

    def test_revoke_refresh_token_prevents_exchange(self, oauth_provider):
        claims = TokenClaims(sub="u", client_id="c", scopes=[])
        refresh_token = issue_refresh_token(claims)
        oauth_provider.revoke_token(refresh_token)
        with pytest.raises(ValueError, match="revoked"):
            oauth_provider.exchange_refresh_token("c", refresh_token)


# ─── Dynamic Client Registration Tests ───────────────────────────────────────

class TestDynamicClientRegistration:
    def test_register_and_get_client(self, oauth_provider):
        client = oauth_provider.register_client(client_name="Test App")
        assert client.client_name == "Test App"
        assert client.client_id
        retrieved = oauth_provider.get_client(client.client_id)
        assert retrieved is not None
        assert retrieved.client_name == "Test App"

    def test_get_unknown_client_returns_none(self, oauth_provider):
        assert oauth_provider.get_client("nonexistent") is None


# ─── Bearer Auth Middleware Tests (HTTP-level) ────────────────────────────────

@pytest.fixture
def auth_client():
    """AsyncClient for testing the FastAPI app with Bearer auth middleware."""
    from open_brain.server import app
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://testserver")


def _make_bearer(scopes: list[str] | None = None) -> str:
    """Issue a valid Bearer token for tests."""
    claims = TokenClaims(sub="testuser", client_id="test-client", scopes=scopes or [])
    return f"Bearer {issue_access_token(claims)}"


class TestBearerAuthMiddleware:
    @pytest.mark.asyncio
    async def test_public_health_no_auth(self, auth_client):
        resp = await auth_client.get("/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_public_oauth_metadata_no_auth(self, auth_client):
        resp = await auth_client.get("/.well-known/oauth-authorization-server")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_mcp_without_token_returns_401(self, auth_client):
        resp = await auth_client.post("/mcp", json={})
        assert resp.status_code == 401
        assert resp.json()["error"] == "unauthorized"
        assert "WWW-Authenticate" in resp.headers

    @pytest.mark.asyncio
    async def test_mcp_with_invalid_token_returns_401(self, auth_client):
        resp = await auth_client.post(
            "/mcp", json={}, headers={"Authorization": "Bearer invalid-token"}
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_mcp_with_refresh_token_returns_401(self, auth_client):
        claims = TokenClaims(sub="testuser", client_id="c", scopes=[])
        refresh = issue_refresh_token(claims)
        resp = await auth_client.post(
            "/mcp", json={}, headers={"Authorization": f"Bearer {refresh}"}
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_mcp_with_valid_token_passes_through(self, auth_client):
        resp = await auth_client.post(
            "/mcp", json={}, headers={"Authorization": _make_bearer()}
        )
        # Should NOT be 401 — whatever status the MCP handler returns is fine
        assert resp.status_code != 401


# ─── Multi-user Auth Tests ────────────────────────────────────────────────────

class TestMultiUserAuth:
    """Tests for users.json file-based multi-user authentication."""

    def test_get_users_map_fallback_to_single_user(self):
        """When USERS_FILE does not exist, falls back to AUTH_USER/AUTH_PASSWORD."""
        from open_brain.config import get_users_map
        users_map = get_users_map()
        assert "testuser" in users_map
        assert users_map["testuser"] == "testpassword123"

    def test_get_users_map_loads_users_file(self, monkeypatch, tmp_path):
        """When USERS_FILE points to a valid JSON file, loads those users."""
        import json
        import open_brain.config as config_module
        users_file = tmp_path / "users.json"
        users_file.write_text(json.dumps([
            {"username": "alice", "password": "alicepass"},
            {"username": "bob", "password": "bobpass"},
        ]))
        config_module._config = None
        monkeypatch.setenv("USERS_FILE", str(users_file))
        from open_brain.config import get_users_map
        users_map = get_users_map()
        assert "alice" in users_map
        assert users_map["alice"] == "alicepass"
        assert "bob" in users_map
        assert users_map["bob"] == "bobpass"

    def test_get_users_map_single_entry_in_file(self, monkeypatch, tmp_path):
        """Single user in users.json is parsed correctly."""
        import json
        import open_brain.config as config_module
        users_file = tmp_path / "users.json"
        users_file.write_text(json.dumps([{"username": "charlie", "password": "charliepass"}]))
        config_module._config = None
        monkeypatch.setenv("USERS_FILE", str(users_file))
        from open_brain.config import get_users_map
        users_map = get_users_map()
        assert users_map == {"charlie": "charliepass"}

    def test_get_users_map_fallback_when_file_missing(self, monkeypatch):
        """When USERS_FILE path doesn't exist, falls back to AUTH_USER/AUTH_PASSWORD."""
        import open_brain.config as config_module
        config_module._config = None
        monkeypatch.setenv("USERS_FILE", "/nonexistent/path/users.json")
        from open_brain.config import get_users_map
        users_map = get_users_map()
        assert "testuser" in users_map
        assert users_map["testuser"] == "testpassword123"

    def test_handle_login_submit_multi_user_success(self, oauth_provider, monkeypatch, tmp_path):
        """Multi-user: second user can log in when users.json is present."""
        import json
        import open_brain.config as config_module
        users_file = tmp_path / "users.json"
        users_file.write_text(json.dumps([
            {"username": "alice", "password": "alicepass"},
            {"username": "bob", "password": "bobpass"},
        ]))
        config_module._config = None
        monkeypatch.setenv("USERS_FILE", str(users_file))
        success, redirect_url = oauth_provider.handle_login_submit(
            username="bob",
            password="bobpass",
            client_id="client1",
            redirect_uri="http://localhost/callback",
            code_challenge="challenge",
            state="",
            scopes_str="",
        )
        assert success is True
        assert "code=" in redirect_url

    def test_handle_login_submit_multi_user_wrong_password(self, oauth_provider, monkeypatch, tmp_path):
        """Multi-user: wrong password for a valid username is rejected."""
        import json
        import open_brain.config as config_module
        users_file = tmp_path / "users.json"
        users_file.write_text(json.dumps([
            {"username": "alice", "password": "alicepass"},
            {"username": "bob", "password": "bobpass"},
        ]))
        config_module._config = None
        monkeypatch.setenv("USERS_FILE", str(users_file))
        success, redirect_url = oauth_provider.handle_login_submit(
            username="alice",
            password="wrongpass",
            client_id="client1",
            redirect_uri="http://localhost/callback",
            code_challenge="challenge",
            state="",
            scopes_str="",
        )
        assert success is False
        assert "error=access_denied" in redirect_url

    def test_handle_login_submit_multi_user_unknown_user(self, oauth_provider, monkeypatch, tmp_path):
        """Multi-user: unknown username is rejected."""
        import json
        import open_brain.config as config_module
        users_file = tmp_path / "users.json"
        users_file.write_text(json.dumps([{"username": "alice", "password": "alicepass"}]))
        config_module._config = None
        monkeypatch.setenv("USERS_FILE", str(users_file))
        success, redirect_url = oauth_provider.handle_login_submit(
            username="mallory",
            password="alicepass",
            client_id="client1",
            redirect_uri="http://localhost/callback",
            code_challenge="challenge",
            state="",
            scopes_str="",
        )
        assert success is False
        assert "error=access_denied" in redirect_url

    def test_exchange_authorization_code_encodes_username_in_sub(self, oauth_provider):
        """JWT sub claim contains the authenticated username."""
        success, redirect_url = oauth_provider.handle_login_submit(
            username="testuser",
            password="testpassword123",
            client_id="client1",
            redirect_uri="http://localhost/callback",
            code_challenge="challenge",
            state="",
            scopes_str="read",
        )
        assert success is True
        code = redirect_url.split("code=")[1].split("&")[0]
        tokens = oauth_provider.exchange_authorization_code("client1", code)
        import jwt as pyjwt
        payload = pyjwt.decode(tokens.access_token, options={"verify_signature": False})
        assert payload["sub"] == "testuser"

    def test_exchange_authorization_code_encodes_second_user_sub(self, oauth_provider, monkeypatch, tmp_path):
        """JWT sub claim correctly reflects the second user's username."""
        import json
        import open_brain.config as config_module
        users_file = tmp_path / "users.json"
        users_file.write_text(json.dumps([
            {"username": "alice", "password": "alicepass"},
            {"username": "bob", "password": "bobpass"},
        ]))
        config_module._config = None
        monkeypatch.setenv("USERS_FILE", str(users_file))
        success, redirect_url = oauth_provider.handle_login_submit(
            username="alice",
            password="alicepass",
            client_id="client1",
            redirect_uri="http://localhost/callback",
            code_challenge="challenge",
            state="",
            scopes_str="read",
        )
        assert success is True
        code = redirect_url.split("code=")[1].split("&")[0]
        tokens = oauth_provider.exchange_authorization_code("client1", code)
        import jwt as pyjwt
        payload = pyjwt.decode(tokens.access_token, options={"verify_signature": False})
        assert payload["sub"] == "alice"

    def test_backward_compat_single_user(self, oauth_provider):
        """Backward compat: existing AUTH_USER/AUTH_PASSWORD still works when no users.json."""
        success, redirect_url = oauth_provider.handle_login_submit(
            username="testuser",
            password="testpassword123",
            client_id="client1",
            redirect_uri="http://localhost/callback",
            code_challenge="challenge",
            state="",
            scopes_str="",
        )
        assert success is True
