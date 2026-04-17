#!/usr/bin/env python3
"""Stop/SubagentStop hook: log turn data to .worktree-turns.jsonl in worktree root.

Only activates when running inside a Claude Code worktree
(path contains '.claude/worktrees/'). No-op otherwise.
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# ─── Worktree detection ────────────────────────────────────────────────────────


def _is_worktree(cwd: str) -> bool:
    """Return True if cwd is inside a Claude Code worktree."""
    return ".claude/worktrees/" in cwd


# ─── Bead ID extraction ───────────────────────────────────────────────────────


def _extract_bead_id(path: str) -> str | None:
    """Extract bead ID from a worktree path segment like 'bead-open-brain-krx'."""
    match = re.search(r"(?:^|/)\.claude/worktrees/bead-([a-z0-9][a-z0-9-]+?)(?:/|$)", path)
    if match:
        return match.group(1)
    return None


# ─── Transcript parsing ───────────────────────────────────────────────────────


def _extract_text_from_content(content: list | str) -> str:
    """Extract plain text from a message content field (str or list of blocks)."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return " ".join(parts)


KNOWN_TARGET_KEYS = ("file_path", "path", "command", "pattern", "query", "url", "prompt", "cmd")


def _extract_tool_calls(content: list | str) -> list[dict]:
    """Extract tool_use items from a message content list."""
    if not isinstance(content, list):
        return []
    calls = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name", "")
            inp = block.get("input", {})
            # Determine target using priority list of known keys, then first string value
            target = next(
                (inp[k] for k in KNOWN_TARGET_KEYS if k in inp and isinstance(inp[k], str)),
                next((v for v in inp.values() if isinstance(v, str)), ""),
            )
            # Truncate long targets (e.g. multi-line Bash scripts)
            target = target[:200] if len(target) > 200 else target
            calls.append({"name": name, "target": target})
    return calls


def _parse_transcript(transcript_path: Path) -> dict:
    """Parse JSONL transcript and extract the last turn's user + assistant data.

    Returns a dict with user_input_excerpt, assistant_summary_excerpt, tool_calls.
    On any error returns empty values (fault-tolerant).
    """
    empty = {"user_input_excerpt": "", "assistant_summary_excerpt": "", "tool_calls": []}

    try:
        if not transcript_path.exists():
            return empty

        lines = transcript_path.read_text(encoding="utf-8").splitlines()
        entries = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        # Find last real user entry (skip tool_result-only entries)
        last_user_idx = -1
        for i, entry in enumerate(entries):
            if entry.get("type") == "user":
                msg = entry.get("message", {})
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    # Skip entries that are purely tool_result blocks (not actual user input)
                    if isinstance(content, list):
                        has_text = any(
                            isinstance(b, dict) and b.get("type") == "text"
                            for b in content
                        )
                        if not has_text:
                            continue
                    last_user_idx = i

        user_excerpt = ""
        if last_user_idx >= 0:
            msg = entries[last_user_idx].get("message", {})
            text = _extract_text_from_content(msg.get("content", ""))
            user_excerpt = text[:500]

        # Collect assistant entries after last user
        assistant_texts = []
        tool_calls = []
        start = last_user_idx + 1 if last_user_idx >= 0 else 0
        for entry in entries[start:]:
            if entry.get("type") == "assistant":
                msg = entry.get("message", {})
                content = msg.get("content", [])
                text = _extract_text_from_content(content)
                if text:
                    assistant_texts.append(text)
                tool_calls.extend(_extract_tool_calls(content))

        assistant_excerpt = " ".join(assistant_texts)[:500]

        return {
            "user_input_excerpt": user_excerpt,
            "assistant_summary_excerpt": assistant_excerpt,
            "tool_calls": tool_calls,
        }
    except Exception as exc:
        print(f"[worktree_turn_log] transcript parse error: {exc}", file=sys.stderr)
        return empty


# ─── Git helpers ──────────────────────────────────────────────────────────────


def _git_run(args: list[str], cwd: str | None = None) -> str | None:
    """Run a git command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True, text=True, timeout=5,
            cwd=cwd,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        print(f"[worktree_turn_log] _git_run failed: {e}", file=sys.stderr)
    return None


def _get_git_common_dir(cwd: str) -> Path | None:
    """Return the git common dir (shared .git for worktrees)."""
    out = _git_run(["rev-parse", "--git-common-dir"], cwd=cwd)
    if out:
        p = Path(out)
        if not p.is_absolute():
            p = Path(cwd) / p
        return p.resolve()
    return None


def _get_worktree_root(cwd: str) -> str | None:
    """Extract the worktree root from a cwd that contains .claude/worktrees/."""
    p = Path(cwd)
    for parent in [p, *p.parents]:
        if parent.parent.name == "worktrees" and parent.parent.parent.name == ".claude":
            return str(parent)
    return None


def _get_worktree_toplevel(cwd: str) -> str | None:
    """Return the current worktree's top-level directory."""
    out = _git_run(["rev-parse", "--show-toplevel"], cwd=cwd)
    return out


