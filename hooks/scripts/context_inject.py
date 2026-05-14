#!/usr/bin/env python3
"""SessionStart hook: inject recent memory context from open-brain server."""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import load_config, resolve_project


def fetch_wake_up_pack(config: dict, project: str, token_budget: int = 500) -> str | None:
    """Fetch wake-up pack markdown from open-brain server."""
    params = urllib.parse.urlencode({"token_budget": token_budget, "project": project})
    url = f"{config['server_url'].rstrip('/')}/api/wake_up_pack?{params}"
    req = urllib.request.Request(
        url,
        headers={"X-API-Key": config.get("api_key", "")},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.read().decode("utf-8")
    except Exception:
        return None


def build_output(context_md: str | None, harness: str) -> dict:
    """Build hook output in the target harness format."""
    if not context_md or not context_md.strip():
        return {"continue": True}

    context = f"# open-brain Memory Context\n\n{context_md}"
    if harness == "codex":
        return {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            },
        }

    return {
        "continue": True,
        "systemMessage": context,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", choices=["claude", "codex"], default="claude")
    args = parser.parse_args()

    config = load_config()
    if config is None:
        print(json.dumps({"continue": True}))
        return

    # Read hook data from stdin
    try:
        raw = sys.stdin.read()
        hook_data = json.loads(raw) if raw.strip() else {}
    except Exception:
        hook_data = {}

    cwd = hook_data.get("cwd", "")
    project = resolve_project(config, cwd or None)

    token_budget = config.get("token_budget", 500)
    context_md = fetch_wake_up_pack(config, project, token_budget)

    print(json.dumps(build_output(context_md, args.harness)))


if __name__ == "__main__":
    main()
