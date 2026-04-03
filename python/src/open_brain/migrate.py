"""Migration helpers for importing memories from JSONL files.

Markdown/Obsidian batch parsing is intentionally LLM-driven (not a Python helper)
because the skill instructs Claude to use the Read tool to load the file and then
parse sections inline. Only JSONL has a testable Python helper since its structure
is unambiguous and machine-parseable.
"""

from __future__ import annotations

import json


def parse_jsonl_line(line: str) -> dict | None:
    """Parse a single JSONL line. Returns None for malformed lines.

    Blank-line filtering is the caller's responsibility (parse_jsonl_batch
    skips blank lines before calling this function).
    """
    line = line.strip()
    try:
        data = json.loads(line)
        if not isinstance(data, dict) or "text" not in data:
            return None
        return data
    except json.JSONDecodeError:
        return None


def parse_jsonl_batch(content: str) -> tuple[list[dict], int]:
    """Parse JSONL content. Returns (valid_items, error_count).

    Blank lines are silently skipped (not counted as errors).
    Lines that are not valid JSON or are missing the required 'text' field
    are counted as errors.
    """
    items = []
    errors = 0
    for line in content.splitlines():
        if not line.strip():
            continue  # blank lines are silently skipped
        result = parse_jsonl_line(line)
        if result is None:
            errors += 1
        else:
            items.append(result)
    return items, errors
