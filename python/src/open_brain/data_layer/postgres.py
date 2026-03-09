"""Postgres+pgvector DataLayer implementation using asyncpg."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import asyncpg

from open_brain.config import get_config
from open_brain.data_layer.embedding import embed, embed_query, to_pg_vector
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


async def get_pool() -> asyncpg.Pool:
    """Return the shared asyncpg connection pool."""
    global _pool
    if _pool is None:
        config = get_config()
        _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=2, max_size=10)
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
        metadata=dict(row["metadata"]) if row.get("metadata") else {},
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
        """Hybrid search: vector + FTS via RRF, with optional filter conditions."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            index_id = await self._resolve_index_id(conn, params.project)

            conditions: list[str] = []
            values: list[Any] = []
            param_idx = 1

            if index_id is not None:
                conditions.append(f"m.index_id = ${param_idx}")
                values.append(index_id)
                param_idx += 1

            type_value = params.type or params.obs_type
            if type_value:
                conditions.append(f"m.type = ${param_idx}")
                values.append(type_value)
                param_idx += 1

            if params.date_start:
                conditions.append(f"m.created_at >= ${param_idx}")
                values.append(params.date_start)
                param_idx += 1

            if params.date_end:
                conditions.append(f"m.created_at <= ${param_idx}")
                values.append(params.date_end)
                param_idx += 1

            if params.file_path:
                conditions.append(f"m.metadata->>'filePath' = ${param_idx}")
                values.append(params.file_path)
                param_idx += 1

            # Hybrid search if query provided
            if params.query:
                try:
                    query_embedding = await embed_query(params.query)
                    limit = params.limit or 20
                    rows = await conn.fetch(
                        f"SELECT * FROM hybrid_search(${param_idx}, ${param_idx+1}::vector, ${param_idx+2}, 60, ${param_idx+3})",
                        *values,
                        params.query,
                        to_pg_vector(query_embedding),
                        limit,
                        index_id,
                    )
                    memories = [_row_to_memory(r) for r in rows]
                    hit_ids = [m.id for m in memories]
                    asyncio.create_task(
                        self._log_usage_background(hit_ids, "search_hit")
                    )
                    return SearchResult(results=memories, total=len(memories))
                except Exception:
                    # Fallback to FTS if embedding fails
                    conditions.append(
                        f"m.search_vector @@ websearch_to_tsquery('english', ${param_idx})"
                    )
                    values.append(params.query)
                    param_idx += 1

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            limit = params.limit or 50
            offset = params.offset or 0
            order_by = "m.created_at ASC" if params.order_by == "oldest" else "m.created_at DESC"

            rows = await conn.fetch(
                f"""SELECT m.id, m.index_id, m.session_id, m.type, m.title, m.subtitle, m.narrative, m.content,
                        m.metadata, m.priority, m.stability, m.access_count, m.last_accessed_at, m.created_at, m.updated_at
                 FROM memories m {where}
                 ORDER BY {order_by}
                 LIMIT ${param_idx} OFFSET ${param_idx+1}""",
                *values,
                limit,
                offset,
            )

            count_row = await conn.fetchrow(
                f"SELECT COUNT(*)::int AS total FROM memories m {where}",
                *values,
            )

            memories = [_row_to_memory(r) for r in rows]
            hit_ids = [m.id for m in memories]
            asyncio.create_task(self._log_usage_background(hit_ids, "search_hit"))

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
        """Get anchor-based context window around a memory."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            anchor_id = params.anchor
            index_id = await self._resolve_index_id(conn, params.project)

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

            index_filter = "AND m.index_id = $3" if index_id is not None else ""
            base_values: list[Any] = [anchor_row["created_at"], depth_before + depth_after + 1]
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
        """Insert a new memory and trigger async embedding."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            index_id = await self._resolve_index_id(conn, params.project)

            row = await conn.fetchrow(
                """INSERT INTO memories (index_id, type, title, subtitle, narrative, content)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   RETURNING id""",
                index_id or 1,
                params.type or "observation",
                params.title,
                params.subtitle,
                params.narrative,
                params.text,
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
        pool = await get_pool()
        async with pool.acquire() as conn:
            index_id = await self._resolve_index_id(conn, project)
            query_embedding = await embed_query(query)
            max_results = limit or 10

            conditions = ["m.embedding IS NOT NULL"]
            values: list[Any] = [to_pg_vector(query_embedding)]
            param_idx = 2

            if index_id is not None:
                conditions.append(f"m.index_id = ${param_idx}")
                values.append(index_id)
                param_idx += 1

            values.append(max_results)

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
            return {"results": [_row_to_memory(r) for r in rows]}

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
        """Get database aggregate statistics."""
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

        size_bytes = int(db_size_row["size"])
        return {
            "memories": memories_row["count"],
            "sessions": sessions_row["count"],
            "relationships": relationships_row["count"],
            "db_size_bytes": size_bytes,
            "db_size_mb": round(size_bytes / 1024 / 1024, 2),
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
                         m1.id, m1.index_id, m1.session_id, m1.type, m1.title, m1.content,
                         m1.metadata, m1.priority, m1.stability, m1.created_at, m1.updated_at,
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

        if not params.dry_run:
            pool = await get_pool()
            async with pool.acquire() as conn:
                for action in actions:
                    await _execute_refine_action(conn, action)

        executed_actions = [
            RefineAction(
                action=a.action,
                memory_ids=a.memory_ids,
                reason=a.reason,
                executed=not params.dry_run,
            )
            for a in actions
        ]

        return RefineResult(
            analyzed=len(candidates),
            actions=executed_actions,
            summary=(
                f"Analyzed {len(candidates)} memories, suggested {len(actions)} actions"
                f"{' (dry run)' if params.dry_run else ''}"
            ),
        )


async def _execute_refine_action(conn: asyncpg.Connection, action: RefineAction) -> None:
    """Execute a single refinement action against the database."""
    match action.action:
        case "merge":
            ids_to_remove = action.memory_ids[1:]
            if ids_to_remove:
                placeholders = ", ".join(f"${i+1}" for i in range(len(ids_to_remove)))
                await conn.execute(
                    f"DELETE FROM memories WHERE id IN ({placeholders})",
                    *ids_to_remove,
                )
                logger.info(
                    "Merged: kept %d, deleted %s",
                    action.memory_ids[0],
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
                f"UPDATE memories SET priority = GREATEST(priority - 0.1, 0.01), updated_at = now() WHERE id IN ({placeholders})",
                *action.memory_ids,
            )
        case "delete":
            placeholders = ", ".join(f"${i+1}" for i in range(len(action.memory_ids)))
            await conn.execute(
                f"DELETE FROM memories WHERE id IN ({placeholders})",
                *action.memory_ids,
            )
