#!/usr/bin/env python3
"""
Migrate learnings from ~/.claude/learnings/learnings.jsonl into open-brain.

Field mapping JSONL → open-brain memories:
  content        → content (required)
  id             → session_ref as 'lrn:<id>'  (idempotency key)
  extracted_at   → created_at (direct INSERT override)
  source.project → project  → resolved to index_id
  metadata JSON  → {status, confidence, feedback_type, scope,
                    affected_skills, content_hash, extracted_at,
                    materialized_to, discard_reason, source, ...all other fields}

Status mapping:
  open          → open
  materialized  → materialized
  discarded     → discarded
  <anything>    → open

All 367 entries are imported (incl. discarded/materialized as historical context).
Idempotency: session_ref = 'lrn:<id>' — duplicate runs skip existing entries.

Usage:
  DATABASE_URL=... VOYAGE_API_KEY=... python scripts/migrate_learnings.py [--dry-run]
  DATABASE_URL=... python scripts/migrate_learnings.py [--dry-run]   # skip embeddings
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import httpx

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DRY_RUN: bool = "--dry-run" in sys.argv

JSONL_PATH = Path.home() / ".claude" / "learnings" / "learnings.jsonl"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")
VOYAGE_MODEL = os.environ.get("VOYAGE_MODEL", "voyage-4")
VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
EMBEDDING_DIM = 1024
BATCH_SIZE = 64

STATUS_MAP = {
    "open": "open",
    "materialized": "materialized",
    "discarded": "discarded",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_dt(s: str | None) -> datetime | None:
    """Parse ISO timestamp string (with or without Z) to aware datetime."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def to_pg_vector(embedding: list[float]) -> str:
    return "[" + ",".join(str(x) for x in embedding) + "]"


