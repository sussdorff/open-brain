/**
 * Prune low-quality discoveries from the Postgres memories table.
 *
 * Removes memories with type='discovery' that contain fewer than N facts/sentences.
 *
 * Usage:
 *   DATABASE_URL=... tsx scripts/prune-discoveries.ts [--force] [--min-facts=3]
 *
 * Options:
 *   --force         Actually delete (default is dry-run)
 *   --min-facts=N   Minimum number of facts to keep (default: 3)
 */

import pg from "pg";

const DATABASE_URL = process.env.DATABASE_URL;
if (!DATABASE_URL) {
  console.error("Error: DATABASE_URL must be set");
  process.exit(1);
}

const DRY_RUN = !process.argv.includes("--force");
const MIN_FACTS = parseInt(
  process.argv.find((a) => a.startsWith("--min-facts="))?.split("=")[1] || "3",
  10
);

/**
 * Count "facts" in a text — roughly the number of meaningful sentences.
 * Splits on sentence-ending punctuation or newlines.
 */
function countFacts(text: string): number {
  return text
    .split(/[.!?\n]+/)
    .filter((s) => s.trim().length > 10)
    .length;
}

const pool = new pg.Pool({ connectionString: DATABASE_URL, max: 3 });

try {
  // Fetch all discoveries
  const { rows } = await pool.query(
    `SELECT id, title, content, created_at
     FROM memories
     WHERE type = 'discovery'
     ORDER BY created_at ASC`
  );

  console.log(`Total discoveries: ${rows.length}`);
  console.log(`Min facts threshold: ${MIN_FACTS}`);
  console.log(`Mode: ${DRY_RUN ? "DRY RUN" : "FORCE DELETE"}\n`);

  const toPrune: Array<{ id: number; title: string | null; facts: number; preview: string }> = [];

  for (const row of rows) {
    const facts = countFacts(row.content);
    if (facts < MIN_FACTS) {
      toPrune.push({
        id: row.id,
        title: row.title,
        facts,
        preview: row.content.slice(0, 80).replace(/\n/g, " "),
      });
    }
  }

  console.log(`Discoveries below threshold: ${toPrune.length}/${rows.length}\n`);

  if (toPrune.length === 0) {
    console.log("Nothing to prune.");
    process.exit(0);
  }

  // Print table
  for (const item of toPrune) {
    console.log(`  [id=${item.id}] facts=${item.facts} | ${item.title || "(no title)"} | ${item.preview}...`);
  }

  if (DRY_RUN) {
    console.log(`\nDry run — no deletions. Pass --force to delete.`);
  } else {
    const ids = toPrune.map((p) => p.id);
    const placeholders = ids.map((_, i) => `$${i + 1}`).join(", ");

    // Delete related usage logs first
    await pool.query(
      `DELETE FROM memory_usage_log WHERE memory_id IN (${placeholders})`,
      ids
    );

    // Delete related relationships
    await pool.query(
      `DELETE FROM memory_relationships WHERE source_id IN (${placeholders}) OR target_id IN (${placeholders})`,
      [...ids, ...ids]
    );

    // Delete the memories
    const result = await pool.query(
      `DELETE FROM memories WHERE id IN (${placeholders})`,
      ids
    );

    console.log(`\nDeleted ${result.rowCount} discoveries.`);
  }
} finally {
  await pool.end();
}
