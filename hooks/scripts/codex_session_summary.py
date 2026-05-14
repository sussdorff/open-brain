#!/usr/bin/env python3
"""Codex Stop hook: read Codex JSONL transcript and POST to /api/session-end."""

import json
import logging
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from config import load_config, resolve_project

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("codex-session-summary")

_SKIP_REASONS = {"clear", "resume", "logout", "bypass_permissions_disabled"}


def _extract_text(content: Any) -> str:
    """Extract text from Codex message content blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    chunks: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"input_text", "output_text", "text"}:
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                chunks.append(text)
    return "\n".join(chunks)


def _filter_codex_turns(lines: list[str]) -> list[dict]:
    """Parse Codex rollout JSONL and return user/assistant message turns."""
    turns: list[dict] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "response_item":
            continue

        payload = entry.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "message":
            continue

        role = payload.get("role")
        if role not in {"user", "assistant"}:
            continue

        content = _extract_text(payload.get("content", ""))
        if not content.strip():
            continue

        turns.append({"type": role, "content": content, "isMeta": False})
    return turns


def _session_meta(lines: list[str]) -> dict:
    """Return Codex session_meta payload if present."""
    for raw in lines:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("type") == "session_meta":
            payload = entry.get("payload", {})
            return payload if isinstance(payload, dict) else {}
    return {}


def _post_session_end(config: dict, payload: dict) -> None:
    """POST transcript turns to OpenBrain without blocking the harness."""
    url = f"{config['server_url'].rstrip('/')}/api/session-end"
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
        pass


def main() -> None:
    config = load_config()
    if config is None:
        print(json.dumps({"continue": True}))
        return

    try:
        raw = sys.stdin.read()
        hook_data = json.loads(raw) if raw.strip() else {}
    except Exception:
        hook_data = {}

    reason = hook_data.get("reason", "") or "stop"
    if reason in _SKIP_REASONS:
        print(json.dumps({"continue": True}))
        return

    transcript_path = hook_data.get("transcript_path", "")
    if not transcript_path:
        print(json.dumps({"continue": True}))
        return

    transcript_file = Path(transcript_path)
    if not transcript_file.exists():
        logger.warning("codex-session-summary: transcript not found: %s", transcript_path)
        print(json.dumps({"continue": True}))
        return

    try:
        lines = transcript_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        logger.warning("codex-session-summary: failed to read transcript: %s", exc)
        print(json.dumps({"continue": True}))
        return

    turns = _filter_codex_turns(lines)
    if not turns:
        print(json.dumps({"continue": True}))
        return

    meta = _session_meta(lines)
    cwd = hook_data.get("cwd") or meta.get("cwd") or ""
    session_id = hook_data.get("session_id") or meta.get("id") or ""
    if not session_id:
        print(json.dumps({"continue": True}))
        return

    project = resolve_project(config, cwd or None)
    payload = {
        "session_id": session_id,
        "project": project,
        "turns": turns,
        "reason": reason,
        "transcript_path": transcript_path,
    }
    _post_session_end(config, payload)

    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
