import { pool } from "../db/pool.js";
import { embed, embedQuery, toPgVector } from "./embedding.js";
import type {
  DataLayer,
  SearchParams,
  TimelineParams,
  SaveMemoryParams,
  Memory,
  RefineParams,
  RefineResult,
} from "./index.js";
import { analyzeWithLlm, executeRefineAction } from "./refine.js";

async function resolveIndexId(project?: string): Promise<number | null> {
  if (!project) return null;
  const { rows } = await pool.query(
    "SELECT id FROM memory_indexes WHERE name = $1",
    [project]
  );
  if (rows.length > 0) return rows[0].id;
  // Create new index for this project
  const result = await pool.query(
    "INSERT INTO memory_indexes (name) VALUES ($1) RETURNING id",
    [project]
  );
  return result.rows[0].id;
}

async function logUsage(
  memoryIds: number[],
  eventType: "search_hit" | "retrieved" | "cited",
  sessionContext?: string
) {
  if (memoryIds.length === 0) return;
  const values = memoryIds
    .map((_, i) => `($${i * 3 + 1}, $${i * 3 + 2}, $${i * 3 + 3})`)
    .join(", ");
  const params = memoryIds.flatMap((id) => [
    id,
    eventType,
    sessionContext || null,
  ]);
  await pool.query(
    `INSERT INTO memory_usage_log (memory_id, event_type, session_context) VALUES ${values}`,
    params
  );

  // Update priority: boost by 0.02 per access, cap at 1.0
  const placeholders = memoryIds.map((_, i) => `$${i + 1}`).join(", ");
  await pool.query(
    `UPDATE memories SET priority = LEAST(priority + 0.02, 1.0), updated_at = now() WHERE id IN (${placeholders})`,
    memoryIds
  );
}

