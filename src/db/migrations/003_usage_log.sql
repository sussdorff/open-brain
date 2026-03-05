-- Usage logging for priority decay
CREATE TABLE IF NOT EXISTS memory_usage_log (
  id SERIAL PRIMARY KEY,
  memory_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL CHECK (event_type IN ('search_hit', 'retrieved', 'cited')),
  session_context TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_usage_log_memory ON memory_usage_log(memory_id);
CREATE INDEX IF NOT EXISTS idx_usage_log_created ON memory_usage_log(created_at DESC);
