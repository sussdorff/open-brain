"""Postgres+pgvector DataLayer implementation using asyncpg."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import asyncpg

from open_brain.config import get_config
from open_brain.data_layer.embedding import embed, embed_query, to_pg_vector
from open_brain.data_layer.reranker import rerank
from open_brain.data_layer.interface import (
    Memory,
    RefineAction,
    RefineParams,
    RefineResult,
    SaveMemoryParams,
    SaveMemoryResult,
    SearchParams,
    UpdateMemoryParams,
    SearchResult,
    TimelineParams,
    TimelineResult,
)
from open_brain.data_layer.refine import analyze_with_llm

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


def _parse_date(value: str | None) -> datetime | None:
    """Parse ISO date string to datetime. Accepts 'YYYY-MM-DD' or full ISO datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def get_pool() -> asyncpg.Pool:
    """Return the shared asyncpg connection pool."""
    global _pool
    if _pool is None:
        config = get_config()
        _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=2, max_size=10)
        # Ensure session_ref column exists (idempotent migration)
        async with _pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE memories ADD COLUMN IF NOT EXISTS session_ref TEXT;"
            )
    return _pool


async def close_pool() -> None:
    """Close the connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _row_to_memory(row: asyncpg.Record) -> Memory:
    """Convert an asyncpg Record to a Memory dataclass."""
    return Memory(
        id=row["id"],
        index_id=row["index_id"],
        session_id=row.get("session_id"),
        type=row["type"],
        title=row.get("title"),
        subtitle=row.get("subtitle"),
        narrative=row.get("narrative"),
        content=row["content"],
        metadata=row["metadata"] if isinstance(row.get("metadata"), dict) else {},
        priority=float(row["priority"]),
        stability=row["stability"],
        access_count=row["access_count"],
        last_accessed_at=str(row["last_accessed_at"]) if row.get("last_accessed_at") else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
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
                    query_embedding = await embed_query(query)

                    # Fetch 3x candidates for reranking (capped at 100)
                    fetch_limit = min(limit * 3, 100)

                    # Use hybrid_search for scoring, join memories for full rows + filters
                    post_conditions: list[str] = []
                    post_values: list[Any] = [
                        query, to_pg_vector(query_embedding), fetch_limit * 3, index_id,
                    ]
                    param_idx = 5  # after the 4 hybrid_search params

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

                    post_where = f"AND {' AND '.join(post_conditions)}" if post_conditions else ""

                    rows = await conn.fetch(
                        f"""WITH scored AS (
                            SELECT id, score FROM hybrid_search($1, $2::vector, $3, 60, $4)
                        )
                        SELECT m.id, m.index_id, m.session_id, m.type, m.title, m.subtitle,
                               m.narrative, m.content, m.metadata, m.priority, m.stability,
                               m.access_count, m.last_accessed_at, m.created_at, m.updated_at
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
                        m.metadata, m.priority, m.stability, m.access_count, m.last_accessed_at, m.created_at, m.updated_at
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
            return SearchResult(results=memories, total=count_row["total"])

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
                    f"""SELECT m.id, m.index_id, m.session_id, m.type, m.title, m.subtitle, m.narrative, m.content,
                            m.metadata, m.priority, m.stability, m.access_count, m.last_accessed_at, m.created_at, m.updated_at
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
                f"""(SELECT m.id, m.index_id, m.session_id, m.type, m.title, m.subtitle, m.narrative, m.content,
                         m.metadata, m.priority, m.stability, m.access_count, m.last_accessed_at, m.created_at, m.updated_at
                  FROM memories m WHERE m.created_at <= $1 {index_filter}
                  ORDER BY m.created_at DESC LIMIT {depth_before + 1})
                 UNION ALL
                 (SELECT m.id, m.index_id, m.session_id, m.type, m.title, m.subtitle, m.narrative, m.content,
                         m.metadata, m.priority, m.stability, m.access_count, m.last_accessed_at, m.created_at, m.updated_at
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
        pool = await get_pool()
        async with pool.acquire() as conn:
            index_id = await self._resolve_index_id(conn, params.project)

            # ── Upsert path for session_summary ──
            if params.type == "session_summary" and params.session_ref:
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

            # ── Normal insert path ──
            row = await conn.fetchrow(
                """INSERT INTO memories (index_id, type, title, subtitle, narrative, content, session_ref)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)
                   RETURNING id""",
                index_id or 1,
                params.type or "observation",
                params.title,
                params.subtitle,
                params.narrative,
                params.text,
                params.session_ref,
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

            if not updates:
                return SaveMemoryResult(id=params.id, message="No fields to update")

            # Build parameterized query
            set_parts = []
            values: list[Any] = []
            param_idx = 1
            for col, val in updates.items():
                set_parts.append(f"{col} = ${param_idx}")
                values.append(val)
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

    async def _embed_and_link(self, memory_id: int, text: str) -> None:
        """Background task: embed a memory and auto-link similar ones."""
        try:
            embedding = await embed(text)
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
            query_embedding = await embed_query(query)
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
                        m.metadata, m.priority, m.stability, m.access_count, m.last_accessed_at, m.created_at, m.updated_at,
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
        """Get database aggregate statistics including type taxonomy."""
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

        size_bytes = int(db_size_row["size"])
        return {
            "memories": memories_row["count"],
            "sessions": sessions_row["count"],
            "relationships": relationships_row["count"],
            "db_size_bytes": size_bytes,
            "db_size_mb": round(size_bytes / 1024 / 1024, 2),
            "types": {row["type"]: row["count"] for row in type_rows},
        }

    async def refine_memories(self, params: RefineParams) -> RefineResult:
        """LLM-powered memory consolidation."""
        pool = await get_pool()
        limit = params.limit or 50
        scope = params.scope or "recent"

        # Track similarity scores for duplicates scope
        similarity_map: dict[tuple[int, int], float] = {}

        async with pool.acquire() as conn:
            if scope == "duplicates":
                rows = await conn.fetch(
                    """SELECT DISTINCT ON (m1.id)
                         m1.id, m1.index_id, m1.session_id, m1.type, m1.title, m1.subtitle, m1.narrative, m1.content,
                         m1.metadata, m1.priority, m1.stability, m1.access_count, m1.last_accessed_at, m1.created_at, m1.updated_at,
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
                # Build similarity map for merge decisions
                for r in rows:
                    pair = (min(r["id"], r["similar_id"]), max(r["id"], r["similar_id"]))
                    similarity_map[pair] = float(r["similarity"])
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
                    "SELECT * FROM memories WHERE priority < 0.2 ORDER BY priority ASC LIMIT $1",
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
        for action in actions:
            if action.action != "merge":
                continue
            if scope == "low-priority":
                action.skip_llm_merge = True
            elif scope == "duplicates":
                # Check similarity for any pair in this merge group
                ids = action.memory_ids
                max_sim = max(
                    (similarity_map.get((min(a, b), max(a, b)), 0.0)
                     for a in ids for b in ids if a != b),
                    default=0.0,
                )
                action.similarity = max_sim
                if max_sim >= 0.92:
                    action.skip_llm_merge = True

        if not params.dry_run:
            await _execute_refine_actions(actions)

        executed_actions = [
            RefineAction(
                action=a.action,
                memory_ids=a.memory_ids,
                reason=a.reason,
                executed=not params.dry_run and bool(a.memory_ids),
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
