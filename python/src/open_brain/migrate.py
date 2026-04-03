"""Migration helpers for importing memories from JSONL files."""

from __future__ import annotations

import json


def parse_jsonl_line(line: str) -> dict | None:
    """Parse a single JSONL line. Returns None for blank or malformed lines."""
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
        if not isinstance(data, dict) or "text" not in data:
            return None
        return data
    except json.JSONDecodeError:
        return None


def parse_jsonl_batch(content: str) -> tuple[list[dict], int]:
    """Parse JSONL content. Returns (valid_items, error_count)."""
    items = []
    errors = 0
    for line in content.splitlines():
        if not line.strip():
            continue
        result = parse_jsonl_line(line)
        if result is None:
            errors += 1
        else:
            items.append(result)
    return items, errors
