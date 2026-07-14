"""Integration tests for bootstrap SQL and migration parity."""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

SchemaSnapshot = list[dict[str, str]]
SchemaKey = tuple[str, str, str, str]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_SCHEMA_PATH = PROJECT_ROOT / "python/src/open_brain/data_layer/postgres.py"
BOOTSTRAP_SCHEMA_PATH = PROJECT_ROOT / "scripts/bootstrap_test_schema.sql"
PRIORITY_FACTOR_SQL = "0.35 + 0.65 * LEAST(GREATEST(m.priority, 0.0), 1.0)"


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


def _normalize_schema_row(row: asyncpg.Record) -> dict[str, str]:
    snapshot_row = dict(row)
    if snapshot_row["kind"] == "function":
        # Function definitions preserve free-form SQL formatting. Case-folding
        # also folds string literal case such as 'critical'. That is acceptable
        # for this drift smoke guard; distinguishing literals from keywords
        # adds complexity for negligible benefit at this test's scope.
        snapshot_row["definition"] = (
            " ".join(snapshot_row["definition"].split()).lower()
        )
    return snapshot_row


async def _fetch_schema_snapshot(database_url: str) -> SchemaSnapshot:
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(_SCHEMA_SNAPSHOT_SQL)
    finally:
        await conn.close()
    return [_normalize_schema_row(row) for row in rows]


def _snapshot_by_key(snapshot: SchemaSnapshot) -> dict[SchemaKey, str]:
    return {
        (
            row["kind"],
            row["object_schema"],
            row["object_name"],
            row["detail_name"],
        ): row["definition"]
        for row in snapshot
    }


def _drifted_schema_keys(
    before_snapshot: SchemaSnapshot,
    after_snapshot: SchemaSnapshot,
) -> list[SchemaKey]:
    before_by_key = _snapshot_by_key(before_snapshot)
    after_by_key = _snapshot_by_key(after_snapshot)
    changed_keys = {
        key
        for key in before_by_key.keys() & after_by_key.keys()
        if before_by_key[key] != after_by_key[key]
    }
    return sorted((before_by_key.keys() ^ after_by_key.keys()) | changed_keys)


def _format_schema_keys(keys: list[SchemaKey]) -> str:
    return "\n".join(
        f"- kind={kind}, schema={object_schema}, object={object_name}, detail={detail_name}"
        for kind, object_schema, object_name, detail_name in keys
    )


def test_hybrid_search_definitions_apply_same_priority_factor():
    runtime_sql = RUNTIME_SCHEMA_PATH.read_text(encoding="utf-8")
    bootstrap_sql = BOOTSTRAP_SCHEMA_PATH.read_text(encoding="utf-8")

    assert PRIORITY_FACTOR_SQL in runtime_sql
    assert PRIORITY_FACTOR_SQL in bootstrap_sql
    assert runtime_sql.count("DROP FUNCTION IF EXISTS public.hybrid_search") == 2
    assert bootstrap_sql.count("DROP FUNCTION IF EXISTS public.hybrid_search") == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bootstrap_schema_matches_get_pool_migrations(
    bootstrapped_database_url: str,
):
    """Real-DB proxy for schema parity: bootstrap SQL already includes migrations.

    The ``bootstrapped_database_url`` fixture applies
    ``scripts/bootstrap_test_schema.sql`` against the caller-provided
    ``DATABASE_URL``. Running ``get_pool()`` afterward executes the real
    idempotent migration battery against that schema; semantic catalog changes
    mean the bootstrap SQL no longer matches the migration-managed schema.
    Function whitespace and SQL keyword case are normalized so cosmetic-only
    function definition rewrites do not trip this guard.
    """
    import open_brain.config as config_module
    from open_brain.data_layer import postgres as pg_module

    await pg_module.close_pool()
    before_snapshot = await _fetch_schema_snapshot(bootstrapped_database_url)

    try:
        await pg_module.get_pool()
        after_snapshot = await _fetch_schema_snapshot(bootstrapped_database_url)
        drifted_keys = _drifted_schema_keys(before_snapshot, after_snapshot)

        assert not drifted_keys, (
            "get_pool() changed bootstrap-managed schema objects:\n"
            f"{_format_schema_keys(drifted_keys)}"
        )
    finally:
        await pg_module.close_pool()
        config_module._config = None
