"""Migration: add importance column to memories table.

Run once against the production database:
    uv run python scripts/migrate_add_importance.py
"""

import asyncio
import os

import asyncpg


async def main() -> None:
    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("""
            ALTER TABLE memories
            ADD COLUMN IF NOT EXISTS importance VARCHAR(8) NOT NULL DEFAULT 'medium'
            CHECK (importance IN ('critical', 'high', 'medium', 'low'))
        """)
        # Backfill: existing rows already get DEFAULT 'medium' via ALTER TABLE DEFAULT.
        # Explicit backfill as belt-and-suspenders for rows that pre-date the DEFAULT:
        status = await conn.execute("UPDATE memories SET importance = 'medium' WHERE importance IS NULL")
        # Parse count from status string like "UPDATE 0"
        count = int(status.split()[-1]) if status else 0
        print(f"Migration complete. Column added. Rows backfilled: {count}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
