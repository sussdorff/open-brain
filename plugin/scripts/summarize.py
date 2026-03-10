#!/usr/bin/env python3
"""Stop/SubagentStop hook: trigger session summary on open-brain server."""

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import load_config, detect_project


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

    session_id = hook_data.get("session_id", "")
    cwd = hook_data.get("cwd", "")
    hook_event = hook_data.get("hook_event_name", "Stop")

    project = config.get("project", "auto")
    if project == "auto":
        project = detect_project(cwd or None)

    # Send summarize request to server
    url = f"{config['server_url'].rstrip('/')}/api/summarize"
    payload = {
        "session_id": session_id,
        "project": project,
        "hook_type": hook_event,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": config.get("api_key", ""),
        },
        method="POST",
    )

    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # Fire-and-forget

    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
