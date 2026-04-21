"""Reusable helper for summarizing session transcripts into memory.

Used by /api/session-end and potentially other sources (session-close, transcript-backfill).
"""

from __future__ import annotations

import logging
from typing import Any

from open_brain.data_layer.interface import SaveMemoryParams
from open_brain.data_layer.llm import LlmMessage, llm_complete
from open_brain.data_layer.postgres import PostgresDataLayer, get_pool
from open_brain.utils import parse_llm_json

logger = logging.getLogger(__name__)

_MAX_TURNS_TEXT = 8000

_dl: PostgresDataLayer | None = None


def get_dl() -> PostgresDataLayer:
    """Return or create the shared PostgresDataLayer instance."""
    global _dl
    if _dl is None:
        _dl = PostgresDataLayer()
    return _dl


def _build_turns_text(turns: list[dict[str, Any]]) -> str:
    """Filter and join transcript turns into a single text block.

    Handles two formats:
    - Flat format: {type, content: str} — used by session-end API / hooks
    - Raw JSONL format: {type, message: {content: str | list[block]}} — from ~/.claude/projects/

    Filters: type must be 'user' or 'assistant', isMeta must not be True,
    content must resolve to a non-empty string.
    """
    lines = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        if turn.get("type") not in ("user", "assistant"):
            continue
        if turn.get("isMeta") is True:
            continue

        # Try flat content first (API / hook format)
        raw_content = turn.get("content")
        # Fall back to JSONL message.content
        if raw_content is None:
            msg = turn.get("message", {})
            if isinstance(msg, dict):
                raw_content = msg.get("content")

        # Handle list content (Claude API message.content format)
        if isinstance(raw_content, list):
            parts = []
            for block in raw_content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    parts.append(f"[tool: {block.get('name', '')}]")
            content = "\n".join(p for p in parts if p)
        elif isinstance(raw_content, str):
            content = raw_content
        else:
            continue

        if not content:
            continue

        role = turn["type"].upper()
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


async def summarize_transcript_turns(
    project: str,
    session_id: str,
    turns: list[dict[str, Any]],
    source: str | None,
    reason: str | None = None,
    bypass_dedup: bool = False,
) -> int | None:
    """Summarize a session transcript and save it as a session_summary memory.

    Performs a pessimistic dedup check before writing (unless bypass_dedup=True):
    if ANY existing session_summary row exists for session_ref=session_id, skip
    and return None.

    Args:
        project: Project name.
        session_id: Session identifier (used as session_ref for dedup).
        turns: List of raw transcript turn dicts (filtered internally).
        source: Provenance marker (e.g. 'session-end-hook', 'session-close',
                'transcript-backfill', or None for legacy).
        reason: SessionEnd reason string (e.g. 'stop'), or None.
        bypass_dedup: When True, skip the dedup check — the caller (e.g.
                      regenerate_summaries) handles deletion atomically via
                      upsert_mode='replace' in save_memory.

    Returns:
        memory_id (int) on success, or None if skipped (dedup/empty turns).
    """
    turns_text = _build_turns_text(turns)
    if not turns_text.strip():
        logger.info(
            "session-end: no valid turns after filter, skipping (session_id=%s)", session_id
        )
        return None

    # Truncate to prevent context overflow (head + tail)
    if len(turns_text) > _MAX_TURNS_TEXT:
        half = _MAX_TURNS_TEXT // 2
        turns_text = turns_text[:half] + "\n\n[...truncated...]\n\n" + turns_text[-half:]

    if not bypass_dedup:
        # Dedup check: pessimistically skip if ANY session_summary row exists
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, metadata FROM memories WHERE session_ref = $1 AND type = 'session_summary' LIMIT 1",
                session_id,
            )
            if row is not None:
                logger.info(
                    "session-end dedup: existing summary found, skipping (session_ref=%s)", session_id
                )
                return None

    turn_count = sum(
        1
        for t in turns
        if isinstance(t, dict)
        and t.get("type") in ("user", "assistant")
        and not t.get("isMeta")
        and isinstance(t.get("content"), str)
        and t.get("content")
    )

    prompt = f"""Summarize this session transcript.

Project: {project}

Transcript:
{turns_text}

Respond in JSON:
- "title": short session headline (max 80 chars)
- "content": 3-5 sentence summary covering what was worked on, key actions, and outcome
- "narrative": what was learned or discovered (1-2 sentences, or null)

Respond with ONLY valid JSON, no markdown fences."""

    try:
        response = await llm_complete(
            [LlmMessage(role="user", content=prompt)],
            max_tokens=512,
        )
        summary = parse_llm_json(response)
    except Exception:
        logger.exception("session-end: LLM summarization failed (session_id=%s)", session_id)
        return None

    metadata: dict[str, Any] = {
        "source": source,
        "reason": reason,
        "turn_count": turn_count,
    }

    dl = get_dl()
    result = await dl.save_memory(
        SaveMemoryParams(
            text=summary.get("content", f"Session {session_id} ended."),
            type="session_summary",
            project=project,
            title=summary.get("title", "Session summary"),
            narrative=summary.get("narrative"),
            session_ref=session_id,
            metadata=metadata,
            upsert_mode="replace" if bypass_dedup else "append",
        )
    )
    logger.info(
        "session-end: summary saved (session_id=%s, memory_id=%s, source=%s)",
        session_id,
        result.id,
        source,
    )
    return result.id
