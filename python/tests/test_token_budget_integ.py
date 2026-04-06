"""Integration tests for NBJ-5 Token Budget: AK2 stats() with real database.

AK2: stats() includes embeddings_today, embedding_tokens_today, estimated_embedding_cost_today
"""

from __future__ import annotations

import os

import asyncpg
import pytest


@pytest.mark.integration
class TestStatsIncludesCostInteg:
    """AK2: stats() returns embedding cost metrics from a real database."""

    @pytest.mark.asyncio
    async def test_stats_includes_cost(self):
        """stats() returns embeddings_today, embedding_tokens_today, and estimated cost from real DB."""
        database_url = os.environ.get("DATABASE_URL")
        if not database_url or "localhost" not in database_url and "test" not in database_url:
            pytest.skip("DATABASE_URL not set or not pointing to a test database")

        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
        try:
            async with pool.acquire() as conn:
                # Ensure the table exists
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS embedding_token_log (
                        id SERIAL PRIMARY KEY,
                        operation TEXT NOT NULL,
                        token_count INT NOT NULL,
                        logged_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)
                # Insert some token log rows with today's date
                await conn.execute(
                    "INSERT INTO embedding_token_log (operation, token_count) VALUES ($1, $2)",
                    "document",
                    100,
                )
                await conn.execute(
                    "INSERT INTO embedding_token_log (operation, token_count) VALUES ($1, $2)",
                    "query",
                    50,
                )

            # Patch get_pool to return our test pool
            from unittest.mock import patch
            with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
                from open_brain.data_layer.postgres import PostgresDataLayer
                dl = PostgresDataLayer()
                result = await dl.stats()

            assert "embeddings_today" in result, "stats() must include 'embeddings_today'"
            assert "embedding_tokens_today" in result, "stats() must include 'embedding_tokens_today'"
            assert "estimated_embedding_cost_today" in result, (
                "stats() must include 'estimated_embedding_cost_today'"
            )
            assert result["embeddings_today"] > 0, "embeddings_today should reflect inserted rows"
            assert result["embedding_tokens_today"] > 0, (
                "embedding_tokens_today should reflect inserted token counts"
            )
            assert result["estimated_embedding_cost_today"] >= 0, (
                "estimated_embedding_cost_today must be non-negative"
            )
        finally:
            # Clean up inserted rows to avoid polluting the DB
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM embedding_token_log WHERE operation IN ('document', 'query') "
                    "AND logged_at >= CURRENT_DATE"
                )
            await pool.close()
