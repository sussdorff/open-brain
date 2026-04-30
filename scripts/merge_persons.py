"""One-shot merge utility: merge two duplicate type=person memories.

Usage:
    DATABASE_URL=... python scripts/merge_persons.py --source <id> --target <id> [--dry-run] [--absorb-text]

Acceptance criteria:
- --dry-run prints exactly what would change without writing.
- Without --dry-run, the merge is performed inside a single transaction.
- All interactions/mentions previously pointing to source now point to target.
- All relationships previously pointing to source now point to target.
- Target's aliases include source's primary name and any of its aliases.
- Source memory gets metadata.merged_into=<target_id> and metadata.merged_at.
- Idempotent: re-running the same merge is a no-op.
- Refuses to merge if source.type != 'person' or target.type != 'person'.
- Refuses to merge if source.id == target.id.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure logic functions (testable without DB)
# ---------------------------------------------------------------------------


def validate_pair(source_row: dict[str, Any], target_row: dict[str, Any]) -> list[str]:
    """Return list of validation errors. Empty list means the pair is valid.

    Checks:
    - source.type must be 'person'
    - target.type must be 'person'
    - source.id must not equal target.id

    Args:
        source_row: The source memory dict (to be merged into target).
        target_row: The target memory dict (to keep).

    Returns:
        List of human-readable error strings. Empty if valid.
    """
    errors: list[str] = []
    if source_row.get("type") != "person":
        errors.append(
            f"source memory {source_row.get('id')} has type={source_row.get('type')!r}; "
            "expected 'person'"
        )
    if target_row.get("type") != "person":
        errors.append(
            f"target memory {target_row.get('id')} has type={target_row.get('type')!r}; "
            "expected 'person'"
        )
    if source_row.get("id") == target_row.get("id"):
        errors.append(
            f"source and target are identical (id={source_row.get('id')}); "
            "cannot merge a memory into itself"
        )
    return errors


def compute_merged_aliases(
    source_row: dict[str, Any], target_row: dict[str, Any]
) -> list[str]:
    """Return deduplicated alias list for target after merge.

    Merges: target's existing aliases + source's primary name + source's aliases.
    Deduplication is case-insensitive; original casing of the first occurrence is kept.

    Args:
        source_row: The source memory dict.
        target_row: The target memory dict.

    Returns:
        Deduplicated list of alias strings.
    """
    target_meta = target_row.get("metadata") or {}
    source_meta = source_row.get("metadata") or {}

    target_existing: list[str] = target_meta.get("aliases") or []
    source_name: str = display_name(source_row)
    source_aliases: list[str] = source_meta.get("aliases") or []

    # Build deduplicated list: target aliases first, then source name, then source aliases
    seen_lower: set[str] = set()
    result: list[str] = []

    def add_alias(alias: str) -> None:
        stripped = alias.strip()
        if not stripped:
            return
        key = stripped.lower()
        if key not in seen_lower:
            seen_lower.add(key)
            result.append(stripped)

    for alias in target_existing:
        add_alias(alias)
    if source_name:
        add_alias(source_name)
    for alias in source_aliases:
        add_alias(alias)

    return result


def is_already_merged(source_row: dict[str, Any], target_id: int) -> bool:
    """Return True if source already has merged_into=target_id.

    Supports both integer and string stored values for merged_into.

    Args:
        source_row: The source memory dict.
        target_id: The numeric ID of the target memory.

    Returns:
        True if already merged into the specified target, False otherwise.
    """
    meta = source_row.get("metadata") or {}
    merged_into = meta.get("merged_into")
    if merged_into is None:
        return False
    try:
        return int(merged_into) == int(target_id)
    except (TypeError, ValueError):
        return False


def name_length_warning(
    source_row: dict[str, Any], target_row: dict[str, Any]
) -> str | None:
    """Return warning string if target name is shorter than source name, else None.

    A shorter target name may indicate the merge direction is wrong (e.g. merging
    "Stephan Weihe" into "S. Weihe" when it should be the other way around).

    Args:
        source_row: The source memory dict.
        target_row: The target memory dict.

    Returns:
        Warning string if target name is strictly shorter, None otherwise.
    """
    source_name = display_name(source_row)
    target_name = display_name(target_row)

    if len(target_name) < len(source_name):
        return (
            f"WARNING: target name {target_name!r} (len={len(target_name)}) is shorter than "
            f"source name {source_name!r} (len={len(source_name)}). "
            "Consider swapping source and target — the longer/more complete name usually "
            "belongs to the target (the memory that survives the merge)."
        )
    return None


def display_name(row: dict[str, Any]) -> str:
    """Return the display name for a memory row.

    Args:
        row: Memory dict with optional metadata, title, id keys.

    Returns:
        Best available name string, or empty string if none found.
    """
    return (row.get("metadata") or {}).get("name") or row.get("title") or ""


def format_dry_run_report(
    source_row: dict[str, Any],
    target_row: dict[str, Any],
    interaction_count: int,
    relationship_count: int,
    absorb_text: bool = False,
) -> str:
    """Return human-readable dry-run report showing what would change.

    Args:
        source_row: The source memory dict.
        target_row: The target memory dict.
        interaction_count: Number of interaction/mention rows that would be re-pointed.
        relationship_count: Number of relationship rows that would be updated.
        absorb_text: If True, include absorption line in report.

    Returns:
        Multi-line human-readable report string.
    """
    source_name = display_name(source_row) or str(source_row.get("id"))
    target_name = display_name(target_row) or str(target_row.get("id"))
    source_id = source_row.get("id")
    target_id = target_row.get("id")

    merged_aliases = compute_merged_aliases(source_row, target_row)
    warning = name_length_warning(source_row, target_row)

    lines = [
        "=== DRY RUN — no changes will be made ===",
        "",
        f"Source: [{source_id}] {source_name!r}",
        f"Target: [{target_id}] {target_name!r}",
        "",
        f"Would re-point {interaction_count} interaction/mention rows "
        f"(metadata.person_ref → target)",
        f"Would update {relationship_count} relationship rows "
        f"(source_id/target_id → target)",
        f"Would set target aliases to: {merged_aliases!r}",
        f"Would soft-delete source: metadata.merged_into={target_id}, metadata.merged_at=<now>",
    ]
    if absorb_text:
        source_content = source_row.get("content") or ""
        lines.append(
            f"Would absorb source content into target ({len(source_content)} chars)"
        )
    if warning:
        lines.append("")
        lines.append(warning)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Async DB helpers
# ---------------------------------------------------------------------------


def _parse_row_count(result: str) -> int:
    """Parse row count from asyncpg execute() result string (e.g. 'UPDATE 3').

    Args:
        result: The string returned by asyncpg Connection.execute().

    Returns:
        Parsed integer row count, or 0 if parsing fails.
    """
    parts = result.split() if isinstance(result, str) else []
    if len(parts) >= 2 and parts[-1].isdigit():
        return int(parts[-1])
    logger.warning("Could not parse row count from asyncpg result: %r", result)
    return 0


async def _init_conn(conn: asyncpg.Connection) -> None:
    """Register JSONB codec on the connection.

    Args:
        conn: asyncpg connection to configure.
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def fetch_memory(conn: asyncpg.Connection, memory_id: int) -> dict[str, Any] | None:
    """Fetch a single memory row by ID.

    Args:
        conn: asyncpg connection.
        memory_id: Numeric memory ID.

    Returns:
        Dict with id, type, title, content, metadata keys, or None if not found.
    """
    row = await conn.fetchrow(
        "SELECT id, type, title, content, metadata FROM memories WHERE id = $1",
        memory_id,
    )
    if row is None:
        return None
    metadata_raw = row["metadata"]
    if isinstance(metadata_raw, str):
        metadata = json.loads(metadata_raw)
    elif metadata_raw is None:
        metadata = {}
    else:
        metadata = dict(metadata_raw)
    return {
        "id": row["id"],
        "type": row["type"],
        "title": row["title"],
        "content": row["content"],
        "metadata": metadata,
    }


