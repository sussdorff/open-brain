"""AK 2: JWT auth tests and OAuth flow logic tests."""

from __future__ import annotations

import time

import jwt
import pytest

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
