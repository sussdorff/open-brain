-- Hybrid search function using Reciprocal Rank Fusion
CREATE OR REPLACE FUNCTION hybrid_search(
  query_text TEXT,
  query_embedding vector(1024),
  match_limit INTEGER DEFAULT 20,
  rrf_k INTEGER DEFAULT 60,
  p_index_id INTEGER DEFAULT NULL
)
RETURNS TABLE (
  id INTEGER,
  title TEXT,
  content TEXT,
  type TEXT,
  priority REAL,
  created_at TIMESTAMPTZ,
  fts_rank REAL,
  vec_rank REAL,
  rrf_score REAL
)
LANGUAGE sql STABLE AS $$
  WITH fts AS (
    SELECT m.id,
           ts_rank_cd(to_tsvector('english', coalesce(m.title, '') || ' ' || m.content), plainto_tsquery('english', query_text)) AS rank
    FROM memories m
    WHERE to_tsvector('english', coalesce(m.title, '') || ' ' || m.content) @@ plainto_tsquery('english', query_text)
      AND (p_index_id IS NULL OR m.index_id = p_index_id)
    ORDER BY rank DESC
    LIMIT match_limit * 2
  ),
  vec AS (
    SELECT m.id,
           1 - (m.embedding <=> query_embedding) AS rank
    FROM memories m
    WHERE m.embedding IS NOT NULL
      AND (p_index_id IS NULL OR m.index_id = p_index_id)
    ORDER BY m.embedding <=> query_embedding
    LIMIT match_limit * 2
  ),
  combined AS (
    SELECT COALESCE(f.id, v.id) AS id,
           COALESCE(f.rank, 0.0)::REAL AS fts_rank,
           COALESCE(v.rank, 0.0)::REAL AS vec_rank,
           (COALESCE(1.0 / (rrf_k + ROW_NUMBER() OVER (ORDER BY f.rank DESC NULLS LAST)), 0.0) +
            COALESCE(1.0 / (rrf_k + ROW_NUMBER() OVER (ORDER BY v.rank DESC NULLS LAST)), 0.0))::REAL AS rrf_score
    FROM fts f
    FULL OUTER JOIN vec v ON f.id = v.id
  )
  SELECT m.id, m.title, m.content, m.type, m.priority, m.created_at,
         c.fts_rank, c.vec_rank, c.rrf_score
  FROM combined c
  JOIN memories m ON m.id = c.id
  ORDER BY c.rrf_score DESC
  LIMIT match_limit;
$$;

-- Priority decay function
CREATE OR REPLACE FUNCTION decay_unused_priorities(days_threshold INTEGER DEFAULT 30, decay_factor REAL DEFAULT 0.9)
RETURNS INTEGER
LANGUAGE plpgsql AS $$
DECLARE
  affected INTEGER;
BEGIN
  UPDATE memories SET
    priority = GREATEST(priority * decay_factor, 0.01),
    updated_at = now()
  WHERE id NOT IN (
    SELECT DISTINCT memory_id FROM memory_usage_log
    WHERE created_at > now() - (days_threshold || ' days')::INTERVAL
  )
  AND priority > 0.01;

  GET DIAGNOSTICS affected = ROW_COUNT;
  RETURN affected;
END;
$$;
