"""LLM-powered memory triage: classify memories into lifecycle actions."""

from __future__ import annotations

import json
import logging
import re

from open_brain.data_layer.interface import Memory, TriageAction
from open_brain.data_layer.llm import LlmMessage, llm_complete

logger = logging.getLogger(__name__)

TRIAGE_BATCH_SIZE = 20

# Type-specific default actions when LLM is unavailable
_TYPE_DEFAULTS: dict[str, str] = {
    "learning": "promote",
    "session_summary": "archive",
    "observation": "keep",
}


def _default_action_for_type(memory_type: str) -> str:
    """Return the default triage action for a given memory type."""
    return _TYPE_DEFAULTS.get(memory_type, "keep")


def _parse_json_array(text: str) -> list[dict]:
    """Extract and parse a JSON array from LLM text, with repair for common issues."""
    json_match = re.search(r"\[[\s\S]*\]", text)
    if not json_match:
        return []

    raw = json_match.group()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", raw)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as err:
            logger.warning("JSON repair failed: %s", err)
            return []


async def _triage_batch(memories: list[Memory]) -> list[TriageAction]:
    """Ask the LLM to triage a batch of memories."""
    memory_summary = "\n".join(
        f"[{m.id}] type={m.type}, priority={m.priority:.2f}, stability={m.stability}, "
        f"access_count={m.access_count} | {m.title or '(no title)'}: {(m.content or m.narrative or '')[:200]}"
        for m in memories
    )

    text = await llm_complete(
        [
            LlmMessage(
                role="user",
                content=f"""Classify each memory into a lifecycle action. Return a JSON array.

Memories:
{memory_summary}

Actions and when to use them:
- "keep": valuable, factual, or recently accessed observation — retain as-is
- "merge": near-duplicate of another memory in this list — include BOTH IDs in a note
- "promote": reusable knowledge or a learned pattern that belongs in standards documentation (especially "learning" type)
- "scaffold": an identified task, todo, or improvement that should become a work item (bead/ticket)
- "archive": transient or low-value memory (e.g. session summaries, outdated observations)

Type-specific guidance:
- "learning" type:
  - "promote" if it's a reusable workflow pattern, best practice, or convention applicable across projects
  - "keep" if it's domain-specific knowledge (billing rules, DB schemas, API quirks) — valuable as searchable reference but not a standard
  - "archive" if it states something already implemented or no longer actionable ("already mapped", "no change needed")
  - "merge" if it overlaps significantly with another learning in this batch
- "session_summary" type → prefer "archive" unless it contains critical decisions
- "observation" type → prefer "keep" or "merge" if duplicate; "scaffold" if it describes a todo

Return ONLY a JSON array. Each item MUST have: memory_id (int), action (string), reason (string).
Example: [{{"memory_id":1,"action":"keep","reason":"Core factual knowledge"}},{{"memory_id":2,"action":"archive","reason":"Transient session note"}}]

Every memory in the input must appear exactly once in the output.""",
            )
        ],
        max_tokens=4096,
    )
    logger.debug("LLM triage raw response (first 500 chars): %s", text[:500])

    raw_items = _parse_json_array(text)
    memory_by_id = {m.id: m for m in memories}

    actions: list[TriageAction] = []
    seen_ids: set[int] = set()

    for item in raw_items:
        memory_id = item.get("memory_id")
        action = item.get("action", "keep")
        reason = item.get("reason", "")

        # Coerce string IDs to int (LLMs sometimes return "1234" instead of 1234)
        if isinstance(memory_id, str) and memory_id.isdigit():
            memory_id = int(memory_id)

        if not isinstance(memory_id, int) or memory_id not in memory_by_id:
            logger.debug("Skipping item with unrecognized memory_id=%r (known IDs: %s)", memory_id, list(memory_by_id.keys()))
            continue
        if memory_id in seen_ids:
            continue
        if action not in ("keep", "merge", "promote", "scaffold", "archive"):
            logger.warning("Unknown triage action %r for memory %d — defaulting to keep", action, memory_id)
            action = "keep"

        seen_ids.add(memory_id)
        mem = memory_by_id[memory_id]
        actions.append(
            TriageAction(
                action=action,
                memory_id=memory_id,
                reason=reason,
                memory_type=mem.type,
                memory_title=mem.title,
                executed=False,
            )
        )

    # Fill in any memories the LLM missed with type-based defaults
    for mem in memories:
        if mem.id not in seen_ids:
            logger.info("LLM missed memory %d — using type default", mem.id)
            actions.append(
                TriageAction(
                    action=_default_action_for_type(mem.type),
                    memory_id=mem.id,
                    reason="LLM did not classify this memory; using type default",
                    memory_type=mem.type,
                    memory_title=mem.title,
                    executed=False,
                )
            )

    return actions


def _triage_by_type_defaults(memories: list[Memory]) -> list[TriageAction]:
    """Classify memories using type-based heuristics (no LLM)."""
    return [
        TriageAction(
            action=_default_action_for_type(m.type),
            memory_id=m.id,
            reason=f"Type-based default for '{m.type}' (no LLM available)",
            memory_type=m.type,
            memory_title=m.title,
            executed=False,
        )
        for m in memories
    ]


async def triage_with_llm(memories: list[Memory]) -> list[TriageAction]:
    """Triage memories using LLM classification with type-aware fallback.

    Falls back to type-based defaults if no LLM key is configured or on error.
    """
    from open_brain.config import get_config

    config = get_config()
    has_key = (
        bool(config.OPENROUTER_API_KEY)
        if config.LLM_PROVIDER == "openrouter"
        else bool(config.ANTHROPIC_API_KEY)
    )

    if not has_key:
        return _triage_by_type_defaults(memories)

    try:
        all_actions: list[TriageAction] = []
        for i in range(0, len(memories), TRIAGE_BATCH_SIZE):
            batch = memories[i : i + TRIAGE_BATCH_SIZE]
            logger.info("Triaging batch %d-%d of %d memories", i, i + len(batch), len(memories))
            actions = await _triage_batch(batch)
            all_actions.extend(actions)
        return all_actions
    except Exception as err:
        logger.error("LLM triage error: %s", err)
        return _triage_by_type_defaults(memories)
