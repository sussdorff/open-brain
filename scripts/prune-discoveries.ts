/**
 * Prune low-quality discoveries from the Postgres memories table.
 *
 * Removes memories with type='discovery' that have fewer than N facts.
 * Facts are stored in metadata->'facts' as a JSONB array.
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

const pool = new pg.Pool({ connectionString: DATABASE_URL, max: 3 });

try {
  // Count facts from metadata->'facts' JSONB array
  const { rows } = await pool.query(
    `SELECT id, title,
            COALESCE(
              jsonb_array_length(
                CASE WHEN jsonb_typeof(metadata->'facts') = 'array'
                     THEN metadata->'facts'
                     ELSE '[]'::jsonb END
              ), 0
            ) AS fact_count,
            LEFT(COALESCE(title, content, ''), 80) AS preview
     FROM memories
     WHERE type = 'discovery'
     ORDER BY created_at ASC`
  );

  console.log(`Total discoveries: ${rows.length}`);
  console.log(`Min facts threshold: ${MIN_FACTS}`);
  console.log(`Mode: ${DRY_RUN ? "DRY RUN" : "FORCE DELETE"}\n`);

  const toPrune = rows.filter((r) => r.fact_count < MIN_FACTS);
  const toKeep = rows.filter((r) => r.fact_count >= MIN_FACTS);

  console.log(`Below threshold: ${toPrune.length}`);
  console.log(`Keeping:         ${toKeep.length}\n`);

  // Show sample of what gets pruned
  console.log("Sample pruned (first 10):");
  for (const item of toPrune.slice(0, 10)) {
    console.log(`  [id=${item.id}] facts=${item.fact_count} | ${item.preview}`);
  }

  // Show sample of what gets kept
  console.log("\nSample kept (first 10):");
  for (const item of toKeep.slice(0, 10)) {
    console.log(`  [id=${item.id}] facts=${item.fact_count} | ${item.preview}`);
  }

  // Fact distribution
  const factDist = new Map<number, number>();
  for (const r of rows) {
    factDist.set(r.fact_count, (factDist.get(r.fact_count) || 0) + 1);
  }
  console.log("\nFact count distribution:");
  for (const [count, num] of [...factDist.entries()].sort((a, b) => a[0] - b[0])) {
    const marker = count < MIN_FACTS ? "  PRUNE" : "";
    console.log(`  ${count} facts: ${num} discoveries${marker}`);
  }

  if (DRY_RUN) {
    console.log(`\nDry run — no deletions. Pass --force to delete.`);
  } else {
    const ids = toPrune.map((p) => p.id);

    // Delete in batches to avoid huge IN clauses
    const BATCH = 500;
    let deleted = 0;
    for (let i = 0; i < ids.length; i += BATCH) {
      const batch = ids.slice(i, i + BATCH);
      const placeholders = batch.map((_, j) => `$${j + 1}`).join(", ");

      await pool.query(`DELETE FROM memory_usage_log WHERE memory_id IN (${placeholders})`, batch);
      await pool.query(
        `DELETE FROM memory_relationships WHERE source_id IN (${placeholders}) OR target_id IN (${placeholders})`,
        [...batch, ...batch]
      );
      const result = await pool.query(`DELETE FROM memories WHERE id IN (${placeholders})`, batch);
      deleted += result.rowCount || 0;
      console.log(`  Deleted batch ${Math.min(i + BATCH, ids.length)}/${ids.length}`);
    }

    console.log(`\nDeleted ${deleted} low-quality discoveries.`);

    // Final count
    const { rows: remaining } = await pool.query("SELECT COUNT(*)::int AS c FROM memories");
    console.log(`Remaining memories: ${remaining[0].c}`);
  }
} finally {
  await pool.end();
}
