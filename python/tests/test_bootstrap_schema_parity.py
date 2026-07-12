"""Integration tests for bootstrap SQL and migration parity."""

from __future__ import annotations

import json
from unittest.mock import patch

import asyncpg
import pytest


_SCHEMA_SNAPSHOT_SQL = """
SELECT kind, object_schema, object_name, detail_name, definition
FROM (
  SELECT
    'column' AS kind,
    table_schema AS object_schema,
    table_name AS object_name,
    column_name AS detail_name,
    jsonb_build_object(
      'ordinal_position', ordinal_position,
      'data_type', data_type,
      'udt_schema', udt_schema,
      'udt_name', udt_name,
      'is_nullable', is_nullable,
      'column_default', column_default,
      'generation_expression', generation_expression,
      'identity_generation', identity_generation,
      'character_maximum_length', character_maximum_length,
      'numeric_precision', numeric_precision,
      'datetime_precision', datetime_precision
    )::text AS definition
  FROM information_schema.columns
  WHERE table_schema = 'public'

  UNION ALL

  SELECT
    'constraint' AS kind,
    namespace.nspname AS object_schema,
    relation.relname AS object_name,
    constraint_record.conname AS detail_name,
    pg_get_constraintdef(constraint_record.oid, true) AS definition
  FROM pg_constraint constraint_record
  JOIN pg_class relation ON relation.oid = constraint_record.conrelid
  JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
  WHERE namespace.nspname = 'public'

  UNION ALL

  SELECT
    'function' AS kind,
    namespace.nspname AS object_schema,
    proc.proname AS object_name,
    pg_get_function_identity_arguments(proc.oid) AS detail_name,
    pg_get_function_result(proc.oid) || E'\n' || pg_get_functiondef(proc.oid) AS definition
  FROM pg_proc proc
  JOIN pg_namespace namespace ON namespace.oid = proc.pronamespace
  WHERE namespace.nspname = 'public'
    AND proc.proname IN ('hybrid_search', 'decay_unused_priorities')

  UNION ALL

  SELECT
    'index' AS kind,
    schemaname AS object_schema,
    tablename AS object_name,
    indexname AS detail_name,
    indexdef AS definition
  FROM pg_indexes
  WHERE schemaname = 'public'
) snapshot
ORDER BY kind, object_schema, object_name, detail_name, definition;
"""

_DRIFT_MARKER_COLUMN = "bootstrap_schema_parity_drift_marker"


async def _fetch_schema_snapshot(database_url: str) -> str:
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(_SCHEMA_SNAPSHOT_SQL)
    finally:
        await conn.close()
    return json.dumps([dict(row) for row in rows], indent=2, sort_keys=True)


async def _drop_drift_marker(database_url: str) -> None:
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            f"ALTER TABLE memories DROP COLUMN IF EXISTS {_DRIFT_MARKER_COLUMN};"
        )
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bootstrap_schema_matches_get_pool_migrations(
    bootstrapped_database_url: str,
):
    """Real-DB proxy for schema parity: bootstrap SQL already includes migrations.

    The ``bootstrapped_database_url`` fixture applies
    ``scripts/bootstrap_test_schema.sql`` against the caller-provided
    ``DATABASE_URL``. Running ``get_pool()`` afterward executes the real
    idempotent migration battery against that schema; any catalog change means
    the bootstrap SQL no longer matches the migration-managed schema.
    """
    from open_brain.config import get_config
    from open_brain.data_layer import postgres as pg_module

    get_config().DATABASE_URL = bootstrapped_database_url
    await pg_module.close_pool()
    before_snapshot = await _fetch_schema_snapshot(bootstrapped_database_url)

    original_run_migrations = pg_module._run_migrations

    async def run_migrations_with_fake_drift(conn):
        await original_run_migrations(conn)
        await conn.execute(
            f"ALTER TABLE memories ADD COLUMN IF NOT EXISTS {_DRIFT_MARKER_COLUMN} TEXT;"
        )

    try:
        with patch(
            "open_brain.data_layer.postgres._run_migrations",
            new=run_migrations_with_fake_drift,
        ):
            await pg_module.get_pool()
        after_snapshot = await _fetch_schema_snapshot(bootstrapped_database_url)

        assert after_snapshot == before_snapshot
    finally:
        await _drop_drift_marker(bootstrapped_database_url)
        await pg_module.close_pool()
