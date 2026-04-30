"""One-shot backfill script: unwrap double-encoded metadata from JSONB string/array to object.

Root cause: asyncpg has a JSONB codec that auto-encodes Python dicts. The data layer
previously also called json.dumps() manually before passing values to asyncpg, causing
double-encoding. Rows inserted during that period have metadata stored as a JSONB string
('"{\\"content_hash\\": ...}"') or occasionally a JSONB array instead of a JSONB object.

This script finds all such rows and re-stores metadata as a proper JSONB object.

Idempotent: rows that already have metadata of type 'object' are skipped.
Resumable: processes rows in batches and reports progress per batch.

Usage:
    DATABASE_URL=postgresql://... python scripts/migrate_unwrap_metadata.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 500


async def main() -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL environment variable is not set")
        sys.exit(1)

    conn = await asyncpg.connect(db_url)
    try:
        total_fixed = 0

        # Count how many rows need fixing
        problem_count: int = await conn.fetchval(
            "SELECT COUNT(*) FROM memories WHERE jsonb_typeof(metadata) != 'object'"
        )
        if problem_count == 0:
            logger.info("No rows need fixing — all metadata is already of type 'object'.")
            return

        logger.info("Found %d rows with non-object metadata. Starting backfill...", problem_count)

        # Process in batches using cursor pagination on id
        last_id = 0
        while True:
            rows = await conn.fetch(
                """
                SELECT id, metadata
                FROM memories
                WHERE id > $1
                  AND jsonb_typeof(metadata) != 'object'
                ORDER BY id
                LIMIT $2
                """,
                last_id,
                BATCH_SIZE,
            )
            if not rows:
                break

            batch_fixed = 0
            for row in rows:
                memory_id: int = row["id"]
                raw_metadata = row["metadata"]  # asyncpg decodes JSONB → Python object

                unwrapped: dict
                if isinstance(raw_metadata, str):
                    # Double-encoded: JSONB string contains a JSON-encoded dict
                    try:
                        parsed = json.loads(raw_metadata)
                    except json.JSONDecodeError:
                        logger.warning(
                            "id=%d: metadata is a string but not valid JSON, skipping: %r",
                            memory_id, raw_metadata,
                        )
                        continue
                    if not isinstance(parsed, dict):
                        logger.warning(
                            "id=%d: metadata string decodes to %s (not dict), skipping",
                            memory_id, type(parsed).__name__,
                        )
                        continue
                    unwrapped = parsed

                elif isinstance(raw_metadata, list):
                    # Rare case: JSONB array of JSON-encoded strings — merge all dicts
                    merged: dict = {}
                    for item in raw_metadata:
                        if isinstance(item, str):
                            try:
                                part = json.loads(item)
                            except json.JSONDecodeError:
                                logger.warning(
                                    "id=%d: array element is not valid JSON, skipping element: %r",
                                    memory_id, item,
                                )
                                continue
                            if isinstance(part, dict):
                                merged.update(part)
                        elif isinstance(item, dict):
                            merged.update(item)
                    unwrapped = merged

                else:
                    # Already an object or unexpected type — skip
                    logger.debug(
                        "id=%d: metadata type=%s, skipping", memory_id, type(raw_metadata).__name__
                    )
                    continue

                # Re-store as proper JSONB object
                await conn.execute(
                    "UPDATE memories SET metadata = $1::jsonb WHERE id = $2",
                    json.dumps(unwrapped),
                    memory_id,
                )
                batch_fixed += 1

            total_fixed += batch_fixed
            last_id = rows[-1]["id"]
            logger.info(
                "Batch up to id=%d: fixed %d rows (total so far: %d)",
                last_id, batch_fixed, total_fixed,
            )

        logger.info("Backfill complete. Total rows fixed: %d", total_fixed)

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
