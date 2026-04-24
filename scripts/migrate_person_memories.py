"""One-shot migration: port existing type=person memories to people-v1 schema.

Usage:
    DATABASE_URL=... python scripts/migrate_person_memories.py          # dry-run (default)
    DATABASE_URL=... python scripts/migrate_person_memories.py --apply  # execute migration
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import unicodedata
from typing import Any

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure logic functions (testable without DB)
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Convert arbitrary text to a URL-safe ASCII slug.

    Normalises unicode (e.g. Müller → Muller), lowercases, replaces
    non-alphanumeric runs with hyphens, and strips leading/trailing hyphens.
    """
    # Decompose unicode characters (Müller → Müller) then strip combining marks
    normalised = unicodedata.normalize("NFD", text)
    ascii_text = "".join(c for c in normalised if not unicodedata.combining(c))
    lower = ascii_text.lower()
    # Replace any non-alphanumeric char with a hyphen
    slug = re.sub(r"[^a-z0-9]+", "-", lower)
    return slug.strip("-")


def classify_memory(row: dict[str, Any]) -> str:
    """Return 'directory' or 'single' for a person memory row.

    Classification rules:
    - If metadata contains 'members' key (list of dicts) → 'directory'
    - If title contains 'directory' (case-insensitive) → 'directory'
    - Otherwise → 'single'

    Note: already-migrated detection is handled by plan_migration, not here.
    """
    metadata: dict[str, Any] = row.get("metadata") or {}
    members_val = metadata.get("members")
    if isinstance(members_val, list) and members_val and isinstance(members_val[0], dict):
        return "directory"

    title: str = row.get("title") or ""
    title_lower = title.lower()
    if "directory" in title_lower:
        return "directory"

    return "single"


def derive_person_ref(name: str, memory_id: int) -> str:
    """Derive a stable slug identifier for a person.

    Format: 'person-<last>-<first>[-<middle>...]'
    Falls back to 'person-<memory_id>' when name is empty.

    Examples:
        "Stefanie Weihe"      → "person-weihe-stefanie"
        "Maria Clara Schneider" → "person-schneider-maria-clara"
        "Madonna"             → "person-madonna"
    """
    stripped = name.strip()
    if not stripped:
        return f"person-{memory_id}"

    parts = stripped.split()
    if len(parts) == 1:
        return f"person-{_slugify(parts[0])}"

    # Convention: last word is the family name, rest are given names
    last = parts[-1]
    given = parts[:-1]
    components = [_slugify(last)] + [_slugify(g) for g in given]
    return "person-" + "-".join(components)


def plan_migration(row: dict[str, Any]) -> dict[str, Any]:
    """Return a migration plan dict for a single memory row.

    Returns one of:
        {"action": "skip",             "memory_id": int, "reason": str}
        {"action": "normalize",        "memory_id": int, "changes": dict}
        {"action": "split_directory",  "memory_id": int, "members": list[str],
         "archive_original": bool}
    """
    memory_id: int = row["id"]
    metadata: dict[str, Any] = row.get("metadata") or {}

    # Idempotency: skip already-migrated rows (both individual and archived)
    already_migrated = (
        metadata.get("schema_version") == "people-v1"
        or metadata.get("schema_version") == "people-v1-archived"
    )
    if already_migrated:
        return {
            "action": "skip",
            "memory_id": memory_id,
            "reason": f"already migrated (schema_version={metadata['schema_version']!r})",
        }

    kind = classify_memory(row)

    if kind == "directory":
        members: list[dict[str, Any]] = metadata.get("members") or []
        return {
            "action": "split_directory",
            "memory_id": memory_id,
            "members": members,
            "archive_original": True,
        }

    # Single-person normalisation — preserve any existing aliases
    name: str = metadata.get("name") or row.get("title") or ""
    person_ref = derive_person_ref(name, memory_id)
    existing_aliases: list[str] = metadata.get("aliases") or []
    changes: dict[str, Any] = {
        "person_ref": person_ref,
        "aliases": existing_aliases,
        "schema_version": "people-v1",
    }
    return {
        "action": "normalize",
        "memory_id": memory_id,
        "changes": changes,
    }


def format_dry_run_plan(plan: dict[str, Any]) -> str:
    """Format a migration plan as human-readable text for dry-run output."""
    action = plan["action"]
    memory_id = plan["memory_id"]

    if action == "skip":
        reason = plan.get("reason", "unknown")
        return f"[{memory_id}] SKIP — {reason}"

    if action == "normalize":
        changes = plan.get("changes", {})
        person_ref = changes.get("person_ref", "")
        aliases = changes.get("aliases", [])
        return (
            f"[{memory_id}] NORMALIZE → person_ref={person_ref!r}, "
            f"aliases={aliases!r}, schema_version='people-v1'"
        )

    if action == "split_directory":
        members = plan.get("members", [])
        member_names = [m["name"] if isinstance(m, dict) else m for m in members]
        member_list = ", ".join(member_names)
        return (
            f"[{memory_id}] SPLIT DIRECTORY into {len(members)} person memories "
            f"({member_list}) + archive original as curated_content"
        )

    return f"[{memory_id}] UNKNOWN action={action!r}"


# ---------------------------------------------------------------------------
# DB helpers (asyncpg)
# ---------------------------------------------------------------------------


