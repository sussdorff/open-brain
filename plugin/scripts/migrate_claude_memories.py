#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncpg",
#   "httpx",
# ]
# ///
"""
Migrate Claude Code memory files (~/.claude/projects/*/memory/*.md) into open-brain.

Each memory file has YAML frontmatter (name, description, type) and a markdown body.
The folder name encodes the project path (e.g. -Users-malte-code-mira -> mira).

Field mapping:
  frontmatter.name        -> title
  frontmatter.description -> subtitle
  frontmatter.type        -> type (feedback, project, decision, reference, domain)
  markdown body           -> content (text)
  decoded project         -> project -> resolved to index_id
  metadata JSON           -> {source, memory_type, original_path, migration_date, content_hash}

Idempotency: session_ref = 'ccmem:<sha256(original_path)[:12]>'
  - Duplicate runs skip existing entries by session_ref.
  - Changed content is NOT re-imported automatically (same path = same ref).
    To re-import a changed file, delete the old memory first.

Config: reads ~/.config/open-brain/migrate.toml (XDG), falls back to env vars.
  The config file may contain credentials; ensure it is chmod 600.

Usage:
  python scripts/migrate_claude_memories.py --dry-run
  python scripts/migrate_claude_memories.py
  python scripts/migrate_claude_memories.py --project mira
  python scripts/migrate_claude_memories.py --config /path/to/config.toml
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path

import asyncpg
import httpx

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DRY_RUN: bool = "--dry-run" in sys.argv
PROJECT_FILTER: str | None = None
CONFIG_PATH: Path | None = None

for i, arg in enumerate(sys.argv):
    if arg == "--project" and i + 1 < len(sys.argv):
        PROJECT_FILTER = sys.argv[i + 1]
    if arg == "--config" and i + 1 < len(sys.argv):
        CONFIG_PATH = Path(sys.argv[i + 1])

# ---------------------------------------------------------------------------
# Config (XDG -> env vars -> defaults)
# ---------------------------------------------------------------------------

_XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
_DEFAULT_CONFIG = _XDG_CONFIG_HOME / "open-brain" / "migrate.toml"


def _load_config() -> dict[str, str]:
    """Load config from TOML file. Returns flat dict of key=value strings."""
    config_path = CONFIG_PATH or _DEFAULT_CONFIG
    if not config_path.exists():
        return {}

    # Warn if config file is readable by group/others (may contain credentials)
    try:
        mode = config_path.stat().st_mode
        if mode & 0o077:
            print(f"Warning: {config_path} is readable by others (mode {oct(mode)}). "
                  f"Run: chmod 600 {config_path}")
    except OSError:
        pass

    config: dict[str, str] = {}
    try:
        # stdlib tomllib available in Python 3.11+
        import tomllib

        with open(config_path, "rb") as f:
            data = tomllib.load(f)

        # Flatten: top-level keys and [section] keys both become flat
        for key, val in data.items():
            if isinstance(val, dict):
                for sub_key, sub_val in val.items():
                    config[f"{key}.{sub_key}"] = str(sub_val)
            else:
                config[key] = str(val)

        print(f"Loaded config from {config_path}")
    except Exception as e:
        print(f"Warning: could not parse {config_path}: {e}")

    return config


def _cfg(config: dict[str, str], toml_key: str, env_key: str, default: str = "") -> str:
    """Resolve value: TOML config -> env var -> default."""
    return config.get(toml_key) or os.environ.get(env_key, default)


_config = _load_config()

CLAUDE_PROJECTS_DIR = Path(
    _cfg(_config, "claude.projects_dir", "CLAUDE_PROJECTS_DIR",
         str(Path.home() / ".claude" / "projects"))
)
DATABASE_URL = _cfg(_config, "database.url", "DATABASE_URL")
VOYAGE_API_KEY = _cfg(_config, "voyage.api_key", "VOYAGE_API_KEY")
VOYAGE_MODEL = _cfg(_config, "voyage.model", "VOYAGE_MODEL", "voyage-4")
VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
EMBEDDING_DIM = 1024
BATCH_SIZE = 64

# Skip files matching these names
SKIP_FILES = {"MEMORY.md", ".consolidate-lock"}

# Skip files whose body contains these markers (merged stubs)
MERGED_MARKERS = ["Merged into", "MERGED"]

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# Dynamically derive the user prefix from $HOME
_HOME_PREFIX = "-" + str(Path.home()).replace("/", "-").lstrip("-") + "-code-"
_HOME_BARE = "-" + str(Path.home()).replace("/", "-").lstrip("-")


def decode_project_name(folder_name: str) -> str:
    """Decode project folder name to a short project name.

    Dynamically uses $HOME to derive the prefix, so this works on any machine.

    Examples (on /Users/malte):
      -Users-malte-code-mira           -> mira
      -Users-malte-code-ai-beads       -> ai-beads
      -Users-malte-code-open-brain     -> open-brain
      -Users-malte                     -> home
      -Users-malte-Documents-cognovis-Kunden-ATR -> Kunden-ATR
    """
    # ~/code/<project>
    if folder_name.startswith(_HOME_PREFIX):
        remainder = folder_name[len(_HOME_PREFIX):]
        return remainder if remainder else "code"

    # ~/Documents/ or other subdirs
    docs_prefix = _HOME_BARE + "-Documents-"
    if folder_name.startswith(docs_prefix):
        remainder = folder_name[len(docs_prefix):]
        # Keep last 2 meaningful segments (e.g. cognovis-Kunden-ATR -> Kunden-ATR)
        parts = remainder.split("-")
        return "-".join(parts[-2:]) if len(parts) >= 2 else remainder

    # Bare home directory
    if folder_name == _HOME_BARE:
        return "home"

    # Other paths (tmp, private, etc.)
    parts = folder_name.strip("-").split("-")
    home_parts = set(Path.home().parts[1:])  # e.g. {"Users", "malte"}
    skip_parts = home_parts | {"private", "var", "folders", "tmp"}
    meaningful = [p for p in parts if p not in skip_parts and len(p) > 2]
    if meaningful:
        return "-".join(meaningful[-2:])

    return folder_name.strip("-")


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse YAML frontmatter from markdown text.

    Returns (frontmatter_dict, body_text).
    If no frontmatter found, returns ({}, full_text).
    """
    if not text.startswith("---"):
        return {}, text

    # Find closing ---
    end_match = re.search(r"\n---\s*\n", text[3:])
    if not end_match:
        return {}, text

    fm_text = text[3:end_match.start() + 3]
    body = text[end_match.end() + 3:].strip()

    # Simple YAML parsing (these files use flat key: value only)
    fm: dict[str, str] = {}
    for line in fm_text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip()

    return fm, body


