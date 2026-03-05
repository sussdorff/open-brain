/**
 * SQLite → Postgres migration for open-brain.
 *
 * Reads observations, sessions, and session_summaries from the claude-mem
 * SQLite database and imports them into the Postgres schema.
 *
 * Usage:
 *   DATABASE_URL=... VOYAGE_API_KEY=... tsx scripts/migrate-from-sqlite.ts <sqlite-path>
 *
 * Options:
 *   --dry-run       Print counts without writing to Postgres
 *   --skip-embed    Skip the embedding step (import data only)
 *   --export=<path> Export JSONL to file instead of importing
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
const EXPORT_PATH = process.argv.find((a) => a.startsWith("--export="))?.split("=")[1];

if (!SQLITE_PATH) {
  console.error("Usage: tsx scripts/migrate-from-sqlite.ts <sqlite-path>");
  console.error("  DATABASE_URL must be set (unless --export)");
  process.exit(1);
}

if (!DATABASE_URL && !EXPORT_PATH && !DRY_RUN) {
  console.error("Error: DATABASE_URL must be set (or use --export / --dry-run)");
  process.exit(1);
}

// ---------------------------------------------------------------------------
// Embedding helpers (inline to avoid importing config.ts which requires all env vars)
// ---------------------------------------------------------------------------

const VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings";
const VOYAGE_MODEL = "voyage-3-large";
const EMBEDDING_DIM = 1024;

async function embedBatch(texts: string[]): Promise<number[][]> {
  const VOYAGE_API_KEY = process.env.VOYAGE_API_KEY;
  if (!VOYAGE_API_KEY) throw new Error("VOYAGE_API_KEY must be set for embedding");

  const BATCH_SIZE = 64;
  const results: number[][] = [];

  for (let i = 0; i < texts.length; i += BATCH_SIZE) {
    const batch = texts.slice(i, i + BATCH_SIZE);
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

    if (i + BATCH_SIZE < texts.length) {
      await new Promise((r) => setTimeout(r, 200));
    }

    console.log(`  Embedded ${Math.min(i + BATCH_SIZE, texts.length)}/${texts.length}`);
  }

  return results;
}

function toPgVector(embedding: number[]): string {
  return `[${embedding.join(",")}]`;
}

// ---------------------------------------------------------------------------
// SQLite types
// ---------------------------------------------------------------------------

interface SqliteObservation {
  id: number;
  session_id: string | null;
  type: string | null;
  title: string | null;
  content: string;
  obs_type: string | null;
  project: string | null;
  file_path: string | null;
  metadata: string | null;
  created_at: string;
}

interface SqliteSession {
  id: number;
  session_id: string;
  project: string | null;
  started_at: string;
  ended_at: string | null;
}

interface SqliteSessionSummary {
  id: number;
  session_id: string;
  summary: string;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Read from SQLite
// ---------------------------------------------------------------------------

console.log(`Opening SQLite database: ${SQLITE_PATH}`);
const db = new Database(SQLITE_PATH, { readonly: true });

const observations = db.prepare("SELECT * FROM observations ORDER BY id").all() as SqliteObservation[];
const sessions = db.prepare("SELECT * FROM sessions ORDER BY id").all() as SqliteSession[];
const sessionSummaries = db.prepare("SELECT * FROM session_summaries ORDER BY id").all() as SqliteSessionSummary[];

console.log(`SQLite counts:`);
console.log(`  observations:      ${observations.length}`);
console.log(`  sessions:          ${sessions.length}`);
console.log(`  session_summaries: ${sessionSummaries.length}`);

db.close();

// Collect unique projects
const projects = new Set<string>();
for (const obs of observations) {
  if (obs.project) projects.add(obs.project);
}
for (const sess of sessions) {
  if (sess.project) projects.add(sess.project);
}
console.log(`  unique projects:   ${projects.size} (${[...projects].join(", ")})`);

// ---------------------------------------------------------------------------
// JSONL export mode
// ---------------------------------------------------------------------------

if (EXPORT_PATH) {
  const { writeFileSync } = await import("node:fs");
  const lines: string[] = [];

  for (const obs of observations) {
    lines.push(JSON.stringify({ _table: "observation", ...obs }));
  }
  for (const sess of sessions) {
    lines.push(JSON.stringify({ _table: "session", ...sess }));
  }
  for (const ss of sessionSummaries) {
    lines.push(JSON.stringify({ _table: "session_summary", ...ss }));
  }

  if (EXPORT_PATH === "-") {
    for (const line of lines) console.log(line);
  } else {
    writeFileSync(EXPORT_PATH, lines.join("\n") + "\n");
    console.log(`Exported ${lines.length} records to ${EXPORT_PATH}`);
  }
  process.exit(0);
}

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
  const indexMap = new Map<string, number>(); // project name → index_id

  // Ensure "default" exists
  const { rows: defaultRows } = await pool.query(
    "SELECT id FROM memory_indexes WHERE name = 'default'"
  );
  if (defaultRows.length > 0) {
    indexMap.set("default", defaultRows[0].id);
  }

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

  function getIndexId(project: string | null): number {
    if (!project) return indexMap.get("default") || 1;
    return indexMap.get(project) || indexMap.get("default") || 1;
  }

  // 2. Import sessions
  console.log("\n--- Importing sessions ---");
  const sessionIdMap = new Map<string, number>(); // sqlite session_id → postgres sessions.id

  for (const sess of sessions) {
    const indexId = getIndexId(sess.project);
    const { rows } = await pool.query(
      `INSERT INTO sessions (session_id, index_id, project, started_at, ended_at)
       VALUES ($1, $2, $3, $4, $5)
       ON CONFLICT (session_id) DO UPDATE SET ended_at = COALESCE(EXCLUDED.ended_at, sessions.ended_at)
       RETURNING id`,
      [sess.session_id, indexId, sess.project, sess.started_at, sess.ended_at]
    );
    sessionIdMap.set(sess.session_id, rows[0].id);
  }
  console.log(`  Imported ${sessionIdMap.size} sessions`);

  // 3. Import observations → memories
  console.log("\n--- Importing memories (observations) ---");
  let memoriesImported = 0;
  const memoryIds: number[] = [];
  const memoryTexts: string[] = [];

  for (const obs of observations) {
    const indexId = getIndexId(obs.project);
    const sessionPgId = obs.session_id ? sessionIdMap.get(obs.session_id) || null : null;
    const memType = obs.obs_type || obs.type || "observation";

    // Build metadata JSONB
    let metadata: Record<string, unknown> = {};
    if (obs.metadata) {
      try {
        metadata = JSON.parse(obs.metadata);
      } catch {
        // ignore parse errors
      }
    }
    if (obs.file_path) {
      metadata.filePath = obs.file_path;
    }
    // Preserve original sqlite id for traceability
    metadata._sqlite_id = obs.id;

    const { rows } = await pool.query(
      `INSERT INTO memories (index_id, session_id, type, title, content, metadata, created_at)
       VALUES ($1, $2, $3, $4, $5, $6, $7)
       RETURNING id`,
      [
        indexId,
        sessionPgId,
        memType,
        obs.title || null,
        obs.content,
        JSON.stringify(metadata),
        obs.created_at,
      ]
    );

    memoryIds.push(rows[0].id);
    const textToEmbed = [obs.title, obs.content].filter(Boolean).join(": ");
    memoryTexts.push(textToEmbed);
    memoriesImported++;

    if (memoriesImported % 100 === 0) {
      console.log(`  ... ${memoriesImported}/${observations.length}`);
    }
  }
  console.log(`  Imported ${memoriesImported} memories`);

  // 4. Import session_summaries
  console.log("\n--- Importing session summaries ---");
  let summariesImported = 0;

  for (const ss of sessionSummaries) {
    const sessionPgId = sessionIdMap.get(ss.session_id);
    if (!sessionPgId) {
      console.warn(`  Warning: session_id "${ss.session_id}" not found in session map, skipping summary`);
      continue;
    }

    await pool.query(
      `INSERT INTO session_summaries (session_id, summary, created_at)
       VALUES ($1, $2, $3)`,
      [sessionPgId, ss.summary, ss.created_at]
    );
    summariesImported++;
  }
  console.log(`  Imported ${summariesImported} session summaries`);

  // 5. Batch re-embed
  if (!SKIP_EMBED && memoryTexts.length > 0) {
    console.log(`\n--- Embedding ${memoryTexts.length} memories ---`);
    try {
      const embeddings = await embedBatch(memoryTexts);

      console.log(`  Writing embeddings to Postgres...`);
      for (let i = 0; i < embeddings.length; i++) {
        await pool.query(
          "UPDATE memories SET embedding = $1 WHERE id = $2",
          [toPgVector(embeddings[i]), memoryIds[i]]
        );

        if ((i + 1) % 100 === 0) {
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

  console.log(`  Postgres memories:          ${memCount[0].count} (source: ${observations.length})`);
  console.log(`  Postgres sessions:          ${sessCount[0].count} (source: ${sessions.length})`);
  console.log(`  Postgres session_summaries: ${sumCount[0].count} (source: ${sessionSummaries.length})`);

  const memOk = memCount[0].count >= observations.length;
  const sessOk = sessCount[0].count >= sessions.length;
  console.log(`  Status: ${memOk && sessOk ? "OK" : "MISMATCH - check data"}`);

  console.log("\nMigration complete.");
} finally {
  await pool.end();
}
