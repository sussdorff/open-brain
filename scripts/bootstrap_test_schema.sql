-- Bootstrap schema for open-brain integration tests.
--
-- This file recreates the legacy base schema that predates the Python
-- idempotent migration battery, then applies the current test-time schema
-- additions expected by python/src/open_brain/data_layer/postgres.py.
-- It is intended for fresh disposable Postgres+pgvector databases only.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS memory_indexes (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

INSERT INTO memory_indexes (name, description)
VALUES ('default', 'Default memory index')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS sessions (
  id SERIAL PRIMARY KEY,
  session_id TEXT NOT NULL UNIQUE,
  index_id INTEGER REFERENCES memory_indexes(id) DEFAULT 1,
  project TEXT,
  started_at TIMESTAMPTZ DEFAULT now(),
  ended_at TIMESTAMPTZ,
  metadata JSONB DEFAULT '{}',
  status TEXT DEFAULT 'active' CHECK (status IN ('active', 'completed', 'failed')),
  prompt_counter INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS session_summaries (
  id SERIAL PRIMARY KEY,
  session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
  summary TEXT,
  request TEXT,
  investigated TEXT,
  learned TEXT,
  completed TEXT,
  next_steps TEXT,
  files_read JSONB,
  files_edited JSONB,
  notes TEXT,
  prompt_number INTEGER,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memories (
  id SERIAL PRIMARY KEY,
  index_id INTEGER REFERENCES memory_indexes(id) DEFAULT 1,
  session_id INTEGER REFERENCES sessions(id),
  session_ref TEXT,
  user_id TEXT,
  type TEXT NOT NULL DEFAULT 'observation',
  title TEXT,
  subtitle TEXT,
  narrative TEXT,
  content TEXT NOT NULL,
  embedding vector(1024),
  metadata JSONB DEFAULT '{}',
  priority REAL DEFAULT 0.5,
  importance VARCHAR(8) NOT NULL DEFAULT 'medium'
    CHECK (importance IN ('critical', 'high', 'medium', 'low')),
  stability TEXT DEFAULT 'tentative'
    CHECK (stability IN ('tentative', 'stable', 'canonical')),
  access_count INTEGER DEFAULT 0,
  last_accessed_at TIMESTAMPTZ,
  last_decay_at TIMESTAMPTZ,
  last_boost_at TIMESTAMPTZ,
  search_vector TSVECTOR GENERATED ALWAYS AS (
    setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
    setweight(to_tsvector('english', COALESCE(subtitle, '')), 'B') ||
    setweight(to_tsvector('english', COALESCE(narrative, '')), 'C') ||
    setweight(to_tsvector('english', COALESCE(content, '')), 'D')
  ) STORED,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE memories ADD COLUMN IF NOT EXISTS session_ref TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS user_id TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS subtitle TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS narrative TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS access_count INTEGER DEFAULT 0;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS importance VARCHAR(8) NOT NULL DEFAULT 'medium'
  CHECK (importance IN ('critical', 'high', 'medium', 'low'));
ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_decay_at TIMESTAMPTZ;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_boost_at TIMESTAMPTZ;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS search_vector TSVECTOR
  GENERATED ALWAYS AS (
    setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
    setweight(to_tsvector('english', COALESCE(subtitle, '')), 'B') ||
    setweight(to_tsvector('english', COALESCE(narrative, '')), 'C') ||
    setweight(to_tsvector('english', COALESCE(content, '')), 'D')
  ) STORED;
UPDATE memories SET last_decay_at = updated_at WHERE last_decay_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_index_id ON memories(index_id);
CREATE INDEX IF NOT EXISTS idx_memories_session_id ON memories(session_id);
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_priority ON memories(priority DESC);
DROP INDEX IF EXISTS idx_memories_fts;
CREATE INDEX IF NOT EXISTS idx_memories_search_vector ON memories USING gin(search_vector);
CREATE INDEX IF NOT EXISTS idx_memories_content_trgm ON memories USING gin(content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_memories_embedding
  ON memories USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_memories_content_hash
  ON memories ((metadata->>'content_hash'))
  WHERE metadata->>'content_hash' IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_capture_status
  ON memories ((metadata->>'capture_status'));
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_knowledge_capture_identity
  ON memories ((metadata->>'session_knowledge_capture_identity'))
  WHERE type = 'session_event'
    AND metadata->>'session_knowledge_capture_identity' IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_knowledge_record_identity
  ON memories ((metadata->>'session_knowledge_record_identity'))
  WHERE metadata->>'session_knowledge_record_identity' IS NOT NULL;

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

CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_actions_state
  ON memory_lifecycle_actions (policy_version, state);

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

CREATE INDEX IF NOT EXISTS idx_session_learning_reviews_key_created
  ON session_learning_reviews (review_key, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS session_learning_analysis_runs (
  run_id UUID PRIMARY KEY,
  status TEXT NOT NULL
    CHECK (status IN ('running', 'completed', 'failed')),
  parameters JSONB NOT NULL,
  source_memory_ids BIGINT[] NOT NULL DEFAULT '{}',
  next_cursor TEXT,
  report JSONB,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ,
  CHECK (
    (status = 'running' AND report IS NULL AND error IS NULL AND completed_at IS NULL)
    OR (status = 'completed' AND report IS NOT NULL AND error IS NULL AND completed_at IS NOT NULL)
    OR (status = 'failed' AND report IS NULL AND error IS NOT NULL AND completed_at IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_session_learning_analysis_runs_status_created
  ON session_learning_analysis_runs (status, created_at DESC);

CREATE TABLE IF NOT EXISTS memory_relationships (
  id SERIAL PRIMARY KEY,
  source_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
  target_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL DEFAULT 'similar_to',
  confidence REAL DEFAULT 1.0,
  link_type TEXT NOT NULL DEFAULT 'similar_to',
  metadata JSONB,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(source_id, target_id, relation_type)
);

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'memory_relationships' AND column_name = 'weight'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'memory_relationships' AND column_name = 'confidence'
  ) THEN
    ALTER TABLE memory_relationships RENAME COLUMN weight TO confidence;
  END IF;
END $$;

ALTER TABLE memory_relationships ADD COLUMN IF NOT EXISTS confidence REAL DEFAULT 1.0;
ALTER TABLE memory_relationships ADD COLUMN IF NOT EXISTS link_type TEXT NOT NULL DEFAULT 'similar_to';
ALTER TABLE memory_relationships ADD COLUMN IF NOT EXISTS metadata JSONB;
ALTER TABLE memory_relationships DROP CONSTRAINT IF EXISTS memory_relationships_relation_type_check;

CREATE INDEX IF NOT EXISTS idx_relationships_source ON memory_relationships(source_id);
CREATE INDEX IF NOT EXISTS idx_relationships_target ON memory_relationships(target_id);
CREATE INDEX IF NOT EXISTS idx_relationships_type ON memory_relationships(relation_type);
CREATE INDEX IF NOT EXISTS idx_memrel_linktype ON memory_relationships(link_type);

CREATE TABLE IF NOT EXISTS memory_usage_log (
  id SERIAL PRIMARY KEY,
  memory_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  session_context TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE memory_usage_log DROP CONSTRAINT IF EXISTS memory_usage_log_event_type_check;
ALTER TABLE memory_usage_log ADD CONSTRAINT memory_usage_log_event_type_check
  CHECK (event_type IN ('search_hit', 'retrieved', 'cited', 'updated'));

CREATE INDEX IF NOT EXISTS idx_usage_log_memory ON memory_usage_log(memory_id);
CREATE INDEX IF NOT EXISTS idx_usage_log_created ON memory_usage_log(created_at DESC);

CREATE TABLE IF NOT EXISTS embedding_token_log (
  id BIGSERIAL PRIMARY KEY,
  operation TEXT NOT NULL,
  token_count INT NOT NULL,
  logged_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_embedding_token_log_logged_at
  ON embedding_token_log(logged_at);

CREATE TABLE IF NOT EXISTS url_tokens (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  token_hash TEXT NOT NULL UNIQUE,
  scopes JSONB NOT NULL DEFAULT '[]',
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Append-only epistemic promotion ledger (open-brain-ekn.5).
CREATE TABLE IF NOT EXISTS memory_promotion_events (
  id BIGSERIAL PRIMARY KEY,
  memory_id INTEGER NOT NULL,
  actor TEXT NOT NULL CHECK (length(btrim(actor)) > 0),
  source_state TEXT NOT NULL,
  target_state TEXT NOT NULL,
  reason TEXT NOT NULL CHECK (length(btrim(reason)) > 0),
  evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  policy_version TEXT NOT NULL,
  rule_version TEXT,
  grant_jti TEXT,
  grant_digest TEXT,
  origin_attestation_digest TEXT,
  decision TEXT NOT NULL
    CHECK (decision IN ('accepted', 'rejected')),
  outcome TEXT NOT NULL,
  rejection_code TEXT,
  relationship_id INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (
    (decision = 'accepted' AND rejection_code IS NULL)
    OR (decision = 'rejected' AND rejection_code IS NOT NULL)
  )
);

-- Idempotent for DBs created before origin attestation digests existed.
ALTER TABLE memory_promotion_events
  ADD COLUMN IF NOT EXISTS origin_attestation_digest TEXT;

CREATE INDEX IF NOT EXISTS idx_memory_promotion_events_memory_created
  ON memory_promotion_events (memory_id, created_at ASC, id ASC);

CREATE UNIQUE INDEX IF NOT EXISTS memory_promotion_events_accepted_grant_jti_uidx
  ON memory_promotion_events (grant_jti)
  WHERE grant_jti IS NOT NULL AND decision = 'accepted';

CREATE OR REPLACE FUNCTION reject_memory_promotion_events_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'memory_promotion_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS memory_promotion_events_no_update
  ON memory_promotion_events;
CREATE TRIGGER memory_promotion_events_no_update
  BEFORE UPDATE ON memory_promotion_events
  FOR EACH ROW
  EXECUTE PROCEDURE reject_memory_promotion_events_mutation();

DROP TRIGGER IF EXISTS memory_promotion_events_no_delete
  ON memory_promotion_events;
CREATE TRIGGER memory_promotion_events_no_delete
  BEFORE DELETE ON memory_promotion_events
  FOR EACH ROW
  EXECUTE PROCEDURE reject_memory_promotion_events_mutation();

-- Drop every historical hybrid_search overload before recreating the canonical
-- 8-argument version below. Legacy migrations 004/005 created a 5-argument
-- hybrid_search(TEXT, vector, INTEGER, INTEGER, INTEGER); the current server
-- (postgres.py get_pool) and this bootstrap install an 8-argument overload.
-- Postgres identifies functions by (name, argument types), so a different
-- argument list is a SEPARATE overload rather than a replacement. A stale 5-arg
-- overload left in place would make hybrid_search(...) calls ambiguous once the
-- 8-arg version's trailing DEFAULTs overlap it, and CREATE OR REPLACE cannot
-- change an existing function's return type. Dropping BOTH known signatures
-- (schema-qualified, and independent of return type) guarantees an idempotent
-- rebuild from ANY of this codebase's historical hybrid_search states.
DROP FUNCTION IF EXISTS public.hybrid_search(TEXT, vector, INTEGER, INTEGER, INTEGER);
DROP FUNCTION IF EXISTS public.hybrid_search(TEXT, vector, INTEGER, INTEGER, INTEGER, TEXT);
DROP FUNCTION IF EXISTS public.hybrid_search(TEXT, vector, INTEGER, INTEGER, INTEGER, TEXT, JSONB);
DROP FUNCTION IF EXISTS public.hybrid_search(TEXT, vector, INTEGER, INTEGER, INTEGER, TEXT, JSONB, TEXT);
CREATE OR REPLACE FUNCTION public.hybrid_search(
  query_text TEXT,
  query_embedding vector,
  match_limit INTEGER DEFAULT 20,
  rrf_k INTEGER DEFAULT 60,
  p_index_id INTEGER DEFAULT NULL,
  p_user_id TEXT DEFAULT NULL,
  p_metadata_filter JSONB DEFAULT NULL,
  p_capture_status TEXT DEFAULT NULL
)
RETURNS TABLE(
  id INTEGER,
  title TEXT,
  subtitle TEXT,
  type TEXT,
  score REAL,
  created_at TIMESTAMPTZ
)
LANGUAGE sql STABLE AS $fn$
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

CREATE OR REPLACE FUNCTION update_priority(memory_id INTEGER)
RETURNS VOID
LANGUAGE sql AS $fn$
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
$fn$;

-- Drop every historical decay_unused_priorities overload before recreating it.
-- Legacy migration 004 created decay_unused_priorities(INTEGER, REAL) — REAL is
-- float4 — while this bootstrap and postgres.py get_pool create the
-- (INTEGER, FLOAT) overload. FLOAT with no precision is DOUBLE PRECISION (float8)
-- in Postgres, which is a DISTINCT overload from REAL. Because the two differ only
-- by the float width, recreating just the float8 version would leave any
-- pre-existing float4 overload in place, making decay_unused_priorities(<int>,
-- <numeric literal>) ambiguous. Drop BOTH float widths (schema-qualified, and
-- independent of return type) for an idempotent rebuild from ANY historical state.
DROP FUNCTION IF EXISTS public.decay_unused_priorities(INTEGER, REAL);
DROP FUNCTION IF EXISTS public.decay_unused_priorities(INTEGER, DOUBLE PRECISION);
CREATE OR REPLACE FUNCTION decay_unused_priorities(
  p_stale_days INTEGER,
  p_decay_factor FLOAT
) RETURNS INTEGER
LANGUAGE plpgsql AS $fn$
DECLARE
  v_updated INTEGER;
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
      AND (m.metadata->>'canonical_entity') IS DISTINCT FROM 'true'
    RETURNING m.id
  )
  SELECT COUNT(*) INTO v_updated FROM updated;
  RETURN v_updated;
END;
$fn$;