async def count_person_ref_rows(conn: asyncpg.Connection, source_person_ref: str) -> int:
    """Count interactions/mentions pointing to source.

    Args:
        conn: asyncpg connection.
        source_person_ref: The person_ref string stored in interaction/mention metadata.

    Returns:
        Number of matching rows.
    """
    result = await conn.fetchval(
        "SELECT COUNT(*) FROM memories "
        "WHERE type IN ('interaction', 'mention') "
        "AND metadata->>'person_ref' = $1",
        source_person_ref,
    )
    return int(result or 0)


async def count_relationship_rows(conn: asyncpg.Connection, source_id: int) -> int:
    """Count relationship rows referencing source memory.

    Args:
        conn: asyncpg connection.
        source_id: Numeric ID of the source memory.

    Returns:
        Number of matching rows.
    """
    result = await conn.fetchval(
        "SELECT COUNT(*) FROM memory_relationships "
        "WHERE source_id = $1 OR target_id = $1",
        source_id,
    )
    return int(result or 0)


async def repoint_person_refs(
    conn: asyncpg.Connection, source_person_ref: str, target_person_ref: str
) -> int:
    """UPDATE interactions/mentions with person_ref=source to use target.

    Args:
        conn: asyncpg connection.
        source_person_ref: The person_ref value to replace.
        target_person_ref: The person_ref value to set.

    Returns:
        Number of rows updated.
    """
    result = await conn.execute(
        "UPDATE memories "
        "SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('person_ref', $2::text) "
        "WHERE type IN ('interaction', 'mention') "
        "AND metadata->>'person_ref' = $1",
        source_person_ref,
        target_person_ref,
    )
    # asyncpg returns "UPDATE N" string
    return _parse_row_count(result)


