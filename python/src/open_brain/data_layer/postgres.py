"""Postgres+pgvector DataLayer implementation using asyncpg."""

from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

import asyncpg

from open_brain.config import get_config
from open_brain.data_layer.embedding import (
    embed,
    embed_query,
    embed_with_usage,
    embed_query_with_usage,
    to_pg_vector,
)
from open_brain.data_layer.reranker import rerank
from open_brain.data_layer.interface import (
    IMPORTANCE_VALUES,
    VALID_LINK_TYPES,
    ClusterPlan,
    CompactParams,
    CompactResult,
    DecayParams,
    DecayResult,
    DeleteByRunIdResult,
    DeleteParams,
    DeleteResult,
    MaterializeParams,
    MaterializeResult,
    Memory,
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
    rank_importance,
)
from open_brain.data_layer.refine import analyze_with_llm

from open_brain.ingest.runs import get_current_run_id

logger = logging.getLogger(__name__)

DEDUP_WINDOW_DAYS = 30  # How far back the content-hash dedup check looks

# ─── compact_memories helpers ─────────────────────────────────────────────────

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
_compact_lifecycle_filter = (
    "AND (metadata->>'status' IS NULL "
    "OR metadata->>'status' NOT IN ('materialized', 'discarded', 'archived'))"
)

_pool: asyncpg.Pool | None = None


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


async def get_pool() -> asyncpg.Pool:
    """Return the shared asyncpg connection pool."""
    global _pool
    if _pool is None:
        config = get_config()
        _pool = await asyncpg.create_pool(
            config.DATABASE_URL, min_size=2, max_size=10, init=_init_conn
        )
        # Idempotent migrations
        async with _pool.acquire() as conn:
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
            await conn.execute("""
                CREATE OR REPLACE FUNCTION public.hybrid_search(
                    query_text text,
                    query_embedding vector,
                    match_limit integer DEFAULT 20,
                    rrf_k integer DEFAULT 60,
                    p_index_id integer DEFAULT NULL,
                    p_user_id text DEFAULT NULL,
                    p_metadata_filter jsonb DEFAULT NULL
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
                  SELECT m.id, m.title, m.subtitle, m.type, c.score, m.created_at
                  FROM combined c
                  JOIN memories m ON m.id = c.id
                  ORDER BY c.score DESC
                  LIMIT match_limit;
                $fn$;
            """)
            # Importance-aware decay function: skips critical (mult=0.0), applies importance
            # multipliers (high=0.5x, medium=1.0x, low=2.0x), includes 24h race guard via
            # last_decay_at so concurrent calls are safe without advisory locks.
            # Typed-relationship schema migration (idempotent)
            await _ensure_link_type_column(conn)
            await conn.execute("""
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
                        RETURNING m.id
                    )
                    SELECT COUNT(*) INTO v_updated FROM updated;
                    RETURN v_updated;
                END;
                $$;
            """)
    return _pool


