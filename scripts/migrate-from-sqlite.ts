/**
 * SQLite → Postgres migration for open-brain.
 *
 * Reads observations, sdk_sessions, and session_summaries from the claude-mem
 * SQLite database and imports them into the Postgres schema.
 *
 * Usage:
 *   DATABASE_URL=... VOYAGE_API_KEY=... tsx scripts/migrate-from-sqlite.ts <sqlite-path>
 *
 * Options:
 *   --dry-run       Print counts without writing to Postgres
 *   --skip-embed    Skip the embedding step (import data only)
 *   --batch=N       Embedding batch size (default: 64)
 */

import Database from "better-sqlite3";
import pg from "pg";

// ---------------------------------------------------------------------------
// CLI args + env
// ---------------------------------------------------------------------------

const SQLITE_PATH = process.argv.find((a) => !a.startsWith("-") && a !== process.argv[0] && a !== process.argv[1])
  || process.env.SQLITE_DB_PATH;
const DATABASE_URL = process.env.DATABASE_URL;
const DRY_RUN = process.argv.includes("--dry-run");
const SKIP_EMBED = process.argv.includes("--skip-embed");
const EMBED_BATCH_SIZE = parseInt(
  process.argv.find((a) => a.startsWith("--batch="))?.split("=")[1] || "64", 10
);

if (!SQLITE_PATH) {
  console.error("Usage: tsx scripts/migrate-from-sqlite.ts <sqlite-path>");
  console.error("  DATABASE_URL must be set");
  process.exit(1);
}

if (!DATABASE_URL && !DRY_RUN) {
  console.error("Error: DATABASE_URL must be set (or use --dry-run)");
  process.exit(1);
}

// ---------------------------------------------------------------------------
// Embedding helpers
// ---------------------------------------------------------------------------

const VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings";
const VOYAGE_MODEL = process.env.VOYAGE_MODEL || "voyage-4";
const EMBEDDING_DIM = 1024;

