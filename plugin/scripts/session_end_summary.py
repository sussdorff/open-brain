#!/usr/bin/env python3
"""SessionEnd hook: read transcript, filter turns, POST to /api/session-end.

This is a safety-net writer: it fires on every SessionEnd and sends filtered
transcript turns to the server for async summarization. The server performs
dedup so re-fires are harmless.
"""

import json
import logging
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import load_config, detect_project

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("session-end-summary")

# Reasons that should NOT trigger a session summary
_SKIP_REASONS = {"clear", "resume", "logout", "bypass_permissions_disabled"}

_MAX_TURNS_TEXT = 8000


def _filter_turns(lines: list[str]) -> list[dict]:
    """Parse JSONL lines and filter to valid user/assistant turns."""
    turns = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue
        if entry.get("isMeta") is True:
            continue
        content = entry.get("content", "")
        if not isinstance(content, str):
            continue
        turns.append({"type": entry["type"], "content": content, "isMeta": False})
    return turns


def _truncate_turns_text(turns: list[dict]) -> str:
    """Join turns text and truncate to _MAX_TURNS_TEXT chars (head + tail)."""
    lines = [f"[{t['type'].upper()}] {t['content']}" for t in turns]
    text = "\n".join(lines)
    if len(text) > _MAX_TURNS_TEXT:
        half = _MAX_TURNS_TEXT // 2
        text = text[:half] + "\n\n[...truncated...]\n\n" + text[-half:]
    return text


def main() -> None:
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
    reason = hook_data.get("reason", "")
    transcript_path = hook_data.get("transcript_path", "")

    # Skip non-meaningful session ends
    if reason in _SKIP_REASONS:
        print(json.dumps({"continue": True}))
        return

    # Detect project
    project = config.get("project", "auto")
    if project == "auto":
        project = detect_project(cwd or None)

    # Read transcript
    if not transcript_path:
        print(json.dumps({"continue": True}))
        return

    transcript_file = Path(transcript_path)
    if not transcript_file.exists():
        logger.warning("session-end-summary: transcript not found: %s", transcript_path)
        print(json.dumps({"continue": True}))
        return

    try:
        lines = transcript_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        logger.warning("session-end-summary: failed to read transcript: %s", exc)
        print(json.dumps({"continue": True}))
        return

    turns = _filter_turns(lines)
    if not turns:
        print(json.dumps({"continue": True}))
        return

    # POST to /api/session-end (fire-and-forget)
    url = f"{config['server_url'].rstrip('/')}/api/session-end"
    payload = {
        "session_id": session_id,
        "project": project,
        "turns": turns,
        "reason": reason,
        "transcript_path": transcript_path,
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
        pass  # Fire-and-forget — never block the session

    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
