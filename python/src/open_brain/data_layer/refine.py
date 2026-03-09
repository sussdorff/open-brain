"""LLM-powered memory consolidation and refinement."""

import json
import logging
import re

from open_brain.config import get_config
from open_brain.data_layer.interface import Memory, RefineAction
from open_brain.data_layer.llm import LlmMessage, llm_complete

logger = logging.getLogger(__name__)

ANALYSIS_BATCH_SIZE = 50


def _parse_json_array(text: str) -> list[dict]:
    """Extract and parse a JSON array from LLM text, with repair for common issues."""
    json_match = re.search(r"\[[\s\S]*\]", text)
    if not json_match:
        return []

    raw = json_match.group()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try to repair: remove trailing commas before ] or }
        repaired = re.sub(r",\s*([}\]])", r"\1", raw)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as err:
            logger.warning("JSON repair failed: %s", err)
            return []


async def _analyze_batch(memories: list[Memory]) -> list[RefineAction]:
    """Analyze a single batch of memories with the LLM."""
    memory_summary = "\n".join(
        f"[{m.id}] ({m.type}, priority={m.priority:.2f}, stability={m.stability}) "
        f"{m.title or ''}: {(m.content or m.narrative or '')[:200]}"
        for m in memories
    )

    text = await llm_complete(
        [
            LlmMessage(
                role="user",
                content=f"""Analyze these memories and suggest consolidation actions. Return JSON array of actions.

Memories:
{memory_summary}

Rules:
- "merge": combine near-duplicate memories (keep the better one, delete others). Requires at least 2 IDs.
- "promote": change stability from tentative->stable or stable->canonical for high-quality, frequently-accessed memories
- "demote": lower priority for outdated or low-quality memories
- "delete": remove truly redundant or obsolete memories
- IMPORTANT: Each memory ID must appear in AT MOST ONE action. Never reference the same ID in multiple actions.

Return ONLY a JSON array like:
[{{"action":"merge","memory_ids":[1,2],"reason":"Near-duplicate observations about X"}},{{"action":"promote","memory_ids":[5],"reason":"High-quality canonical knowledge"}}]

If no actions needed, return [].""",
            )
        ]
    )

    raw_actions = _parse_json_array(text)
    return [
        RefineAction(
            action=a["action"],
            memory_ids=a["memory_ids"],
            reason=a.get("reason", ""),
            executed=False,
        )
        for a in raw_actions
        if len(a.get("memory_ids", [])) >= (2 if a["action"] == "merge" else 1)
    ]


async def analyze_with_llm(memories: list[Memory]) -> list[RefineAction]:
    """Analyze memories with LLM and suggest consolidation actions.

    Batches large sets into chunks to avoid LLM context limits.
    Falls back to simple duplicate detection if no LLM key is configured.
    """
    config = get_config()
    has_key = (
        bool(config.OPENROUTER_API_KEY)
        if config.LLM_PROVIDER == "openrouter"
        else bool(config.ANTHROPIC_API_KEY)
    )

    if not has_key:
        return find_obvious_duplicates(memories)

    try:
        all_actions: list[RefineAction] = []
        for i in range(0, len(memories), ANALYSIS_BATCH_SIZE):
            batch = memories[i : i + ANALYSIS_BATCH_SIZE]
            logger.info("Analyzing batch %d-%d of %d memories", i, i + len(batch), len(memories))
            actions = await _analyze_batch(batch)
            all_actions.extend(actions)
        return all_actions
    except Exception as err:
        logger.error("LLM analysis error: %s", err)
        return find_obvious_duplicates(memories)


async def merge_memories_with_llm(memories: list[Memory]) -> dict[str, str]:
    """Ask the LLM to produce a combined version of memories being merged.

    Args:
        memories: The memories to merge (first one will be updated, rest deleted).

    Returns:
        Dict with updated fields: title, subtitle, narrative, content, type.
    """
    memory_details = "\n---\n".join(
        f"[{m.id}] type={m.type}, priority={m.priority}, stability={m.stability}\n"
        f"Title: {m.title or '(none)'}\n"
        f"Subtitle: {m.subtitle or '(none)'}\n"
        f"Narrative: {m.narrative or '(none)'}\n"
        f"Content: {m.content or '(none)'}"
        for m in memories
    )

    text = await llm_complete(
        [
            LlmMessage(
                role="user",
                content=f"""Merge these memories into a single consolidated version. Combine the best information from all sources into one coherent memory.

Memories to merge:
{memory_details}

Return ONLY a JSON object with these fields:
- "type": the best fitting type (discovery/change/feature/decision/bugfix/refactor/test)
- "title": concise title for the merged memory (max 80 chars)
- "subtitle": one-sentence summary (max 150 chars)
- "narrative": consolidated narrative — keep key facts, decisions, and specifics but eliminate redundancy and verbose descriptions. Aim for 3-8 sentences, not walls of text.
- "content": any structured content to preserve (empty string if narrative covers everything)

Prioritize density over completeness: a tight summary that captures the essential knowledge is better than a verbose narrative that preserves every detail.""",
        )
    ]
    )

    json_match = re.search(r"\{[\s\S]*\}", text)
    if not json_match:
        logger.warning("LLM merge returned no JSON, keeping original")
        return {}

    try:
        return json.loads(json_match.group())
    except json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", json_match.group())
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as err:
            logger.warning("LLM merge JSON repair failed: %s", err)
            return {}


def find_obvious_duplicates(memories: list[Memory]) -> list[RefineAction]:
    """Find obvious duplicates by title/content prefix without LLM.

    Args:
        memories: List of Memory objects to check

    Returns:
        List of merge actions for duplicate groups
    """
    by_title: dict[str, list[Memory]] = {}
    for m in memories:
        key = (m.title or m.content[:50]).lower().strip()
        by_title.setdefault(key, []).append(m)

    actions: list[RefineAction] = []
    for group in by_title.values():
        if len(group) > 1:
            actions.append(
                RefineAction(
                    action="merge",
                    memory_ids=[m.id for m in group],
                    reason=f'Duplicate title/content: "{group[0].title or group[0].content[:50]}"',
                    executed=False,
                )
            )
    return actions
