import { readdir, readFile } from "node:fs/promises";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import pg from "pg";

const __dirname = dirname(fileURLToPath(import.meta.url));

async function migrate() {
  const databaseUrl = process.env.DATABASE_URL;
  if (!databaseUrl) {
    console.error("DATABASE_URL is required");
    process.exit(1);
  }

  const client = new pg.Client({ connectionString: databaseUrl });
  await client.connect();

  try {
    // Create migrations tracking table
    await client.query(`
      CREATE TABLE IF NOT EXISTS _migrations (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        applied_at TIMESTAMPTZ DEFAULT now()
      )
    `);

    // Get applied migrations
    const { rows: applied } = await client.query("SELECT name FROM _migrations ORDER BY name");
    const appliedSet = new Set(applied.map((r: { name: string }) => r.name));

    // Read migration files
    const migrationsDir = join(__dirname, "migrations");
    const files = (await readdir(migrationsDir))
      .filter(f => f.endsWith(".sql"))
      .sort();

    if (process.argv.includes("--verify")) {
      console.log(`Applied: ${appliedSet.size}/${files.length} migrations`);
      for (const file of files) {
        const status = appliedSet.has(file) ? "✓" : "✗";
        console.log(`  ${status} ${file}`);
      }
      const pending = files.filter(f => !appliedSet.has(f));
      if (pending.length > 0) {
        console.log(`\n${pending.length} pending migration(s)`);
        process.exit(1);
      }
      console.log("\nAll migrations applied.");
      return;
    }

    // Apply pending migrations
    let count = 0;
    for (const file of files) {
      if (appliedSet.has(file)) continue;

      console.log(`Applying ${file}...`);
      const sql = await readFile(join(migrationsDir, file), "utf-8");

      await client.query("BEGIN");
      try {
        await client.query(sql);
        await client.query("INSERT INTO _migrations (name) VALUES ($1)", [file]);
        await client.query("COMMIT");
        console.log(`  ✓ ${file}`);
        count++;
      } catch (err) {
        await client.query("ROLLBACK");
        console.error(`  ✗ ${file}: ${err}`);
        process.exit(1);
      }
    }

    if (count === 0) {
      console.log("No pending migrations.");
    } else {
      console.log(`\nApplied ${count} migration(s) successfully.`);
    }
  } finally {
    await client.end();
  }
}

migrate().catch(err => {
  console.error("Migration failed:", err);
  process.exit(1);
});