export function createPostgresDataLayer(): DataLayer {
  return {
    async search(params: SearchParams) {
      const conditions: string[] = [];
      const values: unknown[] = [];
      let paramIdx = 1;

      const indexId = await resolveIndexId(params.project);
      if (indexId) {
        conditions.push(`m.index_id = $${paramIdx++}`);
        values.push(indexId);
      }
      if (params.type || params.obs_type) {
        conditions.push(`m.type = $${paramIdx++}`);
        values.push(params.type || params.obs_type);
      }
      if (params.dateStart) {
        conditions.push(`m.created_at >= $${paramIdx++}`);
        values.push(params.dateStart);
      }
      if (params.dateEnd) {
        conditions.push(`m.created_at <= $${paramIdx++}`);
        values.push(params.dateEnd);
      }
      if (params.filePath) {
        conditions.push(`m.metadata->>'filePath' = $${paramIdx++}`);
        values.push(params.filePath);
      }

      // If query is provided, use hybrid search (vector + FTS via RRF)
      if (params.query) {
        try {
          const queryEmbedding = await embedQuery(params.query);
          const limit = params.limit || 20;
          const { rows } = await pool.query(
            `SELECT * FROM hybrid_search($${paramIdx++}, $${paramIdx++}::vector, $${paramIdx++}, 60, $${paramIdx++})`,
            [
              ...values,
              params.query,
              toPgVector(queryEmbedding),
              limit,
              indexId,
            ]
          );
          // Fire-and-forget: log search hits
          const hitIds = (rows as Memory[]).map((r) => r.id);
          logUsage(hitIds, "search_hit").catch(() => {});

          return { results: rows as Memory[], total: rows.length };
        } catch {
          // Fallback to FTS if embedding fails
          conditions.push(
            `to_tsvector('english', coalesce(m.title, '') || ' ' || m.content) @@ plainto_tsquery('english', $${paramIdx++})`
          );
          values.push(params.query);
        }
      }

      const where =
        conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";
      const limit = params.limit || 50;
      const offset = params.offset || 0;
      const orderBy =
        params.orderBy === "oldest"
          ? "m.created_at ASC"
          : "m.created_at DESC";

      const { rows } = await pool.query(
        `SELECT m.id, m.index_id, m.session_id, m.type, m.title, m.content,
                m.metadata, m.priority, m.stability, m.created_at, m.updated_at
         FROM memories m ${where}
         ORDER BY ${orderBy}
         LIMIT $${paramIdx++} OFFSET $${paramIdx++}`,
        [...values, limit, offset]
      );

      // Get total count
      const countResult = await pool.query(
        `SELECT COUNT(*)::int AS total FROM memories m ${where}`,
        values
      );

      // Fire-and-forget: log search hits
      const hitIds = (rows as Memory[]).map((r) => r.id);
      logUsage(hitIds, "search_hit").catch(() => {});

      return {
        results: rows as Memory[],
        total: countResult.rows[0].total,
      };
    },

    async timeline(params: TimelineParams) {
      let anchorId = params.anchor || null;
      const indexId = await resolveIndexId(params.project);

      // If query provided, find best match as anchor
      if (!anchorId && params.query) {
        const indexFilter = indexId ? `AND m.index_id = $2` : "";
        const queryValues: unknown[] = [params.query];
        if (indexId) queryValues.push(indexId);

        const { rows } = await pool.query(
          `SELECT m.id FROM memories m
           WHERE to_tsvector('english', coalesce(m.title, '') || ' ' || m.content)
                 @@ plainto_tsquery('english', $1)
           ${indexFilter}
           ORDER BY ts_rank_cd(
             to_tsvector('english', coalesce(m.title, '') || ' ' || m.content),
             plainto_tsquery('english', $1)
           ) DESC
           LIMIT 1`,
          queryValues
        );
        if (rows.length > 0) anchorId = rows[0].id;
      }

      if (!anchorId) {
        return { results: [], anchor_id: null };
      }

      const depthBefore = params.depth_before ?? 5;
      const depthAfter = params.depth_after ?? 5;

      // Get anchor's created_at
      const {
        rows: [anchor],
      } = await pool.query(
        "SELECT created_at, session_id FROM memories WHERE id = $1",
        [anchorId]
      );
      if (!anchor) return { results: [], anchor_id: null };

      const indexFilter = indexId ? `AND m.index_id = $3` : "";
      const baseValues: unknown[] = [
        anchor.created_at,
        depthBefore + depthAfter + 1,
      ];
      if (indexId) baseValues.push(indexId);

      const { rows } = await pool.query(
        `(SELECT m.id, m.index_id, m.session_id, m.type, m.title, m.content,
                 m.metadata, m.priority, m.stability, m.created_at, m.updated_at
          FROM memories m WHERE m.created_at <= $1 ${indexFilter}
          ORDER BY m.created_at DESC LIMIT ${depthBefore + 1})
         UNION ALL
         (SELECT m.id, m.index_id, m.session_id, m.type, m.title, m.content,
                 m.metadata, m.priority, m.stability, m.created_at, m.updated_at
          FROM memories m WHERE m.created_at > $1 ${indexFilter}
          ORDER BY m.created_at ASC LIMIT ${depthAfter})
         ORDER BY created_at ASC`,
        baseValues
      );

      return { results: rows as Memory[], anchor_id: anchorId };
    },

    async getObservations(ids: number[]) {
      if (ids.length === 0) return [];
      const placeholders = ids.map((_, i) => `$${i + 1}`).join(", ");
      const { rows } = await pool.query(
        `SELECT * FROM memories WHERE id IN (${placeholders}) ORDER BY created_at ASC`,
        ids
      );
      // Fire-and-forget: log retrieved
      const retrievedIds = (rows as Memory[]).map((r) => r.id);
      logUsage(retrievedIds, "retrieved").catch(() => {});

      return rows as Memory[];
    },

    async saveMemory(params: SaveMemoryParams) {
      const indexId = await resolveIndexId(params.project);

      const { rows } = await pool.query(
        `INSERT INTO memories (index_id, type, title, content)
         VALUES ($1, $2, $3, $4)
         RETURNING id`,
        [
          indexId || 1,
          params.type || "observation",
          params.title || null,
          params.text,
        ]
      );

      const memoryId = rows[0].id;

      // Embed async (don't block response), then auto-link similar memories
      const textToEmbed = [params.title, params.text]
        .filter(Boolean)
        .join(": ");
      embed(textToEmbed)
        .then(async (embedding) => {
          const pgVec = toPgVector(embedding);
          await pool.query(
            "UPDATE memories SET embedding = $1 WHERE id = $2",
            [pgVec, memoryId]
          );

          // Auto-link: find top 5 similar memories with cosine similarity > 0.65
          const { rows: similar } = await pool.query(
            `SELECT m.id, 1 - (m.embedding <=> $1::vector) AS similarity
             FROM memories m
             WHERE m.id != $2
               AND m.embedding IS NOT NULL
               AND 1 - (m.embedding <=> $1::vector) > 0.65
             ORDER BY m.embedding <=> $1::vector
             LIMIT 5`,
            [pgVec, memoryId]
          );

          // Create bidirectional similar_to relationships
          for (const row of similar) {
            await pool.query(
              `INSERT INTO memory_relationships (source_id, target_id, relation_type, weight)
               VALUES ($1, $2, 'similar_to', $3)
               ON CONFLICT (source_id, target_id, relation_type) DO UPDATE SET weight = $3`,
              [memoryId, row.id, row.similarity]
            );
          }

          if (similar.length > 0) {
            console.log(
              `Auto-linked memory ${memoryId} to ${similar.length} similar memories`
            );
          }
        })
        .catch((err) => {
          console.error(
            `Embedding/linking failed for memory ${memoryId}:`,
            err
          );
        });

      return { id: memoryId, message: "Memory saved" };
    },

    async searchByConcept(query: string, limit?: number, project?: string) {
      const indexId = await resolveIndexId(project);
      const queryEmbedding = await embedQuery(query);
      const maxResults = limit || 10;

      const conditions = ["m.embedding IS NOT NULL"];
      const values: unknown[] = [toPgVector(queryEmbedding)];
      let paramIdx = 2;

      if (indexId) {
        conditions.push(`m.index_id = $${paramIdx++}`);
        values.push(indexId);
      }

      values.push(maxResults);

      const { rows } = await pool.query(
        `SELECT m.id, m.index_id, m.session_id, m.type, m.title, m.content,
                m.metadata, m.priority, m.stability, m.created_at, m.updated_at,
                1 - (m.embedding <=> $1::vector) AS similarity
         FROM memories m
         WHERE ${conditions.join(" AND ")}
         ORDER BY m.embedding <=> $1::vector
         LIMIT $${paramIdx}`,
        values
      );

      return { results: rows as Memory[] };
    },

    async getContext(limit?: number, project?: string) {
      const maxSessions = limit || 5;
      const indexId = await resolveIndexId(project);
      const conditions: string[] = [];
      const values: unknown[] = [];
      let paramIdx = 1;

      if (indexId) {
        conditions.push(`s.index_id = $${paramIdx++}`);
        values.push(indexId);
      }

      const where =
        conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";
      values.push(maxSessions);

      const { rows: sessions } = await pool.query(
        `SELECT s.id, s.session_id, s.project, s.started_at, s.ended_at, s.metadata,
                (SELECT json_agg(json_build_object('summary', ss.summary, 'created_at', ss.created_at))
                 FROM session_summaries ss WHERE ss.session_id = s.id) AS summaries
         FROM sessions s ${where}
         ORDER BY s.started_at DESC
         LIMIT $${paramIdx}`,
        values
      );

      return { sessions };
    },

    async refineMemories(params: RefineParams): Promise<RefineResult> {
      const limit = params.limit || 50;
      const scope = params.scope || "recent";

      let candidates: Memory[];

      if (scope === "duplicates") {
        const { rows } = await pool.query(
          `SELECT DISTINCT ON (m1.id)
             m1.id, m1.index_id, m1.session_id, m1.type, m1.title, m1.content,
             m1.metadata, m1.priority, m1.stability, m1.created_at, m1.updated_at,
             m2.id AS similar_id, 1 - (m1.embedding <=> m2.embedding) AS similarity
           FROM memories m1
           JOIN memories m2 ON m1.id < m2.id
           WHERE m1.embedding IS NOT NULL AND m2.embedding IS NOT NULL
             AND 1 - (m1.embedding <=> m2.embedding) > 0.85
           ORDER BY m1.id, similarity DESC
           LIMIT $1`,
          [limit]
        );
        candidates = rows as Memory[];
      } else if (scope.startsWith("project:")) {
        const project = scope.slice(8);
        const indexId = await resolveIndexId(project);
        const { rows } = await pool.query(
          "SELECT * FROM memories WHERE index_id = $1 ORDER BY created_at DESC LIMIT $2",
          [indexId || 1, limit]
        );
        candidates = rows as Memory[];
      } else if (scope === "low-priority") {
        const { rows } = await pool.query(
          "SELECT * FROM memories WHERE priority < 0.2 ORDER BY priority ASC LIMIT $1",
          [limit]
        );
        candidates = rows as Memory[];
      } else {
        // "recent" - last N memories
        const { rows } = await pool.query(
          "SELECT * FROM memories ORDER BY created_at DESC LIMIT $1",
          [limit]
        );
        candidates = rows as Memory[];
      }

      if (candidates.length === 0) {
        return { analyzed: 0, actions: [], summary: "No candidates found" };
      }

      const actions = await analyzeWithLlm(candidates);

      if (!params.dryRun) {
        for (const action of actions) {
          await executeRefineAction(action);
        }
      }

      return {
        analyzed: candidates.length,
        actions: actions.map((a) => ({ ...a, executed: !params.dryRun })),
        summary: `Analyzed ${candidates.length} memories, suggested ${actions.length} actions${params.dryRun ? " (dry run)" : ""}`,
      };
    },

    async stats() {
      const [memories, sessions, relationships, dbSize] = await Promise.all([
        pool.query("SELECT COUNT(*)::int AS count FROM memories"),
        pool.query("SELECT COUNT(*)::int AS count FROM sessions"),
        pool.query("SELECT COUNT(*)::int AS count FROM memory_relationships"),
        pool.query("SELECT pg_database_size(current_database()) AS size"),
      ]);

      return {
        memories: memories.rows[0].count,
        sessions: sessions.rows[0].count,
        relationships: relationships.rows[0].count,
        db_size_bytes: dbSize.rows[0].size,
        db_size_mb:
          Math.round((dbSize.rows[0].size / 1024 / 1024) * 100) / 100,
      };
    },
  };
}
