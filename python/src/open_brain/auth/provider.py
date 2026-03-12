"""OAuth 2.1 provider: authorization code flow with PKCE."""

import secrets
import time
from dataclasses import dataclass, field

from open_brain.auth.tokens import TokenClaims, issue_access_token, issue_refresh_token, verify_token
from open_brain.config import get_config, get_users_map


LOGIN_FORM_TEMPLATE = """<!DOCTYPE html>
<html><head><title>open-brain Login</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: system-ui; max-width: 400px; margin: 100px auto; padding: 20px; }}
  input {{ display: block; width: 100%; padding: 8px; margin: 8px 0; box-sizing: border-box; }}
  button {{ padding: 10px 20px; background: #2563eb; color: white; border: none; border-radius: 4px; cursor: pointer; width: 100%; }}
  button:hover {{ background: #1d4ed8; }}
  h2 {{ margin-bottom: 4px; }}
  p {{ color: #666; margin-top: 0; }}
</style></head>
<body>
  <h2>open-brain</h2>
  <p>Authorize <strong>{client_name}</strong></p>
  <form method="POST" action="/authorize/submit">
    <input type="hidden" name="client_id" value="{client_id}">
    <input type="hidden" name="redirect_uri" value="{redirect_uri}">
    <input type="hidden" name="code_challenge" value="{code_challenge}">
    <input type="hidden" name="state" value="{state}">
    <input type="hidden" name="scopes" value="{scopes}">
    <input type="text" name="username" placeholder="Username" required autofocus>
    <input type="password" name="password" placeholder="Password" required>
    <button type="submit">Sign In</button>
  </form>
</body></html>"""


@dataclass
class AuthCodeEntry:
    """In-memory authorization code entry."""

    client_id: str
    code_challenge: str
    redirect_uri: str
    scopes: list[str]
    expires_at: float  # Unix timestamp
    username: str = ""  # authenticated username


@dataclass
class OAuthTokens:
    """OAuth token response."""

    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str


@dataclass
class AuthInfo:
    """Verified access token info."""

    token: str
    client_id: str
    scopes: list[str]
    expires_at: int | None


@dataclass
class RegisteredClient:
    """Dynamically registered OAuth client (RFC 7591)."""

    client_id: str
    client_name: str
    redirect_uris: list[str]
    grant_types: list[str]
    response_types: list[str]
    scope: str
    client_id_issued_at: int


