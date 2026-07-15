"""Postgres+pgvector DataLayer implementation using asyncpg."""

from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import asyncpg

from open_brain.config import get_config
from open_brain.data_layer.embedding import (
    embed,
    embed_with_usage,
    embed_query_with_usage,
    to_pg_vector,
)
from open_brain.data_layer.reranker import RerankResult, rerank
from open_brain.data_layer.interface import (
    CANONICAL_ENTITY_METADATA_KEY,
    CANONICAL_KIND_METADATA_KEY,
    CANONICAL_KINDS,
    IMPORTANCE_VALUES,
    VALID_LINK_TYPES,
    ApprovedCanonicalEntityUpdateParams,
    ClusterPlan,
    CompactParams,
    CompactResult,
    CaptureTransitionParams,
    DecayParams,
    DecayResult,
    DeleteByRunIdResult,
    DeleteParams,
    DeleteResult,
    LifecycleActionQueryParams,
    LifecycleActionRecord,
    LifecycleActionStateParams,
    MaterializeParams,
    MaterializeResult,
    Memory,
    OriginProvenance,
    OriginProvenanceConflictError,
    RefineAction,
    RefineParams,
    RefineResult,
    SaveMemoryParams,
    SaveMemoryResult,
    SearchParams,
    TriageParams,
    TriageResult,
    UpdateMemoryParams,
    SearchResult,
    TimelineParams,
    TimelineResult,
    is_canonical_entity,
    rank_importance,
    validate_capture_status,
    validate_origin_provenance,
)
from open_brain.data_layer.refine import analyze_with_llm

from open_brain.ingest.runs import get_current_run_id

logger = logging.getLogger(__name__)

DEDUP_WINDOW_DAYS = 30  # How far back the content-hash dedup check looks


def _metadata_with_origin_provenance(
    metadata: dict[str, Any] | None,
    provenance: OriginProvenance,
) -> dict[str, Any]:
    """Merge canonical origin fields into metadata without dropping judge fields."""
    merged = dict(metadata) if metadata else {}
    existing = merged.get("provenance")
    if existing is not None and not isinstance(existing, dict):
        merged.setdefault("provenance_summary", str(existing))
    nested = dict(existing) if isinstance(existing, dict) else {}
    merged["provenance"] = _provenance_container_with_origin(nested, provenance)
    return merged


def _provenance_container_with_origin(
    container: dict[str, Any],
    provenance: OriginProvenance,
) -> dict[str, Any]:
    """Add origin below a collision-free key and reject incompatible lineage."""
    normalized = dict(container)
    if "origin" in normalized:
        if normalized["origin"] != provenance:
            raise OriginProvenanceConflictError(
                "origin provenance conflicts with metadata.provenance.origin"
            )
    elif "producer" in normalized:
        legacy_origin = {
            "producer": normalized.get("producer"),
            "source_ref": normalized.get("source_ref"),
        }
        if legacy_origin != provenance:
            raise OriginProvenanceConflictError(
                "legacy origin provenance conflicts with canonical provenance"
            )
        normalized.pop("producer", None)
        normalized.pop("source_ref", None)
    normalized["origin"] = dict(provenance)
    return normalized


def _record_metadata(record: Any) -> dict[str, Any]:
    raw = record["metadata"]
    if isinstance(raw, str):
        parsed = _json.loads(raw)
        return dict(parsed) if isinstance(parsed, dict) else {}
    return dict(raw) if isinstance(raw, dict) else {}


def _merge_append_metadata(
    existing: dict[str, Any],
    incoming: dict[str, Any] | None,
    provenance: OriginProvenance,
) -> dict[str, Any]:
    """Merge append metadata while keeping one unambiguous canonical origin."""
    merged = {**existing, **(incoming or {})}
    existing_raw = existing.get("provenance")
    incoming_raw = (incoming or {}).get("provenance")
    if existing_raw is not None and not isinstance(existing_raw, dict):
        merged.setdefault("provenance_summary", str(existing_raw))
    if incoming_raw is not None and not isinstance(incoming_raw, dict):
        merged["provenance_summary"] = str(incoming_raw)

    combined_nested: dict[str, Any] = {}
    if isinstance(existing_raw, dict):
        combined_nested.update(
            _provenance_container_with_origin(existing_raw, provenance)
        )
    if isinstance(incoming_raw, dict):
        combined_nested.update(
            _provenance_container_with_origin(incoming_raw, provenance)
        )
    merged["provenance"] = _provenance_container_with_origin(
        combined_nested,
        provenance,
    )
    return merged


def _priority_factor(priority: float) -> float:
    bounded_priority = min(max(priority, 0.0), 1.0)
    return 0.35 + 0.65 * bounded_priority


def _order_by_priority_score(
    candidates: list[tuple[Memory, float]],
    limit: int,
) -> list[Memory]:
    scored = [
        (memory, relevance_score * _priority_factor(memory.priority))
        for memory, relevance_score in candidates
    ]
    return [
        memory
        for memory, _score in sorted(scored, key=lambda item: item[1], reverse=True)[:limit]
    ]


def _order_by_rerank_results(
    memories: list[Memory],
    rerank_results: list[RerankResult | int],
    limit: int,
) -> list[Memory]:
    candidates: list[tuple[Memory, float]] = []
    for fallback_rank, result in enumerate(rerank_results):
        if isinstance(result, int):
            index = result
            relevance_score = 1.0 / (fallback_rank + 1)
        else:
            index = result.index
            relevance_score = result.relevance_score
        if 0 <= index < len(memories):
            candidates.append((memories[index], relevance_score))
    return _order_by_priority_score(candidates, limit)


def _row_float(row: Any, key: str, default: float = 0.0) -> float:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = row.get(key, default)
    return float(default if value is None else value)

# ─── compact_memories helpers ─────────────────────────────────────────────────

def canonical_entity_protection_predicate(alias: str | None = None) -> str:
    """Return the shared SQL predicate that excludes protected canonical entities."""
    metadata_ref = f"{alias}.metadata" if alias else "metadata"
    return f"({metadata_ref}->>'{CANONICAL_ENTITY_METADATA_KEY}') IS DISTINCT FROM 'true'"


def canonical_entity_select_predicate(alias: str | None = None) -> str:
    """Return the shared SQL predicate that selects protected canonical entities."""
    metadata_ref = f"{alias}.metadata" if alias else "metadata"
    return f"{metadata_ref}->>'{CANONICAL_ENTITY_METADATA_KEY}' = 'true'"


def _canonical_entity_protection_filter(alias: str | None = None) -> str:
    """Return an AND-prefixed canonical entity protection SQL clause."""
    return f"AND {canonical_entity_protection_predicate(alias)}"


async def _filter_out_newly_canonical(
    conn: asyncpg.Connection, ids: list[int]
) -> list[int]:
    """Return the subset of ids that are still safe to repoint/delete.

    Re-checks canonical protection immediately before a destructive
    mutation-site step (relationship repointing, usage-log deletion) to narrow
    the race window between candidate planning and execution. A memory promoted
    to a protected canonical entity in that window is dropped here so its
    relationships and usage history are left intact. The final memories DELETE
    is still separately guarded by canonical_entity_protection_predicate() as
    the last line of defense. Input order is preserved.
    """
    if not ids:
        return []
    rows = await conn.fetch(
        f"SELECT id FROM memories WHERE id = ANY($1::int[]) "
        f"AND {canonical_entity_protection_predicate()}",
        ids,
    )
    safe = {row["id"] for row in rows}
    return [memory_id for memory_id in ids if memory_id in safe]


def _parse_execute_count(result: str | None) -> int:
    """Parse asyncpg execute() row-count strings such as 'UPDATE 3'."""
    parts = result.split() if isinstance(result, str) else []
    if len(parts) >= 2 and parts[-1].isdigit():
        return int(parts[-1])
    return 0


