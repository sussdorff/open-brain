#!/usr/bin/env python3
"""SessionStart hook: inject recent memory context from open-brain server."""

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import load_config, detect_project


def fetch_context(config: dict, project: str) -> str:
    """Fetch recent context from open-brain server."""
    limit = config.get("context_limit", 50)
    url = f"{config['server_url'].rstrip('/')}/api/context?project={project}&limit={limit}"

    req = urllib.request.Request(
        url,
        headers={"X-API-Key": config.get("api_key", "")},
    )

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.read().decode("utf-8")
    except Exception:
        return ""


def main():
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
    project = config.get("project", "auto")
    if project == "auto":
        project = detect_project(cwd or None)

    context_md = fetch_context(config, project)

    if context_md:
        output = {
            "continue": True,
            "systemMessage": f"# open-brain Memory Context\n\n{context_md}",
        }
    else:
        output = {"continue": True}

    print(json.dumps(output))


if __name__ == "__main__":
    main()