class OAuthProvider:
    """Custom OAuth 2.1 provider with in-memory auth codes and PKCE support."""

    def __init__(self) -> None:
        self._auth_codes: dict[str, AuthCodeEntry] = {}
        self._revoked_tokens: set[str] = set()
        self._clients: dict[str, RegisteredClient] = {}

    def register_client(
        self,
        client_name: str = "MCP Client",
        redirect_uris: list[str] | None = None,
        grant_types: list[str] | None = None,
        response_types: list[str] | None = None,
        scope: str = "",
    ) -> RegisteredClient:
        """Register a new OAuth client dynamically (RFC 7591)."""
        client_id = secrets.token_hex(16)
        client = RegisteredClient(
            client_id=client_id,
            client_name=client_name,
            redirect_uris=redirect_uris or [],
            grant_types=grant_types or ["authorization_code", "refresh_token"],
            response_types=response_types or ["code"],
            scope=scope,
            client_id_issued_at=int(time.time()),
        )
        self._clients[client_id] = client
        return client

    def get_client(self, client_id: str) -> RegisteredClient | None:
        """Look up a registered client."""
        return self._clients.get(client_id)

    def build_login_form(
        self,
        client_name: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        state: str,
        scopes: list[str],
    ) -> str:
        """Render the HTML login form."""
        return LOGIN_FORM_TEMPLATE.format(
            client_name=client_name,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            state=state,
            scopes=" ".join(scopes),
        )

    def handle_login_submit(
        self,
        username: str,
        password: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        state: str,
        scopes_str: str,
    ) -> tuple[bool, str]:
        """Handle login form submission.

        Returns:
            (success, redirect_url) tuple.
            On failure, redirect_url contains error parameters.
            On success, redirect_url contains the authorization code.
        """
        import hmac
        users_map = get_users_map()
        stored = users_map.get(username)
        if stored is None or not hmac.compare_digest(stored, password):
            error_url = _build_url(redirect_uri, {
                "error": "access_denied",
                "error_description": "Invalid credentials",
            })
            if state:
                error_url = _append_param(error_url, "state", state)
            return False, error_url

        code = secrets.token_hex(32)
        scopes = [s for s in scopes_str.split(" ") if s] if scopes_str else []
        self._auth_codes[code] = AuthCodeEntry(
            client_id=client_id,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            scopes=scopes,
            expires_at=time.time() + 5 * 60,  # 5 minutes
            username=username,
        )

        redirect_url = _build_url(redirect_uri, {"code": code})
        if state:
            redirect_url = _append_param(redirect_url, "state", state)
        return True, redirect_url

    def get_code_challenge(self, authorization_code: str) -> str:
        """Return the PKCE code_challenge for an authorization code.

        Raises:
            ValueError: if code is invalid or expired
        """
        entry = self._auth_codes.get(authorization_code)
        if not entry or entry.expires_at < time.time():
            raise ValueError("Invalid or expired authorization code")
        return entry.code_challenge

    def exchange_authorization_code(
        self, client_id: str, authorization_code: str
    ) -> OAuthTokens:
        """Exchange an authorization code for tokens.

        Raises:
            ValueError: if code is invalid/expired or client_id mismatches
        """
        entry = self._auth_codes.get(authorization_code)
        if not entry or entry.expires_at < time.time():
            raise ValueError("Invalid or expired authorization code")
        if entry.client_id != client_id:
            raise ValueError("Client ID mismatch")

        # Consume the code
        del self._auth_codes[authorization_code]

        claims = TokenClaims(
            sub=entry.username,
            client_id=client_id,
            scopes=entry.scopes,
        )
        access_token = issue_access_token(claims)
        refresh_token = issue_refresh_token(claims)

        return OAuthTokens(
            access_token=access_token,
            token_type="Bearer",
            expires_in=3600,
            refresh_token=refresh_token,
        )

    def exchange_refresh_token(
        self,
        client_id: str,
        refresh_token: str,
        scopes: list[str] | None = None,
    ) -> OAuthTokens:
        """Exchange a refresh token for new tokens.

        Raises:
            ValueError: if token is invalid, wrong type, or revoked
        """
        if refresh_token in self._revoked_tokens:
            raise ValueError("Token has been revoked")

        verified = verify_token(refresh_token)
        if verified.token_type != "refresh":
            raise ValueError("Not a refresh token")

        claims = TokenClaims(
            sub=verified.sub,
            client_id=client_id,
            scopes=scopes if scopes is not None else verified.scopes,
        )
        new_access_token = issue_access_token(claims)
        new_refresh_token = issue_refresh_token(claims)

        return OAuthTokens(
            access_token=new_access_token,
            token_type="Bearer",
            expires_in=3600,
            refresh_token=new_refresh_token,
        )

    def verify_access_token(self, token: str) -> AuthInfo:
        """Verify an access token and return auth info.

        Raises:
            ValueError: if token is revoked or is a refresh token
            jwt.InvalidTokenError: if token is invalid/expired
        """
        if token in self._revoked_tokens:
            raise ValueError("Token has been revoked")

        verified = verify_token(token)
        if verified.token_type == "refresh":
            raise ValueError("Cannot use refresh token as access token")

        return AuthInfo(
            token=token,
            client_id=verified.client_id,
            scopes=verified.scopes,
            expires_at=verified.exp,
        )

    def revoke_token(self, token: str) -> None:
        """Revoke a token."""
        self._revoked_tokens.add(token)


def _build_url(base_url: str, params: dict[str, str]) -> str:
    """Build URL with query parameters."""
    from urllib.parse import urlencode, urlparse, urlunparse, parse_qs, urlencode as ue

    parsed = urlparse(base_url)
    query_parts = []
    for k, v in params.items():
        query_parts.append(f"{k}={v}")
    query = "&".join(query_parts)
    if parsed.query:
        query = parsed.query + "&" + query
    return urlunparse(parsed._replace(query=query))


def _append_param(url: str, key: str, value: str) -> str:
    """Append a single query parameter to a URL."""
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{key}={value}"


# Module-level singleton provider
_provider: OAuthProvider | None = None


def get_provider() -> OAuthProvider:
    """Return the singleton OAuthProvider instance."""
    global _provider
    if _provider is None:
        _provider = OAuthProvider()
    return _provider
