#!/usr/bin/env python3
"""
Run the triage pipeline over all migrated Claude Code memories (ccmem: prefix).

Usage:
  python scripts/triage_ccmem.py                # dry-run (default)
  python scripts/triage_ccmem.py --execute      # materialize non-keep actions

The script connects directly to the production DB via asyncpg, using the same
config pattern as migrate_claude_memories.py (~/.config/open-brain/migrate.toml).

It first runs a dry_run=True pass and prints the action breakdown.
With --execute, it re-runs with dry_run=False to materialize non-keep actions.
"""

import asyncio
import json as _json
import os
import sys
from pathlib import Path

import asyncpg

# ---------------------------------------------------------------------------
# Config loading (reuses pattern from migrate_claude_memories.py)
# ---------------------------------------------------------------------------

_XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
_DEFAULT_CONFIG = _XDG_CONFIG_HOME / "open-brain" / "migrate.toml"

EXECUTE: bool = "--execute" in sys.argv


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


def _row_to_dict(row: asyncpg.Record) -> dict:
    """Convert asyncpg record to plain dict."""
    return dict(row)


async def fetch_ccmem_candidates(
    conn: asyncpg.Connection, limit: int
) -> list[asyncpg.Record]:
    """Fetch all ccmem: memories not yet processed."""
    return await conn.fetch(
        f"SELECT * FROM memories WHERE session_ref LIKE $1 {_LIFECYCLE_FILTER} ORDER BY created_at DESC LIMIT $2",
        "ccmem:%",
        limit,
    )


# ---------------------------------------------------------------------------
# Triage + summarize
# ---------------------------------------------------------------------------


async def run_triage_pass(
    conn: asyncpg.Connection,
    limit: int,
    dry_run: bool,
    anthropic_api_key: str,
) -> dict[str, int]:
    """
    Run one triage pass over ccmem: memories.

    Returns action counts dict.
    """
    from open_brain.data_layer.interface import Memory, TriageParams
    from open_brain.data_layer.triage import triage_with_llm

    rows = await fetch_ccmem_candidates(conn, limit)
    if not rows:
        print("No ccmem: memories found (all may already be processed).")
        return {}

    candidates: list[Memory] = []
    for row in rows:
        r = dict(row)
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

    print(f"\nFetched {len(candidates)} ccmem: memories for triage")

    # Provide minimal env vars so Config validates (only ANTHROPIC_API_KEY matters for triage)
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

    # Reset cached config so it picks up new env
    import open_brain.config as _cfg_mod
    _cfg_mod._config = None

    actions = await triage_with_llm(candidates)

    action_counts: dict[str, int] = {}
    for action in actions:
        action_counts[action.action] = action_counts.get(action.action, 0) + 1

    print("\nAction breakdown:")
    for act, count in sorted(action_counts.items(), key=lambda x: -x[1]):
        print(f"  {act:12s}: {count}")

    if not dry_run:
        # Materialize non-keep actions using the postgres module
        from open_brain.data_layer.materialize import execute_triage_actions

        non_keep = [a for a in actions if a.action != "keep"]
        if non_keep:
            print(f"\nMaterializing {len(non_keep)} non-keep actions...")
            memories_by_id = {m.id: m for m in candidates}

            async def _archive_fn(memory_id: int, priority: float) -> None:
                await conn.execute(
                    "UPDATE memories SET priority = $1, updated_at = now() WHERE id = $2",
                    priority,
                    memory_id,
                )

            results = await execute_triage_actions(
                non_keep, memories_by_id, _archive_fn
            )
            succeeded = sum(1 for r in results if r.success)
            failed = len(results) - succeeded
            print(f"Materialized {succeeded}/{len(results)} actions" + (f" ({failed} failed)" if failed else ""))
            for r in results:
                if not r.success:
                    print(f"  [FAIL] memory_id={r.memory_id} action={r.action}: {r.detail}")
        else:
            print("\nNo non-keep actions to materialize.")

    return action_counts


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

    conn = await asyncpg.connect(database_url)
    await _init_conn(conn)
    try:
        dry_run_counts = await run_triage_pass(
            conn, limit=200, dry_run=True, anthropic_api_key=anthropic_api_key
        )

        total = sum(dry_run_counts.values())
        keep = dry_run_counts.get("keep", 0)
        non_keep = total - keep

        print(f"\n{'=' * 60}")
        print("DRY RUN SUMMARY")
        print("=" * 60)
        print(f"Total analyzed  : {total}")
        print(f"Would keep      : {keep}")
        print(f"Non-keep actions: {non_keep}")
        for act, count in sorted(dry_run_counts.items(), key=lambda x: -x[1]):
            if act != "keep":
                print(f"  {act:12s}: {count}")

        if EXECUTE and non_keep > 0:
            print(f"\nRe-running with --execute to materialize {non_keep} actions...")
            await run_triage_pass(
                conn, limit=200, dry_run=False, anthropic_api_key=anthropic_api_key
            )
        elif EXECUTE and non_keep == 0:
            print("\nNothing to materialize (all actions are 'keep').")
        else:
            print("\nRun with --execute to materialize non-keep actions.")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