async function embedBatch(texts: string[]): Promise<number[][]> {
  const VOYAGE_API_KEY = process.env.VOYAGE_API_KEY;
  if (!VOYAGE_API_KEY) throw new Error("VOYAGE_API_KEY must be set for embedding");

  const results: number[][] = [];

  for (let i = 0; i < texts.length; i += EMBED_BATCH_SIZE) {
    const batch = texts.slice(i, i + EMBED_BATCH_SIZE);
    const res = await fetch(VOYAGE_API_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${VOYAGE_API_KEY}`,
      },
      body: JSON.stringify({
        model: VOYAGE_MODEL,
        input: batch,
        input_type: "document",
        output_dimension: EMBEDDING_DIM,
      }),
    });

    if (!res.ok) {
      const body = await res.text();
      throw new Error(`Voyage batch embed error ${res.status}: ${body}`);
    }

    const data = (await res.json()) as { data: Array<{ embedding: number[] }> };
    results.push(...data.data.map((d) => d.embedding));

    // Rate limit: 200ms between batches
    if (i + EMBED_BATCH_SIZE < texts.length) {
      await new Promise((r) => setTimeout(r, 200));
    }

    console.log(`  Embedded ${Math.min(i + EMBED_BATCH_SIZE, texts.length)}/${texts.length}`);
  }

  return results;
}

function toPgVector(embedding: number[]): string {
  return `[${embedding.join(",")}]`;
}

// ---------------------------------------------------------------------------
// SQLite types (matching actual claude-mem schema)
// ---------------------------------------------------------------------------

interface SqliteObservation {
  id: number;
  memory_session_id: string;
  project: string;
  text: string | null;
  type: string;
  title: string | null;
  subtitle: string | null;
  facts: string | null;
  narrative: string | null;
  concepts: string | null;
  files_read: string | null;
  files_modified: string | null;
  prompt_number: number | null;
  discovery_tokens: number;
  created_at: string;
  created_at_epoch: number;
  content_hash: string | null;
}

interface SqliteSdkSession {
  id: number;
  content_session_id: string;
  memory_session_id: string | null;
  project: string;
  user_prompt: string | null;
  started_at: string;
  started_at_epoch: number;
  completed_at: string | null;
  completed_at_epoch: number | null;
  status: string;
  prompt_counter: number;
  custom_title: string | null;
}

interface SqliteSessionSummary {
  id: number;
  memory_session_id: string;
  project: string;
  request: string | null;
  investigated: string | null;
  learned: string | null;
  completed: string | null;
  next_steps: string | null;
  files_read: string | null;
  files_edited: string | null;
  notes: string | null;
  prompt_number: number | null;
  discovery_tokens: number;
  created_at: string;
  created_at_epoch: number;
}

// ---------------------------------------------------------------------------
// Read from SQLite
// ---------------------------------------------------------------------------

console.log(`Opening SQLite database: ${SQLITE_PATH}`);
const db = new Database(SQLITE_PATH, { readonly: true });

const observations = db.prepare("SELECT * FROM observations ORDER BY id").all() as SqliteObservation[];
const sdkSessions = db.prepare("SELECT * FROM sdk_sessions ORDER BY id").all() as SqliteSdkSession[];
const sessionSummaries = db.prepare("SELECT * FROM session_summaries ORDER BY id").all() as SqliteSessionSummary[];

console.log(`SQLite counts:`);
console.log(`  observations:      ${observations.length}`);
console.log(`  sdk_sessions:      ${sdkSessions.length}`);
console.log(`  session_summaries: ${sessionSummaries.length}`);

// Type distribution
const typeCounts = new Map<string, number>();
for (const obs of observations) {
  typeCounts.set(obs.type, (typeCounts.get(obs.type) || 0) + 1);
}
console.log(`  type distribution: ${[...typeCounts.entries()].map(([k, v]) => `${k}=${v}`).join(", ")}`);

// Collect unique projects
const projects = new Set<string>();
for (const obs of observations) projects.add(obs.project);
for (const sess of sdkSessions) projects.add(sess.project);
console.log(`  unique projects:   ${projects.size} (${[...projects].join(", ")})`);

db.close();

if (DRY_RUN) {
  console.log("\n--dry-run: No data written.");
  process.exit(0);
}

// ---------------------------------------------------------------------------
// Import into Postgres
// ---------------------------------------------------------------------------

const pool = new pg.Pool({ connectionString: DATABASE_URL, max: 5 });

try {
  // 1. Create memory_indexes entries
  console.log("\n--- Creating memory indexes ---");
  const indexMap = new Map<string, number>();

  for (const project of projects) {
    const { rows } = await pool.query(
      `INSERT INTO memory_indexes (name)
       VALUES ($1)
       ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
       RETURNING id`,
      [project]
    );
    indexMap.set(project, rows[0].id);
    console.log(`  Index: ${project} → id=${rows[0].id}`);
  }

  function getIndexId(project: string): number {
    return indexMap.get(project) || 1;
  }

  // 2. Import sdk_sessions → sessions
  console.log("\n--- Importing sessions ---");
  const sessionIdMap = new Map<string, number>(); // memory_session_id → postgres sessions.id

  for (const sess of sdkSessions) {
    const { rows } = await pool.query(
      `INSERT INTO sessions (session_id, index_id, project, started_at, ended_at, status, prompt_counter, metadata)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
       ON CONFLICT (session_id) DO UPDATE SET ended_at = COALESCE(EXCLUDED.ended_at, sessions.ended_at)
       RETURNING id`,
      [
        sess.memory_session_id || sess.content_session_id,
        getIndexId(sess.project),
        sess.project,
        sess.started_at,
        sess.completed_at,
        sess.status,
        sess.prompt_counter || 0,
        JSON.stringify({
          content_session_id: sess.content_session_id,
          custom_title: sess.custom_title,
          _sqlite_id: sess.id,
        }),
      ]
    );
    // Map by memory_session_id for FK lookups
    if (sess.memory_session_id) {
      sessionIdMap.set(sess.memory_session_id, rows[0].id);
    }
    sessionIdMap.set(sess.content_session_id, rows[0].id);
  }
  console.log(`  Imported ${sessionIdMap.size} sessions`);

  // 3. Import observations → memories
  console.log("\n--- Importing memories (observations) ---");
  let memoriesImported = 0;
  const memoryIds: number[] = [];
  const memoryTexts: string[] = [];

  for (const obs of observations) {
    const indexId = getIndexId(obs.project);
    const sessionPgId = sessionIdMap.get(obs.memory_session_id) || null;

    // Build content from text field
    const content = obs.text || "";

    // Build metadata
    const metadata: Record<string, unknown> = {
      _sqlite_id: obs.id,
      discovery_tokens: obs.discovery_tokens,
    };
    if (obs.content_hash) metadata.content_hash = obs.content_hash;
    if (obs.files_read) {
      try { metadata.files_read = JSON.parse(obs.files_read); } catch { metadata.files_read = obs.files_read; }
    }
    if (obs.files_modified) {
      try { metadata.files_modified = JSON.parse(obs.files_modified); } catch { metadata.files_modified = obs.files_modified; }
    }
    if (obs.concepts) {
      try { metadata.concepts = JSON.parse(obs.concepts); } catch { metadata.concepts = obs.concepts; }
    }
    if (obs.facts) {
      try { metadata.facts = JSON.parse(obs.facts); } catch { metadata.facts = obs.facts; }
    }

    const { rows } = await pool.query(
      `INSERT INTO memories (index_id, session_id, type, title, subtitle, narrative, content, metadata, created_at)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
       RETURNING id`,
      [
        indexId,
        sessionPgId,
        obs.type,
        obs.title || null,
        obs.subtitle || null,
        obs.narrative || null,
        content,
        JSON.stringify(metadata),
        obs.created_at,
      ]
    );

    memoryIds.push(rows[0].id);
    // Build embedding text: title + subtitle + content
    const textToEmbed = [obs.title, obs.subtitle, obs.text].filter(Boolean).join(": ");
    memoryTexts.push(textToEmbed || "(empty)");
    memoriesImported++;

    if (memoriesImported % 500 === 0) {
      console.log(`  ... ${memoriesImported}/${observations.length}`);
    }
  }
  console.log(`  Imported ${memoriesImported} memories`);

  // 4. Import session_summaries
  console.log("\n--- Importing session summaries ---");
  let summariesImported = 0;
  let summariesSkipped = 0;

  for (const ss of sessionSummaries) {
    const sessionPgId = sessionIdMap.get(ss.memory_session_id);
    if (!sessionPgId) {
      summariesSkipped++;
      continue;
    }

    // Build summary text from structured fields
    const summaryParts = [
      ss.request && `Request: ${ss.request}`,
      ss.investigated && `Investigated: ${ss.investigated}`,
      ss.learned && `Learned: ${ss.learned}`,
      ss.completed && `Completed: ${ss.completed}`,
      ss.next_steps && `Next steps: ${ss.next_steps}`,
    ].filter(Boolean);
    const summary = summaryParts.join("\n") || null;

    await pool.query(
      `INSERT INTO session_summaries (session_id, summary, request, investigated, learned, completed, next_steps,
                                      files_read, files_edited, notes, prompt_number, created_at)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)`,
      [
        sessionPgId,
        summary,
        ss.request,
        ss.investigated,
        ss.learned,
        ss.completed,
        ss.next_steps,
        ss.files_read ? JSON.stringify(ss.files_read) : null,
        ss.files_edited ? JSON.stringify(ss.files_edited) : null,
        ss.notes,
        ss.prompt_number,
        ss.created_at,
      ]
    );
    summariesImported++;
  }
  console.log(`  Imported ${summariesImported} session summaries (skipped ${summariesSkipped} with missing session)`);

  // 5. Batch re-embed
  if (!SKIP_EMBED && memoryTexts.length > 0) {
    console.log(`\n--- Embedding ${memoryTexts.length} memories with ${VOYAGE_MODEL} ---`);
    try {
      const embeddings = await embedBatch(memoryTexts);

      console.log(`  Writing embeddings to Postgres...`);
      for (let i = 0; i < embeddings.length; i++) {
        await pool.query(
          "UPDATE memories SET embedding = $1 WHERE id = $2",
          [toPgVector(embeddings[i]), memoryIds[i]]
        );

        if ((i + 1) % 500 === 0) {
          console.log(`  ... ${i + 1}/${embeddings.length}`);
        }
      }
      console.log(`  Embedded ${embeddings.length} memories`);
    } catch (err) {
      console.error("Embedding failed (data was still imported):", err);
      console.error("Re-run with --skip-embed to skip, or fix VOYAGE_API_KEY and retry.");
    }
  } else if (SKIP_EMBED) {
    console.log("\n--- Skipping embeddings (--skip-embed) ---");
  }

  // 6. Validate counts
  console.log("\n--- Validation ---");
  const { rows: memCount } = await pool.query("SELECT COUNT(*)::int AS count FROM memories");
  const { rows: sessCount } = await pool.query("SELECT COUNT(*)::int AS count FROM sessions");
  const { rows: sumCount } = await pool.query("SELECT COUNT(*)::int AS count FROM session_summaries");
  const { rows: embCount } = await pool.query("SELECT COUNT(*)::int AS count FROM memories WHERE embedding IS NOT NULL");
  const { rows: typeStats } = await pool.query("SELECT type, COUNT(*)::int AS count FROM memories GROUP BY type ORDER BY count DESC");

  console.log(`  Postgres memories:          ${memCount[0].count} (source: ${observations.length})`);
  console.log(`  Postgres sessions:          ${sessCount[0].count} (source: ${sdkSessions.length})`);
  console.log(`  Postgres session_summaries: ${sumCount[0].count} (source: ${sessionSummaries.length})`);
  console.log(`  Embedded memories:          ${embCount[0].count}`);
  console.log(`  Type distribution:          ${typeStats.map((r: any) => `${r.type}=${r.count}`).join(", ")}`);

  const memOk = memCount[0].count >= observations.length;
  const sessOk = sessCount[0].count >= sdkSessions.length;
  console.log(`  Status: ${memOk && sessOk ? "OK" : "MISMATCH - check data"}`);

  console.log("\nMigration complete.");
} finally {
  await pool.end();
}
