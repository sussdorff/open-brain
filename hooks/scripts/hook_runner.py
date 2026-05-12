#!/usr/bin/env python3
"""PostToolUse hook: capture tool observations and send to open-brain server."""

import hashlib
import json
import sys
import time
from pathlib import Path

# Add scripts dir to path for config import
sys.path.insert(0, str(Path(__file__).parent))
from config import load_config, ensure_log_dir, detect_project, LOG_FILE


def truncate(text: str, max_chars: int = 4000) -> str:
    """Truncate text to max_chars."""
    if not text:
        return ""
    s = str(text)
    return s[:max_chars] + "..." if len(s) > max_chars else s


def should_skip(tool_name: str, tool_response: str, config: dict) -> bool:
    """Check if this tool use should be skipped."""
    if tool_name in config.get("skip_tools", []):
        return True
    # Skip large Bash output
    if tool_name == "Bash":
        max_kb = config.get("bash_output_max_kb", 10)
        if len(str(tool_response)) > max_kb * 1024:
            return True
    return False


def content_hash(data: dict) -> str:
    """Generate a content hash for deduplication."""
    key = f"{data.get('tool_name', '')}:{truncate(str(data.get('tool_input', '')), 500)}"
    return hashlib.md5(key.encode()).hexdigest()


DEDUP_FILE = Path("/tmp/ob-dedup.json")
DEDUP_WINDOW = 30  # seconds


def is_duplicate(hash_val: str) -> bool:
    """Check if this observation was already sent within the dedup window."""
    now = time.time()
    try:
        if DEDUP_FILE.exists():
            entries = json.loads(DEDUP_FILE.read_text())
        else:
            entries = {}
    except Exception:
        entries = {}

    # Clean old entries
    entries = {k: v for k, v in entries.items() if now - v < DEDUP_WINDOW}

    if hash_val in entries:
        return True

    entries[hash_val] = now
    # Keep only last 10
    if len(entries) > 10:
        sorted_entries = sorted(entries.items(), key=lambda x: x[1])
        entries = dict(sorted_entries[-10:])

    try:
        DEDUP_FILE.write_text(json.dumps(entries))
    except Exception:
        pass

    return False


def send_to_server(payload: dict, config: dict) -> None:
    """Send observation to open-brain server. Fire-and-forget."""
    import urllib.request
    import urllib.error

    url = f"{config['server_url'].rstrip('/')}/api/ingest"
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
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Fire-and-forget: don't block Claude Code


def main():
    config = load_config()
    if config is None:
        # Not configured — silent no-op
        print(json.dumps({"continue": True}))
        return

    # Read hook data from stdin
    try:
        raw = sys.stdin.read()
        hook_data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, Exception):
        print(json.dumps({"continue": True}))
        return

    tool_name = hook_data.get("tool_name", "")
    tool_input = hook_data.get("tool_input", {})
    tool_response = hook_data.get("tool_response", "")
    session_id = hook_data.get("session_id", "")
    cwd = hook_data.get("cwd", "")

    # Skip check
    if should_skip(tool_name, str(tool_response), config):
        print(json.dumps({"continue": True}))
        return

    # Dedup check
    h = content_hash({"tool_name": tool_name, "tool_input": tool_input})
    if is_duplicate(h):
        print(json.dumps({"continue": True}))
        return

    # Detect project
    project = config.get("project", "auto")
    if project == "auto":
        project = detect_project(cwd or None)

    # Build payload
    payload = {
        "hook_type": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": truncate(json.dumps(tool_input) if isinstance(tool_input, dict) else str(tool_input)),
        "tool_response": truncate(str(tool_response)),
        "project": project,
        "session_id": session_id,
        "cwd": cwd,
    }

    # Log locally
    ensure_log_dir()
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps({**payload, "timestamp": time.time()}) + "\n")
    except Exception:
        pass

    # Send to server
    send_to_server(payload, config)

    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