def embed_batch(texts: list[str]) -> list[list[float]]:
    if not VOYAGE_API_KEY:
        raise RuntimeError("VOYAGE_API_KEY must be set for embedding")
    results: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        resp = httpx.post(
            VOYAGE_API_URL,
            headers={"Authorization": f"Bearer {VOYAGE_API_KEY}"},
            json={
                "model": VOYAGE_MODEL,
                "input": batch,
                "input_type": "document",
                "output_dimension": EMBEDDING_DIM,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        results.extend(d["embedding"] for d in data)
        if i + BATCH_SIZE < len(texts):
            time.sleep(0.2)
        print(f"  Embedded {min(i + BATCH_SIZE, len(texts))}/{len(texts)}")
    return results


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

# Fields that are stored at the top-level of the memory row (not in metadata).
_TOP_LEVEL_FIELDS = {"id", "content", "extracted_at"}


def map_entry(entry: dict) -> dict:
    """Map a JSONL entry to the fields needed for the DB INSERT.

    Returns a dict with keys:
      content, session_ref, project, created_at, metadata
    """
    raw_id: str = entry.get("id", "")
    # IDs in the JSONL may have various prefixes (lrn-, learn-, learning-, or hash-based).
    # Use them directly as session_ref for idempotency.
    # Fallback: generate from content_hash or content for entries without id.
    if raw_id:
        session_ref = raw_id
    else:
        content_hash = entry.get("content_hash", "")
        if content_hash:
            session_ref = f"lrn-{content_hash[:8]}"
        else:
            import hashlib
            content = entry.get("content", "")
            session_ref = f"lrn-{hashlib.sha256(content.encode()).hexdigest()[:8]}"

    content: str = entry.get("content", "")

    # Project: prefer source.project, fall back to top-level 'project'
    raw_source = entry.get("source")
    source: dict = raw_source if isinstance(raw_source, dict) else {}
    project: str | None = source.get("project") or entry.get("project") or None

    # created_at from extracted_at
    created_at: datetime | None = parse_dt(entry.get("extracted_at"))

    # Status mapping
    raw_status = entry.get("status", "unknown")
    mapped_status = STATUS_MAP.get(raw_status, "open")

    # Build metadata: all fields except the ones mapped to columns
    metadata: dict = {}

    # Explicit fields always included (even if None/absent)
    metadata["status"] = mapped_status
    metadata["confidence"] = entry.get("confidence")
    metadata["feedback_type"] = entry.get("feedback_type")
    metadata["scope"] = entry.get("scope")
    metadata["affected_skills"] = entry.get("affected_skills")
    metadata["content_hash"] = entry.get("content_hash")
    metadata["extracted_at"] = entry.get("extracted_at")
    metadata["materialized_to"] = entry.get("materialized_to")
    metadata["discard_reason"] = entry.get("discard_reason")
    # Store original source (dict or string) in metadata
    metadata["source"] = raw_source if raw_source else None

    # Include all remaining fields not already captured
    for key, value in entry.items():
        if key not in _TOP_LEVEL_FIELDS and key not in metadata and key != "source":
            metadata[key] = value

    # Remove None values to keep metadata tidy
    metadata = {k: v for k, v in metadata.items() if v is not None}

    return {
        "content": content,
        "session_ref": session_ref,
        "project": project,
        "created_at": created_at,
        "metadata": metadata,
    }


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def resolve_index_id(conn: asyncpg.Connection, project: str | None) -> int:
    """Resolve project name to memory_indexes.id, creating if needed. Falls back to 1."""
    if not project:
        return 1
    row = await conn.fetchrow("SELECT id FROM memory_indexes WHERE name = $1", project)
    if row:
        return row["id"]
    row = await conn.fetchrow(
        "INSERT INTO memory_indexes (name) VALUES ($1) RETURNING id", project
    )
    return row["id"]  # type: ignore[index]


async def get_existing_session_refs(conn: asyncpg.Connection) -> set[str]:
    """Return all session_refs already in the DB for type='learning'."""
    rows = await conn.fetch(
        "SELECT session_ref FROM memories WHERE type = 'learning' AND session_ref IS NOT NULL"
    )
    return {r["session_ref"] for r in rows}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run(jsonl_path: Path, dry_run: bool) -> None:
    if not jsonl_path.exists():
        print(f"Error: JSONL not found at {jsonl_path}")
        sys.exit(1)

    # Load entries
    entries: list[dict] = []
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  Warning: skipping malformed line: {e}")

    print(f"Loaded {len(entries)} entries from {jsonl_path}")

    # Map all entries
    mapped = [map_entry(e) for e in entries]

    if dry_run:
        # Show a summary of what would be imported
        status_dist: dict[str, int] = {}
        project_dist: dict[str, int] = {}
        for m in mapped:
            s = m["metadata"].get("status", "unknown")
            status_dist[s] = status_dist.get(s, 0) + 1
            p = m["project"] or "(none)"
            project_dist[p] = project_dist.get(p, 0) + 1

        print("\n[dry-run] Would import:")
        print(f"  Total entries: {len(mapped)}")
        print(f"  Status distribution: {status_dist}")
        print(f"  Top projects: {dict(sorted(project_dist.items(), key=lambda x: -x[1])[:10])}")
        print("\n  First 3 entries (mapped):")
        for m in mapped[:3]:
            print(f"    session_ref={m['session_ref']!r}")
            print(f"    project={m['project']!r}")
            print(f"    created_at={m['created_at']!r}")
            print(f"    content[:60]={m['content'][:60]!r}")
            print(f"    metadata keys={sorted(m['metadata'].keys())}")
            print()
        print("--dry-run: no data written.")
        return

    # Connect to DB
    if not DATABASE_URL:
        print("Error: DATABASE_URL must be set")
        sys.exit(1)

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Ensure session_ref column exists
        await conn.execute(
            "ALTER TABLE memories ADD COLUMN IF NOT EXISTS session_ref TEXT;"
        )

        # Find already-imported entries
        existing_refs = await get_existing_session_refs(conn)
        print(f"Already imported: {len(existing_refs)} learnings")

        to_import = [m for m in mapped if m["session_ref"] not in existing_refs]
        skipped = len(mapped) - len(to_import)
        print(f"Skipping {skipped} duplicates, importing {len(to_import)} new entries")

        if not to_import:
            print("Nothing to import.")
        else:
            # Resolve all projects
            projects = list({m["project"] for m in to_import})
            index_cache: dict[str | None, int] = {}
            for p in projects:
                index_cache[p] = await resolve_index_id(conn, p)

            memory_ids: list[int] = []
            memory_texts: list[str] = []

            for m in to_import:
                index_id = index_cache[m["project"]]
                metadata_json = json.dumps(m["metadata"])

                if m["created_at"] is not None:
                    row = await conn.fetchrow(
                        """INSERT INTO memories
                               (index_id, type, content, session_ref, metadata, created_at)
                           VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                           RETURNING id""",
                        index_id,
                        "learning",
                        m["content"],
                        m["session_ref"],
                        metadata_json,
                        m["created_at"],
                    )
                else:
                    row = await conn.fetchrow(
                        """INSERT INTO memories
                               (index_id, type, content, session_ref, metadata)
                           VALUES ($1, $2, $3, $4, $5::jsonb)
                           RETURNING id""",
                        index_id,
                        "learning",
                        m["content"],
                        m["session_ref"],
                        metadata_json,
                    )
                memory_ids.append(row["id"])
                memory_texts.append(m["content"] or "(empty)")

            print(f"Inserted {len(memory_ids)} memories")

            # Embed if API key available
            if VOYAGE_API_KEY:
                print(f"Embedding {len(memory_texts)} memories...")
                embeddings = embed_batch(memory_texts)
                for i, emb in enumerate(embeddings):
                    await conn.execute(
                        "UPDATE memories SET embedding = $1::vector WHERE id = $2",
                        to_pg_vector(emb),
                        memory_ids[i],
                    )
                print(f"Embedded {len(embeddings)} memories")
            else:
                print("VOYAGE_API_KEY not set — skipping embeddings")

        # Verify query: distribution by status
        print("\n--- Verify: distribution by status ---")
        rows = await conn.fetch(
            """SELECT metadata->>'status' AS status, COUNT(*)::int AS cnt
               FROM memories
               WHERE type = 'learning'
               GROUP BY 1
               ORDER BY 2 DESC"""
        )
        for r in rows:
            print(f"  {r['status'] or '(null)'}: {r['cnt']}")

        total = await conn.fetchval(
            "SELECT COUNT(*)::int FROM memories WHERE type = 'learning'"
        )
        print(f"\nTotal learning memories in open-brain: {total}")

        # Distribution by project
        print("\n--- Verify: top projects ---")
        proj_rows = await conn.fetch(
            """SELECT mi.name AS project, COUNT(*)::int AS cnt
               FROM memories m
               JOIN memory_indexes mi ON mi.id = m.index_id
               WHERE m.type = 'learning'
               GROUP BY 1
               ORDER BY 2 DESC
               LIMIT 10"""
        )
        for r in proj_rows:
            print(f"  {r['project']}: {r['cnt']}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run(JSONL_PATH, DRY_RUN))
