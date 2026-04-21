"""Migration: add last_decay_at column to memories table.

Run once against the production database:
    uv run python scripts/migrate_add_last_decay_at.py
"""

from __future__ import annotations

import asyncio
import os

import asyncpg


async def main() -> None:
    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("""
            ALTER TABLE memories
            ADD COLUMN IF NOT EXISTS last_decay_at TIMESTAMPTZ
        """)
        # Backfill: set last_decay_at = updated_at for rows that pre-date this migration,
        # to populate historical baseline so observability/reporting tools see non-NULL values.
        status = await conn.execute(
            "UPDATE memories SET last_decay_at = updated_at WHERE last_decay_at IS NULL"
        )
        count = int(status.split()[-1]) if status else 0
        print(f"Migration complete. Column added. Rows backfilled: {count}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
