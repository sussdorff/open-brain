"""One-shot backfill script: set link_type='similar_to' for any NULL rows.

After the ALTER TABLE migration adds the link_type column with DEFAULT 'similar_to',
existing rows will already have the default value set. This script runs an explicit
UPDATE for any rows that somehow have a NULL link_type (e.g. rows inserted via raw
SQL bypassing the column default).

The script is idempotent: a second run will print "Rows updated: 0".

Usage:
    DATABASE_URL=postgresql://... python scripts/migrate_relationships_backfill.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL environment variable is not set")
        sys.exit(1)

    conn = await asyncpg.connect(db_url)
    try:
        # Ensure the column exists (idempotent)
        await conn.execute(
            "ALTER TABLE memory_relationships ADD COLUMN IF NOT EXISTS link_type text NOT NULL DEFAULT 'similar_to';"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memrel_linktype ON memory_relationships(link_type);"
        )
        logger.info("Schema migration applied (idempotent).")

        # Backfill any rows with NULL link_type
        result = await conn.execute(
            "UPDATE memory_relationships SET link_type='similar_to' WHERE link_type IS NULL"
        )
        # asyncpg returns 'UPDATE N' as string
        rows_updated = int(result.split()[-1])
        print(f"Backfill complete. Rows updated: {rows_updated}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
