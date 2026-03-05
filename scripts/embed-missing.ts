/**
 * Embed all memories that don't have embeddings yet.
 *
 * Usage:
 *   DATABASE_URL=... VOYAGE_API_KEY=... VOYAGE_MODEL=... tsx scripts/embed-missing.ts
 */

import pg from "pg";

const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 5 });
const VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings";
const VOYAGE_MODEL = process.env.VOYAGE_MODEL || "voyage-4";
const VOYAGE_API_KEY = process.env.VOYAGE_API_KEY;
const BATCH_SIZE = 64;

if (!VOYAGE_API_KEY) {
  console.error("VOYAGE_API_KEY must be set");
  process.exit(1);
}

// Get all memories without embeddings
const { rows } = await pool.query(
  `SELECT id, COALESCE(title, '') || ': ' || COALESCE(subtitle, '') || ' ' || COALESCE(content, '') AS text
   FROM memories WHERE embedding IS NULL ORDER BY id`
);
console.log(`Total to embed: ${rows.length}`);

if (rows.length === 0) {
  console.log("Nothing to embed.");
  await pool.end();
  process.exit(0);
}

let embedded = 0;
for (let i = 0; i < rows.length; i += BATCH_SIZE) {
  const batch = rows.slice(i, i + BATCH_SIZE);
  const texts = batch.map((r) => r.text.slice(0, 8000)); // truncate for safety

  const res = await fetch(VOYAGE_API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${VOYAGE_API_KEY}`,
    },
    body: JSON.stringify({
      model: VOYAGE_MODEL,
      input: texts,
      input_type: "document",
      output_dimension: 1024,
    }),
  });

  if (!res.ok) {
    const body = await res.text();
    console.error(`Error ${res.status}: ${body}`);
    break;
  }

  const data = (await res.json()) as { data: Array<{ embedding: number[] }> };

  for (let j = 0; j < data.data.length; j++) {
    const vec = `[${data.data[j].embedding.join(",")}]`;
    await pool.query("UPDATE memories SET embedding = $1 WHERE id = $2", [vec, batch[j].id]);
  }

  embedded += batch.length;
  console.log(`Embedded ${embedded}/${rows.length}`);

  // Rate limit
  if (i + BATCH_SIZE < rows.length) {
    await new Promise((r) => setTimeout(r, 200));
  }
}

const { rows: count } = await pool.query(
  "SELECT COUNT(*)::int AS c FROM memories WHERE embedding IS NOT NULL"
);
console.log(`Done. Total embedded: ${count[0].c}`);
await pool.end();
