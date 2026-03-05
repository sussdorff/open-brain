"""JWT token issuance and verification (HS256 with PyJWT)."""

from datetime import UTC, datetime, timedelta
from dataclasses import dataclass

import jwt

from open_brain.config import get_config


@dataclass
class TokenClaims:
    """Claims embedded in a JWT token."""

    sub: str
    client_id: str
    scopes: list[str]


@dataclass
class VerifiedToken:
    """Result of successful token verification."""

    sub: str
    client_id: str
    scopes: list[str]
    token_type: str | None
    exp: int | None
    iat: int | None
    iss: str | None


def _get_secret() -> str:
    return get_config().JWT_SECRET


def issue_access_token(claims: TokenClaims) -> str:
    """Issue a short-lived (1h) access token."""
    config = get_config()
    now = datetime.now(UTC)
    payload = {
        "sub": claims.sub,
        "clientId": claims.client_id,
        "scopes": claims.scopes,
        "iat": now,
        "exp": now + timedelta(hours=1),
        "iss": config.MCP_SERVER_URL,
    }
    return jwt.encode(payload, _get_secret(), algorithm="HS256")


def issue_refresh_token(claims: TokenClaims) -> str:
    """Issue a long-lived (30d) refresh token."""
    config = get_config()
    now = datetime.now(UTC)
    payload = {
        "sub": claims.sub,
        "clientId": claims.client_id,
        "scopes": claims.scopes,
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(days=30),
        "iss": config.MCP_SERVER_URL,
    }
    return jwt.encode(payload, _get_secret(), algorithm="HS256")


def verify_token(token: str) -> VerifiedToken:
    """Verify and decode a JWT token.

    Args:
        token: JWT token string

    Returns:
        VerifiedToken with decoded claims

    Raises:
        jwt.InvalidTokenError: if the token is invalid or expired
    """
    config = get_config()
    payload = jwt.decode(
        token,
        _get_secret(),
        algorithms=["HS256"],
        issuer=config.MCP_SERVER_URL,
    )
    return VerifiedToken(
        sub=payload.get("sub", ""),
        client_id=payload.get("clientId", ""),
        scopes=payload.get("scopes", []),
        token_type=payload.get("type"),
        exp=payload.get("exp"),
        iat=payload.get("iat"),
        iss=payload.get("iss"),
    )
