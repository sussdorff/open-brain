-- 005: Align schema with plan (elysium-proxmox/docs/plans/open-brain.md)

-- =============================================================================
-- 1. memories: add missing fields
-- =============================================================================
ALTER TABLE memories ADD COLUMN IF NOT EXISTS subtitle TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS narrative TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS access_count INTEGER DEFAULT 0;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ;

-- Add stored search_vector with weighted fields (title=A, subtitle=B, narrative=C, content=D)
ALTER TABLE memories ADD COLUMN IF NOT EXISTS search_vector TSVECTOR
  GENERATED ALWAYS AS (
    setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
    setweight(to_tsvector('english', COALESCE(subtitle, '')), 'B') ||
    setweight(to_tsvector('english', COALESCE(narrative, '')), 'C') ||
    setweight(to_tsvector('english', COALESCE(content, '')), 'D')
  ) STORED;

-- Replace old FTS index with one on stored search_vector
DROP INDEX IF EXISTS idx_memories_fts;
CREATE INDEX IF NOT EXISTS idx_memories_search_vector ON memories USING gin(search_vector);

-- =============================================================================
-- 2. sessions: add status + prompt_counter
-- =============================================================================
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'
  CHECK (status IN ('active', 'completed', 'failed'));
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS prompt_counter INTEGER DEFAULT 0;

-- =============================================================================
-- 3. session_summaries: add structured fields
-- =============================================================================
ALTER TABLE session_summaries ADD COLUMN IF NOT EXISTS request TEXT;
ALTER TABLE session_summaries ADD COLUMN IF NOT EXISTS investigated TEXT;
ALTER TABLE session_summaries ADD COLUMN IF NOT EXISTS learned TEXT;
ALTER TABLE session_summaries ADD COLUMN IF NOT EXISTS completed TEXT;
ALTER TABLE session_summaries ADD COLUMN IF NOT EXISTS next_steps TEXT;
ALTER TABLE session_summaries ADD COLUMN IF NOT EXISTS files_read JSONB;
ALTER TABLE session_summaries ADD COLUMN IF NOT EXISTS files_edited JSONB;
ALTER TABLE session_summaries ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE session_summaries ADD COLUMN IF NOT EXISTS prompt_number INTEGER;

-- Make summary nullable (structured fields may replace it)
ALTER TABLE session_summaries ALTER COLUMN summary DROP NOT NULL;

-- =============================================================================
-- 4. memory_relationships: add CHECK constraint, rename weight -> confidence
-- =============================================================================
ALTER TABLE memory_relationships RENAME COLUMN weight TO confidence;

-- Add CHECK constraint on relation_type
ALTER TABLE memory_relationships DROP CONSTRAINT IF EXISTS memory_relationships_relation_type_check;
ALTER TABLE memory_relationships ADD CONSTRAINT memory_relationships_relation_type_check
  CHECK (relation_type IN (
    'supports', 'contradicts', 'supersedes', 'similar_to',
    'causes', 'example_of', 'generalizes', 'sequence_next'
  ));

-- Add index on relation_type (plan has it, implementation didn't)
CREATE INDEX IF NOT EXISTS idx_relationships_type ON memory_relationships(relation_type);

-- =============================================================================
-- 5. memory_usage_log: add 'updated' event type
-- =============================================================================
ALTER TABLE memory_usage_log DROP CONSTRAINT IF EXISTS memory_usage_log_event_type_check;
ALTER TABLE memory_usage_log ADD CONSTRAINT memory_usage_log_event_type_check
  CHECK (event_type IN ('search_hit', 'retrieved', 'cited', 'updated'));

-- =============================================================================
-- 6. Replace hybrid_search() to use stored search_vector + websearch_to_tsquery
-- =============================================================================
DROP FUNCTION IF EXISTS hybrid_search(TEXT, vector, INTEGER, INTEGER, INTEGER);
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
  subtitle TEXT,
  type TEXT,
  score REAL,
  created_at TIMESTAMPTZ
)
LANGUAGE sql STABLE AS $$
  WITH fts AS (
    SELECT m.id,
           ROW_NUMBER() OVER (
             ORDER BY ts_rank_cd(m.search_vector, websearch_to_tsquery('english', query_text)) DESC
           ) AS rank
    FROM memories m
    WHERE m.search_vector @@ websearch_to_tsquery('english', query_text)
      AND (p_index_id IS NULL OR m.index_id = p_index_id)
    ORDER BY ts_rank_cd(m.search_vector, websearch_to_tsquery('english', query_text)) DESC
    LIMIT match_limit * 2
  ),
  vec AS (
    SELECT m.id,
           ROW_NUMBER() OVER (ORDER BY m.embedding <=> query_embedding) AS rank
    FROM memories m
    WHERE m.embedding IS NOT NULL
      AND (p_index_id IS NULL OR m.index_id = p_index_id)
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
$$;

-- =============================================================================
-- 7. Replace priority system: update_priority() with 3-factor formula
-- =============================================================================
CREATE OR REPLACE FUNCTION update_priority(memory_id INTEGER)
RETURNS VOID
LANGUAGE sql AS $$
  UPDATE memories SET
    priority = (
      0.4 * (1.0 / (1.0 + EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0)) +
      0.4 * CASE
        WHEN stability = 'canonical' THEN 1.0
        WHEN stability = 'stable' THEN 0.7
        ELSE 0.4
      END +
      0.2 * LEAST(access_count::FLOAT / 10.0, 1.0)
    )::REAL,
    access_count = access_count + 1,
    last_accessed_at = NOW(),
    updated_at = NOW()
  WHERE id = memory_id;
$$;

-- Keep decay_unused_priorities as complementary function (unchanged)