def _get_main_repo_root(cwd: str) -> str | None:
    """Return the main repo root even from inside a linked worktree.

    In a linked worktree, `git rev-parse --git-common-dir` returns a path like:
      /repo/.git/worktrees/<name>   (linked worktree)
    or:
      /repo/.git                    (main worktree)

    We normalise both to return the parent of the .git directory.
    """
    result = _git_run(["rev-parse", "--git-common-dir"], cwd=cwd)
    if result is None:
        return None
    git_common = Path(result.strip())
    # Make absolute if relative (rare, but git can return relative paths)
    if not git_common.is_absolute():
        git_common = Path(cwd) / git_common
    git_common = git_common.resolve()
    # Walk up until we find the directory named ".git"
    # For /repo/.git                   -> parent is /repo
    # For /repo/.git/worktrees/<name>  -> we need to find /repo/.git first
    for part in [git_common, *git_common.parents]:
        if part.name == ".git":
            return str(part.parent)
    return None


def _ensure_exclude(git_common_dir: Path) -> None:
    """Idempotently add /.worktree-turns.jsonl to git exclude file."""
    try:
        exclude_file = git_common_dir / "info" / "exclude"
        if not exclude_file.exists():
            exclude_file.parent.mkdir(parents=True, exist_ok=True)
            exclude_file.touch(mode=0o644)
            content = ""
        else:
            content = exclude_file.read_text(encoding="utf-8")
        existing_lines = set(content.splitlines())
        if "/.worktree-turns.jsonl" not in existing_lines:
            if not content.endswith("\n"):
                content += "\n"
            content += "/.worktree-turns.jsonl\n"
            exclude_file.write_text(content, encoding="utf-8")
    except Exception as exc:
        print(f"[worktree_turn_log] exclude update error: {exc}", file=sys.stderr)


# ─── Main logic ───────────────────────────────────────────────────────────────


def handle(hook_data: dict) -> dict | None:
    """Core handler: write turn log entry if in a worktree, else no-op."""
    cwd = hook_data.get("cwd", "")
    session_id = hook_data.get("session_id", "")
    parent_session_id = hook_data.get("parent_session_id", None)
    hook_type = hook_data.get("hook_event_name", "Stop")
    transcript_path_str = hook_data.get("transcript_path", "")

    if not _is_worktree(cwd):
        print(json.dumps({"continue": True}))
        return None

    # Parse transcript
    transcript_path = Path(transcript_path_str) if transcript_path_str else Path("/nonexistent")
    turn_data = _parse_transcript(transcript_path)

    # Extract bead_id from path
    bead_id = _extract_bead_id(cwd)

    # Derive worktree root from cwd (handles subdirectory cwds correctly)
    worktree_root = _get_worktree_root(cwd) or cwd

    # Get worktree relative path (relative to main repo root, not worktree toplevel)
    # Using --git-common-dir correctly handles linked worktrees where
    # --show-toplevel returns the worktree root itself (yielding "." when relativized).
    main_root = _get_main_repo_root(cwd)
    if main_root:
        try:
            worktree_rel = str(Path(worktree_root).relative_to(main_root))
        except ValueError:
            worktree_rel = worktree_root
    else:
        worktree_rel = worktree_root

    # Build record
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": "claude-code",
        "session_id": session_id,
        "parent_session_id": parent_session_id,
        "hook_type": hook_type,
        "worktree": worktree_rel,
        "bead_id": bead_id,
        "user_input_excerpt": turn_data["user_input_excerpt"],
        "assistant_summary_excerpt": turn_data["assistant_summary_excerpt"],
        "tool_calls": turn_data["tool_calls"],
    }

    # Write to .worktree-turns.jsonl in worktree root
    try:
        output_file = Path(worktree_root) / ".worktree-turns.jsonl"
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        print(f"[worktree_turn_log] write error: {exc}", file=sys.stderr)

    # Self-healing: update git exclude
    try:
        git_common_dir = _get_git_common_dir(cwd)
        if git_common_dir:
            _ensure_exclude(git_common_dir)
    except Exception as exc:
        print(f"[worktree_turn_log] exclude error: {exc}", file=sys.stderr)

    print(json.dumps({"continue": True}))
    return None


def main() -> None:
    """Entry point called by Claude Code hook."""
    try:
        raw = sys.stdin.read()
        hook_data = json.loads(raw) if raw.strip() else {}
    except Exception:
        hook_data = {}

    try:
        handle(hook_data)
    except Exception as exc:
        print(f"[worktree_turn_log] unexpected error: {exc}", file=sys.stderr)
        print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