async def close_pool() -> None:
    """Close the connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


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


class PostgresDataLayer:
    """DataLayer implementation backed by Postgres + pgvector."""

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
                    metadata_jsonb = (
                        _json.dumps(params.metadata_filter) if params.metadata_filter else None
                    )
                    post_conditions: list[str] = []
                    post_values: list[Any] = [
                        query, to_pg_vector(query_embedding), fetch_limit * 3, index_id, params.author,
                        metadata_jsonb,
                    ]
                    param_idx = 7  # after the 6 hybrid_search params ($1–$6)

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
                    # metadata_filter is now pre-constrained inside hybrid_search ($6), not a post-filter

                    post_where = f"AND {' AND '.join(post_conditions)}" if post_conditions else ""

                    rows = await conn.fetch(
                        f"""WITH scored AS (
                            SELECT id, score FROM hybrid_search($1, $2::vector, $3, 60, $4, $5, $6)
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
                        reranked_indices = await rerank(
                            query=query,
                            documents=documents,
                            model=config.RERANK_MODEL,
                            top_k=limit,
                        )
                        memories = [memories[i] for i in reranked_indices]
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
                conditions.append(f"m.metadata @> ${param_idx}::jsonb")
                values.append(_json.dumps(params.metadata_filter))
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
        an existing memory with the same session_ref is updated (content appended,
        title/subtitle/narrative replaced if provided) instead of inserting a new row.
        """
        # Validate importance BEFORE any DB access (V6)
        if params.importance not in IMPORTANCE_VALUES:
            raise ValueError(
                f"Invalid importance: {params.importance!r}. "
                f"Must be one of: {sorted(IMPORTANCE_VALUES)}"
            )

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
                if params.upsert_mode == "replace":
                    # Delete existing rows in FK-safe order, then insert fresh.
                    # Wrapped in a transaction for atomicity.
                    async with conn.transaction():
                        existing_ids_rows = await conn.fetch(
                            "SELECT id FROM memories WHERE session_ref = $1 AND type = 'session_summary'",
                            params.session_ref,
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
                                "DELETE FROM memories WHERE session_ref = $1 AND type = 'session_summary'",
                                params.session_ref,
                            )

                        # Content-hash dedup is skipped for replace mode (we're intentionally
                        # replacing content for this session_ref; duplicates across other
                        # sessions are irrelevant).
                        base_metadata = dict(params.metadata) if params.metadata else {}
                        content_hash_replace = hashlib.sha256(params.text.encode()).hexdigest()
                        base_metadata["content_hash"] = content_hash_replace
                        # Inject run_id if inside an ingest_run context
                        if run_id := get_current_run_id():
                            base_metadata["run_id"] = run_id
                        metadata_json_replace = _json.dumps(base_metadata)
                        row_replace = await conn.fetchrow(
                            """INSERT INTO memories (index_id, type, title, subtitle, narrative, content, session_ref, metadata, user_id, importance)
                               VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
                               RETURNING id""",
                            index_id or 1,
                            params.type or "observation",
                            params.title,
                            params.subtitle,
                            params.narrative,
                            params.text,
                            params.session_ref,
                            metadata_json_replace,
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
                        "SELECT id, content FROM memories WHERE session_ref = $1 LIMIT 1",
                        params.session_ref,
                    )
                    if existing:
                        existing_id: int = existing["id"]
                        merged_content = existing["content"] + "\n\n---\n\n" + params.text
                        updates: dict[str, Any] = {"content": merged_content}
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
                # Normalize index_id for dedup: inserts use `index_id or 1`; match the same scope
                search_index_id = index_id if index_id is not None else 1
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
            # Normalize index_id for dedup: inserts use `index_id or 1`; match the same scope
            content_hash_index_id = index_id if index_id is not None else 1
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
            base_metadata = dict(params.metadata) if params.metadata else {}
            base_metadata["content_hash"] = content_hash
            # Inject run_id if inside an ingest_run context
            if run_id := get_current_run_id():
                base_metadata["run_id"] = run_id
            metadata_json = _json.dumps(base_metadata)
            row = await conn.fetchrow(
                """INSERT INTO memories (index_id, type, title, subtitle, narrative, content, session_ref, metadata, user_id, importance)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
                   RETURNING id""",
                index_id or 1,
                params.type or "observation",
                params.title,
                params.subtitle,
                params.narrative,
                params.text,
                params.session_ref,
                metadata_json,
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
                updates["index_id"] = index_id or 1

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
                values.append(_json.dumps(params.metadata))
                param_idx += 1
            set_parts.append(f"updated_at = NOW()")

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
                    """SELECT COUNT(*) FROM memories
                       WHERE (last_accessed_at IS NULL OR last_accessed_at < NOW() - ($1 || ' days')::interval)
                         AND created_at < NOW() - ($1 || ' days')::interval
                         AND importance != 'critical'
                         AND (last_decay_at IS NULL OR last_decay_at < NOW() - interval '24 hours')""",
                    str(params.stale_days),
                )
                boosted = await conn.fetchval(
                    """SELECT COUNT(*) FROM memories
                       WHERE access_count >= $1""",
                    params.boost_threshold,
                )
                recent_memories = await conn.fetchval(
                    """SELECT COUNT(*) FROM memories
                       WHERE created_at >= NOW() - ($1 || ' days')::interval""",
                    str(params.boost_days),
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
                               updated_at = NOW()
                           WHERE access_count >= $2
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

        decayed = int(decayed or 0)
        boosted = int(boosted or 0)
        recent_memories = int(recent_memories or 0)
        summary = (
            f"Decay run complete: {decayed} memories decayed, "
            f"{boosted} memories boosted, {recent_memories} recent memories (< {params.boost_days} days)."
            + (" (dry_run)" if params.dry_run else "")
        )
        return DecayResult(decayed=decayed, boosted=boosted, recent_memories=recent_memories, summary=summary)

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
            """UPDATE memories
               SET priority = GREATEST(0.0, priority - priority * $1),
                   last_decay_at = NOW()
               WHERE id = $2
                 AND importance != 'critical'
                 AND (last_decay_at IS NULL OR last_decay_at < NOW() - interval '24 hours')""",
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
                reranked_indices = await rerank(
                    query=query,
                    documents=documents,
                    model=config.RERANK_MODEL,
                    top_k=max_results,
                )
                memories = [memories[i] for i in reranked_indices]
            else:
                memories = memories[:max_results]

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
                    index_id or 1,
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
            await _execute_refine_actions(actions)

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
                f"{' (dry run)' if params.dry_run else ''}"
            ),
        )

    async def triage_memories(self, params: TriageParams) -> TriageResult:
        """Classify memories into lifecycle actions using LLM triage."""
        from open_brain.data_layer.triage import triage_with_llm

        pool = await get_pool()
        limit = params.limit or 50
        scope = params.scope or "recent"

        # Exclude memories already processed (materialized/discarded)
        _lifecycle_filter = (
            "AND (metadata->>'status' IS NULL "
            "OR metadata->>'status' NOT IN ('materialized', 'discarded'))"
        )

        async with pool.acquire() as conn:
            if scope.startswith("project:"):
                project = scope[8:]
                index_id = await self._resolve_index_id(conn, project)
                rows = await conn.fetch(
                    f"SELECT * FROM memories WHERE index_id = $1 {_lifecycle_filter} ORDER BY created_at DESC LIMIT $2",
                    index_id or 1,
                    limit,
                )
            elif scope.startswith("type:"):
                mem_type = scope[5:]
                rows = await conn.fetch(
                    f"SELECT * FROM memories WHERE type = $1 {_lifecycle_filter} ORDER BY created_at DESC LIMIT $2",
                    mem_type,
                    limit,
                )
            elif scope == "low-priority":
                rows = await conn.fetch(
                    f"SELECT * FROM memories WHERE priority < 0.2 AND importance NOT IN ('critical', 'high') {_lifecycle_filter} ORDER BY priority ASC LIMIT $1",
                    limit,
                )
            elif scope.startswith("session_ref:"):
                prefix = scope[len("session_ref:"):]
                rows = await conn.fetch(
                    f"SELECT * FROM memories WHERE session_ref LIKE $1 {_lifecycle_filter} ORDER BY created_at DESC LIMIT $2",
                    prefix + "%",
                    limit,
                )
            else:
                # "recent" — last N memories
                rows = await conn.fetch(
                    f"SELECT * FROM memories WHERE 1=1 {_lifecycle_filter} ORDER BY created_at DESC LIMIT $1", limit
                )
            candidates = [_row_to_memory(r) for r in rows]

        if not candidates:
            return TriageResult(analyzed=0, actions=[], summary="No candidates found")

        actions = await triage_with_llm(candidates)

        if not params.dry_run:
            for action in actions:
                action.executed = True

        action_counts: dict[str, int] = {}
        for a in actions:
            action_counts[a.action] = action_counts.get(a.action, 0) + 1

        summary_parts = [f"{count} {act}" for act, count in sorted(action_counts.items())]
        summary = (
            f"Triaged {len(candidates)} memories: {', '.join(summary_parts)}"
            f"{' (dry run)' if params.dry_run else ''}"
        )

        return TriageResult(analyzed=len(candidates), actions=actions, summary=summary)

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
            if scope is None:
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
                    )
                index_id = index_row["id"]
                mem_rows = await conn.fetch(
                    f"SELECT * FROM memories WHERE index_id = $1 {_compact_lifecycle_filter}",
                    index_id,
                )
            elif scope.startswith("type:"):
                mem_type = scope[5:]
                mem_rows = await conn.fetch(
                    f"SELECT * FROM memories WHERE type = $1 {_compact_lifecycle_filter}",
                    mem_type,
                )
            else:
                raise ValueError(
                    f"Unknown scope format: {scope!r}. "
                    "Expected None, 'project:<name>', or 'type:<name>'"
                )

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
                )

            # Step 7: dry_run=False → DELETE non-canonical IDs.
            # Schema does NOT use ON DELETE CASCADE, so we must delete
            # dependent rows first to avoid dangling references.
            await conn.execute(
                "DELETE FROM memory_usage_log WHERE memory_id = ANY($1::int[])",
                all_to_delete,
            )
            await conn.execute(
                "DELETE FROM memory_relationships "
                "WHERE source_id = ANY($1::int[]) OR target_id = ANY($1::int[])",
                all_to_delete,
            )
            result = await conn.execute(
                "DELETE FROM memories WHERE id = ANY($1::int[])",
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
                _json.dumps(metadata) if metadata is not None else None,
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


async def _execute_refine_actions(actions: list[RefineAction]) -> None:
    """Execute refinement actions, parallelizing independent ones.

    Groups actions into waves: actions within a wave have no overlapping memory IDs
    and can run concurrently. Actions with ID conflicts run in subsequent waves.
    """
    pool = await get_pool()
    deleted_ids: set[int] = set()

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
    assigned: set[int] = set()

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
                await _execute_refine_action(conn, wave[0])
        else:
            async def _run_action(act: RefineAction) -> None:
                async with pool.acquire() as conn:
                    await _execute_refine_action(conn, act)

            await asyncio.gather(*[_run_action(a) for a in wave])


async def _execute_refine_action(conn: asyncpg.Connection, action: RefineAction) -> None:
    """Execute a single refinement action against the database."""
    match action.action:
        case "merge":
            keep_id = action.memory_ids[0]
            ids_to_remove = action.memory_ids[1:]
            if not ids_to_remove:
                return

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

            # Delete the duplicates
            del_placeholders = ", ".join(f"${i+1}" for i in range(len(ids_to_remove)))
            await conn.execute(
                f"DELETE FROM memories WHERE id IN ({del_placeholders})",
                *ids_to_remove,
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
            placeholders = ", ".join(f"${i+1}" for i in range(len(action.memory_ids)))
            await conn.execute(
                f"UPDATE memories SET priority = GREATEST(priority - 0.1, 0.05), updated_at = now() WHERE id IN ({placeholders})",
                *action.memory_ids,
            )
        case "delete":
            placeholders = ", ".join(f"${i+1}" for i in range(len(action.memory_ids)))
            await conn.execute(
                f"DELETE FROM memories WHERE id IN ({placeholders})",
                *action.memory_ids,
            )