async def repoint_relationships(
    conn: asyncpg.Connection, source_id: int, target_id: int
) -> int:
    """UPDATE memory_relationships rows referencing source_id.

    Before updating, deletes self-loops and collision rows to avoid violating
    the UNIQUE constraint on (source_id, target_id, relation_type).

    Steps:
    1. Delete self-loops: source<->target edges that would become self-referential.
    2. Delete source_id collision rows: source rows that would duplicate existing target rows.
    3. Delete target_id collision rows: source rows that would duplicate existing target rows.
    4. UPDATE remaining rows to point to target.

    Args:
        conn: asyncpg connection.
        source_id: Numeric ID of the source memory.
        target_id: Numeric ID of the target memory.

    Returns:
        Total rows affected (deleted + updated).
    """
    total_affected = 0

    # Step 1: Delete self-loops: source<->target edges
    r1 = await conn.execute(
        """
        DELETE FROM memory_relationships
        WHERE (source_id = $1 AND target_id = $2)
           OR (source_id = $2 AND target_id = $1)
        """,
        source_id,
        target_id,
    )
    total_affected += _parse_row_count(r1)

    # Step 2: Delete collision rows for source_id column
    r2 = await conn.execute(
        """
        DELETE FROM memory_relationships
        WHERE source_id = $1
          AND EXISTS (
            SELECT 1 FROM memory_relationships r2
            WHERE r2.source_id = $2
              AND r2.target_id = memory_relationships.target_id
              AND r2.relation_type = memory_relationships.relation_type
          )
        """,
        source_id,
        target_id,
    )
    total_affected += _parse_row_count(r2)

    # Step 3: Delete collision rows for target_id column
    r3 = await conn.execute(
        """
        DELETE FROM memory_relationships
        WHERE target_id = $1
          AND EXISTS (
            SELECT 1 FROM memory_relationships r2
            WHERE r2.target_id = $2
              AND r2.source_id = memory_relationships.source_id
              AND r2.relation_type = memory_relationships.relation_type
          )
        """,
        source_id,
        target_id,
    )
    total_affected += _parse_row_count(r3)

    # Step 4: Now UPDATE won't hit any unique constraint violations
    r4 = await conn.execute(
        """
        UPDATE memory_relationships
        SET source_id = CASE WHEN source_id = $1 THEN $2 ELSE source_id END,
            target_id = CASE WHEN target_id = $1 THEN $2 ELSE target_id END
        WHERE source_id = $1 OR target_id = $1
        """,
        source_id,
        target_id,
    )
    total_affected += _parse_row_count(r4)

    return total_affected