def _coerce_count(value: Any) -> int:
    """Convert DB count values to int, treating mock placeholders as zero."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _metadata_from_row(row: Any) -> dict[str, Any]:
    """Return dict metadata from an asyncpg-like row."""
    raw_metadata = row.get("metadata")
    if isinstance(raw_metadata, dict):
        return raw_metadata
    if isinstance(raw_metadata, str):
        try:
            parsed = _json.loads(raw_metadata)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _row_is_protected_canonical_entity(row: Any) -> bool:
    """Return True when a fetched row has the canonical protection marker."""
    return _metadata_from_row(row).get(CANONICAL_ENTITY_METADATA_KEY) is True


async def _repoint_relationships(
    conn: asyncpg.Connection,
    source_id: int,
    target_id: int,
) -> int:
    """Repoint memory_relationships rows from source_id to target_id."""
    total_affected = 0
    result = await conn.execute(
        """
        DELETE FROM memory_relationships
        WHERE (source_id = $1 AND target_id = $2)
           OR (source_id = $2 AND target_id = $1)
        """,
        source_id,
        target_id,
    )
    total_affected += _parse_execute_count(result)

    result = await conn.execute(
        """
        DELETE FROM memory_relationships
        WHERE source_id = $1
          AND EXISTS (
            SELECT 1 FROM memory_relationships r2
            WHERE r2.source_id = $2
              AND r2.target_id = memory_relationships.target_id
              AND r2.relation_type = memory_relationships.relation_type
          )
        """,
        source_id,
        target_id,
    )
    total_affected += _parse_execute_count(result)

    result = await conn.execute(
        """
        DELETE FROM memory_relationships
        WHERE target_id = $1
          AND EXISTS (
            SELECT 1 FROM memory_relationships r2
            WHERE r2.target_id = $2
              AND r2.source_id = memory_relationships.source_id
              AND r2.relation_type = memory_relationships.relation_type
          )
        """,
        source_id,
        target_id,
    )
    total_affected += _parse_execute_count(result)

    result = await conn.execute(
        """
        UPDATE memory_relationships
        SET source_id = CASE WHEN source_id = $1 THEN $2 ELSE source_id END,
            target_id = CASE WHEN target_id = $1 THEN $2 ELSE target_id END
        WHERE source_id = $1 OR target_id = $1
        """,
        source_id,
        target_id,
    )
    total_affected += _parse_execute_count(result)
    return total_affected

def _build_clusters(ids: list[int], edges: list[tuple[int, int]]) -> list[list[int]]:
    """Union-find over edges; return only clusters with >= 2 members."""
    parent: dict[int, int] = {i: i for i in ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path compression
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in edges:
        union(a, b)

    # Group by root
    groups: dict[int, list[int]] = {}
    for i in ids:
        root = find(i)
        groups.setdefault(root, []).append(i)

    return [members for members in groups.values() if len(members) >= 2]


def _select_canonical(members: list[int], rows: dict[int, Any], strategy: str) -> int:
    """Select the canonical memory ID from a cluster according to strategy."""
    if strategy == "keep_latest":
        return max(members, key=lambda i: rows[i]["created_at"])
    if strategy == "keep_most_comprehensive":
        return max(
            members,
            key=lambda i: len((rows[i]["content"] or "").replace("---", "").strip()),
        )
    # Default: keep_highest_access, tiebreak by updated_at
    return max(
        members,
        key=lambda i: (rows[i]["access_count"], rows[i]["updated_at"]),
    )


# compact_memories also excludes 'archived' (in addition to materialized/discarded)
# because compaction should only touch actively-managed memories.
_active_lifecycle_filter = (
    "AND (metadata->>'status' IS NULL "
    "OR metadata->>'status' NOT IN ('materialized', 'discarded', 'archived')) "
    "AND (metadata->>'do_not_compact' IS NULL "
    "OR metadata->>'do_not_compact' != 'true')"
)
_compact_lifecycle_filter = (
    f"{_active_lifecycle_filter} "
    f"{_canonical_entity_protection_filter()}"
)

_LIFECYCLE_STATUS_VALUES: frozenset[str] = frozenset(
    ["open", "materialized", "discarded", "archived"]
)

_pool: asyncpg.Pool | None = None
_migrations_ensured: bool = False
_migrations_suppressed: bool = False


def _validate_lifecycle_status(status: str) -> None:
    """Validate an explicit metadata.status lifecycle value."""
    if status not in _LIFECYCLE_STATUS_VALUES:
        raise ValueError(
            "Invalid lifecycle_status: "
            f"{status!r}. Must be one of: {sorted(_LIFECYCLE_STATUS_VALUES)}"
        )


async def _ensure_link_type_column(conn: asyncpg.Connection) -> None:
    """Add link_type column to memory_relationships if not present (idempotent).

    The column defaults to 'similar_to' so existing rows get the correct value
    without an explicit backfill.
    """
    await conn.execute(
        "ALTER TABLE memory_relationships ADD COLUMN IF NOT EXISTS link_type text NOT NULL DEFAULT 'similar_to';"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memrel_linktype ON memory_relationships(link_type);"
    )


async def _ensure_metadata_column(conn: asyncpg.Connection) -> None:
    """Add metadata jsonb column to memory_relationships if not present (idempotent)."""
    await conn.execute(
        "ALTER TABLE memory_relationships ADD COLUMN IF NOT EXISTS metadata jsonb;"
    )


async def _init_conn(conn: asyncpg.Connection) -> None:
    """Register JSONB codec so asyncpg returns dicts instead of raw JSON strings."""
    await conn.set_type_codec(
        'jsonb',
        encoder=_json.dumps,
        decoder=_json.loads,
        schema='pg_catalog',
    )


def _parse_date(value: str | None) -> datetime | None:
    """Parse ISO date string to datetime. Accepts 'YYYY-MM-DD' or full ISO datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_portable_timestamp(value: Any) -> Any:
    """Parse portable ISO timestamps for asyncpg timestamptz parameters."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


# Importance multipliers for decay rate: critical=0 (no decay), high=0.5x, medium=1.0x, low=2.0x
_IMPORTANCE_MULTIPLIERS: dict[str, float] = {
    "critical": 0.0,
    "high": 0.5,
    "medium": 1.0,
    "low": 2.0,
}

# Default decay factor applied during recall-triggered decay
RECALL_DECAY_FACTOR: float = 0.9


def compute_decay_delta(importance: str, access_count: int, base_decay_delta: float) -> float:
    """Compute the effective decay delta for a memory given importance and access count.

    Implements: delta = base_decay_delta * mult / (1 + access_count * 0.1)
    where mult is the importance multiplier (critical=0.0, high=0.5, medium=1.0, low=2.0).

    Args:
        importance: Memory importance class (critical|high|medium|low).
        access_count: Number of times the memory has been accessed (reduces decay via damping).
        base_decay_delta: Base decay delta = 1.0 - decay_factor.

    Returns:
        Effective decay delta. Multiply by priority to get the amount to subtract.
    """
    mult = _IMPORTANCE_MULTIPLIERS.get(importance)
    if mult is None:
        # Deliberate exception to the ValueError contract of rank_importance/interface.py:
        # here we default silently to the medium multiplier rather than raising.  This is
        # safe because a DB CHECK constraint prevents any unknown importance value from
        # being stored in the first place; the warning is logged for observability but the
        # function must not crash so that lifecycle pipelines remain resilient.
        logger.warning("Unknown importance %r, defaulting to medium multiplier", importance)
        mult = 1.0
    if mult == 0.0:
        return 0.0
    damping = 1.0 + access_count * 0.1
    return base_decay_delta * mult / damping


def suppress_migrations() -> None:
    """Opt this process out of first-call data-layer migrations."""
    global _migrations_suppressed
    _migrations_suppressed = True


async def _run_migrations(conn: asyncpg.Connection) -> None:
    """Run idempotent schema/data migrations for the data layer."""
    # Idempotent migrations
    await conn.execute(
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS session_ref TEXT;"
    )
    await conn.execute(
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS user_id TEXT;"
    )
    await conn.execute(
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS importance VARCHAR(8) NOT NULL DEFAULT 'medium' "
        "CHECK (importance IN ('critical', 'high', 'medium', 'low'));"
    )
    await conn.execute(
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_decay_at TIMESTAMPTZ;"
    )
    await conn.execute(
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_boost_at TIMESTAMPTZ;"
    )
    await conn.execute(
        "UPDATE memories SET last_decay_at = updated_at WHERE last_decay_at IS NULL;"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS embedding_token_log (
            id BIGSERIAL PRIMARY KEY,
            operation TEXT NOT NULL,
            token_count INT NOT NULL,
            logged_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_embedding_token_log_logged_at
        ON embedding_token_log(logged_at);
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memories_content_hash
        ON memories ((metadata->>'content_hash'))
        WHERE metadata->>'content_hash' IS NOT NULL;
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_capture_status
        ON memories ((metadata->>'capture_status'));
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_lifecycle_actions (
            id BIGSERIAL PRIMARY KEY,
            memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            policy_version TEXT NOT NULL,
            action TEXT
                CHECK (action IS NULL OR action IN ('keep', 'merge', 'promote', 'scaffold', 'archive')),
            reason TEXT,
            state TEXT NOT NULL DEFAULT 'classifying'
                CHECK (state IN ('classifying', 'staged', 'resolved', 'needs_review', 'failed')),
            reservation_token TEXT,
            resolution_note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (
                state = 'classifying'
                OR (state = 'failed' AND reason IS NOT NULL)
                OR (action IS NOT NULL AND reason IS NOT NULL)
            ),
            UNIQUE (memory_id, policy_version)
        );
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_actions_state
        ON memory_lifecycle_actions (policy_version, state);
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS session_learning_reviews (
            id BIGSERIAL PRIMARY KEY,
            review_key TEXT NOT NULL,
            source_memory_ids BIGINT[] NOT NULL,
            decision TEXT NOT NULL
                CHECK (decision IN ('accept', 'covered_obsolete', 'project_only', 'dismiss')),
            reason TEXT NOT NULL CHECK (length(btrim(reason)) > 0),
            canonical_learning TEXT NOT NULL
                CHECK (length(btrim(canonical_learning)) > 0),
            reviewed_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT session_learning_reviews_reviewed_by_not_blank
                CHECK (length(btrim(reviewed_by)) > 0),
            CHECK (cardinality(source_memory_ids) >= 1)
        );
    """)
    await conn.execute("""
        UPDATE session_learning_reviews
        SET reviewed_by = 'legacy-unattributed'
        WHERE reviewed_by IS NULL OR length(btrim(reviewed_by)) = 0;

        ALTER TABLE session_learning_reviews
            DROP CONSTRAINT IF EXISTS session_learning_reviews_reviewed_by_check;

        ALTER TABLE session_learning_reviews
            ALTER COLUMN reviewed_by SET NOT NULL;

        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'session_learning_reviews_reviewed_by_not_blank'
                  AND conrelid = 'session_learning_reviews'::regclass
            ) THEN
                ALTER TABLE session_learning_reviews
                    ADD CONSTRAINT session_learning_reviews_reviewed_by_not_blank
                    CHECK (length(btrim(reviewed_by)) > 0);
            END IF;
        END $$;
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_session_learning_reviews_key_created
        ON session_learning_reviews (review_key, created_at DESC, id DESC);
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS url_tokens (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            token_hash TEXT NOT NULL UNIQUE,
            scopes JSONB NOT NULL DEFAULT '[]',
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)
    # Drop every known hybrid_search signature before recreating the canonical one.
    # Postgres treats different argument-type lists as separate overloads, so
    # CREATE OR REPLACE leaves stale legacy signatures untouched; it also cannot
    # change return types, so dropping current+legacy makes rebuild idempotent.
    # Historical releases produced 5-, 6-, and 7-argument overloads. Wrap the
    # drop+recreate in a single transaction so concurrent READ COMMITTED
    # connections keep resolving the OLD definition until commit (MVCC snapshot),
    # eliminating the undefined-function window that separate autocommit statements
    # would otherwise open between DROP and CREATE OR REPLACE.
    async with conn.transaction():
        await conn.execute("""
            DROP FUNCTION IF EXISTS public.hybrid_search(TEXT, vector, INTEGER, INTEGER, INTEGER);
            DROP FUNCTION IF EXISTS public.hybrid_search(TEXT, vector, INTEGER, INTEGER, INTEGER, TEXT);
            DROP FUNCTION IF EXISTS public.hybrid_search(TEXT, vector, INTEGER, INTEGER, INTEGER, TEXT, JSONB);
            DROP FUNCTION IF EXISTS public.hybrid_search(TEXT, vector, INTEGER, INTEGER, INTEGER, TEXT, JSONB, TEXT);
        """)
        await conn.execute("""
            CREATE OR REPLACE FUNCTION public.hybrid_search(
                query_text text,
                query_embedding vector,
                match_limit integer DEFAULT 20,
                rrf_k integer DEFAULT 60,
                p_index_id integer DEFAULT NULL,
                p_user_id text DEFAULT NULL,
                p_metadata_filter jsonb DEFAULT NULL,
                p_capture_status text DEFAULT NULL
            )
            RETURNS TABLE(id integer, title text, subtitle text, type text, score real, created_at timestamp with time zone)
            LANGUAGE sql
            STABLE
            AS $fn$
              WITH fts AS (
                SELECT m.id,
                       ROW_NUMBER() OVER (
                         ORDER BY ts_rank_cd(m.search_vector, websearch_to_tsquery('english', query_text)) DESC
                       ) AS rank
                FROM memories m
                WHERE m.search_vector @@ websearch_to_tsquery('english', query_text)
                  AND (p_index_id IS NULL OR m.index_id = p_index_id)
                  AND (p_user_id IS NULL OR m.user_id = p_user_id)
                  AND (p_metadata_filter IS NULL OR m.metadata @> p_metadata_filter)
                  AND (p_capture_status IS NULL OR m.metadata->>'capture_status' = p_capture_status)
                ORDER BY ts_rank_cd(m.search_vector, websearch_to_tsquery('english', query_text)) DESC
                LIMIT match_limit * 2
              ),
              vec AS (
                SELECT m.id,
                       ROW_NUMBER() OVER (ORDER BY m.embedding <=> query_embedding) AS rank
                FROM memories m
                WHERE m.embedding IS NOT NULL
                  AND (p_index_id IS NULL OR m.index_id = p_index_id)
                  AND (p_user_id IS NULL OR m.user_id = p_user_id)
                  AND (p_metadata_filter IS NULL OR m.metadata @> p_metadata_filter)
                  AND (p_capture_status IS NULL OR m.metadata->>'capture_status' = p_capture_status)
                ORDER BY m.embedding <=> query_embedding
                LIMIT match_limit * 2
              ),
              combined AS (
                SELECT
                  COALESCE(f.id, v.id) AS id,
                  (COALESCE(1.0 / (rrf_k + f.rank), 0.0) +
                   COALESCE(1.0 / (rrf_k + v.rank), 0.0))::REAL AS score
                FROM fts f
                FULL OUTER JOIN vec v ON f.id = v.id
              )
              SELECT
                m.id,
                m.title,
                m.subtitle,
                m.type,
                (c.score * (0.35 + 0.65 * LEAST(GREATEST(m.priority, 0.0), 1.0)))::REAL AS score,
                m.created_at
              FROM combined c
              JOIN memories m ON m.id = c.id
              ORDER BY score DESC
              LIMIT match_limit;
            $fn$;
        """)
    # Importance-aware decay function: skips critical (mult=0.0), applies importance
    # multipliers (high=0.5x, medium=1.0x, low=2.0x), includes 24h race guard via
    # last_decay_at so concurrent calls are safe without advisory locks.
    # Typed-relationship schema migration (idempotent)
    await _ensure_link_type_column(conn)
    await _ensure_metadata_column(conn)
    # Drop both known decay_unused_priorities signatures before recreating it.
    # Postgres treats REAL and DOUBLE PRECISION as separate argument overloads,
    # so CREATE OR REPLACE leaves stale legacy signatures untouched; it also cannot
    # change return types, so dropping current+legacy makes rebuild idempotent.
    # Wrap the drop+recreate in a single transaction so concurrent READ COMMITTED
    # connections keep resolving the OLD definition until commit (MVCC snapshot),
    # eliminating the undefined-function window that separate autocommit statements
    # would otherwise open between DROP and CREATE OR REPLACE.
    async with conn.transaction():
        await conn.execute("""
            DROP FUNCTION IF EXISTS public.decay_unused_priorities(INTEGER, REAL);
            DROP FUNCTION IF EXISTS public.decay_unused_priorities(INTEGER, DOUBLE PRECISION);
        """)
        await conn.execute(f"""
            CREATE OR REPLACE FUNCTION decay_unused_priorities(
                p_stale_days integer,
                p_decay_factor float
            ) RETURNS integer
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_updated integer;
            BEGIN
                WITH mult_map(importance, mult) AS (
                    VALUES ('critical'::text, 0.0::float),
                           ('high'::text,     0.5::float),
                           ('medium'::text,   1.0::float),
                           ('low'::text,      2.0::float)
                ),
                updated AS (
                    UPDATE memories m
                    SET priority = GREATEST(
                            0.0,
                            priority - priority * (1.0 - p_decay_factor)
                                       * mult_map.mult
                                       / (1.0 + CAST(m.access_count AS float) * 0.1)
                        ),
                        last_decay_at = NOW()
                    FROM mult_map
                    WHERE m.importance = mult_map.importance
                      AND mult_map.mult > 0.0
                      AND (m.last_accessed_at IS NULL OR m.last_accessed_at < NOW() - (p_stale_days || ' days')::interval)
                      AND m.created_at < NOW() - (p_stale_days || ' days')::interval
                      AND (m.last_decay_at IS NULL OR m.last_decay_at < NOW() - interval '24 hours')
                      AND {canonical_entity_protection_predicate("m")}
                    RETURNING m.id
                )
                SELECT COUNT(*) INTO v_updated FROM updated;
                RETURN v_updated;
            END;
            $$;
        """)


async def get_pool(run_migrations: bool | None = None) -> asyncpg.Pool:
    """Return the shared asyncpg connection pool."""
    global _pool, _migrations_ensured
    if _pool is None:
        config = get_config()
        _pool = await asyncpg.create_pool(
            config.DATABASE_URL, min_size=2, max_size=10, init=_init_conn
        )

    do_migrate = run_migrations if run_migrations is not None else not _migrations_suppressed
    if do_migrate and not _migrations_ensured:
        async with _pool.acquire() as conn:
            await _run_migrations(conn)
        _migrations_ensured = True
        logger.info("data-layer migrations applied")
    elif not do_migrate:
        logger.debug("data-layer migrations suppressed")
    return _pool


async def close_pool() -> None:
    """Close the connection pool."""
    global _pool, _migrations_ensured, _migrations_suppressed
    if _pool is not None:
        await _pool.close()
        _pool = None
    _migrations_ensured = False
    _migrations_suppressed = False


def _row_to_memory(row: asyncpg.Record) -> Memory:
    """Convert an asyncpg Record to a Memory dataclass."""
    raw_metadata = row.get("metadata")
    if isinstance(raw_metadata, dict):
        metadata = raw_metadata
    elif isinstance(raw_metadata, str):
        try:
            parsed = _json.loads(raw_metadata)
            metadata = parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            metadata = {}
    else:
        metadata = {}

    return Memory(
        id=row["id"],
        index_id=row["index_id"],
        session_id=row.get("session_id"),
        type=row["type"],
        title=row.get("title"),
        subtitle=row.get("subtitle"),
        narrative=row.get("narrative"),
        content=row["content"],
        metadata=metadata,
        priority=float(row["priority"]),
        stability=row["stability"],
        access_count=row["access_count"],
        last_accessed_at=str(row["last_accessed_at"]) if row.get("last_accessed_at") else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        user_id=row.get("user_id"),
        importance=row.get("importance", "medium"),
        last_decay_at=str(row.get("last_decay_at")) if row.get("last_decay_at") else None,
    )


def _row_to_lifecycle_action(row: asyncpg.Record) -> LifecycleActionRecord:
    """Convert a joined lifecycle-action row into its public record."""
    return LifecycleActionRecord(
        id=row["id"],
        memory_id=row["memory_id"],
        policy_version=row["policy_version"],
        action=row.get("action"),
        reason=row.get("reason"),
        state=row["state"],
        memory_type=row["memory_type"],
        memory_title=row.get("memory_title"),
        resolution_note=row.get("resolution_note"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class PostgresDataLayer:
    """DataLayer implementation backed by Postgres + pgvector."""

    def _scope_index_id(self, index_id: int | None) -> int:
        """Normalize missing project scope to the default memory index."""
        if index_id is None:
            return 1
        return index_id

    async def _resolve_index_id(self, conn: asyncpg.Connection, project: str | None) -> int | None:
        """Resolve a project name to its memory_indexes.id, creating if needed."""
        if not project:
            return None
        row = await conn.fetchrow(
            "SELECT id FROM memory_indexes WHERE name = $1", project
        )
        if row:
            return row["id"]
        # Create new index for this project
        row = await conn.fetchrow(
            "INSERT INTO memory_indexes (name) VALUES ($1) RETURNING id", project
        )
        return row["id"]  # type: ignore[index]

    async def _log_embedding_tokens(self, operation: str, token_count: int) -> None:
        """Log embedding token usage to embedding_token_log table.

        Args:
            operation: Type of embedding operation ('document', 'query', 'batch')
            token_count: Number of tokens used
        """
        if token_count <= 0:
            return
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO embedding_token_log (operation, token_count) VALUES ($1, $2)",
                    operation,
                    token_count,
                )
        except (asyncpg.PostgresError, OSError) as err:
            logger.warning("Failed to log embedding tokens: %s", err)

    async def _log_usage(
        self,
        conn: asyncpg.Connection,
        memory_ids: list[int],
        event_type: str,
        session_context: str | None = None,
    ) -> None:
        """Log memory usage events and update priorities."""
        if not memory_ids:
            return
        await conn.executemany(
            "INSERT INTO memory_usage_log (memory_id, event_type, session_context) VALUES ($1, $2, $3)",
            [(mid, event_type, session_context) for mid in memory_ids],
        )
        for mid in memory_ids:
            await conn.execute("SELECT update_priority($1)", mid)

    async def search(self, params: SearchParams) -> SearchResult:
        """Hybrid search: vector + FTS via RRF, with optional filter conditions.

        Browse mode: when no query (or query is '*'), returns filtered/paginated
        results sorted by date without any semantic scoring.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            index_id = await self._resolve_index_id(conn, params.project)
            type_value = params.type or params.obs_type
            limit = params.limit or 20
            offset = params.offset or 0
            if params.capture_status is not None:
                validate_capture_status(params.capture_status)

            # Normalize empty/wildcard queries to None (browse mode)
            query = params.query.strip() if params.query else None
            if query in ("", "*"):
                query = None

            # ── Hybrid search mode ──
            if query:
                try:
                    config = get_config()
                    query_embedding, query_tokens = await embed_query_with_usage(query)
                    asyncio.create_task(self._log_embedding_tokens("query", query_tokens))

                    # Fetch 3x candidates for reranking (capped at 100)
                    fetch_limit = min(limit * 3, 100)

                    # Use hybrid_search for scoring, join memories for full rows + filters
                    # author (p_user_id) is pre-constrained inside hybrid_search as $5
                    # metadata_filter is pre-constrained inside hybrid_search as $6 (NULL if not set)
                    # capture_status is pre-constrained inside hybrid_search as $7 (NULL if not set)
                    # so inbox items are not dropped by the function's internal candidate truncation.
                    metadata_jsonb = params.metadata_filter if params.metadata_filter else None
                    post_conditions: list[str] = []
                    post_values: list[Any] = [
                        query, to_pg_vector(query_embedding), fetch_limit * 3, index_id, params.author,
                        metadata_jsonb, params.capture_status,
                    ]
                    param_idx = 8  # after the 7 hybrid_search params ($1–$7)

                    if type_value:
                        post_conditions.append(f"m.type = ${param_idx}")
                        post_values.append(type_value)
                        param_idx += 1
                    if params.date_start:
                        post_conditions.append(f"m.created_at >= ${param_idx}")
                        post_values.append(_parse_date(params.date_start))
                        param_idx += 1
                    if params.date_end:
                        post_conditions.append(f"m.created_at <= ${param_idx}")
                        post_values.append(_parse_date(params.date_end))
                        param_idx += 1
                    if params.file_path:
                        post_conditions.append(f"m.metadata->>'filePath' = ${param_idx}")
                        post_values.append(params.file_path)
                        param_idx += 1
                    # metadata_filter ($6) and capture_status ($7) are now pre-constrained inside
                    # hybrid_search, not post-filters — they must gate candidate selection BEFORE
                    # the function's internal LIMIT match_limit * 2 truncation.

                    post_where = f"AND {' AND '.join(post_conditions)}" if post_conditions else ""

                    rows = await conn.fetch(
                        f"""WITH scored AS (
                            SELECT id, score FROM hybrid_search($1, $2::vector, $3, 60, $4, $5, $6, $7)
                        )
                        SELECT m.id, m.index_id, m.session_id, m.type, m.title, m.subtitle,
                               m.narrative, m.content, m.metadata, m.priority, m.stability,
                               m.access_count, m.last_accessed_at, m.last_decay_at, m.created_at, m.updated_at, m.user_id, m.importance
                        FROM scored s
                        JOIN memories m ON m.id = s.id
                        WHERE 1=1 {post_where}
                        ORDER BY s.score DESC
                        LIMIT ${param_idx} OFFSET ${param_idx + 1}""",
                        *post_values, fetch_limit, offset,
                    )

                    memories = [_row_to_memory(r) for r in rows]

                    # Second-pass reranking with Voyage Rerank-2.5
                    if config.RERANK_ENABLED and memories:
                        documents = [m.content for m in memories]
                        rerank_results = await rerank(
                            query=query,
                            documents=documents,
                            model=config.RERANK_MODEL,
                            top_k=limit,
                        )
                        memories = _order_by_rerank_results(memories, rerank_results, limit)
                    else:
                        memories = memories[:limit]

                    asyncio.create_task(
                        self._log_usage_background([m.id for m in memories], "search_hit")
                    )
                    # Recall-triggered decay: batch all candidates into ONE background task
                    # to avoid N concurrent pool acquisitions causing connection contention.
                    decay_candidates = [
                        (m.id, m.importance, m.access_count)
                        for m in memories
                    ]
                    if decay_candidates:
                        asyncio.create_task(self._apply_recall_decay_background(decay_candidates))
                    return SearchResult(results=memories, total=len(memories))

                except Exception:
                    logger.exception("Hybrid search failed, falling back to filter+FTS")

            # ── Browse / filter mode (no query, or hybrid failed) ──
            conditions: list[str] = []
            values: list[Any] = []
            param_idx = 1

            if index_id is not None:
                conditions.append(f"m.index_id = ${param_idx}")
                values.append(index_id)
                param_idx += 1
            if type_value:
                conditions.append(f"m.type = ${param_idx}")
                values.append(type_value)
                param_idx += 1
            if params.date_start:
                conditions.append(f"m.created_at >= ${param_idx}")
                values.append(_parse_date(params.date_start))
                param_idx += 1
            if params.date_end:
                conditions.append(f"m.created_at <= ${param_idx}")
                values.append(_parse_date(params.date_end))
                param_idx += 1
            if params.file_path:
                conditions.append(f"m.metadata->>'filePath' = ${param_idx}")
                values.append(params.file_path)
                param_idx += 1
            if params.metadata_filter:
                if params.metadata_filter == {CANONICAL_ENTITY_METADATA_KEY: True}:
                    conditions.append(canonical_entity_select_predicate("m"))
                else:
                    conditions.append(f"m.metadata @> ${param_idx}::jsonb")
                    values.append(params.metadata_filter)
                    param_idx += 1
            if params.capture_status is not None:
                conditions.append(f"m.metadata->>'capture_status' = ${param_idx}")
                values.append(params.capture_status)
                param_idx += 1
            if params.author:
                conditions.append(f"m.user_id = ${param_idx}")
                values.append(params.author)
                param_idx += 1
            # FTS fallback when hybrid failed but we have a query
            if query:
                conditions.append(
                    f"m.search_vector @@ websearch_to_tsquery('english', ${param_idx})"
                )
                values.append(query)
                param_idx += 1

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            order_by = "m.created_at ASC" if params.order_by == "oldest" else "m.created_at DESC"

            rows = await conn.fetch(
                f"""SELECT m.id, m.index_id, m.session_id, m.type, m.title, m.subtitle, m.narrative, m.content,
                        m.metadata, m.priority, m.stability, m.access_count, m.last_accessed_at, m.last_decay_at, m.created_at, m.updated_at, m.user_id, m.importance
                 FROM memories m {where}
                 ORDER BY {order_by}
                 LIMIT ${param_idx} OFFSET ${param_idx+1}""",
                *values, limit, offset,
            )

            count_row = await conn.fetchrow(
                f"SELECT COUNT(*)::int AS total FROM memories m {where}",
                *values,
            )

            memories = [_row_to_memory(r) for r in rows]
            asyncio.create_task(
                self._log_usage_background([m.id for m in memories], "search_hit")
            )
            # Recall-triggered decay: batch all candidates into ONE background task
            # to avoid N concurrent pool acquisitions causing connection contention.
            decay_candidates = [
                (m.id, m.importance, m.access_count)
                for m in memories
            ]
            if decay_candidates:
                asyncio.create_task(self._apply_recall_decay_background(decay_candidates))
            return SearchResult(results=memories, total=count_row["total"])

    async def ingest_status_by_source_refs(
        self,
        source_refs: list[str],
        memory_type: str | None = "meeting",
    ) -> dict[str, dict[str, Any]]:
        """Return ingest status for source references without recording recall usage."""
        unique_refs: list[str] = []
        seen: set[str] = set()
        for source_ref in source_refs:
            ref = str(source_ref).strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)
            unique_refs.append(ref)

        statuses: dict[str, dict[str, Any]] = {
            ref: {
                "source_ref": ref,
                "ingested": False,
                "memory_id": None,
                "run_id": None,
                "ingested_at": None,
                "title": None,
            }
            for ref in unique_refs
        }
        if not unique_refs:
            return statuses

        pool = await get_pool()
        async with pool.acquire() as conn:
            if memory_type is None:
                rows = await conn.fetch(
                    """
                    SELECT
                        metadata->>'source_ref' AS source_ref,
                        id AS memory_id,
                        metadata->>'run_id' AS run_id,
                        created_at AS ingested_at,
                        title
                    FROM memories
                    WHERE metadata->>'source_ref' = ANY($1::text[])
                    ORDER BY created_at DESC
                    """,
                    unique_refs,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT
                        metadata->>'source_ref' AS source_ref,
                        id AS memory_id,
                        metadata->>'run_id' AS run_id,
                        created_at AS ingested_at,
                        title
                    FROM memories
                    WHERE type = $2
                      AND metadata->>'source_ref' = ANY($1::text[])
                    ORDER BY created_at DESC
                    """,
                    unique_refs,
                    memory_type,
                )

        for row in rows:
            ref = row["source_ref"]
            if ref not in statuses or statuses[ref]["ingested"]:
                continue
            statuses[ref] = {
                "source_ref": ref,
                "ingested": True,
                "memory_id": row["memory_id"],
                "run_id": row["run_id"],
                "ingested_at": row["ingested_at"],
                "title": row["title"],
            }

        return statuses

    async def memory_ids_by_content_hashes(
        self,
        content_hashes: list[str],
        index_id: int = 1,
    ) -> dict[str, int]:
        """Return recent exact-content duplicate IDs in save-memory dedup scope."""
        unique_hashes: list[str] = []
        seen: set[str] = set()
        for content_hash in content_hashes:
            normalized_hash = str(content_hash).strip()
            if not normalized_hash or normalized_hash in seen:
                continue
            seen.add(normalized_hash)
            unique_hashes.append(normalized_hash)
        if not unique_hashes:
            return {}

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    metadata->>'content_hash' AS content_hash,
                    id AS memory_id
                FROM memories
                WHERE metadata->>'content_hash' = ANY($1::text[])
                  AND index_id = $2
                  AND created_at > NOW() - ($3 * INTERVAL '1 day')
                ORDER BY created_at DESC
                """,
                unique_hashes,
                index_id,
                DEDUP_WINDOW_DAYS,
            )

        memory_ids: dict[str, int] = {}
        for row in rows:
            content_hash = row["content_hash"]
            if content_hash not in memory_ids:
                memory_ids[content_hash] = row["memory_id"]
        return memory_ids

    async def _apply_recall_decay_background(
        self, candidates: list[tuple[int, str, int]]
    ) -> None:
        """Fire-and-forget: apply recall decay for a batch of memories."""
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                for memory_id, importance, access_count in candidates:
                    await self._apply_recall_decay(conn, memory_id, importance, access_count)
        except Exception as err:
            logger.warning("Recall decay background batch failed: %s", err)

    async def _log_usage_background(self, memory_ids: list[int], event_type: str) -> None:
        """Log usage in background (fire-and-forget)."""
        if not memory_ids:
            return
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await self._log_usage(conn, memory_ids, event_type)
        except Exception as err:
            logger.error("Failed to log usage: %s", err)

    async def timeline(self, params: TimelineParams) -> TimelineResult:
        """Get context window around a memory (anchor mode) or browse a date range (date window mode).

        Modes:
        - Anchor mode: anchor ID (or query to find one), returns N memories before/after.
        - Date window mode: date_start and/or date_end, returns memories in that range.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            anchor_id = params.anchor
            index_id = await self._resolve_index_id(conn, params.project)

            # ── Date window mode ──
            if not anchor_id and not params.query and (params.date_start or params.date_end):
                conditions: list[str] = []
                values: list[Any] = []
                param_idx = 1

                if index_id is not None:
                    conditions.append(f"m.index_id = ${param_idx}")
                    values.append(index_id)
                    param_idx += 1
                if params.date_start:
                    conditions.append(f"m.created_at >= ${param_idx}")
                    values.append(_parse_date(params.date_start))
                    param_idx += 1
                if params.date_end:
                    conditions.append(f"m.created_at <= ${param_idx}")
                    values.append(_parse_date(params.date_end))
                    param_idx += 1

                where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
                limit = (params.depth_before or 5) + (params.depth_after or 5) + 1

                rows = await conn.fetch(
                    f"""SELECT m.id, m.index_id, m.user_id, m.session_id, m.type, m.title, m.subtitle, m.narrative, m.content,
                            m.metadata, m.priority, m.stability, m.access_count, m.last_accessed_at, m.last_decay_at, m.created_at, m.updated_at, m.importance
                     FROM memories m {where}
                     ORDER BY m.created_at ASC
                     LIMIT ${param_idx}""",
                    *values, limit,
                )
                return TimelineResult(results=[_row_to_memory(r) for r in rows], anchor_id=None)

            # ── Anchor mode ──
            # If query provided, find best match as anchor
            if not anchor_id and params.query:
                index_filter = "AND m.index_id = $2" if index_id is not None else ""
                query_values: list[Any] = [params.query]
                if index_id is not None:
                    query_values.append(index_id)

                row = await conn.fetchrow(
                    f"""SELECT m.id FROM memories m
                       WHERE m.search_vector @@ websearch_to_tsquery('english', $1)
                       {index_filter}
                       ORDER BY ts_rank_cd(m.search_vector, websearch_to_tsquery('english', $1)) DESC
                       LIMIT 1""",
                    *query_values,
                )
                if row:
                    anchor_id = row["id"]

            if not anchor_id:
                return TimelineResult(results=[], anchor_id=None)

            depth_before = params.depth_before if params.depth_before is not None else 5
            depth_after = params.depth_after if params.depth_after is not None else 5

            anchor_row = await conn.fetchrow(
                "SELECT created_at, session_id FROM memories WHERE id = $1", anchor_id
            )
            if not anchor_row:
                return TimelineResult(results=[], anchor_id=None)

            index_filter = "AND m.index_id = $2" if index_id is not None else ""
            base_values: list[Any] = [anchor_row["created_at"]]
            if index_id is not None:
                base_values.append(index_id)

            rows = await conn.fetch(
                f"""(SELECT m.id, m.index_id, m.user_id, m.session_id, m.type, m.title, m.subtitle, m.narrative, m.content,
                         m.metadata, m.priority, m.stability, m.access_count, m.last_accessed_at, m.last_decay_at, m.created_at, m.updated_at, m.importance
                  FROM memories m WHERE m.created_at <= $1 {index_filter}
                  ORDER BY m.created_at DESC LIMIT {depth_before + 1})
                 UNION ALL
                 (SELECT m.id, m.index_id, m.user_id, m.session_id, m.type, m.title, m.subtitle, m.narrative, m.content,
                         m.metadata, m.priority, m.stability, m.access_count, m.last_accessed_at, m.last_decay_at, m.created_at, m.updated_at, m.importance
                  FROM memories m WHERE m.created_at > $1 {index_filter}
                  ORDER BY m.created_at ASC LIMIT {depth_after})
                 ORDER BY created_at ASC""",
                *base_values,
            )

            return TimelineResult(results=[_row_to_memory(r) for r in rows], anchor_id=anchor_id)

    async def get_observations(self, ids: list[int]) -> list[Memory]:
        """Bulk fetch memories by IDs."""
        if not ids:
            return []
        pool = await get_pool()
        async with pool.acquire() as conn:
            placeholders = ", ".join(f"${i+1}" for i in range(len(ids)))
            rows = await conn.fetch(
                f"SELECT * FROM memories WHERE id IN ({placeholders}) ORDER BY created_at ASC",
                *ids,
            )
            memories = [_row_to_memory(r) for r in rows]
            asyncio.create_task(
                self._log_usage_background([m.id for m in memories], "retrieved")
            )
            return memories

    async def save_memory(self, params: SaveMemoryParams) -> SaveMemoryResult:
        """Insert a new memory and trigger async embedding.

        Upsert behaviour: when type='session_summary' and session_ref is provided,
        an existing session_summary in the same project scope is updated (content
        appended, title/subtitle/narrative replaced if provided) instead of
        inserting a new row.
        """
        provenance = validate_origin_provenance(params.provenance)
        canonical_metadata = _metadata_with_origin_provenance(
            params.metadata,
            provenance,
        )

        # Validate importance BEFORE any DB access (V6)
        if params.importance not in IMPORTANCE_VALUES:
            raise ValueError(
                f"Invalid importance: {params.importance!r}. "
                f"Must be one of: {sorted(IMPORTANCE_VALUES)}"
            )
        if params.capture_status is not None:
            validate_capture_status(params.capture_status)

        # ── Caller-provided duplicate_of short-circuit ──
        # Must run BEFORE any DB access so it wins over all other paths
        # (including session_summary upsert) per the "duplicate_of ALWAYS wins" contract.
        if params.duplicate_of is not None:
            return SaveMemoryResult(
                id=params.duplicate_of,
                message="Duplicate (caller-provided)",
                duplicate_of=params.duplicate_of,
            )

        pool = await get_pool()
        async with pool.acquire() as conn:
            index_id = await self._resolve_index_id(conn, params.project)

            # ── Upsert path for session_summary ──
            if params.type == "session_summary" and params.session_ref:
                session_summary_index_id = self._scope_index_id(index_id)
                if params.upsert_mode == "replace":
                    # Delete existing rows in FK-safe order, then insert fresh.
                    # Wrapped in a transaction for atomicity.
                    async with conn.transaction():
                        existing_ids_rows = await conn.fetch(
                            """SELECT id FROM memories
                               WHERE session_ref = $1
                                 AND type = 'session_summary'
                                 AND (index_id IS NOT DISTINCT FROM $2)""",
                            params.session_ref,
                            session_summary_index_id,
                        )
                        existing_ids: list[int] = [r["id"] for r in existing_ids_rows]
                        if existing_ids:
                            await conn.execute(
                                "DELETE FROM memory_usage_log WHERE memory_id = ANY($1::int[])",
                                existing_ids,
                            )
                            await conn.execute(
                                "DELETE FROM memory_relationships WHERE source_id = ANY($1::int[]) OR target_id = ANY($1::int[])",
                                existing_ids,
                            )
                            await conn.execute(
                                """DELETE FROM memories
                                   WHERE session_ref = $1
                                     AND type = 'session_summary'
                                     AND (index_id IS NOT DISTINCT FROM $2)""",
                                params.session_ref,
                                session_summary_index_id,
                            )

                        # Content-hash dedup is skipped for replace mode (we're intentionally
                        # replacing content for this session_ref; duplicates across other
                        # sessions are irrelevant).
                        base_metadata = dict(canonical_metadata)
                        content_hash_replace = hashlib.sha256(params.text.encode()).hexdigest()
                        base_metadata["content_hash"] = content_hash_replace
                        # Inject run_id if inside an ingest_run context
                        if run_id := get_current_run_id():
                            base_metadata["run_id"] = run_id
                        row_replace = await conn.fetchrow(
                            """INSERT INTO memories (index_id, type, title, subtitle, narrative, content, session_ref, metadata, user_id, importance)
                               VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
                               RETURNING id""",
                            self._scope_index_id(index_id),
                            params.type or "observation",
                            params.title,
                            params.subtitle,
                            params.narrative,
                            params.text,
                            params.session_ref,
                            base_metadata,
                            params.user_id,
                            params.importance,
                        )
                        memory_id_replace: int = row_replace["id"]

                    # Kick off embedding + auto-linking outside the transaction
                    text_to_embed_replace = ": ".join(
                        part
                        for part in [params.title, params.subtitle, params.narrative, params.text]
                        if part
                    )
                    asyncio.create_task(self._embed_and_link(memory_id_replace, text_to_embed_replace))
                    return SaveMemoryResult(id=memory_id_replace, message="Memory saved")
                else:
                    # append mode: merge content into existing row
                    existing = await conn.fetchrow(
                        """SELECT id, content, metadata FROM memories
                           WHERE session_ref = $1
                             AND type = 'session_summary'
                             AND (index_id IS NOT DISTINCT FROM $2)
                           LIMIT 1""",
                        params.session_ref,
                        session_summary_index_id,
                    )
                    if existing:
                        existing_id: int = existing["id"]
                        merged_content = existing["content"] + "\n\n---\n\n" + params.text
                        merged_metadata = _merge_append_metadata(
                            _record_metadata(existing),
                            canonical_metadata,
                            provenance,
                        )
                        updates: dict[str, Any] = {
                            "content": merged_content,
                            "metadata": merged_metadata,
                        }
                        if params.title is not None:
                            updates["title"] = params.title
                        if params.subtitle is not None:
                            updates["subtitle"] = params.subtitle
                        if params.narrative is not None:
                            updates["narrative"] = params.narrative

                        set_parts = []
                        values: list[Any] = []
                        param_idx = 1
                        for col, val in updates.items():
                            set_parts.append(f"{col} = ${param_idx}")
                            values.append(val)
                            param_idx += 1
                        set_parts.append("updated_at = NOW()")
                        values.append(existing_id)
                        await conn.execute(
                            f"UPDATE memories SET {', '.join(set_parts)} WHERE id = ${param_idx}",
                            *values,
                        )

                        text_to_embed = ": ".join(
                            part
                            for part in [params.title, params.subtitle, params.narrative, merged_content]
                            if part
                        )
                        asyncio.create_task(self._embed_and_link(existing_id, text_to_embed))
                        return SaveMemoryResult(id=existing_id, message="Memory updated (upsert)")

            # ── Semantic dedup (dedup_mode == "merge" only) ──
            if params.dedup_mode == "merge":
                config = get_config()
                query_embedding, query_tokens = await embed_query_with_usage(params.text)
                asyncio.create_task(self._log_embedding_tokens("query", query_tokens))
                vec_str = to_pg_vector(query_embedding)
                # Normalize index_id for dedup: match the same scope used by inserts.
                search_index_id = self._scope_index_id(index_id)
                match_row = await conn.fetchrow(
                    """SELECT id, importance, content,
                              1 - (embedding <=> $1::vector) AS similarity
                       FROM memories
                       WHERE embedding IS NOT NULL
                         AND (index_id IS NOT DISTINCT FROM $2)
                         AND created_at > NOW() - ($3 * INTERVAL '1 day')
                       ORDER BY embedding <=> $1::vector
                       LIMIT 1""",
                    vec_str,
                    search_index_id,
                    DEDUP_WINDOW_DAYS,
                )
                if match_row is not None and match_row["similarity"] >= config.DEDUP_THRESHOLD:
                    existing_id = match_row["id"]
                    existing_importance: str = match_row["importance"] or "medium"

                    # Higher importance wins
                    new_importance = (
                        params.importance
                        if rank_importance(params.importance) > rank_importance(existing_importance)
                        else existing_importance
                    )
                    # Do not mutate priority — preserve existing value
                    await conn.execute(
                        "UPDATE memories SET updated_at = NOW(), importance = $2 WHERE id = $1",
                        existing_id,
                        new_importance,
                    )
                    return SaveMemoryResult(
                        id=existing_id,
                        message="Duplicate (semantic merge)",
                        duplicate_of=existing_id,
                    )

            # ── Content hash dedup ──
            content_hash = hashlib.sha256(params.text.encode()).hexdigest()
            # Normalize index_id for dedup: match the same scope used by inserts.
            content_hash_index_id = self._scope_index_id(index_id)
            dup_row = await conn.fetchrow(
                """SELECT id FROM memories
                   WHERE metadata->>'content_hash' = $1
                     AND created_at > NOW() - ($3 * INTERVAL '1 day')
                     AND index_id = $2
                   LIMIT 1""",
                content_hash,
                content_hash_index_id,
                DEDUP_WINDOW_DAYS,
            )
            if dup_row:
                return SaveMemoryResult(
                    id=dup_row["id"],
                    message="Duplicate content detected",
                    duplicate_of=dup_row["id"],
                )

            # ── Normal insert path ──
            base_metadata = dict(canonical_metadata)
            base_metadata["content_hash"] = content_hash
            base_metadata["capture_status"] = params.capture_status or "inbox"
            # Inject run_id if inside an ingest_run context
            if run_id := get_current_run_id():
                base_metadata["run_id"] = run_id
            row = await conn.fetchrow(
                """INSERT INTO memories (index_id, type, title, subtitle, narrative, content, session_ref, metadata, user_id, importance)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
                   RETURNING id""",
                self._scope_index_id(index_id),
                params.type or "observation",
                params.title,
                params.subtitle,
                params.narrative,
                params.text,
                params.session_ref,
                base_metadata,
                params.user_id,
                params.importance,
            )
            memory_id: int = row["id"]

        # Kick off embedding + auto-linking as background task
        text_to_embed = ": ".join(
            part
            for part in [params.title, params.subtitle, params.narrative, params.text]
            if part
        )
        asyncio.create_task(self._embed_and_link(memory_id, text_to_embed))

        return SaveMemoryResult(id=memory_id, message="Memory saved")

    async def update_memory(self, params: UpdateMemoryParams) -> SaveMemoryResult:
        """Update an existing memory's fields and re-embed if content changed."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Verify the memory exists
            existing = await conn.fetchrow(
                "SELECT id, content, title, subtitle, narrative FROM memories WHERE id = $1",
                params.id,
            )
            if not existing:
                raise ValueError(f"Memory {params.id} not found")

            # Build SET clause dynamically from provided fields
            updates: dict[str, Any] = {}
            if params.text is not None:
                updates["content"] = params.text
            if params.type is not None:
                updates["type"] = params.type
            if params.title is not None:
                updates["title"] = params.title
            if params.subtitle is not None:
                updates["subtitle"] = params.subtitle
            if params.narrative is not None:
                updates["narrative"] = params.narrative
            if params.project is not None:
                index_id = await self._resolve_index_id(conn, params.project)
                updates["index_id"] = self._scope_index_id(index_id)

            has_metadata_merge = params.metadata is not None

            if not updates and not has_metadata_merge:
                return SaveMemoryResult(id=params.id, message="No fields to update")

            # Build parameterized query
            set_parts = []
            values: list[Any] = []
            param_idx = 1
            for col, val in updates.items():
                set_parts.append(f"{col} = ${param_idx}")
                values.append(val)
                param_idx += 1
            if has_metadata_merge:
                set_parts.append(f"metadata = metadata || ${param_idx}::jsonb")
                values.append(params.metadata)
                param_idx += 1
            set_parts.append("updated_at = NOW()")

            values.append(params.id)
            query = f"UPDATE memories SET {', '.join(set_parts)} WHERE id = ${param_idx}"
            await conn.execute(query, *values)

        # Re-embed if text-related fields changed
        content_changed = any(
            k in updates for k in ("content", "title", "subtitle", "narrative")
        )
        if content_changed:
            # Use updated values, falling back to existing
            text_to_embed = ": ".join(
                part
                for part in [
                    params.title if params.title is not None else existing["title"],
                    params.subtitle if params.subtitle is not None else existing["subtitle"],
                    params.narrative if params.narrative is not None else existing["narrative"],
                    params.text if params.text is not None else existing["content"],
                ]
                if part
            )
            asyncio.create_task(self._embed_and_link(params.id, text_to_embed))

        return SaveMemoryResult(id=params.id, message="Memory updated")

    async def set_capture_status(self, params: CaptureTransitionParams) -> SaveMemoryResult:
        """Change capture inbox status without changing lifecycle status by default."""
        validate_capture_status(params.capture_status)
        if params.lifecycle_status is not None:
            _validate_lifecycle_status(params.lifecycle_status)

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Concurrent capture transitions are last-writer-wins via the JSONB || merge.
            if params.lifecycle_status is None:
                updated = await conn.fetchrow(
                    """UPDATE memories
                       SET metadata = metadata || jsonb_build_object('capture_status', $2::text),
                           updated_at = NOW()
                       WHERE id = $1
                       RETURNING id""",
                    params.memory_id,
                    params.capture_status,
                )
            else:
                updated = await conn.fetchrow(
                    """UPDATE memories
                       SET metadata = metadata || jsonb_build_object('capture_status', $2::text, 'status', $3::text),
                           updated_at = NOW()
                       WHERE id = $1
                       RETURNING id""",
                    params.memory_id,
                    params.capture_status,
                    params.lifecycle_status,
                )
            if not updated:
                raise ValueError(f"Memory {params.memory_id} not found")

        return SaveMemoryResult(id=params.memory_id, message="Capture status updated")

    async def approved_update_canonical_entity(
        self, params: ApprovedCanonicalEntityUpdateParams
    ) -> SaveMemoryResult:
        """Apply an explicitly approved canonical entity update or soft archive."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                """
                SELECT id, content, type, title, subtitle, narrative, metadata
                FROM memories
                WHERE id = $1
                """,
                params.id,
            )
            if not existing:
                raise ValueError(f"Memory {params.id} not found")

            if not _row_is_protected_canonical_entity(existing):
                raise ValueError(f"Memory {params.id} is not a canonical entity")

            # Capture the audit trail from the ORIGINAL server-side metadata
            # BEFORE merging caller-supplied fields. The audit list is
            # append-only and server-computed: a caller-supplied "audit" key in
            # params.metadata must never overwrite or truncate prior history.
            existing_metadata = _metadata_from_row(existing)
            audit_raw = existing_metadata.get("audit")
            audit = list(audit_raw) if isinstance(audit_raw, list) else []

            metadata = existing_metadata.copy()
            metadata.update(params.metadata or {})
            metadata[CANONICAL_ENTITY_METADATA_KEY] = True

            canonical_kind = metadata.get(CANONICAL_KIND_METADATA_KEY)
            if canonical_kind not in CANONICAL_KINDS:
                raise ValueError(
                    f"canonical_kind must be one of {sorted(CANONICAL_KINDS)!r}"
                )

            audit.append(
                {
                    "op": params.operation,
                    "at": datetime.now(UTC).isoformat(),
                    "actor": params.actor,
                    "note": params.note,
                }
            )
            # Always set the server-computed append-only trail last, so any
            # caller-supplied "audit" key merged above is unconditionally
            # overwritten and prior history can only ever grow.
            metadata["audit"] = audit
            if params.operation == "archive":
                metadata["status"] = "archived"

            updates: dict[str, Any] = {"metadata": metadata}
            if params.text is not None:
                updates["content"] = params.text
            if params.type is not None:
                updates["type"] = params.type
            if params.title is not None:
                updates["title"] = params.title
            if params.subtitle is not None:
                updates["subtitle"] = params.subtitle
            if params.narrative is not None:
                updates["narrative"] = params.narrative
            if params.project is not None:
                index_id = await self._resolve_index_id(conn, params.project)
                updates["index_id"] = self._scope_index_id(index_id)

            set_parts: list[str] = []
            values: list[Any] = []
            param_idx = 1
            for col, val in updates.items():
                suffix = "::jsonb" if col == "metadata" else ""
                set_parts.append(f"{col} = ${param_idx}{suffix}")
                values.append(val)
                param_idx += 1
            set_parts.append("updated_at = NOW()")

            values.append(params.id)
            query = f"UPDATE memories SET {', '.join(set_parts)} WHERE id = ${param_idx}"
            await conn.execute(query, *values)

        content_changed = any(
            field is not None
            for field in (params.text, params.title, params.subtitle, params.narrative)
        )
        if content_changed:
            text_to_embed = ": ".join(
                part
                for part in [
                    params.title if params.title is not None else existing["title"],
                    params.subtitle if params.subtitle is not None else existing["subtitle"],
                    params.narrative if params.narrative is not None else existing["narrative"],
                    params.text if params.text is not None else existing["content"],
                ]
                if part
            )
            asyncio.create_task(self._embed_and_link(params.id, text_to_embed))

        message = (
            "Canonical entity archived"
            if params.operation == "archive"
            else "Canonical entity update approved"
        )
        return SaveMemoryResult(id=params.id, message=message)

    async def decay_memories(self, params: DecayParams) -> DecayResult:
        """Apply priority decay to stale memories and boost frequently accessed ones.

        - Stale: memories not accessed in stale_days get priority *= decay_factor
        - Boost: memories with access_count >= boost_threshold get priority *= boost_factor (capped at 1.0)
        - Protected: memories created within boost_days are counted but left unchanged
        - dry_run: returns counts without modifying the DB

        Args:
            params: DecayParams controlling thresholds and factors.

        Returns:
            DecayResult with counts of decayed, boosted, and protected memories.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            if params.dry_run:
                # Count only — no writes
                # dry_run: count candidates without executing. This WHERE clause must mirror
                # decay_unused_priorities(stale_days, factor) DB function.
                # Verified criteria: last_accessed_at < NOW() - stale_days OR
                # (last_accessed_at IS NULL AND created_at < NOW() - stale_days)
                # If the DB function changes, update this query too.
                decayed = await conn.fetchval(
                    f"""SELECT COUNT(*) FROM memories
                       WHERE (last_accessed_at IS NULL OR last_accessed_at < NOW() - ($1 || ' days')::interval)
                         AND created_at < NOW() - ($1 || ' days')::interval
                         AND importance != 'critical'
                         AND (last_decay_at IS NULL OR last_decay_at < NOW() - interval '24 hours')
                         AND {canonical_entity_protection_predicate()}""",
                    str(params.stale_days),
                )
                boosted = await conn.fetchval(
                    """SELECT COUNT(*) FROM memories
                       WHERE access_count >= $1
                         AND LEAST(priority * $2, 1.0) > priority
                         AND (last_boost_at IS NULL OR last_boost_at < NOW() - interval '24 hours')""",
                    params.boost_threshold,
                    params.boost_factor,
                )
                recent_memories = await conn.fetchval(
                    """SELECT COUNT(*) FROM memories
                       WHERE created_at >= NOW() - ($1 || ' days')::interval""",
                    str(params.boost_days),
                )
                protected_canonical_entities = await conn.fetchval(
                    f"""SELECT COUNT(*) FROM memories
                       WHERE (last_accessed_at IS NULL OR last_accessed_at < NOW() - ($1 || ' days')::interval)
                         AND created_at < NOW() - ($1 || ' days')::interval
                         AND importance != 'critical'
                         AND (last_decay_at IS NULL OR last_decay_at < NOW() - interval '24 hours')
                         AND {canonical_entity_select_predicate()}""",
                    str(params.stale_days),
                )
            else:
                # Apply decay: use existing DB function decay_unused_priorities(stale_days, decay_factor)
                # Note: this mirrors the WHERE clause in decay_unused_priorities() DB function — keep in sync
                decayed = await conn.fetchval(
                    "SELECT decay_unused_priorities($1, $2)",
                    params.stale_days,
                    params.decay_factor,
                )
                # Boost applies to frequently-accessed memories regardless of age — recent memories benefit
                # from boost too. A memory that is both stale and frequently accessed will be decayed first
                # and then boosted (net effect: boost partially counteracts decay, intentional behavior).
                # AK3 (protection from decay) is separate: the DB function excludes recently-created memories
                # from decay, but the boost here intentionally applies to all frequently-accessed memories.
                boosted = await conn.fetchval(
                    """WITH updated AS (
                           UPDATE memories
                           SET priority = LEAST(priority * $1, 1.0),
                               last_boost_at = NOW(),
                               updated_at = NOW()
                           WHERE access_count >= $2
                             AND LEAST(priority * $1, 1.0) > priority
                             AND (last_boost_at IS NULL OR last_boost_at < NOW() - interval '24 hours')
                           RETURNING id
                       )
                       SELECT COUNT(*) FROM updated""",
                    params.boost_factor,
                    params.boost_threshold,
                )
                recent_memories = await conn.fetchval(
                    """SELECT COUNT(*) FROM memories
                       WHERE created_at >= NOW() - ($1 || ' days')::interval""",
                    str(params.boost_days),
                )
                protected_canonical_entities = await conn.fetchval(
                    f"""SELECT COUNT(*) FROM memories
                       WHERE (last_accessed_at IS NULL OR last_accessed_at < NOW() - ($1 || ' days')::interval)
                         AND created_at < NOW() - ($1 || ' days')::interval
                         AND importance != 'critical'
                         AND (last_decay_at IS NULL OR last_decay_at < NOW() - interval '24 hours')
                         AND {canonical_entity_select_predicate()}""",
                    str(params.stale_days),
                )

        decayed = int(decayed or 0)
        boosted = int(boosted or 0)
        recent_memories = int(recent_memories or 0)
        protected_canonical_entities = int(protected_canonical_entities or 0)
        summary = (
            f"Decay run complete: {decayed} memories decayed, "
            f"{boosted} memories boosted, {recent_memories} recent memories (< {params.boost_days} days), "
            f"{protected_canonical_entities} protected canonical entities skipped."
            + (" (dry_run)" if params.dry_run else "")
        )
        return DecayResult(
            decayed=decayed,
            boosted=boosted,
            recent_memories=recent_memories,
            summary=summary,
            protected_canonical_entities=protected_canonical_entities,
        )

    async def _apply_recall_decay(
        self,
        conn: asyncpg.Connection,
        memory_id: int,
        importance: str,
        access_count: int,
    ) -> None:
        """Apply decay to a single memory on recall if last_decay_at is older than 24 hours.

        Uses an atomic UPDATE ... WHERE to avoid races: only the first concurrent caller within
        the 24h window will update the row. Critical memories are always skipped.

        Args:
            conn: Active asyncpg connection.
            memory_id: ID of the memory to maybe decay.
            importance: Importance class of the memory (skips if 'critical').
            access_count: Current access count (used for damping).
        """
        delta = compute_decay_delta(importance, access_count, 1.0 - RECALL_DECAY_FACTOR)
        if delta == 0.0:
            return
        await conn.execute(
            f"""UPDATE memories
               SET priority = GREATEST(0.0, priority - priority * $1),
                   last_decay_at = NOW()
               WHERE id = $2
                 AND importance != 'critical'
                 AND (last_decay_at IS NULL OR last_decay_at < NOW() - interval '24 hours')
                         AND {canonical_entity_protection_predicate()}""",
            delta,
            memory_id,
        )

    async def _embed_and_link(self, memory_id: int, text: str) -> None:
        """Background task: embed a memory and auto-link similar ones."""
        try:
            embedding, token_count = await embed_with_usage(text)
            asyncio.create_task(self._log_embedding_tokens("document", token_count))
            pg_vec = to_pg_vector(embedding)

            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE memories SET embedding = $1 WHERE id = $2",
                    pg_vec,
                    memory_id,
                )

                # Auto-link: find top 5 similar memories with cosine similarity > 0.65
                similar_rows = await conn.fetch(
                    """SELECT m.id, 1 - (m.embedding <=> $1::vector) AS similarity
                       FROM memories m
                       WHERE m.id != $2
                         AND m.embedding IS NOT NULL
                         AND 1 - (m.embedding <=> $1::vector) > 0.65
                       ORDER BY m.embedding <=> $1::vector
                       LIMIT 5""",
                    pg_vec,
                    memory_id,
                )

                for similar_row in similar_rows:
                    await conn.execute(
                        """INSERT INTO memory_relationships (source_id, target_id, relation_type, confidence)
                           VALUES ($1, $2, 'similar_to', $3)
                           ON CONFLICT (source_id, target_id, relation_type) DO UPDATE SET confidence = $3""",
                        memory_id,
                        similar_row["id"],
                        float(similar_row["similarity"]),
                    )

                if similar_rows:
                    logger.info(
                        "Auto-linked memory %d to %d similar memories",
                        memory_id,
                        len(similar_rows),
                    )
        except Exception as err:
            logger.error("Embedding/linking failed for memory %d: %s", memory_id, err)

    async def search_by_concept(
        self, query: str, limit: int | None = None, project: str | None = None
    ) -> dict[str, list[Memory]]:
        """Pure vector search using cosine similarity."""
        config = get_config()
        pool = await get_pool()
        async with pool.acquire() as conn:
            index_id = await self._resolve_index_id(conn, project)
            query_embedding, query_tokens = await embed_query_with_usage(query)
            asyncio.create_task(self._log_embedding_tokens("query", query_tokens))
            max_results = limit or 10

            # Fetch 3x candidates when reranking is enabled
            fetch_limit = min(max_results * 3, 100) if config.RERANK_ENABLED else max_results

            conditions = ["m.embedding IS NOT NULL"]
            values: list[Any] = [to_pg_vector(query_embedding)]
            param_idx = 2

            if index_id is not None:
                conditions.append(f"m.index_id = ${param_idx}")
                values.append(index_id)
                param_idx += 1

            values.append(fetch_limit)

            rows = await conn.fetch(
                f"""SELECT m.id, m.index_id, m.session_id, m.type, m.title, m.subtitle, m.narrative, m.content,
                        m.metadata, m.priority, m.stability, m.access_count, m.last_accessed_at, m.last_decay_at, m.created_at, m.updated_at, m.user_id, m.importance,
                        1 - (m.embedding <=> $1::vector) AS similarity
                 FROM memories m
                 WHERE {' AND '.join(conditions)}
                 ORDER BY m.embedding <=> $1::vector
                 LIMIT ${param_idx}""",
                *values,
            )
            memories = [_row_to_memory(r) for r in rows]

            # Second-pass reranking with Voyage Rerank-2.5
            if config.RERANK_ENABLED and memories:
                documents = [m.content for m in memories]
                rerank_results = await rerank(
                    query=query,
                    documents=documents,
                    model=config.RERANK_MODEL,
                    top_k=max_results,
                )
                memories = _order_by_rerank_results(memories, rerank_results, max_results)
            else:
                memories = _order_by_priority_score(
                    [
                        (memory, _row_float(row, "similarity"))
                        for memory, row in zip(memories, rows, strict=True)
                    ],
                    max_results,
                )

            return {"results": memories}

    async def get_context(
        self, limit: int | None = None, project: str | None = None
    ) -> dict[str, list[Any]]:
        """Get recent session context."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            max_sessions = limit or 5
            index_id = await self._resolve_index_id(conn, project)

            conditions: list[str] = []
            values: list[Any] = []
            param_idx = 1

            if index_id is not None:
                conditions.append(f"s.index_id = ${param_idx}")
                values.append(index_id)
                param_idx += 1

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            values.append(max_sessions)

            rows = await conn.fetch(
                f"""SELECT s.id, s.session_id, s.project, s.started_at, s.ended_at, s.metadata,
                        (SELECT json_agg(json_build_object('summary', ss.summary, 'created_at', ss.created_at))
                         FROM session_summaries ss WHERE ss.session_id = s.id) AS summaries
                 FROM sessions s {where}
                 ORDER BY s.started_at DESC
                 LIMIT ${param_idx}""",
                *values,
            )
            return {"sessions": [dict(r) for r in rows]}

    async def stats(self) -> dict[str, Any]:
        """Get database aggregate statistics including type taxonomy and per-user counts."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            memories_row = await conn.fetchrow("SELECT COUNT(*)::int AS count FROM memories")
            sessions_row = await conn.fetchrow("SELECT COUNT(*)::int AS count FROM sessions")
            relationships_row = await conn.fetchrow(
                "SELECT COUNT(*)::int AS count FROM memory_relationships"
            )
            db_size_row = await conn.fetchrow(
                "SELECT pg_database_size(current_database()) AS size"
            )
            type_rows = await conn.fetch(
                "SELECT type, COUNT(*)::int AS count FROM memories GROUP BY type ORDER BY count DESC"
            )
            user_rows = await conn.fetch(
                "SELECT user_id, COUNT(*)::int AS count FROM memories GROUP BY user_id ORDER BY count DESC"
            )
            embedding_row = await conn.fetchrow(
                """SELECT COUNT(*)::int AS count, COALESCE(SUM(token_count), 0)::bigint AS total_tokens
                   FROM embedding_token_log
                   WHERE logged_at >= CURRENT_DATE"""
            )

        size_bytes = int(db_size_row["size"])
        embeddings_today = int(embedding_row["count"]) if embedding_row else 0
        embedding_tokens_today = int(embedding_row["total_tokens"]) if embedding_row else 0
        # Voyage-4 pricing: $0.00000012 per token (= $0.12 per 1M tokens)
        estimated_cost = round(embedding_tokens_today * 0.00000012, 6)
        return {
            "memories": memories_row["count"],
            "sessions": sessions_row["count"],
            "relationships": relationships_row["count"],
            "db_size_bytes": size_bytes,
            "db_size_mb": round(size_bytes / 1024 / 1024, 2),
            "types": {row["type"]: row["count"] for row in type_rows},
            "by_user": {(row["user_id"] or "unknown"): row["count"] for row in user_rows},
            "embeddings_today": embeddings_today,
            "embedding_tokens_today": embedding_tokens_today,
            "estimated_embedding_cost_today": estimated_cost,
        }

    async def origin_provenance_report(self) -> dict[str, Any]:
        """Classify provenance coverage without migrations or memory side effects."""
        pool = await get_pool(run_migrations=False)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """WITH classified AS (
                       SELECT CASE
                           WHEN NULLIF(
                                    BTRIM(metadata->'provenance'->'origin'->>'producer'),
                                    ''
                                ) IS NOT NULL
                            AND COALESCE(
                                    metadata->'provenance'->'origin'->>'source_ref',
                                    ''
                                ) ~ '^[a-z][a-z0-9-]*:[^[:cntrl:]]*[^[:space:][:cntrl:]]$'
                               THEN 'explicit'
                           WHEN NULLIF(BTRIM(session_ref), '') IS NOT NULL
                             OR NULLIF(BTRIM(metadata->>'source_ref'), '') IS NOT NULL
                             OR NULLIF(BTRIM(metadata->>'session_id'), '') IS NOT NULL
                             OR NULLIF(BTRIM(metadata->>'session_ref'), '') IS NOT NULL
                             OR NULLIF(BTRIM(metadata->>'run_id'), '') IS NOT NULL
                             OR NULLIF(BTRIM(metadata->>'_sqlite_id'), '') IS NOT NULL
                               THEN 'deterministic_backfill'
                           WHEN NULLIF(BTRIM(metadata->>'producer'), '') IS NOT NULL
                             OR NULLIF(BTRIM(metadata->>'source'), '') IS NOT NULL
                             OR NULLIF(BTRIM(metadata->>'capture_template'), '') IS NOT NULL
                             OR NULLIF(BTRIM(metadata->>'agent_type'), '') IS NOT NULL
                             OR type IN (
                                 'session_summary', 'meeting', 'transcript', 'document',
                                 'email', 'daily_brief', 'curated_content'
                             )
                               THEN 'inferred'
                           ELSE 'unresolved'
                       END AS cohort
                       FROM memories
                   )
                   SELECT cohort, COUNT(*)::int AS count
                   FROM classified
                   GROUP BY cohort"""
            )

        counts = {str(row["cohort"]): int(row["count"]) for row in rows}
        bases = {
            "explicit": (
                "valid metadata.provenance.origin producer and namespaced source_ref"
            ),
            "deterministic_backfill": "stable legacy session or source reference",
            "inferred": (
                "recognizable producer, source, or memory-type marker without a stable reference"
            ),
            "unresolved": "no trustworthy origin marker",
        }
        cohorts = {
            cohort: {"count": counts.get(cohort, 0), "basis": basis}
            for cohort, basis in bases.items()
        }
        return {
            "read_only": True,
            "total": sum(item["count"] for item in cohorts.values()),
            "cohorts": cohorts,
        }

    async def portable_closure_counts(self) -> dict[str, int]:
        """Return row counts for the portable knowledge closure."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            indexes = await conn.fetchrow("SELECT COUNT(*)::int AS count FROM memory_indexes")
            sessions = await conn.fetchrow("SELECT COUNT(*)::int AS count FROM sessions")
            memories = await conn.fetchrow("SELECT COUNT(*)::int AS count FROM memories")
            relationships = await conn.fetchrow(
                "SELECT COUNT(*)::int AS count FROM memory_relationships"
            )
        return {
            "indexes": int(indexes["count"]),
            "sessions": int(sessions["count"]),
            "memories": int(memories["count"]),
            "relationships": int(relationships["count"]),
        }

    async def _read_portable_closure(
        self, conn: asyncpg.Connection
    ) -> dict[str, list[dict[str, Any]]]:
        """Read the portable knowledge closure (no credentials, no embeddings).

        Uses the caller-provided connection so all reads can be scoped to a
        single transaction/snapshot by the caller (export snapshot / restore
        emptiness check).
        """
        indexes = await conn.fetch("SELECT id, name FROM memory_indexes ORDER BY id")
        sessions = await conn.fetch(
            """SELECT id, session_id, index_id, project, started_at, ended_at,
                      metadata, status, prompt_counter
               FROM sessions
               ORDER BY id"""
        )
        memories = await conn.fetch(
            """SELECT id, index_id, session_id, type, title, subtitle, narrative,
                      content, metadata, priority, stability, access_count,
                      last_accessed_at, created_at, updated_at, user_id,
                      importance, last_decay_at, session_ref
               FROM memories
               ORDER BY id"""
        )
        relationships = await conn.fetch(
            """SELECT id, source_id, target_id, relation_type, link_type, confidence, metadata
               FROM memory_relationships
               ORDER BY source_id, target_id, relation_type"""
        )
        return {
            "indexes": [dict(row) for row in indexes],
            "sessions": [dict(row) for row in sessions],
            "memories": [dict(row) for row in memories],
            "relationships": [dict(row) for row in relationships],
        }

    async def export_portable_records(self) -> dict[str, list[dict[str, Any]]]:
        """Read the portable knowledge closure without credentials or embeddings.

        All closure reads run in one REPEATABLE READ transaction so they observe a
        single consistent snapshot; a concurrent mutation cannot produce an
        internally inconsistent bundle (e.g. a relationship referencing a memory
        that was not included in the same snapshot).
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read"):
                return await self._read_portable_closure(conn)

    async def _memories_missing_embeddings(self, ids: list[int]) -> set[int]:
        """Return the subset of ids whose memory row currently lacks an embedding."""
        if not ids:
            return set()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id FROM memories WHERE id = ANY($1::bigint[]) AND embedding IS NULL",
                ids,
            )
        return {int(row["id"]) for row in rows}

    async def restore_portable_records(
        self,
        indexes: list[dict[str, Any]],
        sessions: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        *,
        regenerate_embeddings: bool,
    ) -> dict[str, Any]:
        """Restore portable records atomically with explicit ids and idempotent conflicts.

        Findings 4 & 8 (race elimination): the emptiness/same-bundle check, the
        id-preserving inserts, and the sequence repair all run inside ONE
        transaction that first takes an EXCLUSIVE lock on the closure
        tables. That lock blocks concurrent INSERT/UPDATE/DELETE (and therefore
        ``nextval`` via ``save_memory``) for the duration of the restore, so
        check-then-write is atomic and the sequence repair cannot be moved behind
        an id allocated by a concurrent transaction. On a populated target we
        refuse unless the existing closure matches the bundle exactly (idempotent
        rerun), in which case no rows are written.

        Returns ``{"already_restored": bool}``.
        """
        from open_brain.portable_backup import (
            RestoreTargetNotEmptyError,
            _canonical_records,
        )

        expected = _canonical_records(
            {
                "indexes": indexes,
                "sessions": sessions,
                "memories": memories,
                "relationships": relationships,
            }
        )

        pool = await get_pool()
        already_restored = False
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Serialize the whole restore against concurrent writers so the
                # emptiness check below and the writes are atomic (findings 4 & 8).
                await conn.execute(
                    "LOCK TABLE memory_indexes, sessions, memories, memory_relationships "
                    "IN EXCLUSIVE MODE"
                )

                existing = await self._read_portable_closure(conn)
                populated = any(
                    existing[key]
                    for key in ("indexes", "sessions", "memories", "relationships")
                )
                if populated:
                    if _canonical_records(existing) == expected:
                        # Idempotent same-bundle rerun: leave rows untouched.
                        already_restored = True
                    else:
                        raise RestoreTargetNotEmptyError(
                            "Restore target already contains portable knowledge rows "
                            "that do not match the bundle"
                        )

                if not already_restored:
                    await self._insert_portable_records(
                        conn, indexes, sessions, memories, relationships
                    )

        if regenerate_embeddings:
            memories_to_embed = memories
            if already_restored:
                # No-op restore path: the record data already matched, but the
                # caller asked for embeddings. Regenerate only for memories that
                # are actually missing an embedding (finding 6) so a prior
                # skip-embeddings restore or a partial failure is repaired.
                missing_ids = await self._memories_missing_embeddings(
                    [m["id"] for m in memories if m.get("id") is not None]
                )
                memories_to_embed = [
                    m for m in memories if m.get("id") in missing_ids
                ]
            for memory in memories_to_embed:
                text_to_embed = ": ".join(
                    str(part)
                    for part in [
                        memory.get("title"),
                        memory.get("subtitle"),
                        memory.get("narrative"),
                        memory.get("content"),
                    ]
                    if part
                )
                if text_to_embed:
                    await self._embed_memory_only(memory["id"], text_to_embed)

        return {"already_restored": already_restored}

    async def _insert_portable_records(
        self,
        conn: asyncpg.Connection,
        indexes: list[dict[str, Any]],
        sessions: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
    ) -> None:
        """Insert id-preserving portable rows and repair sequences (in caller's txn).

        The sequence repair runs in the SAME transaction as the inserts and under
        the EXCLUSIVE table lock held by the caller, so it is serialized against
        concurrent ``nextval`` and cannot be moved behind a concurrently
        allocated id. ``GREATEST(MAX(id)+1, 1)`` never produces a value that
        collides with an existing row (no row has id > MAX(id)).
        """
        for index in indexes:
            await conn.execute(
                """INSERT INTO memory_indexes (id, name)
                   VALUES ($1, $2)
                   ON CONFLICT (id) DO NOTHING""",
                index["id"],
                index["name"],
            )

        for session in sessions:
            await conn.execute(
                """INSERT INTO sessions (
                       id, session_id, index_id, project, started_at, ended_at,
                       metadata, status, prompt_counter
                   )
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
                   ON CONFLICT (id) DO NOTHING""",
                session["id"],
                session["session_id"],
                session.get("index_id"),
                session.get("project"),
                _parse_portable_timestamp(session.get("started_at")),
                _parse_portable_timestamp(session.get("ended_at")),
                session.get("metadata") or {},
                session.get("status"),
                session.get("prompt_counter"),
            )

        for memory in memories:
            await conn.execute(
                """INSERT INTO memories (
                       id, index_id, session_id, type, title, subtitle, narrative,
                       content, metadata, priority, stability, access_count,
                       last_accessed_at, created_at, updated_at, user_id,
                       importance, last_decay_at, session_ref
                   )
                   VALUES (
                       $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10,
                       $11, $12, $13, $14, $15, $16, $17, $18, $19
                   )
                   ON CONFLICT (id) DO NOTHING""",
                memory["id"],
                memory.get("index_id"),
                memory.get("session_id"),
                memory.get("type"),
                memory.get("title"),
                memory.get("subtitle"),
                memory.get("narrative"),
                memory.get("content"),
                memory.get("metadata") or {},
                memory.get("priority"),
                memory.get("stability"),
                memory.get("access_count"),
                _parse_portable_timestamp(memory.get("last_accessed_at")),
                _parse_portable_timestamp(memory.get("created_at")),
                _parse_portable_timestamp(memory.get("updated_at")),
                memory.get("user_id"),
                memory.get("importance"),
                _parse_portable_timestamp(memory.get("last_decay_at")),
                memory.get("session_ref"),
            )

        for relationship in relationships:
            await conn.execute(
                """INSERT INTO memory_relationships (
                       id, source_id, target_id, relation_type, link_type, confidence, metadata
                   )
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                   ON CONFLICT (source_id, target_id, relation_type) DO NOTHING""",
                relationship.get("id"),
                relationship["source_id"],
                relationship["target_id"],
                relationship["relation_type"],
                relationship.get("link_type") or relationship["relation_type"],
                relationship.get("confidence"),
                relationship.get("metadata"),
            )

        await conn.execute(
            """SELECT setval(
                   pg_get_serial_sequence('memory_indexes', 'id'),
                   GREATEST(COALESCE((SELECT MAX(id) FROM memory_indexes), 0) + 1, 1),
                   false
               )"""
        )
        await conn.execute(
            """SELECT setval(
                   pg_get_serial_sequence('sessions', 'id'),
                   GREATEST(COALESCE((SELECT MAX(id) FROM sessions), 0) + 1, 1),
                   false
               )"""
        )
        await conn.execute(
            """SELECT setval(
                   pg_get_serial_sequence('memories', 'id'),
                   GREATEST(COALESCE((SELECT MAX(id) FROM memories), 0) + 1, 1),
                   false
               )"""
        )
        await conn.execute(
            """SELECT setval(
                   pg_get_serial_sequence('memory_relationships', 'id'),
                   GREATEST(COALESCE((SELECT MAX(id) FROM memory_relationships), 0) + 1, 1),
                   false
               )"""
        )

    async def _embed_memory_only(self, memory_id: int, text: str) -> None:
        """Regenerate a restored memory embedding without auto-linking."""
        embedding, token_count = await embed_with_usage(text)
        await self._log_embedding_tokens("document", token_count)
        pg_vec = to_pg_vector(embedding)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE memories SET embedding = $1 WHERE id = $2",
                pg_vec,
                memory_id,
            )

    async def refine_memories(self, params: RefineParams) -> RefineResult:
        """LLM-powered memory consolidation."""
        pool = await get_pool()
        limit = params.limit or 50
        scope = params.scope or "recent"

        async with pool.acquire() as conn:
            if scope == "duplicates":
                rows = await conn.fetch(
                    """SELECT DISTINCT ON (m1.id)
                         m1.id, m1.index_id, m1.user_id, m1.session_id, m1.type, m1.title, m1.subtitle, m1.narrative, m1.content,
                         m1.metadata, m1.priority, m1.stability, m1.access_count, m1.last_accessed_at, m1.created_at, m1.updated_at, m1.importance,
                         m2.id AS similar_id, 1 - (m1.embedding <=> m2.embedding) AS similarity
                       FROM memories m1
                       JOIN memories m2 ON m1.id < m2.id
                       WHERE m1.embedding IS NOT NULL AND m2.embedding IS NOT NULL
                         AND 1 - (m1.embedding <=> m2.embedding) > 0.85
                       ORDER BY m1.id, similarity DESC
                       LIMIT $1""",
                    limit,
                )
                candidates = [_row_to_memory(r) for r in rows]
            elif scope.startswith("project:"):
                project = scope[8:]
                index_id = await self._resolve_index_id(conn, project)
                rows = await conn.fetch(
                    "SELECT * FROM memories WHERE index_id = $1 ORDER BY created_at DESC LIMIT $2",
                    self._scope_index_id(index_id),
                    limit,
                )
                candidates = [_row_to_memory(r) for r in rows]
            elif scope == "low-priority":
                rows = await conn.fetch(
                    "SELECT * FROM memories WHERE priority < 0.2 AND importance NOT IN ('critical', 'high') ORDER BY priority ASC LIMIT $1",
                    limit,
                )
                candidates = [_row_to_memory(r) for r in rows]
            else:
                # "recent" - last N memories
                rows = await conn.fetch(
                    "SELECT * FROM memories ORDER BY created_at DESC LIMIT $1", limit
                )
                candidates = [_row_to_memory(r) for r in rows]

        if not candidates:
            return RefineResult(analyzed=0, actions=[], summary="No candidates found")

        actions = await analyze_with_llm(candidates)
        protected_canonical_entities = _filter_protected_refine_actions(
            actions,
            {memory.id: memory for memory in candidates},
        )

        # Decide skip_llm_merge per action based on scope + similarity
        merge_actions = [a for a in actions if a.action == "merge"]
        if scope == "duplicates" and merge_actions:
            # Compute actual pairwise similarity for LLM-suggested merge groups
            all_merge_ids = list({mid for a in merge_actions for mid in a.memory_ids})
            if all_merge_ids:
                async with pool.acquire() as conn:
                    placeholders = ", ".join(f"${i+1}" for i in range(len(all_merge_ids)))
                    sim_rows = await conn.fetch(
                        f"""SELECT m1.id AS id1, m2.id AS id2,
                                   1 - (m1.embedding <=> m2.embedding) AS similarity
                              FROM memories m1
                              JOIN memories m2 ON m1.id < m2.id
                             WHERE m1.id IN ({placeholders})
                               AND m2.id IN ({placeholders})
                               AND m1.embedding IS NOT NULL
                               AND m2.embedding IS NOT NULL""",
                        *all_merge_ids,
                    )
                    similarity_map = {
                        (r["id1"], r["id2"]): float(r["similarity"]) for r in sim_rows
                    }

        for action in list(actions):
            if action.action != "merge":
                continue
            if scope == "low-priority":
                action.skip_llm_merge = True
            elif scope == "duplicates":
                ids = action.memory_ids
                min_sim = min(
                    (similarity_map.get((min(a, b), max(a, b)), 0.0)
                     for a in ids for b in ids if a != b),
                    default=0.0,
                )
                action.similarity = min_sim
                if min_sim < 0.4:
                    # Too dissimilar — likely a false positive from the LLM
                    logger.info("Dropping merge %s — similarity %.3f below floor", ids, min_sim)
                    actions.remove(action)
                elif min_sim >= 0.92:
                    action.skip_llm_merge = True

        if not params.dry_run:
            protected_canonical_entities += await _execute_refine_actions(actions)

        executed_actions = [
            RefineAction(
                action=a.action,
                memory_ids=a.memory_ids,
                reason=a.reason,
                executed=not params.dry_run and bool(a.memory_ids),
                similarity=a.similarity,
                skip_llm_merge=a.skip_llm_merge,
            )
            for a in actions
            if a.memory_ids  # drop actions with empty IDs (skipped or malformed)
        ]

        return RefineResult(
            analyzed=len(candidates),
            actions=executed_actions,
            summary=(
                f"Analyzed {len(candidates)} memories, suggested {len(actions)} actions"
                f", skipped {protected_canonical_entities} protected canonical entities"
                f"{' (dry run)' if params.dry_run else ''}"
            ),
            protected_canonical_entities=protected_canonical_entities,
        )

    async def triage_memories(self, params: TriageParams) -> TriageResult:
        """Classify memories and persist conflict-safe lifecycle proposals."""
        from open_brain.data_layer.triage import triage_with_llm

        policy_version = params.policy_version
        if not policy_version.strip():
            raise ValueError("policy_version must not be empty")
        pool = await get_pool()
        limit = params.limit or 50
        scope = params.scope or "recent"
        reservation_token = str(uuid.uuid4())

        # The policy-specific ledger makes every classification terminal for this
        # policy version, including keep. A future policy version can deliberately
        # reconsider the same memory without mutating its knowledge lifecycle.
        _lifecycle_filter = _compact_lifecycle_filter
        _unstaged_filter = """
            AND NOT EXISTS (
                SELECT 1
                FROM memory_lifecycle_actions
                WHERE memory_lifecycle_actions.memory_id = memories.id
                  AND memory_lifecycle_actions.policy_version = $1
                  AND memory_lifecycle_actions.state != 'failed'
            )
        """
        candidate_ledger_filter = "" if params.dry_run else _unstaged_filter
        ledger_args = () if params.dry_run else (policy_version,)
        first_scope_param = len(ledger_args) + 1

        async def _fetch_candidates(conn: asyncpg.Connection) -> list[asyncpg.Record]:
            if scope.startswith("project:"):
                project = scope[8:]
                index_id = await self._resolve_index_id(conn, project)
                return await conn.fetch(
                    f"SELECT * FROM memories WHERE index_id = ${first_scope_param} {_lifecycle_filter} {candidate_ledger_filter} ORDER BY created_at DESC LIMIT ${first_scope_param + 1}",
                    *ledger_args,
                    self._scope_index_id(index_id),
                    limit,
                )
            if scope.startswith("type:"):
                mem_type = scope[5:]
                return await conn.fetch(
                    f"SELECT * FROM memories WHERE type = ${first_scope_param} {_lifecycle_filter} {candidate_ledger_filter} ORDER BY created_at DESC LIMIT ${first_scope_param + 1}",
                    *ledger_args,
                    mem_type,
                    limit,
                )
            if scope == "low-priority":
                return await conn.fetch(
                    f"SELECT * FROM memories WHERE priority < 0.2 AND importance NOT IN ('critical', 'high') {_lifecycle_filter} {candidate_ledger_filter} ORDER BY priority ASC LIMIT ${first_scope_param}",
                    *ledger_args,
                    limit,
                )
            if scope.startswith("session_ref:"):
                prefix = scope[len("session_ref:"):]
                return await conn.fetch(
                    f"SELECT * FROM memories WHERE session_ref LIKE ${first_scope_param} {_lifecycle_filter} {candidate_ledger_filter} ORDER BY created_at DESC LIMIT ${first_scope_param + 1}",
                    *ledger_args,
                    prefix + "%",
                    limit,
                )
            return await conn.fetch(
                f"SELECT * FROM memories WHERE 1=1 {_lifecycle_filter} {candidate_ledger_filter} ORDER BY created_at DESC LIMIT ${first_scope_param}",
                *ledger_args,
                limit,
            )

        async with pool.acquire() as conn:
            if params.dry_run:
                rows = await _fetch_candidates(conn)
            else:
                async with conn.transaction():
                    await conn.fetchval(
                        "SELECT pg_advisory_xact_lock(hashtext($1))",
                        f"open-brain:lifecycle:{policy_version}",
                    )
                    await conn.execute(
                        """
                        DELETE FROM memory_lifecycle_actions
                        WHERE policy_version = $1
                          AND state = 'classifying'
                          AND updated_at < NOW() - INTERVAL '1 hour'
                        """,
                        policy_version,
                    )
                    selected_rows = await _fetch_candidates(conn)
                    rows = []
                    for selected_row in selected_rows:
                        reservation = await conn.fetchrow(
                            """
                            INSERT INTO memory_lifecycle_actions (
                                memory_id,
                                policy_version,
                                state,
                                reservation_token
                            ) VALUES ($1, $2, 'classifying', $3)
                            ON CONFLICT (memory_id, policy_version) DO UPDATE
                            SET action = NULL,
                                reason = NULL,
                                state = 'classifying',
                                reservation_token = EXCLUDED.reservation_token,
                                resolution_note = NULL,
                                updated_at = NOW()
                            WHERE memory_lifecycle_actions.state = 'failed'
                            RETURNING id
                            """,
                            selected_row["id"],
                            policy_version,
                            reservation_token,
                        )
                        if reservation is not None:
                            rows.append(selected_row)

            candidates = [_row_to_memory(row) for row in rows]

        reserved_memory_ids = (
            [candidate.id for candidate in candidates] if not params.dry_run else []
        )

        async def _fail_owned_reservations(memory_ids: list[int]) -> None:
            if not memory_ids:
                return
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE memory_lifecycle_actions
                    SET state = 'failed',
                        reason = 'Lifecycle classification did not complete',
                        reservation_token = NULL,
                        updated_at = NOW()
                    WHERE memory_id = ANY($1::int[])
                      AND policy_version = $2
                      AND state = 'classifying'
                      AND reservation_token = $3
                    """,
                    memory_ids,
                    policy_version,
                    reservation_token,
                )

        if not candidates:
            return TriageResult(
                analyzed=0,
                actions=[],
                summary=f"No candidates found for policy {policy_version}",
            )

        failed_count = 0
        try:
            actions = await triage_with_llm(candidates)

            if not params.dry_run:
                staged_actions = []
                returned_memory_ids = {action.memory_id for action in actions}
                missing_memory_ids = [
                    memory_id
                    for memory_id in reserved_memory_ids
                    if memory_id not in returned_memory_ids
                ]
                async with pool.acquire() as conn:
                    for action in actions:
                        reason = (
                            action.reason
                            if isinstance(action.reason, str)
                            else "" if action.reason is None else str(action.reason)
                        )
                        row = await conn.fetchrow(
                            """
                            UPDATE memory_lifecycle_actions
                            SET action = $3,
                                reason = $4,
                                state = 'staged',
                                reservation_token = NULL,
                                updated_at = NOW()
                            WHERE memory_id = $1
                              AND policy_version = $2
                              AND state = 'classifying'
                              AND reservation_token = $5
                            RETURNING id
                            """,
                            action.memory_id,
                            policy_version,
                            action.action,
                            reason,
                            reservation_token,
                        )
                        if row is None:
                            continue
                        action.reason = reason
                        action.lifecycle_action_id = row["id"]
                        action.policy_version = policy_version
                        action.state = "staged"
                        staged_actions.append(action)
                await _fail_owned_reservations(missing_memory_ids)
                failed_count = len(missing_memory_ids)
                actions = staged_actions
        except Exception:
            await _fail_owned_reservations(reserved_memory_ids)
            raise

        action_counts: dict[str, int] = {}
        for a in actions:
            action_counts[a.action] = action_counts.get(a.action, 0) + 1

        summary_parts = [f"{count} {act}" for act, count in sorted(action_counts.items())]
        if params.dry_run:
            summary = (
                f"Proposed {len(actions)} actions for {len(candidates)} memories: "
                f"{', '.join(summary_parts)} (dry run; policy {policy_version})"
            )
        else:
            summary = (
                f"Staged {len(actions)} actions from {len(candidates)} analyzed memories: "
                f"{', '.join(summary_parts)} (policy {policy_version}; "
                f"{failed_count} incomplete classifications failed)"
            )

        return TriageResult(analyzed=len(candidates), actions=actions, summary=summary)

    async def list_lifecycle_actions(
        self, params: LifecycleActionQueryParams
    ) -> list[LifecycleActionRecord]:
        """Read persisted lifecycle proposals for review or recovery."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    lifecycle_actions.*,
                    memories.type AS memory_type,
                    memories.title AS memory_title
                FROM memory_lifecycle_actions AS lifecycle_actions
                JOIN memories ON memories.id = lifecycle_actions.memory_id
                WHERE ($1::text IS NULL OR lifecycle_actions.policy_version = $1)
                  AND ($2::text IS NULL OR lifecycle_actions.state = $2)
                ORDER BY lifecycle_actions.created_at ASC
                LIMIT $3
                """,
                params.policy_version,
                params.state,
                params.limit,
            )
        return [_row_to_lifecycle_action(row) for row in rows]

    async def set_lifecycle_action_state(
        self, params: LifecycleActionStateParams
    ) -> LifecycleActionRecord:
        """Record the reviewed state of a persisted lifecycle proposal."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH updated AS (
                    UPDATE memory_lifecycle_actions
                    SET state = $2,
                        resolution_note = COALESCE($3, resolution_note),
                        updated_at = NOW()
                    WHERE id = $1
                      AND state != 'classifying'
                      AND action IS NOT NULL
                      AND reason IS NOT NULL
                    RETURNING *
                )
                SELECT
                    updated.*,
                    memories.type AS memory_type,
                    memories.title AS memory_title
                FROM updated
                JOIN memories ON memories.id = updated.memory_id
                """,
                params.action_id,
                params.state,
                params.note,
            )
        if row is None:
            raise ValueError(
                f"Lifecycle action {params.action_id} not found or not reviewable"
            )
        return _row_to_lifecycle_action(row)

    async def materialize_memories(self, params: MaterializeParams) -> MaterializeResult:
        """Execute materialization for a list of triage actions."""
        from open_brain.data_layer.materialize import execute_triage_actions

        if not params.triage_actions:
            return MaterializeResult(processed=0, results=[], summary="No actions to materialize")

        # Collect all memory IDs and fetch them in one query
        memory_ids = [a.memory_id for a in params.triage_actions]
        pool = await get_pool()
        async with pool.acquire() as conn:
            placeholders = ", ".join(f"${i+1}" for i in range(len(memory_ids)))
            rows = await conn.fetch(
                f"SELECT * FROM memories WHERE id IN ({placeholders})",
                *memory_ids,
            )
            # Also fetch project names for index_ids
            index_ids = list({row["index_id"] for row in rows if row["index_id"]})
            project_rows = []
            if index_ids:
                idx_placeholders = ", ".join(f"${i+1}" for i in range(len(index_ids)))
                project_rows = await conn.fetch(
                    f"SELECT id, name FROM memory_indexes WHERE id IN ({idx_placeholders})",
                    *index_ids,
                )

        memories_by_id = {_row_to_memory(r).id: _row_to_memory(r) for r in rows}
        project_by_index_id = {r["id"]: r["name"] for r in project_rows}

        async def _archive_fn(memory_id: int, priority: float) -> None:
            pool_ = await get_pool()
            async with pool_.acquire() as conn_:
                await conn_.execute(
                    "UPDATE memories SET priority = $1, updated_at = now() WHERE id = $2",
                    priority,
                    memory_id,
                )

        if params.dry_run:
            # In dry run, return what would happen without executing
            from open_brain.data_layer.interface import MaterializeActionResult
            results = [
                MaterializeActionResult(
                    memory_id=a.memory_id,
                    action=a.action,
                    success=True,
                    detail="dry run — not executed",
                )
                for a in params.triage_actions
            ]
        else:
            results = await execute_triage_actions(
                params.triage_actions,
                memories_by_id,
                _archive_fn,
                project_by_index_id,
            )

        succeeded = sum(1 for r in results if r.success)
        failed = len(results) - succeeded
        summary = f"Materialized {succeeded}/{len(results)} actions" + (
            f" ({failed} failed)" if failed else ""
        )
        return MaterializeResult(processed=len(results), results=results, summary=summary)

    async def compact_memories(self, params: CompactParams) -> CompactResult:
        """Cluster and hard-delete near-duplicate memories using pgvector cosine similarity."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Step 1: Fetch memories for scope
            scope = params.scope
            protected_canonical_entities = 0
            if scope is None:
                protected_canonical_entities = _coerce_count(
                    await conn.fetchval(
                        f"""SELECT COUNT(*) FROM memories
                            WHERE 1=1 {_active_lifecycle_filter}
                              AND {canonical_entity_select_predicate()}"""
                    )
                )
                mem_rows = await conn.fetch(
                    f"SELECT * FROM memories WHERE 1=1 {_compact_lifecycle_filter}",
                )
            elif scope.startswith("project:"):
                project = scope[8:]
                # Read-only lookup — do NOT auto-create missing indexes
                # (would be a side effect of dry_run=True, and `index_id or 1`
                # fallback would silently target a different project)
                index_row = await conn.fetchrow(
                    "SELECT id FROM memory_indexes WHERE name = $1", project
                )
                if index_row is None:
                    # Project doesn't exist → no memories to compact
                    return CompactResult(
                        clusters_found=0,
                        memories_deleted=0,
                        memories_kept=[],
                        deleted_ids=[],
                        strategy_used=params.strategy,
                        plan=[],
                        protected_canonical_entities=0,
                    )
                index_id = index_row["id"]
                protected_canonical_entities = _coerce_count(
                    await conn.fetchval(
                        f"""SELECT COUNT(*) FROM memories
                            WHERE index_id = $1 {_active_lifecycle_filter}
                              AND {canonical_entity_select_predicate()}""",
                        index_id,
                    )
                )
                mem_rows = await conn.fetch(
                    f"SELECT * FROM memories WHERE index_id = $1 {_compact_lifecycle_filter}",
                    index_id,
                )
            elif scope.startswith("type:"):
                mem_type = scope[5:]
                protected_canonical_entities = _coerce_count(
                    await conn.fetchval(
                        f"""SELECT COUNT(*) FROM memories
                            WHERE type = $1 {_active_lifecycle_filter}
                              AND {canonical_entity_select_predicate()}""",
                        mem_type,
                    )
                )
                mem_rows = await conn.fetch(
                    f"SELECT * FROM memories WHERE type = $1 {_compact_lifecycle_filter}",
                    mem_type,
                )
            else:
                raise ValueError(
                    f"Unknown scope format: {scope!r}. "
                    "Expected None, 'project:<name>', or 'type:<name>'"
                )

            protected_rows = [row for row in mem_rows if _row_is_protected_canonical_entity(row)]
            if protected_rows:
                protected_canonical_entities = max(
                    protected_canonical_entities,
                    len(protected_rows),
                )
                mem_rows = [
                    row for row in mem_rows if not _row_is_protected_canonical_entity(row)
                ]

            # Step 2: If fewer than 2 memories → return early
            # (A single existing memory is still retained — report it in memories_kept.)
            if len(mem_rows) < 2:
                existing_ids = [row["id"] for row in mem_rows]
                return CompactResult(
                    clusters_found=0,
                    memories_deleted=0,
                    memories_kept=existing_ids,
                    deleted_ids=[],
                    strategy_used=params.strategy,
                    plan=[],
                    protected_canonical_entities=protected_canonical_entities,
                )

            # Build rows dict for strategy selection
            rows_by_id: dict[int, Any] = {row["id"]: row for row in mem_rows}
            all_ids = list(rows_by_id.keys())

            if scope is None and len(all_ids) > 500:
                logger.warning(
                    "compact_memories: scope=None fetched %d memories — "
                    "pairwise similarity query may be slow",
                    len(all_ids),
                )

            # Step 3: Fetch all-pairs cosine similarity via pgvector
            sim_rows = await conn.fetch(
                """SELECT m1.id AS id1, m2.id AS id2,
                          1 - (m1.embedding <=> m2.embedding) AS similarity
                   FROM memories m1 JOIN memories m2 ON m1.id < m2.id
                   WHERE m1.id = ANY($1::int[]) AND m2.id = ANY($1::int[])
                     AND m1.embedding IS NOT NULL AND m2.embedding IS NOT NULL
                     AND 1 - (m1.embedding <=> m2.embedding) >= $2""",
                all_ids,
                params.threshold,
            )

            # Step 4: Build edge list and run pure-Python union-find
            edges = [(row["id1"], row["id2"]) for row in sim_rows]
            clusters = _build_clusters(all_ids, edges)

            # Step 5: Per cluster >= 2 members, choose Canonical via strategy
            plan: list[ClusterPlan] = []
            all_to_delete: list[int] = []
            for cluster_id, members in enumerate(clusters):
                canonical = _select_canonical(members, rows_by_id, params.strategy)
                to_delete = [m for m in members if m != canonical]
                plan.append(ClusterPlan(
                    cluster_id=cluster_id,
                    members=members,
                    canonical_id=canonical,
                    to_delete=to_delete,
                ))
                all_to_delete.extend(to_delete)

            # Memories kept = all_ids minus to_delete
            deleted_set = set(all_to_delete)
            memories_kept = [i for i in all_ids if i not in deleted_set]

            # Step 6: dry_run=True → return plan without deleting
            if params.dry_run or not all_to_delete:
                return CompactResult(
                    clusters_found=len(clusters),
                    memories_deleted=0,
                    memories_kept=memories_kept,
                    deleted_ids=all_to_delete,
                    strategy_used=params.strategy,
                    plan=plan,
                    protected_canonical_entities=protected_canonical_entities,
                )

            # Step 7: dry_run=False → repoint relationships, then DELETE non-survivor IDs.
            # Schema does NOT use ON DELETE CASCADE, so we must delete
            # dependent usage rows first to avoid dangling references.
            survivor_by_loser = {
                loser: planned.canonical_id
                for planned in plan
                for loser in planned.to_delete
            }

            # Re-check canonical protection immediately before the destructive
            # repoint / usage-log / delete steps. A memory promoted to a
            # protected canonical entity between planning (Step 5) and here must
            # not have its relationships repointed or usage logs deleted; the
            # final guarded DELETE is still the last line of defense.
            planned_delete = all_to_delete
            all_to_delete = await _filter_out_newly_canonical(conn, all_to_delete)
            newly_protected = len(planned_delete) - len(all_to_delete)
            if newly_protected:
                protected_canonical_entities += newly_protected
                delete_set = set(all_to_delete)
                survivor_by_loser = {
                    loser: survivor
                    for loser, survivor in survivor_by_loser.items()
                    if loser in delete_set
                }
                memories_kept = [i for i in all_ids if i not in delete_set]

            for loser_id in all_to_delete:
                await _repoint_relationships(conn, loser_id, survivor_by_loser[loser_id])

            await conn.execute(
                "DELETE FROM memory_usage_log WHERE memory_id = ANY($1::int[])",
                all_to_delete,
            )
            result = await conn.execute(
                f"DELETE FROM memories WHERE id = ANY($1::int[]) {_canonical_entity_protection_filter()}",
                all_to_delete,
            )
            count = int(result.split()[-1])

            return CompactResult(
                clusters_found=len(clusters),
                memories_deleted=count,
                memories_kept=memories_kept,
                deleted_ids=all_to_delete,
                strategy_used=params.strategy,
                plan=plan,
                protected_canonical_entities=protected_canonical_entities,
            )

    async def get_wake_up_memories(self, limit: int = 500, project: str | None = None) -> list[Memory]:
        """Fetch memories with project_name for wake-up pack construction.

        Returns memories ordered by updated_at DESC, optionally filtered to a specific project.
        Each Memory's project_name field is populated from memory_indexes.name.

        Args:
            limit: Maximum number of memories to return (applied after project filter).
            project: Optional project name to filter by. When provided, only memories
                belonging to this project are returned.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT m.*, mi.name AS project_name
                   FROM memories m
                   LEFT JOIN memory_indexes mi ON mi.id = m.index_id
                   WHERE ($2::text IS NULL OR mi.name = $2)
                   ORDER BY m.updated_at DESC
                   LIMIT $1""",
                limit,
                project,
            )
            memories = []
            for row in rows:
                m = _row_to_memory(row)
                m.project_name = row.get("project_name")
                memories.append(m)
            return memories

    async def create_relationship(
        self,
        source_id: int,
        target_id: int,
        link_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Create a typed relationship between two memories.

        Args:
            source_id: ID of the source memory.
            target_id: ID of the target memory.
            link_type: Semantic relationship type. Must be in VALID_LINK_TYPES.
            metadata: Optional JSON metadata stored alongside the relationship.

        Returns:
            The ID of the created (or updated) relationship row.

        Raises:
            ValueError: If link_type is not in VALID_LINK_TYPES.
        """
        if link_type not in VALID_LINK_TYPES:
            raise ValueError(
                f"Invalid link_type: {link_type!r}. Must be one of: {sorted(VALID_LINK_TYPES)}"
            )
        pool = await get_pool()
        async with pool.acquire() as conn:
            rel_id: int = await conn.fetchval(
                # On conflict, confidence is set to 1.0 — typed relationships are
                # considered authoritative, overriding the auto-linked similarity score.
                """INSERT INTO memory_relationships (source_id, target_id, relation_type, link_type, confidence, metadata)
                   VALUES ($1, $2, $3, $3, 1.0, $4::jsonb)
                   ON CONFLICT (source_id, target_id, relation_type) DO UPDATE
                       SET link_type = EXCLUDED.link_type,
                           metadata = COALESCE(EXCLUDED.metadata, memory_relationships.metadata),
                           confidence = EXCLUDED.confidence
                   RETURNING id""",
                source_id,
                target_id,
                link_type,
                metadata,
            )
        logger.info(
            "Created relationship id=%d source=%d target=%d link_type=%s",
            rel_id, source_id, target_id, link_type,
        )
        return rel_id

    async def traverse(
        self,
        anchor_id: int,
        link_types: list[str],
        depth: int = 1,
        direction: Literal["outbound", "inbound", "both"] = "outbound",
    ) -> list[dict[str, Any]]:
        """Traverse the relationship graph using iterative BFS.

        Args:
            anchor_id: Starting memory ID.
            link_types: List of link_type values to follow.
            depth: Number of hops to traverse (1 = direct neighbors only).
                Must be between 1 and 10 inclusive.
            direction: 'outbound' (source→target), 'inbound' (target→source),
                       or 'both'.

        Returns:
            List of dicts with keys: id, link_type, depth, source_id, target_id.
            Each dict represents one edge found during the traversal.

        Raises:
            ValueError: If depth is not between 1 and 10, or direction is invalid.
        """
        if not (1 <= depth <= 10):
            raise ValueError(f"depth must be between 1 and 10, got {depth!r}")
        if direction not in ("outbound", "inbound", "both"):
            raise ValueError(
                f"direction must be 'outbound', 'inbound', or 'both', got {direction!r}"
            )
        if not link_types:
            return []

        pool = await get_pool()
        results: list[dict] = []
        visited: set[int] = {anchor_id}
        visited_edges: set[int] = set()
        current_frontier: list[int] = [anchor_id]

        placeholders_lt = ", ".join(f"${i + 2}" for i in range(len(link_types)))

        for current_depth in range(1, depth + 1):
            if not current_frontier:
                break

            # Build query dynamically based on direction
            frontier_param = "$1"
            lt_params = list(link_types)

            if direction == "outbound":
                where_clause = f"source_id = ANY({frontier_param}::int[]) AND link_type IN ({placeholders_lt})"
            elif direction == "inbound":
                where_clause = f"target_id = ANY({frontier_param}::int[]) AND link_type IN ({placeholders_lt})"
            else:  # both
                where_clause = (
                    f"(source_id = ANY({frontier_param}::int[]) OR target_id = ANY({frontier_param}::int[]))"
                    f" AND link_type IN ({placeholders_lt})"
                )

            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT id, source_id, target_id, link_type FROM memory_relationships WHERE {where_clause}",
                    current_frontier,
                    *lt_params,
                )

            next_frontier: list[int] = []
            for row in rows:
                src = row["source_id"]
                tgt = row["target_id"]
                # Determine the "neighbor" node (the one we're moving to)
                if direction == "inbound":
                    neighbor = src
                elif direction == "outbound":
                    neighbor = tgt
                else:
                    # For 'both': pick the node that is NOT in current_frontier
                    neighbor = tgt if src in current_frontier else src

                # Skip edges already reported at a previous depth — this
                # prevents direction="both" from re-reporting depth-1 edges
                # at depth-2 when the backward query returns already-traversed
                # edges connected to the current frontier.
                edge_id = row["id"]
                if edge_id in visited_edges:
                    continue
                visited_edges.add(edge_id)
                results.append({
                    "id": edge_id,
                    "link_type": row["link_type"],
                    "depth": current_depth,
                    "source_id": src,
                    "target_id": tgt,
                })
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                next_frontier.append(neighbor)

            current_frontier = next_frontier

        return results

    async def get_relationships(
        self,
        memory_id: int,
        link_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return all relationship edges where memory_id is source or target.

        Args:
            memory_id: The memory to look up edges for.
            link_types: Optional filter — only return edges with these link_type values.

        Returns:
            List of dicts with keys: id, source_id, target_id, link_type, relation_type, confidence.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            if link_types:
                placeholders = ", ".join(f"${i + 2}" for i in range(len(link_types)))
                rows = await conn.fetch(
                    f"""SELECT id, source_id, target_id, link_type, relation_type, confidence
                        FROM memory_relationships
                        WHERE (source_id = $1 OR target_id = $1)
                          AND link_type IN ({placeholders})""",
                    memory_id,
                    *link_types,
                )
            else:
                rows = await conn.fetch(
                    """SELECT id, source_id, target_id, link_type, relation_type, confidence
                       FROM memory_relationships
                       WHERE source_id = $1 OR target_id = $1""",
                    memory_id,
                )
        return [
            {
                "id": row["id"],
                "source_id": row["source_id"],
                "target_id": row["target_id"],
                "link_type": row["link_type"],
                "relation_type": row["relation_type"],
                "confidence": row["confidence"],
            }
            for row in rows
        ]

    async def people_discussed_with(
        self,
        person_id: int,
        since: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return meetings + mentions linking to person_id, sorted by date desc.

        Uses traverse() to collect the related (non-person) memory on each edge,
        then fetches those memories. Edge conventions (see VALID_LINK_TYPES):
          - attended_by: meeting -> person   (person is target; traverse inbound)
          - mentioned_in: person -> memory   (person is source; traverse outbound)

        Args:
            person_id: The person memory ID to query.
            since: Optional ISO date string (e.g. '2026-01-01'). Filters out
                memories created before this date.
            limit: Maximum number of results to return (default 20).

        Returns:
            List of dicts with keys: memory_id, title, date, link_type.
        """
        # attended_by: meeting -> person, so person is the target (inbound).
        inbound_edges = await self.traverse(
            anchor_id=person_id,
            link_types=["attended_by"],
            depth=1,
            direction="inbound",
        )
        # mentioned_in: person -> memory, so person is the source (outbound).
        outbound_edges = await self.traverse(
            anchor_id=person_id,
            link_types=["mentioned_in"],
            depth=1,
            direction="outbound",
        )

        # Map neighbor memory id -> link_type. For inbound edges the neighbor is
        # source_id (the meeting); for outbound edges the neighbor is target_id
        # (the memory where the person is mentioned).
        neighbor_link: dict[int, str] = {}
        for e in inbound_edges:
            neighbor_link[e["source_id"]] = e["link_type"]
        for e in outbound_edges:
            neighbor_link[e["target_id"]] = e["link_type"]

        if not neighbor_link:
            return []

        neighbor_ids = list(neighbor_link.keys())

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title, created_at FROM memories WHERE id = ANY($1::int[])",
                neighbor_ids,
            )

        results: list[dict[str, Any]] = []
        for row in rows:
            date_val = row["created_at"]
            date_str = date_val.isoformat() if hasattr(date_val, "isoformat") else str(date_val)

            # Filter by since if provided
            if since is not None:
                if date_str < since:
                    continue

            results.append({
                "memory_id": row["id"],
                "title": row["title"],
                "date": date_str,
                "link_type": neighbor_link.get(row["id"], "unknown"),
            })

        # Sort by date descending, apply limit
        results.sort(key=lambda r: r["date"], reverse=True)
        return results[:limit]

    async def people_stale_contacts(
        self,
        min_days: int = 90,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return person memories whose last_contact is older than min_days or null.

        Args:
            min_days: Minimum age in days to be considered stale (default 90).
            limit: Maximum number of results to return (default 50).

        Returns:
            List of dicts with keys: memory_id, title, last_contact, days_stale.
            last_contact and days_stale are None when last_contact is absent.
        """
        if min_days < 0:
            raise ValueError(f"min_days must be >= 0, got {min_days!r}")
        if limit <= 0:
            raise ValueError(f"limit must be > 0, got {limit!r}")

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, created_at, metadata
                FROM memories
                WHERE type = 'person'
                  AND (
                    metadata->>'last_contact' IS NULL
                    OR (metadata->>'last_contact')::timestamptz < NOW() - ($1 || ' days')::interval
                  )
                ORDER BY
                  CASE WHEN metadata->>'last_contact' IS NULL THEN '1970-01-01'::timestamptz
                       ELSE (metadata->>'last_contact')::timestamptz END ASC
                LIMIT $2
                """,
                str(min_days),
                limit,
            )

        now = datetime.now(UTC)
        results: list[dict[str, Any]] = []
        for row in rows:
            metadata = row["metadata"] or {}
            last_contact_raw = metadata.get("last_contact") if isinstance(metadata, dict) else None
            if last_contact_raw:
                try:
                    lc_dt = datetime.fromisoformat(last_contact_raw)
                    if lc_dt.tzinfo is None:
                        lc_dt = lc_dt.replace(tzinfo=UTC)
                    days_stale = (now - lc_dt).days
                    last_contact = last_contact_raw
                except ValueError:
                    last_contact = None
                    days_stale = None
            else:
                last_contact = None
                days_stale = None

            results.append({
                "memory_id": row["id"],
                "title": row["title"],
                "last_contact": last_contact,
                "days_stale": days_stale,
            })

        return results

    async def people_mentions_window(
        self,
        days: int = 30,
        min_count: int = 1,
    ) -> list[dict[str, Any]]:
        """Aggregate mention memories in the last N days, grouped by person_ref.

        Args:
            days: Look-back window in days (default 30).
            min_count: Minimum number of mentions to include (default 1).

        Returns:
            List of dicts with keys: person_id, mention_count, last_mentioned_at.
            Sorted by mention_count descending.
        """
        if days < 0:
            raise ValueError(f"days must be >= 0, got {days!r}")
        if min_count < 0:
            raise ValueError(f"min_count must be >= 0, got {min_count!r}")

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Edge direction conventions (see VALID_LINK_TYPES):
            #   - attended_by:  meeting -> person   (person=target_id, dated memory=source_id)
            #   - mentioned_in: person  -> memory   (person=source_id, dated memory=target_id)
            # We normalize each edge to (person_id, dated_memory_id) via UNION ALL,
            # then aggregate mention counts + last-seen timestamps per person.
            rows = await conn.fetch(
                """
                WITH edges AS (
                    SELECT target_id AS person_id, source_id AS dated_id
                    FROM memory_relationships
                    WHERE link_type = 'attended_by'
                    UNION ALL
                    SELECT source_id AS person_id, target_id AS dated_id
                    FROM memory_relationships
                    WHERE link_type = 'mentioned_in'
                )
                SELECT
                    e.person_id AS person_id,
                    COUNT(*) AS mention_count,
                    MAX(m.created_at) AS last_mentioned_at
                FROM edges e
                JOIN memories m ON m.id = e.dated_id
                WHERE m.created_at >= NOW() - ($1 || ' days')::interval
                GROUP BY e.person_id
                HAVING COUNT(*) >= $2
                ORDER BY mention_count DESC
                """,
                str(days),
                min_count,
            )

        return [
            {
                "person_id": row["person_id"],
                "mention_count": row["mention_count"],
                "last_mentioned_at": (
                    row["last_mentioned_at"].isoformat()
                    if hasattr(row["last_mentioned_at"], "isoformat")
                    else str(row["last_mentioned_at"])
                ),
            }
            for row in rows
        ]

    async def delete_memories(self, params: DeleteParams) -> DeleteResult:
        """Delete memories by IDs or by filter (project + type + before)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            if params.ids is not None:
                result = await conn.execute(
                    "DELETE FROM memories WHERE id = ANY($1::int[])",
                    params.ids,
                )
                count = int(result.split()[-1])
                return DeleteResult(deleted=count)

            # Filter-based delete: build WHERE clause dynamically
            conditions: list[str] = []
            values: list[Any] = []
            idx = 1

            if params.project:
                index_id = await self._resolve_index_id(conn, params.project)
                if index_id is None:
                    return DeleteResult(deleted=0)
                conditions.append(f"index_id = ${idx}")
                values.append(index_id)
                idx += 1

            if params.type:
                conditions.append(f"type = ${idx}")
                values.append(params.type)
                idx += 1

            if params.before:
                conditions.append(f"created_at < ${idx}::timestamptz")
                values.append(params.before)
                idx += 1

            if not conditions:
                raise ValueError("At least one filter (ids, project, type, before) is required")

            where = " AND ".join(conditions)
            result = await conn.execute(
                f"DELETE FROM memories WHERE {where}", *values
            )
            count = int(result.split()[-1])
            logger.info("Deleted %d memories (project=%s, type=%s, before=%s)",
                        count, params.project, params.type, params.before)
            return DeleteResult(deleted=count)

    async def delete_by_run_id(self, run_id: str) -> DeleteByRunIdResult:
        """Delete all memories and relationships created in the given ingest run.

        Deletion is atomic: relationships are removed first (no CASCADE FK), then memories.
        Returns counts of deleted rows.
        Returns DeleteByRunIdResult(memories=0, relationships=0) for a non-existent run_id
        (not an error).

        Args:
            run_id: The run_id string stored in metadata->>'run_id'.

        Returns:
            DeleteByRunIdResult with counts of deleted memories and relationships.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Collect memory IDs for this run_id first
            rows = await conn.fetch(
                "SELECT id FROM memories WHERE metadata->>'run_id' = $1",
                run_id,
            )
            memory_ids: list[int] = [r["id"] for r in rows]

            if not memory_ids:
                return DeleteByRunIdResult(memories=0, relationships=0)

            async with conn.transaction():
                # Delete relationships touching these memories (no CASCADE FK)
                rel_result = await conn.execute(
                    "DELETE FROM memory_relationships WHERE source_id = ANY($1::int[]) OR target_id = ANY($1::int[])",
                    memory_ids,
                )
                rel_count = int(rel_result.split()[-1]) if rel_result else 0

                # Delete usage log entries (FK constraint)
                await conn.execute(
                    "DELETE FROM memory_usage_log WHERE memory_id = ANY($1::int[])",
                    memory_ids,
                )

                # Delete the memories themselves
                mem_result = await conn.execute(
                    "DELETE FROM memories WHERE id = ANY($1::int[])",
                    memory_ids,
                )
                mem_count = int(mem_result.split()[-1]) if mem_result else 0

        logger.info(
            "delete_by_run_id: deleted %d memories, %d relationships for run_id=%s",
            mem_count, rel_count, run_id,
        )
        return DeleteByRunIdResult(memories=mem_count, relationships=rel_count)


