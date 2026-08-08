"""Configuration management using pydantic-settings."""

from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    PORT: int = 8091
    DATABASE_URL: str
    MCP_SERVER_URL: str
    AUTH_USER: str
    AUTH_PASSWORD: str
    JWT_SECRET: str
    # Optional secret for verifying promotion grants (min 32 chars when set).
    # Never falls back to JWT_SECRET. Empty disables grant verification (fail closed).
    # get_config() is process-cached: restart after rotating this secret.
    PROMOTION_GRANT_SECRET: str = ""
    # Comma-separated exact OAuth subjects allowed to call promote_memory_authority.
    # Default empty = fail closed (self-requested admin scope alone is insufficient).
    # get_config() is process-cached: restart after changing this allowlist.
    PROMOTION_ADMIN_USERS: str = ""
    # Exact automatic promotion rule version. Empty disables automated promotion.
    PROMOTION_AUTOMATIC_RULE_VERSION: str = ""
    CLIENTS_FILE: str = "/opt/open-brain/clients.json"
    # Path to users.json file for multi-user auth. NOT in git — managed on the server.
    # Format: [{"username": "alice", "password": "secret"}, ...]
    USERS_FILE: str = "/opt/open-brain/users.json"
    VOYAGE_API_KEY: str
    VOYAGE_MODEL: str = "voyage-4"
    RERANK_ENABLED: bool = True
    RERANK_MODEL: str = "rerank-2.5"

    # API key auth for hooks and CLI clients (comma-separated list of valid keys)
    API_KEYS: str = ""

    # Optional override for MacWhisper history directory path.
    # If empty, MacWhisperConnector auto-discovers the path.
    MACWHISPER_HISTORY_PATH: str = ""

    # SearXNG instance URL for people enrichment web searches.
    # If empty, web search enrichment is disabled.
    SEARXNG_URL: str = ""

    # Paperless-ngx document reference resolution.
    # If empty, Paperless reference resolution returns not_configured.
    PAPERLESS_BASE_URL: str = ""
    PAPERLESS_API_TOKEN: str = ""

    # IMAP email ingest settings (cr3.4)
    IMAP_SERVER: str = ""
    IMAP_PORT: int = 993
    IMAP_USER: str = ""
    # op reference for IMAP password, e.g. "op://Private/email/app-password"
    IMAP_PASSWORD_OP: str = ""
    EMAIL_STORE_RAW_BODIES: bool = False
    EMAIL_EXTRACTION_MODEL: str = "claude-haiku-4-5-20251001"

    # Logging configuration
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_FORMAT: Literal["human", "json"] = "human"

    # Daily ingestion guard: reject save_memory calls beyond this threshold per day
    MAX_MEMORIES_PER_DAY: int = 500

    # Semantic dedup threshold: minimum cosine similarity to treat a memory as a duplicate
    DEDUP_THRESHOLD: float = 0.85

    # LLM for metadata extraction / refinement.
    #
    # LLM_MODEL is the default used by all "small" calls: entity extraction,
    # capture-router classification, tool-use observation extraction, and
    # the short session/worktree summaries. These calls have small inputs
    # (≤4k chars) and ≤512 output tokens, so a cheap model suffices.
    #
    # LLM_MODEL_CAPTURE is an OPTIONAL override for the heavier
    # /api/session-capture endpoint (up to ~8k chars input, 1024 output
    # tokens). If unset, LLM_MODEL is used. Set this when you want a
    # stronger model for full-conversation extraction while keeping cheap
    # models for the high-volume small calls.
    LLM_PROVIDER: Literal["anthropic", "openrouter"] = "anthropic"
    LLM_MODEL: str = "claude-haiku-4-5-20251001"
    LLM_MODEL_CAPTURE: str | None = None
    ANTHROPIC_API_KEY: str | None = None
    OPENROUTER_API_KEY: str | None = None

    # OpenRouter provider routing (privacy + structured-output controls).
    # OPENROUTER_DATA_COLLECTION="deny" routes only to providers that do not
    # retain or train on prompt data (zero prompt retention). Set to "allow"
    # to permit all providers. Applies to every OpenRouter call.
    OPENROUTER_DATA_COLLECTION: Literal["allow", "deny"] = "deny"
    # Comma-separated provider slugs to prefer, in order (e.g. "deepinfra").
    # Falls back to any other policy-compliant provider when unset/unavailable.
    OPENROUTER_PROVIDER_ORDER: str | None = None

    @field_validator("JWT_SECRET")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        """JWT secret must be at least 32 characters."""
        if len(v) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return v

    @field_validator("AUTH_PASSWORD")
    @classmethod
    def validate_auth_password(cls, v: str) -> str:
        """Auth password must be at least 8 characters."""
        if len(v) < 8:
            raise ValueError("AUTH_PASSWORD must be at least 8 characters")
        return v


_config: Config | None = None


def get_config() -> Config:
    """Return the singleton config instance."""
    global _config
    if _config is None:
        _config = Config()  # type: ignore[call-arg]
    return _config


def get_users_map() -> dict[str, str]:
    """Return a username→password map for authentication.

    If USERS_FILE exists on disk, load it and parse as JSON array:
    ``[{"username": "alice", "password": "secret"}, ...]``

    If the file does not exist or cannot be read, fall back to the single-user
    AUTH_USER / AUTH_PASSWORD env vars.

    The users.json file is NOT in git — it is managed directly on the server,
    analogous to clients.json.
    """
    import json
    import logging
    from pathlib import Path

    logger = logging.getLogger(__name__)
    config = get_config()
    users_path = Path(config.USERS_FILE)
    if users_path.exists():
        try:
            entries = json.loads(users_path.read_text())
            users: dict[str, str] = {}
            for entry in entries:
                username = entry.get("username", "").strip()
                password = entry.get("password", "").strip()
                if username and password:
                    users[username] = password
            return users
        except Exception as exc:
            logger.error("Failed to load users from %s: %s — falling back to AUTH_USER", users_path, exc)
    return {config.AUTH_USER: config.AUTH_PASSWORD}
