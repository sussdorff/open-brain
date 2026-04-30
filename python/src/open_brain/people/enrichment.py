"""People enrichment module.

Provides opt-in web search enrichment for person memories that are missing
org/role information. Enrichment candidates are person memories with the
``enrich_pending="true"`` metadata flag, which is set at ingest time for new
or ambiguous person entries.

Usage:
    candidates = await list_enrichment_candidates(dl)
    for candidate in candidates:
        results = await search_person_web(
            name=candidate.name,
            context=candidate.transcript_context,
            searxng_url=get_config().SEARXNG_URL,
        )
        if results and should_auto_apply(results[0], min_confidence=0.8):
            await apply_enrichment(dl, candidate.memory_id, results[0])
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

from open_brain.data_layer.interface import DataLayer, SearchParams, UpdateMemoryParams

logger = logging.getLogger(__name__)

# Minimum absolute confidence threshold: below this, NEVER auto-apply.
_ABSOLUTE_MIN_CONFIDENCE: float = 0.6

# Maximum results returned from SearXNG per query.
_MAX_RESULTS: int = 3


@dataclass(slots=True)
class EnrichmentCandidate:
    """A person memory that is a candidate for web enrichment.

    Attributes:
        memory_id: The open-brain memory ID of the person memory.
        name: The person's name as stored in metadata.
        transcript_context: Relevant text from the linked meeting memory,
            or empty string if no meeting context could be found.
    """

    memory_id: int
    name: str
    transcript_context: str


@dataclass(slots=True)
class EnrichmentResult:
    """Result of a web search enrichment attempt.

    Attributes:
        name: The person's name that was searched.
        org: Proposed organisation, or None if not found.
        role: Proposed role/title, or None if not found.
        profile_url: Direct profile URL (e.g. LinkedIn), or None.
        confidence: Confidence score in [0.0, 1.0].
        provenance_url: Source URL where the information was found.
        provenance_snippet: Short text snippet from the source.
    """

    name: str
    org: str | None
    role: str | None
    profile_url: str | None
    confidence: float
    provenance_url: str | None
    provenance_snippet: str | None


def should_auto_apply(result: EnrichmentResult, min_confidence: float = 0.8) -> bool:
    """Return True if this result should be auto-applied.

    Rules:
    - Confidence < 0.6 is NEVER auto-applied, regardless of min_confidence.
    - Confidence must also meet or exceed min_confidence.
    - Results with neither org nor role are not auto-applied (URL-only results
      would clear enrich_pending without actually filling in the missing data).

    Args:
        result: The enrichment result to evaluate.
        min_confidence: The caller-specified minimum confidence threshold.

    Returns:
        True if all conditions are met.
    """
    if result.confidence < _ABSOLUTE_MIN_CONFIDENCE:
        return False
    if not result.org and not result.role:
        return False
    return result.confidence >= min_confidence


def _score_result(
    url: str,
    title: str,
    content: str,
    name: str,
    context: str,
) -> float:
    """Compute a confidence score for a single SearXNG result.

    Scoring heuristic:
    - URL is a LinkedIn or Xing profile: base 0.7
    - Other URLs: base 0.4
    - Title or content contains exact name match: +0.15
    - Content contains a context keyword (non-empty context): +0.15
    - Capped at 1.0

    Args:
        url: The result URL.
        title: The result page title.
        content: The result snippet/content.
        name: The person name being searched.
        context: Transcript context keywords.

    Returns:
        Confidence score in [0.0, 1.0].
    """
    url_lower = url.lower()
    is_professional_profile = (
        "linkedin.com/in/" in url_lower
        or "linkedin.com/pub/" in url_lower
        or "xing.com/profile/" in url_lower
        or "xing.com/people/" in url_lower
        or "/xing.com/" in url_lower
        or url_lower.startswith("https://www.xing.com")
        or url_lower.startswith("https://xing.com")
    )

    base = 0.7 if is_professional_profile else 0.4

    # Name match bonus
    name_lower = name.lower()
    combined_text = (title + " " + content).lower()
    name_bonus = 0.15 if name_lower in combined_text else 0.0

    # Context keyword bonus — check if any significant word from context appears
    context_bonus = 0.0
    if context:
        context_words = [w for w in context.lower().split() if len(w) >= 4]
        if any(word in combined_text for word in context_words):
            context_bonus = 0.15

    score = base + name_bonus + context_bonus
    return min(score, 1.0)


def _extract_org_and_role(title: str, content: str) -> tuple[str | None, str | None]:
    """Extract organisation and role from a result title/snippet.

    Simple heuristic: look for patterns like "Name - Role at Org" or
    "Name | Role | Org" in the title, or "Role at Org" in the content.

    Returns:
        Tuple of (org, role), either of which may be None.
    """
    org: str | None = None
    role: str | None = None

    # Pattern: "Name - Role at Org | Platform" or "Name | Role | Org | Platform"
    # Try title first
    patterns = [
        r"[-–]\s*([^|@-]+?)\s+at\s+([^|]+?)(?:\s*\||$)",  # "Role at Org"
        r"\|\s*([^|]+?)\s+at\s+([^|]+?)(?:\s*\||$)",        # "| Role at Org"
    ]
    for pattern in patterns:
        m = re.search(pattern, title, re.IGNORECASE)
        if m:
            role = m.group(1).strip() or None
            org = m.group(2).strip() or None
            if role or org:
                break

    # Try content if nothing from title
    if not role and not org and content:
        m = re.search(r"([A-Z][^.]+?)\s+at\s+([^.]+?)\.", content)
        if m:
            role = m.group(1).strip() or None
            org = m.group(2).strip() or None

    # Truncate to reasonable lengths
    if role and len(role) > 100:
        role = role[:100]
    if org and len(org) > 100:
        org = org[:100]

    return org, role


async def list_enrichment_candidates(dl: DataLayer) -> list[EnrichmentCandidate]:
    """Return person memories that need enrichment as EnrichmentCandidate list.

    Candidates are person memories that meet either of these criteria:
    1. ``enrich_pending="true"`` metadata flag is set (new/ambiguous persons
       flagged at ingest time).
    2. Have a ``name`` field but are missing both ``org`` and ``role`` — this
       covers pre-existing person memories created before the enrich_pending
       flag was introduced.

    For each candidate, attempts to find linked meeting memories to extract
    transcript context. If no meeting context is found, transcript_context
    will be empty string.

    Args:
        dl: DataLayer implementation for searching memories.

    Returns:
        List of EnrichmentCandidate, deduplicated by memory ID.
    """
    seen_ids: set[int] = set()
    raw_memories = []

    # Fetch memories with the explicit enrich_pending flag.
    try:
        flagged = await dl.search(
            SearchParams(
                type="person",
                project="people",
                metadata_filter={"enrich_pending": "true"},
                limit=100,
            )
        )
        raw_memories.extend(flagged.results or [])
    except Exception as exc:
        logger.warning("Failed to search for flagged enrichment candidates: %s", exc)

    # Also fetch all person memories (up to 500) to find pre-existing persons
    # that have a name but no org/role — even if they lack the enrich_pending flag.
    try:
        all_persons = await dl.search(
            SearchParams(
                type="person",
                project="people",
                limit=500,
            )
        )
        for mem in all_persons.results or []:
            meta = mem.metadata or {}
            has_name = bool(meta.get("name") or mem.title)
            missing_org = not meta.get("org")
            missing_role = not meta.get("role")
            not_already_flagged = meta.get("enrich_pending") != "true"
            if has_name and missing_org and missing_role and not_already_flagged:
                raw_memories.append(mem)
    except Exception as exc:
        logger.warning("Failed to search for pre-existing enrichment candidates: %s", exc)

    if not raw_memories:
        return []

    candidates: list[EnrichmentCandidate] = []
    for memory in raw_memories:
        if memory.id in seen_ids:
            continue
        seen_ids.add(memory.id)

        name = (memory.metadata or {}).get("name") or memory.title or ""
        if not name:
            continue

        transcript_context = await _get_transcript_context(dl, memory.id, name)

        candidates.append(
            EnrichmentCandidate(
                memory_id=memory.id,
                name=name,
                transcript_context=transcript_context,
            )
        )

    return candidates


async def _get_transcript_context(
    dl: DataLayer, person_memory_id: int, name: str
) -> str:
    """Find transcript context for a person from linked meeting memories.

    Traverses the relationship graph to find meetings linked to this person,
    then extracts relevant text from the first found meeting memory.

    Args:
        dl: DataLayer for graph traversal.
        person_memory_id: The person's memory ID.
        name: The person's name for relevance filtering.

    Returns:
        Up to 500 characters of relevant transcript text, or empty string.
    """
    try:
        # Look for relationships: meeting --attended_by--> person
        # (the meeting is source, person is target)
        relationships = await dl.get_relationships(
            memory_id=person_memory_id,
            link_types=["attended_by", "mentioned_in"],
        )
    except Exception as exc:
        logger.debug("Could not get relationships for person %d: %s", person_memory_id, exc)
        return ""

    if not relationships:
        return ""

    # Find meeting memory IDs from the relationships
    meeting_ids: list[int] = []
    for rel in relationships:
        # attended_by: meeting(source) -> person(target)
        # mentioned_in: person(source) -> meeting(target)
        source_id = rel.get("source_id")
        target_id = rel.get("target_id")
        link_type = rel.get("link_type", "")
        if link_type == "attended_by" and target_id == person_memory_id and source_id:
            meeting_ids.append(source_id)
        elif link_type == "mentioned_in" and source_id == person_memory_id and target_id:
            meeting_ids.append(target_id)

    if not meeting_ids:
        return ""

    # Fetch the first meeting memory
    try:
        observations = await dl.get_observations(meeting_ids[:1])
    except Exception as exc:
        logger.debug("Could not fetch meeting observations: %s", exc)
        return ""

    if not observations:
        return ""

    meeting = observations[0]
    text = meeting.content or ""

    # Find the relevant passage containing the person's name
    name_lower = name.lower()
    text_lower = text.lower()
    pos = text_lower.find(name_lower)
    if pos >= 0:
        start = max(0, pos - 100)
        end = min(len(text), pos + 400)
        return text[start:end].strip()

    # Fall back to first 500 chars
    return text[:500].strip()


async def search_person_web(
    name: str,
    context: str,
    searxng_url: str,
) -> list[EnrichmentResult]:
    """Search SearXNG for person enrichment data.

    Performs a web search using the person's name and context keywords,
    scoring and ranking results by confidence.

    Args:
        name: The person's full name.
        context: Transcript context to improve search relevance (e.g. company name).
        searxng_url: Base URL of the SearXNG instance (from get_config().SEARXNG_URL).

    Returns:
        Up to 3 EnrichmentResult objects, sorted by confidence descending.
        Returns empty list on network errors.
    """
    if not searxng_url:
        logger.warning("SEARXNG_URL not configured; skipping web search for %r", name)
        return []

    query = f'"{name}" site:linkedin.com OR site:xing.com OR company bio'
    if context:
        # Add context keywords, excluding words that are part of the person's name
        # to avoid redundant "Name Name ..." queries.
        name_lower = name.lower()
        name_parts = set(name_lower.split())
        context_words = [
            w for w in context.split()
            if w.lower() not in name_parts and len(w) > 2
        ]
        context_str = " ".join(context_words[:3]) if context_words else ""
        if context_str:
            query = f'"{name}" {context_str} site:linkedin.com OR site:xing.com OR company bio'

    search_url = f"{searxng_url.rstrip('/')}/search"
    params = {
        "q": query,
        "format": "json",
        "categories": "general",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(search_url, params=params)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("SearXNG search failed for %r: %s", name, exc)
        return []

    raw_results = data.get("results") or []
    enrichment_results: list[EnrichmentResult] = []

    for item in raw_results[:_MAX_RESULTS]:
        url = item.get("url", "")
        title = item.get("title", "")
        content = item.get("content", "")

        confidence = _score_result(url, title, content, name, context)
        org, role = _extract_org_and_role(title, content)

        enrichment_results.append(
            EnrichmentResult(
                name=name,
                org=org,
                role=role,
                profile_url=url or None,
                confidence=confidence,
                provenance_url=url or None,
                provenance_snippet=content[:300] if content else None,
            )
        )

    # Sort by confidence descending
    enrichment_results.sort(key=lambda r: r.confidence, reverse=True)
    return enrichment_results[:_MAX_RESULTS]


async def apply_enrichment(
    dl: DataLayer,
    memory_id: int,
    result: EnrichmentResult,
) -> None:
    """Apply enrichment data to an existing person memory.

    Updates the person memory with org, role, profile_url, confidence,
    and provenance fields. Clears the enrich_pending flag.

    Args:
        dl: DataLayer for updating memories.
        memory_id: The person memory ID to update.
        result: The enrichment result to apply.
    """
    metadata: dict = {
        "enrich_pending": "false",
        "confidence": result.confidence,
    }

    if result.org:
        metadata["org"] = result.org
    if result.role:
        metadata["role"] = result.role
    if result.profile_url:
        metadata["profile_url"] = result.profile_url
    if result.provenance_url:
        metadata["provenance_url"] = result.provenance_url
    if result.provenance_snippet:
        metadata["provenance_snippet"] = result.provenance_snippet
    if result.provenance_url and result.provenance_snippet:
        metadata["provenance"] = f"{result.provenance_url}: {result.provenance_snippet}"
    elif result.provenance_url:
        metadata["provenance"] = result.provenance_url
    elif result.provenance_snippet:
        metadata["provenance"] = result.provenance_snippet

    await dl.update_memory(
        UpdateMemoryParams(
            id=memory_id,
            metadata=metadata,
        )
    )

    logger.info(
        "enrichment_applied memory_id=%d name=%r org=%r role=%r confidence=%.2f",
        memory_id,
        result.name,
        result.org,
        result.role,
        result.confidence,
    )
