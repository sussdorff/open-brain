-- Memory relationships
CREATE TABLE IF NOT EXISTS memory_relationships (
  id SERIAL PRIMARY KEY,
  source_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
  target_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL DEFAULT 'similar_to',
  weight REAL DEFAULT 1.0,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(source_id, target_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_relationships_source ON memory_relationships(source_id);
CREATE INDEX IF NOT EXISTS idx_relationships_target ON memory_relationships(target_id);
