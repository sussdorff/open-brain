#!/usr/bin/env python3
"""SessionStart hook: inject recent memory context from open-brain server."""

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import load_config, detect_project


def fetch_context_json(config: dict, project: str) -> dict | None:
    """Fetch recent context as structured JSON from open-brain server."""
    limit = config.get("context_limit", 50)
    url = f"{config['server_url'].rstrip('/')}/api/context?project={project}&limit={limit}&format=json"

    req = urllib.request.Request(
        url,
        headers={"X-API-Key": config.get("api_key", "")},
    )

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def format_narrative(data: dict) -> str:
    """Format structured context data into a concise narrative summary."""
    lines: list[str] = []

    # Session summaries — show what happened recently
    sessions = data.get("sessions", [])
    if sessions:
        lines.append("## Recent Work\n")
        for s in sessions[:3]:
            title = s.get("title", "Session")
            # Prefer narrative (concise reasoning), fall back to content
            body = s.get("narrative") or (s.get("content") or "")[:300]
            lines.append(f"- **{title}**: {body}")
        lines.append("")

    # Group observations by type for compact overview
    observations = data.get("observations", [])
    if observations:
        by_type: dict[str, list[str]] = {}
        for obs in observations:
            t = obs.get("type") or "observation"
            title = obs.get("title") or ""
            if title:
                by_type.setdefault(t, []).append(title)

        lines.append("## Recent Observations\n")
        for obs_type, titles in by_type.items():
            lines.append(f"**{obs_type}** ({len(titles)}):")
            for t in titles[:3]:
                lines.append(f"- {t}")
            if len(titles) > 3:
                lines.append(f"- ... and {len(titles) - 3} more")
            lines.append("")

    return "\n".join(lines)


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

    data = fetch_context_json(config, project)

    if data:
        context_md = format_narrative(data)
        if context_md.strip():
            output = {
                "continue": True,
                "systemMessage": f"# open-brain Memory Context\n\n{context_md}",
            }
        else:
            output = {"continue": True}
    else:
        output = {"continue": True}

    print(json.dumps(output))


if __name__ == "__main__":
    main()
