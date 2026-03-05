-- Enable extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Memory indexes (one per project/namespace)
CREATE TABLE IF NOT EXISTS memory_indexes (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Insert default index
INSERT INTO memory_indexes (name, description)
VALUES ('default', 'Default memory index')
ON CONFLICT (name) DO NOTHING;

-- Sessions
CREATE TABLE IF NOT EXISTS sessions (
  id SERIAL PRIMARY KEY,
  session_id TEXT NOT NULL UNIQUE,
  index_id INTEGER REFERENCES memory_indexes(id) DEFAULT 1,
  project TEXT,
  started_at TIMESTAMPTZ DEFAULT now(),
  ended_at TIMESTAMPTZ,
  metadata JSONB DEFAULT '{}'
);

-- Session summaries
CREATE TABLE IF NOT EXISTS session_summaries (
  id SERIAL PRIMARY KEY,
  session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
  summary TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Memories (observations)
CREATE TABLE IF NOT EXISTS memories (
  id SERIAL PRIMARY KEY,
  index_id INTEGER REFERENCES memory_indexes(id) DEFAULT 1,
  session_id INTEGER REFERENCES sessions(id),
  type TEXT NOT NULL DEFAULT 'observation',
  title TEXT,
  content TEXT NOT NULL,
  embedding vector(1024),
  metadata JSONB DEFAULT '{}',
  priority REAL DEFAULT 0.5,
  stability TEXT DEFAULT 'tentative' CHECK (stability IN ('tentative', 'stable', 'canonical')),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- Indexes for fast querying
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_index_id ON memories(index_id);
CREATE INDEX IF NOT EXISTS idx_memories_session_id ON memories(session_id);
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_priority ON memories(priority DESC);

-- Full-text search index
CREATE INDEX IF NOT EXISTS idx_memories_fts ON memories USING gin(to_tsvector('english', coalesce(title, '') || ' ' || content));

-- Trigram index for fuzzy search
CREATE INDEX IF NOT EXISTS idx_memories_content_trgm ON memories USING gin(content gin_trgm_ops);

-- Vector similarity index (HNSW for fast approximate nearest neighbor)
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories USING hnsw(embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
