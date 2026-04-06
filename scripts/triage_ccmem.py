#!/usr/bin/env python3
"""
Run a sharpened triage pipeline over all migrated Claude Code memories (ccmem: prefix).

Usage:
  python scripts/triage_ccmem.py                # dry-run (default)
  python scripts/triage_ccmem.py --execute      # materialize non-keep actions

Improvements over v1:
  - Phase 1: Duplicate detection across ALL memories (not just within 20-item batches)
  - Phase 2: Classification with full title index as cross-reference context
  - Content shown at 600 chars instead of 200
  - Comparison against existing non-ccmem memories for cross-source dedup
"""

import asyncio
import json as _json
import logging
import os
import re
import sys
from pathlib import Path

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loading (reuses pattern from migrate_claude_memories.py)
# ---------------------------------------------------------------------------

_XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
_DEFAULT_CONFIG = _XDG_CONFIG_HOME / "open-brain" / "migrate.toml"

EXECUTE: bool = "--execute" in sys.argv
VERBOSE: bool = "--verbose" in sys.argv or "-v" in sys.argv

if VERBOSE:
    logging.getLogger().setLevel(logging.DEBUG)


def _load_config() -> dict[str, str]:
    """Load config from TOML file. Returns flat dict of key=value strings."""
    config_path = _DEFAULT_CONFIG
    if not config_path.exists():
        return {}

    try:
        mode = config_path.stat().st_mode
        if mode & 0o077:
            print(
                f"Warning: {config_path} is readable by others (mode {oct(mode)}). "
                f"Run: chmod 600 {config_path}"
            )
    except OSError:
        pass

    config: dict[str, str] = {}
    try:
        import tomllib

        with open(config_path, "rb") as f:
            data = tomllib.load(f)

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


# ---------------------------------------------------------------------------
# Setup: inject the open_brain package into sys.path
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_PYTHON_SRC = _REPO_ROOT / "python" / "src"
if str(_PYTHON_SRC) not in sys.path:
    sys.path.insert(0, str(_PYTHON_SRC))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_LIFECYCLE_FILTER = (
    "AND (metadata->>'status' IS NULL "
    "OR metadata->>'status' NOT IN ('materialized', 'discarded'))"
)


async def _init_conn(conn: asyncpg.Connection) -> None:
    """Register JSONB codec so asyncpg returns dicts instead of raw JSON strings."""
    await conn.set_type_codec(
        "jsonb",
        encoder=_json.dumps,
        decoder=_json.loads,
        schema="pg_catalog",
    )


async def fetch_ccmem_candidates(
    conn: asyncpg.Connection, limit: int
) -> list[asyncpg.Record]:
    """Fetch all ccmem: memories not yet processed."""
    return await conn.fetch(
        f"SELECT * FROM memories WHERE session_ref LIKE $1 {_LIFECYCLE_FILTER} ORDER BY created_at DESC LIMIT $2",
        "ccmem:%",
        limit,
    )


async def fetch_existing_titles(conn: asyncpg.Connection) -> list[dict]:
    """Fetch titles + types of all non-ccmem memories for cross-source dedup."""
    rows = await conn.fetch(
        "SELECT id, title, type, session_ref FROM memories "
        "WHERE (session_ref IS NULL OR session_ref NOT LIKE 'ccmem:%') "
        "AND title IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 500"
    )
    return [{"id": r["id"], "title": r["title"], "type": r["type"], "session_ref": r["session_ref"]} for r in rows]


# ---------------------------------------------------------------------------
# Phase 1: Cross-batch duplicate clustering
# ---------------------------------------------------------------------------


def _build_title_index(memories: list[dict]) -> str:
    """Build a compact title index for cross-reference context."""
    lines = []
    for m in memories:
        title = m.get("title") or "(no title)"
        mtype = m.get("type") or "?"
        lines.append(f"  [{m['id']}] {mtype}: {title}")
    return "\n".join(lines)


