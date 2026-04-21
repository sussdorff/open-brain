"""Transcript-based session-summary regeneration.

Reads raw JSONL transcripts from ~/.claude/projects/ and regenerates
session_summary memories for matching sessions.
"""

import glob
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from open_brain.data_layer.interface import DataLayer, DeleteParams, SearchParams
from open_brain.data_layer.postgres import get_pool
from open_brain.session_summary import summarize_transcript_turns

logger = logging.getLogger(__name__)


# ─── Params & Result dataclasses ─────────────────────────────────────────────


@dataclass
class RegenerateParams:
    """Parameters for regenerate_summaries_from_transcripts."""

    scope: str | None = None
    transcript_root: str = "~/.claude/projects/"
    dry_run: bool = True
    delete_orphans: bool = False


@dataclass
class SessionPlan:
    """Plan for a single session's regeneration action."""

    session_ref: str
    existing_memory_ids: list[int]
    transcript_found: bool
    transcript_path: str | None
    transcript_turn_count: int | None
    action: str  # "regenerate"|"orphan-skip"|"orphan-compact"|"already-backfilled-skip"


@dataclass
class RegenerateResult:
    """Result of regenerate_summaries operation."""

    regenerated_count: int
    orphan_count: int
    transcript_missing_list: list[str]
    new_summary_ids: list[int]
    failures: list[dict[str, Any]] = field(default_factory=list)
    plan: list[SessionPlan] = field(default_factory=list)


# ─── Transcript helpers ───────────────────────────────────────────────────────


def _find_transcript(session_ref: str, transcript_root: str) -> Path | None:
    """Glob for a JSONL transcript matching session_ref under transcript_root."""
    root = Path(transcript_root).expanduser()
    pattern = str(root / "*" / f"{session_ref}.jsonl")
    matches = sorted(glob.glob(pattern))
    if len(matches) > 1:
        logger.warning(
            "Multiple transcripts for session_ref=%s: %s — using first",
            session_ref,
            matches,
        )
    if matches:
        return Path(matches[0])
    # Also check directly under root (no subdirectory)
    direct = root / f"{session_ref}.jsonl"
    if direct.exists():
        return direct
    return None


def _parse_raw_jsonl_turns(path: Path) -> list[dict[str, Any]]:
    """Parse a raw JSONL transcript into normalized turns.

    Normalizes raw JSONL entries by promoting ``message.content`` to the
    top-level ``content`` field so that the shared ``_build_turns_text``
    / ``_is_valid_turn`` logic in session_summary.py handles all filtering
    (type, isMeta, empty content, list vs string content).

    Returns a list of entry dicts with a top-level ``content`` key (raw value
    preserved — may be str or list[block]).
    """
    from open_brain.session_summary import _is_valid_turn

    turns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue

        # Promote message.content to top-level content so the shared predicate
        # and _build_turns_text can handle both formats uniformly.
        if "content" not in entry:
            msg = entry.get("message", {})
            if isinstance(msg, dict) and "content" in msg:
                entry = {**entry, "content": msg["content"]}

        if not _is_valid_turn(entry):
            continue

        turns.append(entry)
    return turns


def _extract_project_from_scope(scope: str | None) -> str | None:
    """Extract project name from a scope string like 'project:open-brain'."""
    if scope and scope.startswith("project:"):
        return scope[len("project:"):]
    return None


async def _resolve_index_name(index_id: int | None) -> str | None:
    """Look up the name of a memory index by its ID.

    Returns the index name, or None if not found or index_id is None.
    """
    if index_id is None:
        return None
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name FROM memory_indexes WHERE id = $1",
                index_id,
            )
            return row["name"] if row else None
    except Exception:
        logger.debug("_resolve_index_name: failed for index_id=%s", index_id, exc_info=True)
        return None


# ─── Core orchestration ───────────────────────────────────────────────────────