_DESTRUCTIVE_REFINE_ACTIONS = {"merge", "demote", "delete"}


def _filter_refine_action_protected_ids(
    action: RefineAction,
    protected_ids: set[int],
) -> int:
    """Remove protected IDs from destructive refine actions."""
    if action.action not in _DESTRUCTIVE_REFINE_ACTIONS or not protected_ids:
        return 0

    original_ids = list(action.memory_ids)
    action.memory_ids = [mid for mid in action.memory_ids if mid not in protected_ids]
    if action.action == "merge" and len(action.memory_ids) < 2:
        action.memory_ids = []
    return sum(1 for mid in original_ids if mid in protected_ids)


def _filter_protected_refine_actions(
    actions: list[RefineAction],
    memories_by_id: dict[int, Memory],
) -> int:
    """Filter protected canonical entities from planned refine actions."""
    protected_ids = {
        memory_id
        for memory_id, memory in memories_by_id.items()
        if is_canonical_entity(memory)
    }
    return sum(
        _filter_refine_action_protected_ids(action, protected_ids)
        for action in actions
    )


async def _fetch_protected_canonical_ids(
    conn: asyncpg.Connection,
    memory_ids: list[int],
) -> set[int]:
    """Fetch protected canonical IDs from the database for mutation-site checks."""
    if not memory_ids:
        return set()
    rows = await conn.fetch(
        f"SELECT id FROM memories WHERE id = ANY($1::int[]) AND {canonical_entity_select_predicate()}",
        memory_ids,
    )
    return {int(row["id"]) for row in rows}