async def update_target_aliases(
    conn: asyncpg.Connection, target_id: int, merged_aliases: list[str]
) -> None:
    """Update target's metadata.aliases.

    Args:
        conn: asyncpg connection.
        target_id: Numeric ID of the target memory.
        merged_aliases: New alias list to set.
    """
    await conn.execute(
        "UPDATE memories "
        "SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('aliases', $2::jsonb) "
        "WHERE id = $1",
        target_id,
        json.dumps(merged_aliases),
    )


async def soft_delete_source(
    conn: asyncpg.Connection, source_id: int, target_id: int
) -> None:
    """Mark source as merged_into=target_id with merged_at=<now ISO>.

    Args:
        conn: asyncpg connection.
        source_id: Numeric ID of the source memory to soft-delete.
        target_id: Numeric ID of the target memory.
    """
    merged_at = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "UPDATE memories "
        "SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('merged_into', $2::int, 'merged_at', $3::text) "
        "WHERE id = $1",
        source_id,
        target_id,
        merged_at,
    )


async def absorb_text_into_target(
    conn: asyncpg.Connection, source_row: dict[str, Any], target_row: dict[str, Any]
) -> None:
    """Append source's text/content to target's content as provenance note.

    Args:
        conn: asyncpg connection.
        source_row: The source memory dict.
        target_row: The target memory dict (avoids re-fetching from DB).
    """
    source_id = source_row.get("id")
    source_name = display_name(source_row)
    source_content = source_row.get("content") or ""
    target_id = target_row.get("id")

    current_content = target_row.get("content") or ""
    provenance = (
        f"\n\n--- Absorbed from [{source_id}] {source_name!r} ---\n{source_content}"
    )
    new_content = current_content + provenance

    await conn.execute(
        "UPDATE memories SET content = $1 WHERE id = $2",
        new_content,
        target_id,
    )


async def run_dry_run(
    conn: asyncpg.Connection, source_id: int, target_id: int, absorb_text: bool = False
) -> str:
    """Execute dry-run: fetch data, count what would change, return report.

    No write operations are performed.

    Args:
        conn: asyncpg connection.
        source_id: Numeric ID of the source memory.
        target_id: Numeric ID of the target memory.
        absorb_text: If True, include absorption line in report.

    Returns:
        Human-readable dry-run report string.
    """
    source_row = await fetch_memory(conn, source_id)
    target_row = await fetch_memory(conn, target_id)

    if source_row is None:
        return f"ERROR: source memory {source_id} not found."
    if target_row is None:
        return f"ERROR: target memory {target_id} not found."

    if is_already_merged(source_row, target_id):
        return (
            f"SKIP: source [{source_id}] is already merged into target [{target_id}] — no-op."
        )

    errors = validate_pair(source_row, target_row)
    if errors:
        return "VALIDATION ERRORS:\n" + "\n".join(f"  - {e}" for e in errors)

    source_meta = source_row.get("metadata") or {}
    source_person_ref = source_meta.get("person_ref") or str(source_id)

    interaction_count = await count_person_ref_rows(conn, source_person_ref)
    relationship_count = await count_relationship_rows(conn, source_id)

    return format_dry_run_report(
        source_row, target_row, interaction_count, relationship_count, absorb_text=absorb_text
    )