async def _find_duplicate_clusters(
    memories: list[dict],
    existing_titles: list[dict],
    anthropic_api_key: str,
) -> list[list[int]]:
    """
    Phase 1: Ask the LLM to identify duplicate/near-duplicate clusters
    across ALL memories at once (title + first 150 chars only — fits in context).
    Returns list of clusters, each a list of memory IDs that should be merged.
    """
    from open_brain.data_layer.llm import LlmMessage, llm_complete

    # Build compact view: title + short snippet for all memories
    ccmem_lines = []
    for m in memories:
        title = m.get("title") or "(no title)"
        content = (m.get("content") or m.get("narrative") or "")[:150]
        ccmem_lines.append(f"  [{m['id']}] {m.get('type', '?')}: {title} | {content}")

    existing_lines = []
    for e in existing_titles[:200]:  # cap to keep prompt manageable
        existing_lines.append(f"  [EXISTING-{e['id']}] {e['type']}: {e['title']}")

    prompt = f"""You are reviewing a memory database for duplicates and near-duplicates.

## Imported memories (ccmem: source) — check these for duplicates:
{chr(10).join(ccmem_lines)}

## Existing memories (already in DB) — if an imported memory duplicates one of these, include it:
{chr(10).join(existing_lines) if existing_lines else "  (none)"}

Find ALL groups of memories that are duplicates or near-duplicates (same topic, same facts, just different wording or timestamps). Include EXISTING-* IDs in clusters if an imported memory duplicates an existing one.

Return a JSON array of clusters. Each cluster is an array of memory IDs (integers for ccmem, strings like "EXISTING-123" for existing).
Only include clusters with 2+ members. If no duplicates found, return [].

Example: [[101, 102], [205, "EXISTING-44", 208]]

Return ONLY the JSON array, nothing else."""

    text = await llm_complete(
        [LlmMessage(role="user", content=prompt)],
        max_tokens=4096,
    )

    # Parse response
    json_match = re.search(r"\[[\s\S]*\]", text)
    if not json_match:
        logger.info("Phase 1: No duplicate clusters found")
        return []

    try:
        raw = _json.loads(json_match.group())
    except _json.JSONDecodeError:
        logger.warning("Phase 1: Could not parse cluster response")
        return []

    # Extract ccmem-only IDs from clusters
    clusters = []
    for cluster in raw:
        if not isinstance(cluster, list) or len(cluster) < 2:
            continue
        ccmem_ids = [x for x in cluster if isinstance(x, int)]
        has_existing = any(isinstance(x, str) and x.startswith("EXISTING-") for x in cluster)
        if ccmem_ids:
            clusters.append({"ccmem_ids": ccmem_ids, "has_existing_dup": has_existing})

    logger.info("Phase 1: Found %d duplicate clusters", len(clusters))
    for i, c in enumerate(clusters):
        logger.info("  Cluster %d: %s%s", i + 1, c["ccmem_ids"],
                     " (duplicates existing memory)" if c["has_existing_dup"] else "")
    return clusters


# ---------------------------------------------------------------------------
# Phase 2: Sharpened batch triage
# ---------------------------------------------------------------------------


async def _triage_batch_with_context(
    memories: list,  # list[Memory]
    title_index: str,
    duplicate_clusters: list[dict],
    existing_titles: list[dict],
) -> list:
    """Triage a batch with full cross-reference context."""
    from open_brain.data_layer.interface import TriageAction
    from open_brain.data_layer.llm import LlmMessage, llm_complete

    # Build the memory summaries with MORE content (600 chars instead of 200)
    memory_summary = "\n".join(
        f"[{m.id}] type={m.type}, priority={m.priority:.2f}, stability={m.stability}, "
        f"access_count={m.access_count} | {m.title or '(no title)'}: {(m.content or m.narrative or '')[:600]}"
        for m in memories
    )

    # Build duplicate context from Phase 1
    dup_context = ""
    batch_ids = {m.id for m in memories}
    relevant_clusters = [c for c in duplicate_clusters if any(mid in batch_ids for mid in c["ccmem_ids"])]
    if relevant_clusters:
        dup_lines = []
        for c in relevant_clusters:
            ids_str = ", ".join(str(x) for x in c["ccmem_ids"])
            suffix = " ← also duplicates an existing (non-ccmem) memory" if c["has_existing_dup"] else ""
            dup_lines.append(f"  Cluster: [{ids_str}]{suffix}")
        dup_context = f"""
KNOWN DUPLICATES (from cross-batch analysis):
{chr(10).join(dup_lines)}
For these clusters: mark the BEST one as "keep" and the rest as "merge" (or "archive" if it duplicates an existing memory).
"""

    text = await llm_complete(
        [
            LlmMessage(
                role="user",
                content=f"""Classify each memory into a lifecycle action. Return a JSON array.

Memories to classify:
{memory_summary}

ALL MEMORY TITLES (for cross-reference — duplicates may be in other batches):
{title_index}
{dup_context}
Actions and when to use them:
- "keep": valuable, factual, unique knowledge — retain as-is
- "merge": near-duplicate of another memory (include BOTH IDs in reason). Prefer merging into the more complete version.
- "promote": reusable pattern/convention that belongs in standards docs (especially "learning" type)
- "scaffold": identified task/todo/improvement that should become a work item
- "archive": transient, outdated, already-implemented, or low-value memory

IMPORTANT classification rules:
- Be AGGRESSIVE about finding duplicates. If two memories cover the same topic (e.g. "Dolt server mode", "Dolt embedded vs server"), they are likely duplicates.
- "learning" type: "promote" if it's a reusable workflow pattern; "keep" if domain-specific; "archive" if already implemented; "merge" if overlapping
- "session_summary" type: prefer "archive" unless it contains critical unrepeated decisions
- "observation" type: "keep" if unique; "merge" if duplicate; "scaffold" if describes a todo
- If a memory just says something was already done/fixed/implemented, it's "archive"
- If a memory describes a bead/ticket snapshot, it's "archive" (beads have their own tracking)

Return ONLY a JSON array. Each item: {{"memory_id": int, "action": string, "reason": string}}.
Every memory in the input must appear exactly once.""",
            )
        ],
        max_tokens=4096,
    )
    logger.debug("LLM triage raw response (first 500 chars): %s", text[:500])

    raw_items = _parse_json_array(text)
    memory_by_id = {m.id: m for m in memories}

    actions = []
    seen_ids: set[int] = set()

    for item in raw_items:
        memory_id = item.get("memory_id")
        action = item.get("action", "keep")
        reason = item.get("reason", "")

        if isinstance(memory_id, str) and memory_id.isdigit():
            memory_id = int(memory_id)

        if not isinstance(memory_id, int) or memory_id not in memory_by_id:
            logger.debug("Skipping unrecognized memory_id=%r", memory_id)
            continue
        if memory_id in seen_ids:
            continue
        if action not in ("keep", "merge", "promote", "scaffold", "archive"):
            logger.warning("Unknown action %r for memory %d — defaulting to keep", action, memory_id)
            action = "keep"

        seen_ids.add(memory_id)
        mem = memory_by_id[memory_id]
        actions.append(
            TriageAction(
                action=action,
                memory_id=memory_id,
                reason=reason,
                memory_type=mem.type,
                memory_title=mem.title,
                executed=False,
            )
        )

    # Fill in missed memories
    for mem in memories:
        if mem.id not in seen_ids:
            logger.info("LLM missed memory %d — defaulting to keep", mem.id)
            actions.append(
                TriageAction(
                    action="keep",
                    memory_id=mem.id,
                    reason="LLM did not classify this memory",
                    memory_type=mem.type,
                    memory_title=mem.title,
                    executed=False,
                )
            )

    return actions


