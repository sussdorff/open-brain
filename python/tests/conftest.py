"""Shared test fixtures and configuration."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_TEST_ENV = {
    "MCP_SERVER_URL": "http://localhost:8091",
    "AUTH_USER": "testuser",
    "AUTH_PASSWORD": "testpassword123",
    "JWT_SECRET": "this-is-a-test-secret-that-is-long-enough-32chars",
    "VOYAGE_API_KEY": "test-voyage-key",
    "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
}
_DATABASE_URL_WAS_PROVIDED = bool(os.environ.get("DATABASE_URL"))

# Inject test env vars without polluting the real environment between test runs.
# Uses setdefault so that values already set in the shell (e.g. a real key in CI)
# take precedence, but tests that need isolation reset via reset_config_singleton.
for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)


def _make_server_mock_pool():
    """Build a mock pool for open_brain.server.get_pool that returns 0 for fetchval."""
    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=0)  # safe default: 0 memories today
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)
    return mock_pool


@pytest.fixture(autouse=True)
def mock_server_get_pool():
    """Patch open_brain.server.get_pool so save_memory daily guard doesn't hit real DB.

    Individual tests that need specific pool behavior can override this with their own patch.
    """
    with patch("open_brain.server.get_pool", new_callable=AsyncMock, return_value=_make_server_mock_pool()):
        yield


@pytest.fixture(autouse=True)
def reset_save_timestamps():
    """Reset rate-limiter deque between tests to avoid cross-test interference."""
    import open_brain.server as server_module
    server_module._save_timestamps.clear()
    yield
    server_module._save_timestamps.clear()


@pytest.fixture(autouse=True)
def reset_config_singleton():
    """Reset config singleton between tests."""
    import open_brain.config as config_module
    config_module._config = None
    yield
    config_module._config = None


@pytest.fixture(autouse=True)
def reset_oauth_provider():
    """Reset OAuth provider singleton between tests."""
    import open_brain.auth.provider as provider_module
    provider_module._provider = None
    yield
    provider_module._provider = None


@pytest.fixture
def config():
    """Return a fresh Config instance."""
    from open_brain.config import get_config
    return get_config()


@pytest.fixture
def oauth_provider():
    """Return a fresh OAuthProvider instance."""
    from open_brain.auth.provider import OAuthProvider
    return OAuthProvider()


def _real_integration_database_url() -> str:
    """Return the caller-provided DATABASE_URL, or skip when only the test default exists."""
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not _DATABASE_URL_WAS_PROVIDED or db_url == _TEST_ENV["DATABASE_URL"]:
        pytest.skip("Requires real DATABASE_URL (not test placeholder)")
    return db_url


@pytest.fixture(scope="session")
def bootstrapped_database_url() -> str:
    """Bootstrap the repository-owned test schema for real-DB integration tests."""
    db_url = _real_integration_database_url()
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "bootstrap_test_schema.sql"
    if not script_path.exists():
        pytest.fail(f"Test schema bootstrap script not found: {script_path}")
    sql = script_path.read_text(encoding="utf-8")

    async def _bootstrap() -> None:
        import asyncpg

        conn = await asyncpg.connect(db_url)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    try:
        asyncio.run(_bootstrap())
    except Exception as exc:
        pytest.fail(f"Failed to bootstrap test schema with {script_path}: {exc}")

    return db_url


@pytest.fixture
def integration_database_url(bootstrapped_database_url: str) -> str:
    """Return a bootstrapped caller-provided DATABASE_URL for real-DB integration tests."""
    return bootstrapped_database_url


@pytest.fixture
def sample_memories():
    """Return a list of sample Memory objects."""
    from open_brain.data_layer.interface import Memory
    return [
        Memory(
            id=1,
            index_id=1,
            session_id=None,
            type="observation",
            title="Python best practices",
            subtitle=None,
            narrative=None,
            content="Use type hints everywhere for better IDE support",
            metadata={},
            priority=0.8,
            stability="stable",
            access_count=5,
            last_accessed_at=None,
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
            user_id=None,
            importance="medium",
        ),
        Memory(
            id=2,
            index_id=1,
            session_id=None,
            type="observation",
            title="Python best practices",
            subtitle=None,
            narrative=None,
            content="Use type hints for better IDE support",
            metadata={},
            priority=0.5,
            stability="tentative",
            access_count=1,
            last_accessed_at=None,
            created_at="2026-01-02T00:00:00",
            updated_at="2026-01-02T00:00:00",
            user_id=None,
            importance="medium",
        ),
        Memory(
            id=3,
            index_id=1,
            session_id=None,
            type="decision",
            title="Use asyncpg",
            subtitle=None,
            narrative=None,
            content="asyncpg is faster than psycopg2 for async workloads",
            metadata={},
            priority=0.9,
            stability="canonical",
            access_count=10,
            last_accessed_at=None,
            created_at="2026-01-03T00:00:00",
            updated_at="2026-01-03T00:00:00",
            user_id=None,
            importance="medium",
        ),
    ]