async def _filter_refine_action_at_mutation_site(
    conn: asyncpg.Connection,
    action: RefineAction,
) -> int:
    """Re-check and filter protected IDs immediately before a refine mutation."""
    if action.action not in _DESTRUCTIVE_REFINE_ACTIONS:
        return 0
    protected_ids = await _fetch_protected_canonical_ids(conn, action.memory_ids)
    skipped = _filter_refine_action_protected_ids(action, protected_ids)
    if skipped:
        logger.info(
            "Skipping %d protected canonical entities for refine action %s",
            skipped,
            action.action,
        )
    return skipped


async def _execute_refine_actions(actions: list[RefineAction]) -> int:
    """Execute refinement actions, parallelizing independent ones.

    Groups actions into waves: actions within a wave have no overlapping memory IDs
    and can run concurrently. Actions with ID conflicts run in subsequent waves.
    """
    pool = await get_pool()
    deleted_ids: set[int] = set()
    protected_skipped = 0

    # First pass: filter out actions with already-deleted IDs
    valid_actions: list[RefineAction] = []
    for action in actions:
        remaining = [mid for mid in action.memory_ids if mid not in deleted_ids]
        if not remaining or (action.action == "merge" and len(remaining) < 2):
            logger.info("Skipping %s on %s — IDs already deleted", action.action, action.memory_ids)
            action.memory_ids = []
            continue
        action.memory_ids = remaining
        valid_actions.append(action)
        # Pre-compute which IDs will be deleted to avoid conflicts
        if action.action == "merge":
            deleted_ids.update(action.memory_ids[1:])
        elif action.action == "delete":
            deleted_ids.update(action.memory_ids)

    # Build waves of non-overlapping actions for parallel execution
    waves: list[list[RefineAction]] = []

    for action in valid_actions:
        action_ids = set(action.memory_ids)
        # Try to fit into an existing wave
        placed = False
        for wave in waves:
            wave_ids = {mid for a in wave for mid in a.memory_ids}
            if not action_ids & wave_ids:
                wave.append(action)
                placed = True
                break
        if not placed:
            waves.append([action])

    # Execute waves: actions within a wave run in parallel
    for wave_idx, wave in enumerate(waves):
        logger.info("Executing wave %d/%d (%d actions)", wave_idx + 1, len(waves), len(wave))
        if len(wave) == 1:
            async with pool.acquire() as conn:
                protected_skipped += await _execute_refine_action(conn, wave[0])
        else:
            async def _run_action(act: RefineAction) -> int:
                async with pool.acquire() as conn:
                    return await _execute_refine_action(conn, act)

            protected_skipped += sum(await asyncio.gather(*[_run_action(a) for a in wave]))

    return protected_skipped