async def fetch_person_memories(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Fetch all type=person memories from the database."""
    rows = await conn.fetch(
        "SELECT id, index_id, type, title, content, metadata FROM memories WHERE type = 'person' ORDER BY id"
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        metadata_raw = row["metadata"]
        if isinstance(metadata_raw, str):
            metadata = json.loads(metadata_raw)
        elif metadata_raw is None:
            metadata = {}
        else:
            metadata = dict(metadata_raw)
        result.append({
            "id": row["id"],
            "index_id": row["index_id"],
            "type": row["type"],
            "title": row["title"],
            "content": row["content"],
            "metadata": metadata,
        })
    return result


async def apply_normalize(conn: asyncpg.Connection, plan: dict[str, Any]) -> None:
    """Apply a normalize plan: merge new fields into metadata, update DB row."""
    memory_id = plan["memory_id"]
    changes = plan["changes"]

    async with conn.transaction():
        # Fetch current metadata
        row = await conn.fetchrow("SELECT metadata FROM memories WHERE id = $1", memory_id)
        if row is None:
            logger.warning(f"Memory {memory_id} not found; skipping.")
            return

        metadata_raw = row["metadata"]
        if isinstance(metadata_raw, str):
            metadata: dict[str, Any] = json.loads(metadata_raw)
        elif metadata_raw is None:
            metadata = {}
        else:
            metadata = dict(metadata_raw)

        metadata.update(changes)

        await conn.execute(
            "UPDATE memories SET metadata = $1 WHERE id = $2",
            json.dumps(metadata),
            memory_id,
        )
    logger.info(f"  Normalized memory {memory_id}: person_ref={changes['person_ref']!r}")


async def apply_split_directory(conn: asyncpg.Connection, plan: dict[str, Any], original_row: dict[str, Any]) -> None:
    """Apply a split_directory plan.

    Steps:
    1. For each member: INSERT a new type=person memory with people-v1 metadata.
    2. Archive the original directory memory by changing its type to curated_content.

    The entire operation is wrapped in a transaction for atomicity.
    """
    memory_id = plan["memory_id"]
    members: list[dict[str, Any]] = plan.get("members", [])
    original_metadata: dict[str, Any] = original_row.get("metadata") or {}
    index_id: int = original_row.get("index_id", 1) or 1

    async with conn.transaction():
        for member in members:
            member_name: str = member["name"]
            member_org: str = member.get("org") or ""
            aliases: list[str] = member.get("aliases") or []
            person_ref = derive_person_ref(member_name, memory_id)
            new_metadata: dict[str, Any] = {
                "name": member_name,
                "org": member_org,
                "linkedin": member.get("linkedin"),
                "person_ref": person_ref,
                "aliases": aliases,
                "schema_version": "people-v1",
                "split_from": memory_id,
            }
            title = member_name
            content = f"{member_name}, {member_org}" if member_org else member_name
            await conn.execute(
                """
                INSERT INTO memories (index_id, type, title, content, metadata, stability, importance, priority)
                VALUES ($1, 'person', $2, $3, $4, 'tentative', 'medium', 0.5)
                """,
                index_id,
                title,
                content,
                json.dumps(new_metadata),
            )
            logger.info(f"  Created person memory for {member_name!r} (ref={person_ref!r})")

        # Archive original as curated_content
        archive_metadata = dict(original_metadata)
        archive_metadata["archival_note"] = (
            f"Original directory archived after split into {len(members)} individual person memories."
        )
        archive_metadata["schema_version"] = "people-v1-archived"

        await conn.execute(
            "UPDATE memories SET type = 'curated_content', metadata = $1 WHERE id = $2",
            json.dumps(archive_metadata),
            memory_id,
        )
        logger.info(f"  Archived directory memory {memory_id} as curated_content")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate type=person memories to people-v1 schema."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Execute the migration. Without this flag the script runs in dry-run mode.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print migration plan without executing. This is the default when --apply is not given.",
    )
    args = parser.parse_args()

    # apply_mode is True only when --apply is explicitly passed.
    # --dry-run is the default; passing it is equivalent to omitting --apply.
    apply_mode: bool = args.apply

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL environment variable is not set")
        sys.exit(1)

    conn = await asyncpg.connect(db_url)
    try:
        rows = await fetch_person_memories(conn)
        logger.info(f"Found {len(rows)} type=person memories.")

        plans = [plan_migration(row) for row in rows]

        counts: dict[str, int] = {"normalize": 0, "split_directory": 0, "skip": 0}

        for row, plan in zip(rows, plans):
            action = plan["action"]
            counts[action] = counts.get(action, 0) + 1
            formatted = format_dry_run_plan(plan)

            if not apply_mode:
                print(formatted)
                continue

            # Apply mode
            if action == "skip":
                logger.info(f"  {formatted}")
            elif action == "normalize":
                await apply_normalize(conn, plan)
            elif action == "split_directory":
                await apply_split_directory(conn, plan, row)

        print(
            f"\nSummary: {counts.get('normalize', 0)} normalize, "
            f"{counts.get('split_directory', 0)} split_directory, "
            f"{counts.get('skip', 0)} skip"
        )
        if not apply_mode:
            print("\n(Dry-run mode — no changes made. Use --apply to execute.)")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
