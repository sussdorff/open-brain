import "dotenv/config";
import pg from "pg";

const client = new pg.Client({ connectionString: process.env.DATABASE_URL });

async function main() {
  await client.connect();
  try {
    const {
      rows: [{ decay_unused_priorities: affected }],
    } = await client.query("SELECT decay_unused_priorities($1, $2)", [
      parseInt(process.argv[2] || "30"),
      parseFloat(process.argv[3] || "0.9"),
    ]);
    console.log(`Decayed priority for ${affected} unused memories`);
  } finally {
    await client.end();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