async def _execute_refine_action(conn: asyncpg.Connection, action: RefineAction) -> int:
    """Execute a single refinement action against the database."""
    protected_skipped = await _filter_refine_action_at_mutation_site(conn, action)
    if not action.memory_ids or (action.action == "merge" and len(action.memory_ids) < 2):
        return protected_skipped

    match action.action:
        case "merge":
            keep_id = action.memory_ids[0]
            ids_to_remove = action.memory_ids[1:]
            if not ids_to_remove:
                return protected_skipped

            merged: dict[str, str] = {}

            if not action.skip_llm_merge:
                from open_brain.data_layer.refine import merge_memories_with_llm

                # Fetch full memories for LLM merge
                all_ids = action.memory_ids
                placeholders = ", ".join(f"${i+1}" for i in range(len(all_ids)))
                rows = await conn.fetch(
                    f"SELECT * FROM memories WHERE id IN ({placeholders})", *all_ids
                )
                memories = [_row_to_memory(r) for r in rows]

                try:
                    merged = await merge_memories_with_llm(memories)
                except Exception as err:
                    logger.warning("LLM merge failed (%s), keeping original", err)
                    merged = {}
            else:
                logger.info(
                    "Skipping LLM merge for IDs %s (similarity=%.3f)",
                    action.memory_ids,
                    action.similarity or 0.0,
                )

            # Update the kept memory with merged content and re-embed
            if merged:
                await conn.execute(
                    """UPDATE memories SET
                        type = COALESCE($2, type),
                        title = COALESCE($3, title),
                        subtitle = COALESCE($4, subtitle),
                        narrative = COALESCE($5, narrative),
                        content = COALESCE($6, content),
                        updated_at = now()
                      WHERE id = $1""",
                    keep_id,
                    merged.get("type"),
                    merged.get("title"),
                    merged.get("subtitle"),
                    merged.get("narrative"),
                    merged.get("content"),
                )

                # Re-embed with updated content
                text_to_embed = ": ".join(
                    part for part in [
                        merged.get("title"),
                        merged.get("subtitle"),
                        merged.get("narrative"),
                        merged.get("content"),
                    ] if part
                )
                if text_to_embed:
                    try:
                        embedding = await embed(text_to_embed)
                        pg_vec = to_pg_vector(embedding)
                        await conn.execute(
                            "UPDATE memories SET embedding = $1 WHERE id = $2",
                            pg_vec, keep_id,
                        )
                    except Exception as err:
                        logger.warning("Re-embedding failed for %d: %s", keep_id, err)

            # Re-check canonical protection immediately before repoint/delete.
            # The mutation-site filter ran at function entry, but the LLM merge
            # and re-embed above open a window in which a loser could be promoted
            # to a protected canonical entity; drop any such id so its
            # relationships are not silently disconnected. The guarded DELETE
            # below remains the last line of defense.
            before_recheck = ids_to_remove
            ids_to_remove = await _filter_out_newly_canonical(conn, ids_to_remove)
            protected_skipped += len(before_recheck) - len(ids_to_remove)

            # Repoint relationships, then delete the duplicates.
            for remove_id in ids_to_remove:
                await _repoint_relationships(conn, remove_id, keep_id)
            await conn.execute(
                f"DELETE FROM memories WHERE id = ANY($1::int[]) {_canonical_entity_protection_filter()}",
                ids_to_remove,
            )
            logger.info(
                "Merged: updated %d%s, deleted %s",
                keep_id,
                " (LLM-combined)" if merged else " (kept original)",
                ", ".join(str(i) for i in ids_to_remove),
            )
        case "promote":
            for mid in action.memory_ids:
                await conn.execute(
                    """UPDATE memories SET
                        stability = CASE stability WHEN 'tentative' THEN 'stable' WHEN 'stable' THEN 'canonical' ELSE stability END,
                        updated_at = now()
                      WHERE id = $1""",
                    mid,
                )
        case "demote":
            await conn.execute(
                f"""UPDATE memories
                    SET priority = GREATEST(priority - 0.1, 0.05),
                        updated_at = now()
                    WHERE id = ANY($1::int[]) {_canonical_entity_protection_filter()}""",
                action.memory_ids,
            )
        case "delete":
            await conn.execute(
                f"DELETE FROM memories WHERE id = ANY($1::int[]) {_canonical_entity_protection_filter()}",
                action.memory_ids,
            )
    return protected_skipped
