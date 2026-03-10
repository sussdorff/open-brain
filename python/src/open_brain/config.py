"""Configuration management using pydantic-settings."""

from typing import Literal

from pydantic import AnyHttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    PORT: int = 8091
    DATABASE_URL: str = "postgresql://open_brain:password@localhost:5432/open_brain"
    MCP_SERVER_URL: str
    AUTH_USER: str
    AUTH_PASSWORD: str
    JWT_SECRET: str
    CLIENTS_FILE: str = "/opt/open-brain/clients.json"
    VOYAGE_API_KEY: str
    VOYAGE_MODEL: str = "voyage-4"
    RERANK_ENABLED: bool = True
    RERANK_MODEL: str = "rerank-2.5"

    # API key auth for plugin hooks (comma-separated list of valid keys)
    API_KEYS: str = ""

    # LLM for metadata extraction / refinement
    LLM_PROVIDER: Literal["anthropic", "openrouter"] = "anthropic"
    LLM_MODEL: str = "claude-haiku-4-5-20251001"
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
