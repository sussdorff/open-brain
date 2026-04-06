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
# Main
# ---------------------------------------------------------------------------


async def run_triage(database_url: str, dry_run: bool) -> dict[str, int]:
    """Run triage over all ccmem: memories. Returns action count dict."""
    # Patch the config so PostgresDataLayer connects to the production DB
    os.environ["DATABASE_URL"] = database_url

    # Import after path setup and env patching
    from open_brain.data_layer.postgres import PostgresDataLayer
    from open_brain.data_layer.interface import TriageParams, MaterializeParams
    from open_brain.data_layer import postgres as pg_mod

    # Reset the shared pool so it picks up the new DATABASE_URL
    pg_mod._pool = None

    dl = PostgresDataLayer()
    params = TriageParams(scope="session_ref:ccmem:", limit=200, dry_run=dry_run)

    print(f"\n{'=' * 60}")
    if dry_run:
        print("TRIAGE DRY RUN — no changes will be made")
    else:
        print("TRIAGE EXECUTE — materializing non-keep actions")
    print("=" * 60)
    print(f"Scope  : {params.scope}")
    print(f"Limit  : {params.limit}")
    print(f"Dry run: {dry_run}")

    result = await dl.triage_memories(params)

    print(f"\nAnalyzed : {result.analyzed} memories")
    print(f"Summary  : {result.summary}")

    action_counts: dict[str, int] = {}
    for action in result.actions:
        action_counts[action.action] = action_counts.get(action.action, 0) + 1

    print("\nAction breakdown:")
    for act, count in sorted(action_counts.items(), key=lambda x: -x[1]):
        print(f"  {act:12s}: {count}")

    if not dry_run:
        non_keep = [a for a in result.actions if a.action != "keep"]
        if non_keep:
            print(f"\nMaterializing {len(non_keep)} non-keep actions...")
            mat_result = await dl.materialize_memories(
                MaterializeParams(triage_actions=non_keep, dry_run=False)
            )
            print(f"Materialization: {mat_result.summary}")
            for r in mat_result.results:
                status = "OK" if r.success else "FAIL"
                print(f"  [{status}] memory_id={r.memory_id} action={r.action}: {r.detail}")
        else:
            print("\nNo non-keep actions to materialize.")

    # Close the pool
    if pg_mod._pool is not None:
        await pg_mod._pool.close()
        pg_mod._pool = None

    return action_counts


async def main() -> None:
    _config = _load_config()
    database_url = _cfg(_config, "database.url", "DATABASE_URL")

    if not database_url:
        print("Error: DATABASE_URL must be set (via config or DATABASE_URL env var)")
        sys.exit(1)

    # Always run dry-run first
    dry_run_counts = await run_triage(database_url, dry_run=True)

    total = sum(dry_run_counts.values())
    keep = dry_run_counts.get("keep", 0)
    non_keep = total - keep

    print(f"\n{'=' * 60}")
    print("DRY RUN SUMMARY")
    print("=" * 60)
    print(f"Total analyzed : {total}")
    print(f"Would keep     : {keep}")
    print(f"Non-keep actions: {non_keep}")
    for act, count in sorted(dry_run_counts.items(), key=lambda x: -x[1]):
        if act != "keep":
            print(f"  {act:12s}: {count}")

    if EXECUTE and non_keep > 0:
        print(f"\nRe-running with --execute to materialize {non_keep} actions...")
        await run_triage(database_url, dry_run=False)
    elif EXECUTE and non_keep == 0:
        print("\nNothing to materialize (all actions are 'keep').")
    else:
        print("\nRun with --execute to materialize non-keep actions.")


if __name__ == "__main__":
    asyncio.run(main())
