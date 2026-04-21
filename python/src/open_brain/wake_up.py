"""Wake-up pack: categorized, token-budgeted memory injection for session start."""

from __future__ import annotations

import logging

from open_brain.data_layer.interface import Memory, rank_importance

logger = logging.getLogger(__name__)

CATEGORY_ORDER = ["identity", "decisions", "constraints", "errors", "project"]
CATEGORY_DISPLAY = {
    "identity": "Identity",
    "decisions": "Decisions",
    "constraints": "Constraints",
    "errors": "Errors",
    "project": "Project",
    "context": "Context",
}


def token_estimate(text: str) -> int:
    """Rough token count estimate: len(text) // 4."""
    return len(text) // 4


def classify_memory(memory: Memory) -> str:
    """Classify a memory into one of the 6 buckets.

    Returns: "identity" | "decisions" | "constraints" | "errors" | "project" | "context"
    """
    t = memory.type or ""
    meta_cat = (memory.metadata or {}).get("category", "")
    stability = memory.stability or ""
    project_name = memory.project_name or ""

    if t == "identity" or meta_cat == "identity":
        return "identity"
    if t == "decision" or meta_cat == "decision":
        return "decisions"
    if t == "constraint" or meta_cat == "constraint" or (
        stability == "canonical" and t in ("rule", "policy")
    ):
        return "constraints"
    if t == "error_resolved" or meta_cat == "error":
        return "errors"
    if project_name or meta_cat == "project":
        return "project"
    return "context"


def _importance_rank(importance: str) -> int:
    """Return importance rank (3=critical, 2=high, 1=medium, 0=low). Unknown values → 0 with warning."""
    known = {"critical": 3, "high": 2, "medium": 1, "low": 0}
    if importance not in known:
        logger.warning("Unknown importance value %r, treating as low", importance)
    return known.get(importance, 0)


def _sort_key(memory: Memory) -> tuple[int, float, int, float]:
    """Sort key for descending order: importance rank, priority, access_count, updated_at (newest first)."""
    imp = memory.importance if memory.importance in ("critical", "high", "medium", "low") else "low"
    rank = _importance_rank(imp)
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(memory.updated_at.replace("Z", "+00:00"))
        ts = -dt.timestamp()
    except Exception:
        ts = 0.0
    return (-rank, -memory.priority, -memory.access_count, ts)


def _format_entry(memory: Memory) -> str:
    """Format a memory as a single markdown list item."""
    title = memory.title or memory.content[:60]
    importance = memory.importance or "medium"
    content_preview = memory.content[:200] if memory.content else ""
    return f"- **{title}** ({importance}): {content_preview}"


def build_wake_up_pack(memories: list[Memory], token_budget: int = 500) -> str:
    """Build a token-budgeted, categorized wake-up pack from a list of memories.

    Algorithm:
    1. Classify each memory into one of 6 buckets.
    2. Sort each bucket by importance DESC, priority DESC, access_count DESC, updated_at DESC.
    3. Process CATEGORY_ORDER first, then "context" last.
    4. For each category: add header + entries until budget is exhausted.
    5. Empty categories are omitted.
    6. "context" section only added if budget remains after named categories.

    Returns:
        Markdown-formatted string with sections per category.
    """
    if token_budget <= 0:
        return ""

    # Step 1: Classify
    buckets: dict[str, list[Memory]] = {cat: [] for cat in CATEGORY_ORDER + ["context"]}
    for memory in memories:
        cat = classify_memory(memory)
        buckets[cat].append(memory)

    # Step 2: Sort each bucket
    for cat in buckets:
        buckets[cat].sort(key=_sort_key)

    # Step 3-5: Build output within token budget
    output_parts: list[str] = []
    remaining = token_budget

    for category in CATEGORY_ORDER + ["context"]:
        entries = buckets[category]
        if not entries:
            continue

        header = f"## {CATEGORY_DISPLAY[category]}\n"
        header_tokens = token_estimate(header)
        if header_tokens > remaining:
            break

        section_lines: list[str] = [header]
        section_tokens = header_tokens

        for memory in entries:
            line = _format_entry(memory) + "\n"
            line_tokens = token_estimate(line)
            if line_tokens > remaining - section_tokens:
                break  # drop this and all lower-ranked entries
            section_lines.append(line)
            section_tokens += line_tokens

        # Only include section if it has at least one entry beyond the header
        if len(section_lines) > 1:
            section_text = "".join(section_lines)
            output_parts.append(section_text)
            remaining -= token_estimate(section_text)

    return "".join(output_parts)
