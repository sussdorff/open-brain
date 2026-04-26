#!/usr/bin/env python3
"""
Fleet-wide compact_memories run across ALL projects in open-brain.

Discovers all projects from memory_indexes, runs compact_memories with
dry_run=True to preview, then executes for projects that have clusters.

Usage:
  DATABASE_URL=... uv run python scripts/fleet-compact.py
  DATABASE_URL=... uv run python scripts/fleet-compact.py --dry-run
  DATABASE_URL=... uv run python scripts/fleet-compact.py --threshold 0.90
"""

import asyncio
import os
import sys
from dataclasses import dataclass

import asyncpg

# ─── CLI flags ────────────────────────────────────────────────────────────────

DRY_RUN_ONLY = "--dry-run" in sys.argv

THRESHOLD = 0.87
for arg in sys.argv[1:]:
    if arg.startswith("--threshold="):
        THRESHOLD = float(arg.split("=", 1)[1])

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    print("Error: DATABASE_URL must be set")
    sys.exit(1)


# ─── asyncpg helpers ──────────────────────────────────────────────────────────


async def _init_conn(conn: asyncpg.Connection) -> None:
    """Register JSONB codec so asyncpg returns dicts instead of raw JSON strings."""
    import json
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


# ─── Compact logic (mirrors PostgresDataLayer.compact_memories) ───────────────


@dataclass
class ClusterInfo:
    cluster_id: int
    members: list[int]
    canonical_id: int
    to_delete: list[int]


@dataclass
class CompactSummary:
    project: str
    total_memories: int
    clusters_found: int
    memories_deleted: int
    memories_kept: list[int]
    deleted_ids: list[int]
    strategy_used: str
    clusters: list[ClusterInfo]


_LIFECYCLE_EXCLUDE = ("materialized", "discarded", "archived")
_STRATEGY = "keep_highest_access"