async def do_merge(
    conn: asyncpg.Connection,
    source_id: int,
    target_id: int,
    absorb_text: bool = False,
) -> dict[str, Any]:
    """Execute the full merge in a single transaction.

    Steps:
    1. Fetch source and target rows.
    2. Validate pair (type checks, same-id check).
    3. Check idempotency (already merged → skip).
    4. Re-point interaction/mention person_refs.
    5. Re-point relationship rows.
    6. Update target aliases.
    7. Optionally absorb source text into target.
    8. Soft-delete source.

    Args:
        conn: asyncpg connection (JSONB codec must be registered).
        source_id: Numeric ID of the source memory to merge from.
        target_id: Numeric ID of the target memory to merge into.
        absorb_text: If True, append source's content to target's content.

    Returns:
        Summary dict with keys: status, interactions_updated, relationships_updated,
        aliases_updated, source_soft_deleted.
    """
    source_row = await fetch_memory(conn, source_id)
    target_row = await fetch_memory(conn, target_id)

    if source_row is None:
        raise ValueError(f"Source memory {source_id} not found.")
    if target_row is None:
        raise ValueError(f"Target memory {target_id} not found.")

    if is_already_merged(source_row, target_id):
        logger.info(f"[{source_id}] already merged into [{target_id}] — no-op.")
        return {
            "status": "skipped",
            "reason": "already_merged",
            "interactions_updated": 0,
            "relationships_updated": 0,
            "aliases_updated": False,
            "source_soft_deleted": False,
        }

    errors = validate_pair(source_row, target_row)
    if errors:
        raise ValueError("Validation failed:\n" + "\n".join(f"  - {e}" for e in errors))

    # Warn about potential wrong merge direction
    warning = name_length_warning(source_row, target_row)
    if warning:
        logger.warning(warning)

    source_meta = source_row.get("metadata") or {}
    source_person_ref = source_meta.get("person_ref") or str(source_id)
    target_meta = target_row.get("metadata") or {}
    target_person_ref = target_meta.get("person_ref") or str(target_id)

    merged_aliases = compute_merged_aliases(source_row, target_row)

    async with conn.transaction():
        # Step 1: Re-point interaction/mention person_refs
        interactions_updated = await repoint_person_refs(
            conn, source_person_ref, target_person_ref
        )
        logger.info(
            f"  Re-pointed {interactions_updated} interaction/mention rows: "
            f"person_ref {source_person_ref!r} → {target_person_ref!r}"
        )

        # Step 2: Re-point relationship rows
        relationships_updated = await repoint_relationships(conn, source_id, target_id)
        logger.info(
            f"  Updated {relationships_updated} relationship rows: "
            f"source_id/target_id {source_id} → {target_id}"
        )

        # Step 3: Update target aliases
        await update_target_aliases(conn, target_id, merged_aliases)
        logger.info(f"  Updated target [{target_id}] aliases: {merged_aliases!r}")

        # Step 4: Optionally absorb source text
        if absorb_text:
            await absorb_text_into_target(conn, source_row, target_row)
            logger.info(f"  Absorbed source [{source_id}] content into target [{target_id}]")

        # Step 5: Soft-delete source
        await soft_delete_source(conn, source_id, target_id)
        logger.info(
            f"  Soft-deleted source [{source_id}]: "
            f"merged_into={target_id}, merged_at=<now>"
        )

    return {
        "status": "merged",
        "source_id": source_id,
        "target_id": target_id,
        "interactions_updated": interactions_updated,
        "relationships_updated": relationships_updated,
        "aliases_updated": True,
        "source_soft_deleted": True,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    """CLI entry point for merge_persons script."""
    parser = argparse.ArgumentParser(
        description="Merge two duplicate type=person memories (source → target)."
    )
    parser.add_argument(
        "--source",
        type=int,
        required=True,
        help="Memory ID of the source person (will be soft-deleted).",
    )
    parser.add_argument(
        "--target",
        type=int,
        required=True,
        help="Memory ID of the target person (will be kept and enriched).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would change without executing.",
    )
    parser.add_argument(
        "--absorb-text",
        action="store_true",
        default=False,
        help="Append source content to target content as provenance.",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL environment variable is not set")
        sys.exit(1)

    conn = await asyncpg.connect(db_url)
    try:
        await _init_conn(conn)

        if args.dry_run:
            report = await run_dry_run(conn, args.source, args.target, absorb_text=args.absorb_text)
            print(report)
        else:
            summary = await do_merge(conn, args.source, args.target, absorb_text=args.absorb_text)
            print("\n=== Merge complete ===")
            for key, value in summary.items():
                print(f"  {key}: {value}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
