"""One-shot migration: drop legacy CHECK constraint on memory_relationships.relation_type.

The legacy CHECK constraint memory_relationships_relation_type_check only allows:
  supports, contradicts, supersedes, similar_to, causes, example_of, generalizes, sequence_next

But the application inserts link_type values (e.g. attended_by, mentioned_in, spawned_task,
co_occurs) into BOTH the relation_type AND link_type columns. This CHECK constraint causes
silent failures for 4 of the 7 VALID_LINK_TYPES.

This migration drops the constraint idempotently. The link_type column (with its own
application-level validation) is the authoritative source of allowed values.

The migration is idempotent: a second run prints the same confirmation message and exits 0.

Usage:
    DATABASE_URL=postgresql://... python scripts/migrate_drop_relation_type_check.py
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
        await conn.execute(
            "ALTER TABLE memory_relationships DROP CONSTRAINT IF EXISTS memory_relationships_relation_type_check;"
        )
        print("Constraint dropped (or was already absent)")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