def is_merged_stub(body: str) -> bool:
    """Check if this file is a merged stub that should be skipped."""
    return any(marker in body for marker in MERGED_MARKERS)


def session_ref_for_path(file_path: str) -> str:
    """Generate a deterministic session_ref from the file path."""
    h = hashlib.sha256(file_path.encode()).hexdigest()[:12]
    return f"ccmem:{h}"


class SkipReason(Enum):
    """Why a memory file was skipped during mapping."""
    EMPTY = auto()
    MERGED = auto()
    FILTERED = auto()


def map_memory_file(file_path: Path) -> dict | SkipReason:
    """Parse a memory file and return mapped fields, or a SkipReason.

    Returns SkipReason.EMPTY for empty body, SkipReason.MERGED for merged stubs,
    SkipReason.FILTERED for project-filter mismatches.
    """
    text = file_path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)

    if not body.strip():
        return SkipReason.EMPTY

    if is_merged_stub(body):
        return SkipReason.MERGED

    # Derive project from folder name
    # Path: ~/.claude/projects/<folder>/memory/<file>.md
    folder_name = file_path.parent.parent.name
    project = decode_project_name(folder_name)

    # Apply project filter
    if PROJECT_FILTER and project != PROJECT_FILTER:
        return SkipReason.FILTERED

    title = fm.get("name")
    subtitle = fm.get("description")
    memory_type = fm.get("type", "observation")

    content_hash = hashlib.sha256(body.encode()).hexdigest()
    migration_date = datetime.now(timezone.utc).isoformat()

    metadata = {
        "source": "claude-code-memory",
        "memory_type": memory_type,
        "original_path": str(file_path),
        "migration_date": migration_date,
        "content_hash": content_hash,
    }

    return {
        "content": body,
        "title": title,
        "subtitle": subtitle,
        "type": memory_type,
        "project": project,
        "session_ref": session_ref_for_path(str(file_path)),
        "metadata": metadata,
    }


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def discover_memory_files() -> list[Path]:
    """Find all memory .md files across all project folders."""
    if not CLAUDE_PROJECTS_DIR.exists():
        print(f"Error: {CLAUDE_PROJECTS_DIR} does not exist")
        sys.exit(1)

    files: list[Path] = []
    for memory_dir in sorted(CLAUDE_PROJECTS_DIR.glob("*/memory")):
        for md_file in sorted(memory_dir.glob("*.md")):
            if md_file.name in SKIP_FILES:
                continue
            files.append(md_file)

    return files


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts using Voyage API."""
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


def to_pg_vector(embedding: list[float]) -> str:
    return "[" + ",".join(str(x) for x in embedding) + "]"


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
    """Return all ccmem: session_refs already in the DB."""
    rows = await conn.fetch(
        "SELECT session_ref FROM memories WHERE session_ref LIKE 'ccmem:%'"
    )
    return {r["session_ref"] for r in rows}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run() -> None:
    files = discover_memory_files()
    print(f"Discovered {len(files)} memory files")

    if PROJECT_FILTER:
        print(f"Filtering to project: {PROJECT_FILTER}")

    # Parse all files
    mapped: list[dict] = []
    skipped_empty = 0
    skipped_merged = 0
    skipped_filter = 0
    errors: list[str] = []

    for f in files:
        try:
            result = map_memory_file(f)
            if isinstance(result, SkipReason):
                if result is SkipReason.EMPTY:
                    skipped_empty += 1
                elif result is SkipReason.MERGED:
                    skipped_merged += 1
                elif result is SkipReason.FILTERED:
                    skipped_filter += 1
            else:
                mapped.append(result)
        except Exception as e:
            errors.append(f"  {f}: {e}")

    print(f"\nParsed: {len(mapped)} memories to import")
    print(f"Skipped: {skipped_empty} empty, {skipped_merged} merged stubs, {skipped_filter} filtered out")
    if errors:
        print(f"Errors ({len(errors)}):")
        for e in errors:
            print(e)

    if not mapped:
        print("Nothing to import.")
        return

    # Show distribution
    type_dist: dict[str, int] = {}
    project_dist: dict[str, int] = {}
    for m in mapped:
        t = m["type"]
        type_dist[t] = type_dist.get(t, 0) + 1
        p = m["project"]
        project_dist[p] = project_dist.get(p, 0) + 1

    print(f"\nType distribution: {dict(sorted(type_dist.items(), key=lambda x: -x[1]))}")
    print(f"Top projects: {dict(sorted(project_dist.items(), key=lambda x: -x[1])[:15])}")

    if DRY_RUN:
        print("\n--- Sample entries ---")
        for m in mapped[:3]:
            print(f"  title={m['title']!r}")
            print(f"  subtitle={m['subtitle']!r}")
            print(f"  type={m['type']!r}")
            print(f"  project={m['project']!r}")
            print(f"  session_ref={m['session_ref']!r}")
            print(f"  content[:80]={m['content'][:80]!r}")
            print()
        print("--dry-run: no data written.")
        return

    # Connect to DB
    if not DATABASE_URL:
        print("Error: DATABASE_URL must be set (via config or DATABASE_URL env var)")
        sys.exit(1)

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # No set_type_codec -- we pass pre-serialized JSON strings
        # with ::jsonb cast, matching migrate_learnings.py pattern

        # Find already-imported entries
        existing_refs = await get_existing_session_refs(conn)
        print(f"\nAlready imported: {len(existing_refs)} claude-code memories")

        to_import = [m for m in mapped if m["session_ref"] not in existing_refs]
        skipped_dup = len(mapped) - len(to_import)
        print(f"Skipping {skipped_dup} duplicates, importing {len(to_import)} new entries")

        if not to_import:
            print("Nothing new to import.")
        else:
            # Resolve all projects upfront
            projects = list({m["project"] for m in to_import})
            index_cache: dict[str | None, int] = {}
            for p in projects:
                index_cache[p] = await resolve_index_id(conn, p)

            memory_ids: list[int] = []
            memory_texts: list[str] = []

            # Wrap insert+embed in a transaction so partial imports roll back
            async with conn.transaction():
                for m in to_import:
                    index_id = index_cache[m["project"]]
                    metadata_json = json.dumps(m["metadata"])

                    # Build the embed text from title + subtitle + content
                    embed_parts = [p for p in [m["title"], m["subtitle"], m["content"]] if p]
                    embed_text = ": ".join(embed_parts)

                    row = await conn.fetchrow(
                        """INSERT INTO memories
                               (index_id, type, title, subtitle, content, session_ref, metadata)
                           VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                           RETURNING id""",
                        index_id,
                        m["type"],
                        m["title"],
                        m["subtitle"],
                        m["content"],
                        m["session_ref"],
                        metadata_json,
                    )
                    memory_ids.append(row["id"])
                    memory_texts.append(embed_text or "(empty)")

                print(f"Inserted {len(memory_ids)} memories")

                # Embed if API key available (inside transaction)
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
                    print("VOYAGE_API_KEY not set — skipping embeddings (run embed-missing later)")

        # Verify: distribution by type
        print("\n--- Verify: ccmem memories by type ---")
        rows = await conn.fetch(
            """SELECT type, COUNT(*)::int AS cnt
               FROM memories
               WHERE session_ref LIKE 'ccmem:%'
               GROUP BY 1
               ORDER BY 2 DESC"""
        )
        for r in rows:
            print(f"  {r['type']}: {r['cnt']}")

        total = await conn.fetchval(
            "SELECT COUNT(*)::int FROM memories WHERE session_ref LIKE 'ccmem:%'"
        )
        print(f"\nTotal claude-code memories in open-brain: {total}")

        # Distribution by project
        print("\n--- Verify: top projects ---")
        proj_rows = await conn.fetch(
            """SELECT mi.name AS project, COUNT(*)::int AS cnt
               FROM memories m
               JOIN memory_indexes mi ON mi.id = m.index_id
               WHERE m.session_ref LIKE 'ccmem:%'
               GROUP BY 1
               ORDER BY 2 DESC
               LIMIT 15"""
        )
        for r in proj_rows:
            print(f"  {r['project']}: {r['cnt']}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run())