def _parse_json_array(text: str) -> list[dict]:
    """Extract and parse a JSON array from LLM text."""
    json_match = re.search(r"\[[\s\S]*\]", text)
    if not json_match:
        return []
    raw = json_match.group()
    try:
        return _json.loads(raw)
    except _json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", raw)
        try:
            return _json.loads(repaired)
        except _json.JSONDecodeError as err:
            logger.warning("JSON repair failed: %s", err)
            return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    _config = _load_config()
    database_url = _cfg(_config, "database.url", "DATABASE_URL")
    anthropic_api_key = _cfg(_config, "anthropic.api_key", "ANTHROPIC_API_KEY")

    if not database_url:
        print("Error: DATABASE_URL must be set (via config or DATABASE_URL env var)")
        sys.exit(1)

    print(f"\n{'=' * 60}")
    if EXECUTE:
        print("TRIAGE EXECUTE — will materialize non-keep actions after dry-run")
    else:
        print("TRIAGE DRY RUN — no changes will be made")
    print("=" * 60)
    print("Scope  : session_ref:ccmem:")
    print("Limit  : 200")
    print(f"Mode   : {'execute' if EXECUTE else 'dry-run'}")
    print(f"Version: v2 (cross-batch dedup, 600-char content, existing memory comparison)")

    conn = await asyncpg.connect(database_url)
    await _init_conn(conn)
    try:
        rows = await fetch_ccmem_candidates(conn, 200)
        if not rows:
            print("No ccmem: memories found.")
            return

        # Build Memory objects
        from open_brain.data_layer.interface import Memory

        candidates = []
        candidate_dicts = []
        for row in rows:
            r = dict(row)
            candidate_dicts.append(r)
            candidates.append(
                Memory(
                    id=r["id"],
                    index_id=r["index_id"],
                    session_id=r.get("session_id"),
                    type=r.get("type"),
                    title=r.get("title"),
                    subtitle=r.get("subtitle"),
                    narrative=r.get("narrative"),
                    content=r.get("content") or "",
                    metadata=r.get("metadata") or {},
                    priority=r.get("priority") or 0.5,
                    stability=r.get("stability") or "stable",
                    access_count=r.get("access_count") or 0,
                    last_accessed_at=r.get("last_accessed_at"),
                    created_at=str(r.get("created_at", "")),
                    updated_at=str(r.get("updated_at", "")),
                    user_id=r.get("user_id"),
                )
            )

        print(f"\nFetched {len(candidates)} ccmem: memories")

        # Set up env for LLM (must happen before any LLM call)
        _dummy_env = {
            "DATABASE_URL": os.environ.get("DATABASE_URL", "postgresql://localhost/dummy"),
            "MCP_SERVER_URL": os.environ.get("MCP_SERVER_URL", "http://localhost:8091"),
            "AUTH_USER": os.environ.get("AUTH_USER", "admin"),
            "AUTH_PASSWORD": os.environ.get("AUTH_PASSWORD", "dummypassword123"),
            "JWT_SECRET": os.environ.get("JWT_SECRET", "dummy-jwt-secret-that-is-long-enough-32chars"),
            "VOYAGE_API_KEY": os.environ.get("VOYAGE_API_KEY", "dummy"),
        }
        for k, v in _dummy_env.items():
            if k not in os.environ:
                os.environ[k] = v
        if anthropic_api_key:
            os.environ["ANTHROPIC_API_KEY"] = anthropic_api_key

        import open_brain.config as _cfg_mod
        _cfg_mod._config = None

        # Fetch existing memories for cross-source dedup
        existing = await fetch_existing_titles(conn)
        print(f"Fetched {len(existing)} existing (non-ccmem) memory titles for cross-reference")

        # Phase 1: Find duplicate clusters across ALL memories
        print(f"\n{'─' * 40}")
        print("Phase 1: Cross-batch duplicate detection")
        print("─" * 40)
        clusters = await _find_duplicate_clusters(candidate_dicts, existing, anthropic_api_key)

        if clusters:
            print(f"\nFound {len(clusters)} duplicate clusters:")
            for i, c in enumerate(clusters):
                ids = c["ccmem_ids"]
                titles = []
                for mid in ids:
                    mem = next((m for m in candidates if m.id == mid), None)
                    if mem:
                        titles.append(f"  [{mid}] {mem.title or '(no title)'}")
                print(f"\n  Cluster {i + 1}:{' (also in existing DB)' if c['has_existing_dup'] else ''}")
                for t in titles:
                    print(t)
        else:
            print("No duplicate clusters detected.")

        # Phase 2: Batch triage with context
        print(f"\n{'─' * 40}")
        print("Phase 2: Classification with cross-reference context")
        print("─" * 40)

        title_index = _build_title_index(candidate_dicts)
        batch_size = 25  # slightly larger batches for better context

        all_actions = []
        for i in range(0, len(candidates), batch_size):
            batch = candidates[i:i + batch_size]
            logger.info("Triaging batch %d-%d of %d", i + 1, i + len(batch), len(candidates))
            actions = await _triage_batch_with_context(
                batch, title_index, clusters, existing
            )
            all_actions.extend(actions)

        # Summarize
        action_counts: dict[str, int] = {}
        for a in all_actions:
            action_counts[a.action] = action_counts.get(a.action, 0) + 1

        total = sum(action_counts.values())
        keep = action_counts.get("keep", 0)
        non_keep = total - keep

        print(f"\n{'=' * 60}")
        print("TRIAGE SUMMARY")
        print("=" * 60)
        print(f"Total analyzed  : {total}")
        print(f"Keep            : {keep}")
        print(f"Non-keep actions: {non_keep}")
        for act, count in sorted(action_counts.items(), key=lambda x: -x[1]):
            print(f"  {act:12s}: {count}")

        # Show non-keep details
        if non_keep > 0:
            print(f"\n{'─' * 40}")
            print("Non-keep actions detail:")
            print("─" * 40)
            for a in all_actions:
                if a.action != "keep":
                    print(f"  [{a.memory_id}] {a.action:8s} | {a.memory_title or '(no title)'}")
                    print(f"           reason: {a.reason}")

        if EXECUTE and non_keep > 0:
            from open_brain.data_layer.materialize import execute_triage_actions

            non_keep_actions = [a for a in all_actions if a.action != "keep"]
            memories_by_id = {m.id: m for m in candidates}

            async def _archive_fn(memory_id: int, priority: float) -> None:
                await conn.execute(
                    "UPDATE memories SET priority = $1, updated_at = now() WHERE id = $2",
                    priority, memory_id,
                )

            print(f"\nMaterializing {len(non_keep_actions)} non-keep actions...")
            results = await execute_triage_actions(non_keep_actions, memories_by_id, _archive_fn)
            succeeded = sum(1 for r in results if r.success)
            failed = len(results) - succeeded
            print(f"Materialized {succeeded}/{len(results)} actions" + (f" ({failed} failed)" if failed else ""))
            for r in results:
                if not r.success:
                    print(f"  [FAIL] memory_id={r.memory_id} action={r.action}: {r.detail}")
        elif EXECUTE and non_keep == 0:
            print("\nNothing to materialize (all actions are 'keep').")
        else:
            print("\nRun with --execute to materialize non-keep actions.")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
