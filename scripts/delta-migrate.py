#!/usr/bin/env python3
"""
Delta migration: Import new observations from claude-mem SQLite
that are not yet in open-brain Postgres (based on _sqlite_id in metadata).

Usage:
  DATABASE_URL=... VOYAGE_API_KEY=... python scripts/delta-migrate.py <sqlite-path>
  python scripts/delta-migrate.py <sqlite-path> --dry-run
"""

import asyncio
import json
import os
import sqlite3
import sys
import time

from datetime import datetime, timezone

import asyncpg
import httpx


def parse_dt(s: str | None) -> datetime | None:
    """Parse ISO timestamp string to datetime."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s)

# --- CLI ---
DRY_RUN = "--dry-run" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("-")]
SQLITE_PATH = args[0] if args else None

if not SQLITE_PATH:
    print("Usage: python scripts/delta-migrate.py <sqlite-path> [--dry-run]")
    sys.exit(1)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")
VOYAGE_MODEL = os.environ.get("VOYAGE_MODEL", "voyage-4")

if not DATABASE_URL and not DRY_RUN:
    print("Error: DATABASE_URL must be set (or use --dry-run)")
    sys.exit(1)

# --- Embedding ---

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
EMBEDDING_DIM = 1024
BATCH_SIZE = 64


def embed_batch(texts: list[str]) -> list[list[float]]:
    if not VOYAGE_API_KEY:
        raise RuntimeError("VOYAGE_API_KEY must be set")

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


def to_pg_vector(embedding: list[float]) -> str:
    return "[" + ",".join(str(x) for x in embedding) + "]"


async def main():
    # --- Read SQLite ---
    print(f"Opening SQLite: {SQLITE_PATH}")
    sdb = sqlite3.connect(SQLITE_PATH)
    sdb.row_factory = sqlite3.Row

    # --- Connect Postgres ---
    conn = None
    if not DRY_RUN:
        conn = await asyncpg.connect(DATABASE_URL)

    try:
        # Find max _sqlite_id already in Postgres
        max_imported_id = 0
        if conn:
            row = await conn.fetchval(
                "SELECT COALESCE(MAX((metadata->>'_sqlite_id')::int), 0) FROM memories "
                "WHERE metadata->>'_sqlite_id' IS NOT NULL"
            )
            max_imported_id = row or 0
        print(f"Max _sqlite_id in open-brain: {max_imported_id}")

        # Get delta
        delta = sdb.execute(
            "SELECT * FROM observations WHERE id > ? ORDER BY id", (max_imported_id,)
        ).fetchall()
        print(f"Delta observations to migrate: {len(delta)}")

        if not delta:
            print("Nothing to migrate.")
            return

        for obs in delta:
            print(f"  #{obs['id']} [{obs['created_at']}] {obs['project']}: {(obs['title'] or '')[:60]}")

        if DRY_RUN:
            print("\n--dry-run: No data written.")
            return

        assert conn is not None

        # Ensure memory_indexes for all projects in delta
        projects = list({obs["project"] for obs in delta})
        index_map: dict[str, int] = {}
        for project in projects:
            row = await conn.fetchrow(
                "INSERT INTO memory_indexes (name) VALUES ($1) "
                "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
                project,
            )
            index_map[project] = row["id"]

        # Ensure sessions exist
        session_ids = list({obs["memory_session_id"] for obs in delta})
        session_map: dict[str, int] = {}
        for sid in session_ids:
            row = await conn.fetchrow("SELECT id FROM sessions WHERE session_id = $1", sid)
            if row:
                session_map[sid] = row["id"]
            else:
                sess = sdb.execute(
                    "SELECT * FROM sdk_sessions WHERE memory_session_id = ?", (sid,)
                ).fetchone()
                if sess:
                    row = await conn.fetchrow(
                        "INSERT INTO sessions (session_id, index_id, project, started_at, ended_at, "
                        "status, prompt_counter, metadata) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
                        sess["memory_session_id"] or sess["content_session_id"],
                        index_map.get(sess["project"], 1),
                        sess["project"],
                        parse_dt(sess["started_at"]),
                        parse_dt(sess["completed_at"]),
                        sess["status"],
                        sess["prompt_counter"] or 0,
                        json.dumps({
                            "content_session_id": sess["content_session_id"],
                            "custom_title": sess["custom_title"],
                            "_sqlite_id": sess["id"],
                        }),
                    )
                    session_map[sid] = row["id"]

        # Import observations as memories
        memory_ids: list[int] = []
        memory_texts: list[str] = []

        for obs in delta:
            index_id = index_map.get(obs["project"], 1)
            session_pg_id = session_map.get(obs["memory_session_id"])
            content = obs["text"] or ""

            metadata: dict = {"_sqlite_id": obs["id"], "discovery_tokens": obs["discovery_tokens"]}
            if obs["content_hash"]:
                metadata["content_hash"] = obs["content_hash"]
            for field in ("files_read", "files_modified", "concepts", "facts"):
                val = obs[field]
                if val:
                    try:
                        metadata[field] = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        metadata[field] = val

            row = await conn.fetchrow(
                "INSERT INTO memories (index_id, session_id, type, title, subtitle, narrative, "
                "content, metadata, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id",
                index_id,
                session_pg_id,
                obs["type"],
                obs["title"],
                obs["subtitle"],
                obs["narrative"],
                content,
                json.dumps(metadata),
                parse_dt(obs["created_at"]),
            )
            memory_ids.append(row["id"])
            text_to_embed = ": ".join(
                filter(None, [obs["title"], obs["subtitle"], obs["text"]])
            ) or "(empty)"
            memory_texts.append(text_to_embed)

        print(f"\nImported {len(memory_ids)} memories")

        # Embed
        if VOYAGE_API_KEY:
            print(f"Embedding {len(memory_texts)} memories...")
            embeddings = embed_batch(memory_texts)
            for i, emb in enumerate(embeddings):
                await conn.execute(
                    "UPDATE memories SET embedding = $1::vector WHERE id = $2",
                    to_pg_vector(emb), memory_ids[i],
                )
            print(f"Embedded {len(embeddings)} memories")
        else:
            print("VOYAGE_API_KEY not set — skipping embeddings")

        # Validate
        total = await conn.fetchval("SELECT COUNT(*)::int FROM memories")
        print(f"\nTotal memories in open-brain: {total}")
        print("Delta migration complete.")

    finally:
        sdb.close()
        if conn:
            await conn.close()


asyncio.run(main())