def _build_clusters(ids: list[int], edges: list[tuple[int, int]]) -> list[list[int]]:
    """Union-find over edges; return only clusters with >= 2 members."""
    parent: dict[int, int] = {i: i for i in ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in edges:
        union(a, b)

    groups: dict[int, list[int]] = {}
    for i in ids:
        root = find(i)
        groups.setdefault(root, []).append(i)

    return [members for members in groups.values() if len(members) >= 2]


def _select_canonical(members: list[int], rows: dict[int, dict]) -> int:
    """Select canonical memory ID using keep_highest_access strategy."""
    return max(
        members,
        key=lambda i: (rows[i]["access_count"], rows[i]["updated_at"]),
    )


async def compact_project(
    conn: asyncpg.Connection,
    project: str,
    index_id: int,
    threshold: float,
    dry_run: bool,
) -> CompactSummary:
    """Run compact logic for a single project."""
    # Fetch candidate memories (must have embeddings, not excluded statuses)
    status_placeholders = ", ".join(f"${i + 2}" for i in range(len(_LIFECYCLE_EXCLUDE)))
    query = f"""
        SELECT id, content, access_count, updated_at,
               metadata->>'status' AS status
        FROM memories
        WHERE index_id = $1
          AND embedding IS NOT NULL
          AND (
            metadata->>'status' IS NULL
            OR metadata->>'status' NOT IN ({status_placeholders})
          )
          AND (
            metadata->>'do_not_compact' IS NULL
            OR metadata->>'do_not_compact' != 'true'
          )
        ORDER BY id
    """
    rows_raw = await conn.fetch(query, index_id, *_LIFECYCLE_EXCLUDE)
    total_memories = len(rows_raw)

    if total_memories < 2:
        return CompactSummary(
            project=project,
            total_memories=total_memories,
            clusters_found=0,
            memories_deleted=0,
            memories_kept=[],
            deleted_ids=[],
            strategy_used=_STRATEGY,
            clusters=[],
        )

    ids = [r["id"] for r in rows_raw]
    rows: dict[int, dict] = {
        r["id"]: {
            "content": r["content"] or "",
            "access_count": r["access_count"] or 0,
            "updated_at": r["updated_at"],
        }
        for r in rows_raw
    }

    # Find near-duplicate pairs via pgvector cosine similarity
    pairs_query = """
        SELECT a.id AS id_a, b.id AS id_b
        FROM memories a
        JOIN memories b ON b.id > a.id
        WHERE a.index_id = $1
          AND b.index_id = $1
          AND a.embedding IS NOT NULL
          AND b.embedding IS NOT NULL
          AND 1 - (a.embedding <=> b.embedding) >= $2
          AND (a.metadata->>'status' IS NULL
               OR a.metadata->>'status' NOT IN ('materialized', 'discarded', 'archived'))
          AND (b.metadata->>'status' IS NULL
               OR b.metadata->>'status' NOT IN ('materialized', 'discarded', 'archived'))
          AND (a.metadata->>'do_not_compact' IS NULL
               OR a.metadata->>'do_not_compact' != 'true')
          AND (b.metadata->>'do_not_compact' IS NULL
               OR b.metadata->>'do_not_compact' != 'true')
    """
    pair_rows = await conn.fetch(pairs_query, index_id, threshold)
    edges = [(r["id_a"], r["id_b"]) for r in pair_rows]

    clusters_raw = _build_clusters(ids, edges)

    plan: list[ClusterInfo] = []
    all_deleted_ids: list[int] = []
    all_kept_ids: list[int] = []

    for cluster_idx, members in enumerate(clusters_raw):
        canonical_id = _select_canonical(members, rows)
        to_delete = [m for m in members if m != canonical_id]
        plan.append(
            ClusterInfo(
                cluster_id=cluster_idx,
                members=members,
                canonical_id=canonical_id,
                to_delete=to_delete,
            )
        )
        all_deleted_ids.extend(to_delete)
        all_kept_ids.append(canonical_id)

    if not dry_run and all_deleted_ids:
        await conn.execute(
            "DELETE FROM memories WHERE id = ANY($1::int[])",
            all_deleted_ids,
        )

    return CompactSummary(
        project=project,
        total_memories=total_memories,
        clusters_found=len(plan),
        memories_deleted=len(all_deleted_ids) if not dry_run else 0,
        memories_kept=all_kept_ids,
        deleted_ids=all_deleted_ids,
        strategy_used=_STRATEGY,
        clusters=plan,
    )


# ─── Main ─────────────────────────────────────────────────────────────────────


async def main() -> None:
    print(f"fleet-compact.py | threshold={THRESHOLD} | dry_run_only={DRY_RUN_ONLY}")
    print(f"DATABASE_URL: {DATABASE_URL[:40]}...")
    print()

    conn = await asyncpg.connect(DATABASE_URL)
    await _init_conn(conn)
    try:
        # 1. List all projects
        project_rows = await conn.fetch(
            "SELECT id, name FROM memory_indexes ORDER BY name"
        )
        projects = [(r["id"], r["name"]) for r in project_rows]
        print(f"Found {len(projects)} project(s): {[p[1] for p in projects]}")
        print()

        # 2. Dry-run pass for all projects
        print("=" * 70)
        print("DRY-RUN PASS")
        print("=" * 70)
        dry_results: list[CompactSummary] = []

        for index_id, project_name in projects:
            result = await compact_project(
                conn,
                project=project_name,
                index_id=index_id,
                threshold=THRESHOLD,
                dry_run=True,
            )
            dry_results.append(result)
            print(
                f"  {project_name:<35} "
                f"memories={result.total_memories:>4}  "
                f"clusters={result.clusters_found:>3}  "
                f"would_delete={len(result.deleted_ids):>3}"
            )

        total_would_delete = sum(len(r.deleted_ids) for r in dry_results)
        total_clusters = sum(r.clusters_found for r in dry_results)
        print()
        print(f"TOTAL: {total_clusters} clusters, {total_would_delete} deletions planned")

        # 3. Early exit if dry-run-only or nothing to do
        if DRY_RUN_ONLY:
            print("\n--dry-run: stopping before execution.")
            return

        projects_with_clusters = [r for r in dry_results if r.clusters_found > 0]
        if not projects_with_clusters:
            print("\nNo clusters found across any project — nothing to compact.")
            return

        # 4. Execute compact for projects that have clusters
        print()
        print("=" * 70)
        print("EXECUTION PASS")
        print("=" * 70)
        exec_results: list[CompactSummary] = []

        for dry_result in dry_results:
            if dry_result.clusters_found == 0:
                print(f"  {dry_result.project:<35} SKIP (0 clusters)")
                continue

            # Re-fetch the index_id
            index_id = next(pid for pid, pname in projects if pname == dry_result.project)
            result = await compact_project(
                conn,
                project=dry_result.project,
                index_id=index_id,
                threshold=THRESHOLD,
                dry_run=False,
            )
            exec_results.append(result)
            print(
                f"  {result.project:<35} "
                f"clusters={result.clusters_found:>3}  "
                f"deleted={len(result.deleted_ids):>3}"
            )

        total_deleted = sum(len(r.deleted_ids) for r in exec_results)
        print()
        print(f"DONE: {total_deleted} memories deleted across {len(exec_results)} project(s)")

        # 5. Post-compact memory count check
        print()
        print("POST-COMPACT MEMORY COUNTS:")
        for index_id, project_name in projects:
            count = await conn.fetchval(
                "SELECT COUNT(*)::int FROM memories WHERE index_id = $1", index_id
            )
            print(f"  {project_name:<35} remaining={count}")

        total_remaining = await conn.fetchval("SELECT COUNT(*)::int FROM memories")
        print(f"\n  {'TOTAL':<35} remaining={total_remaining}")

    finally:
        await conn.close()


asyncio.run(main())