async def regenerate_summaries(
    dl: DataLayer,
    params: RegenerateParams,
) -> RegenerateResult:
    """Regenerate session_summary memories from raw JSONL transcripts.

    Algorithm:
    1. Fetch all session_summary memories (filtered by scope if provided).
    2. For each unique session_ref, find its transcript.
    3. Build a plan (dry_run=True: return plan only; dry_run=False: execute).
    4. For each session with a transcript: call summarize_transcript_turns with
       bypass_dedup=True (replace mode deletes old row atomically).
    5. For orphans (no transcript): skip or delete depending on delete_orphans.

    Returns RegenerateResult with counts and plan.
    """
    # Build search params
    project = _extract_project_from_scope(params.scope)
    search_params = SearchParams(
        type="session_summary",
        project=project,
        limit=1000,
    )
    search_result = await dl.search(search_params)
    memories = search_result.results

    # Group memories by session_ref
    sessions: dict[str, list[Any]] = {}
    no_session_ref: list[Any] = []
    for mem in memories:
        if not mem.session_ref:
            no_session_ref.append(mem)
            continue
        sessions.setdefault(mem.session_ref, []).append(mem)

    plan: list[SessionPlan] = []
    regenerated_count = 0
    orphan_count = 0
    transcript_missing_list: list[str] = []
    new_summary_ids: list[int] = []
    failures: list[dict[str, Any]] = []

    for session_ref, mems in sessions.items():
        existing_ids = [m.id for m in mems]

        # Check if already backfilled — skip if ANY memory has source=transcript-backfill
        already_backfilled = any(
            (m.metadata or {}).get("source") == "transcript-backfill"
            for m in mems
        )
        if already_backfilled:
            plan.append(SessionPlan(
                session_ref=session_ref,
                existing_memory_ids=existing_ids,
                transcript_found=False,
                transcript_path=None,
                transcript_turn_count=None,
                action="already-backfilled-skip",
            ))
            continue

        # Find transcript
        transcript_path = _find_transcript(session_ref, params.transcript_root)

        if transcript_path is None:
            # Orphan: no transcript found
            orphan_count += 1
            transcript_missing_list.append(session_ref)
            action = "orphan-compact" if params.delete_orphans else "orphan-skip"
            plan.append(SessionPlan(
                session_ref=session_ref,
                existing_memory_ids=existing_ids,
                transcript_found=False,
                transcript_path=None,
                transcript_turn_count=None,
                action=action,
            ))
            if not params.dry_run and params.delete_orphans:
                try:
                    await dl.delete_memories(DeleteParams(ids=existing_ids))
                except Exception as exc:
                    failures.append({"session_ref": session_ref, "error": str(exc)})
            continue

        # Transcript found
        turns = _parse_raw_jsonl_turns(transcript_path)
        plan.append(SessionPlan(
            session_ref=session_ref,
            existing_memory_ids=existing_ids,
            transcript_found=True,
            transcript_path=str(transcript_path),
            transcript_turn_count=len(turns),
            action="regenerate",
        ))

        if params.dry_run:
            continue

        # Determine project name for summarization.
        # Prefer scope-derived project, then look up from the memory's index_id.
        if project:
            mem_project: str = project
        else:
            first_mem = mems[0]
            index_id_val = getattr(first_mem, "index_id", None)
            resolved = await _resolve_index_name(index_id_val)
            if resolved is None:
                logger.debug(
                    "regenerate: could not resolve project for session_ref=%s index_id=%s, using 'unknown'",
                    session_ref,
                    index_id_val,
                )
            mem_project = resolved or "unknown"

        try:
            new_id = await summarize_transcript_turns(
                project=mem_project,
                session_id=session_ref,
                turns=turns,
                source="transcript-backfill",
                bypass_dedup=True,
            )
            if new_id is not None:
                regenerated_count += 1
                new_summary_ids.append(new_id)
        except Exception as exc:
            logger.exception(
                "regenerate: failed for session_ref=%s", session_ref
            )
            failures.append({"session_ref": session_ref, "error": str(exc)})

    return RegenerateResult(
        regenerated_count=regenerated_count,
        orphan_count=orphan_count,
        transcript_missing_list=transcript_missing_list,
        new_summary_ids=new_summary_ids,
        failures=failures,
        plan=plan,
    )
