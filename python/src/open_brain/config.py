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
    CLIENTS_FILE: str = "/opt/open-brain/clients.json"
    # Path to users.json file for multi-user auth. NOT in git — managed on the server.
    # Format: [{"username": "alice", "password": "secret"}, ...]
    USERS_FILE: str = "/opt/open-brain/users.json"
    VOYAGE_API_KEY: str
    VOYAGE_MODEL: str = "voyage-4"
    RERANK_ENABLED: bool = True
    RERANK_MODEL: str = "rerank-2.5"

    # API key auth for plugin hooks (comma-separated list of valid keys)
    API_KEYS: str = ""

    # Optional override for MacWhisper history directory path.
    # If empty, MacWhisperConnector auto-discovers the path.
    MACWHISPER_HISTORY_PATH: str = ""

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
