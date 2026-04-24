"""LLM-based extraction from transcript text.

Calls Haiku to extract structured data: attendees, mentioned people, topics,
and follow-up tasks.
"""

import json
import logging

from open_brain.data_layer.llm import LlmMessage, llm_complete

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """
You are an assistant that extracts structured information from meeting transcripts.
Return ONLY valid JSON with these fields:
{
  "attendees": ["Name1", "Name2"],
  "mentioned_people": ["Name3"],
  "topics": ["topic1"],
  "follow_up_tasks": ["task1"]
}

Rules:
- attendees: people who were present (spoke or are listed as participants)
- mentioned_people: people mentioned by name but not present
- topics: main discussion topics (short phrases)
- follow_up_tasks: concrete action items or follow-up tasks identified

Return ONLY the JSON object, no other text.
"""


async def extract_from_transcript(text: str) -> dict:
    """Call Haiku to extract structured data from transcript text.

    Args:
        text: The transcript text to extract from.

    Returns:
        Dict with keys: attendees, mentioned_people, topics, follow_up_tasks.
        Falls back to empty lists on parse error.
    """
    prompt = f"{EXTRACTION_PROMPT}\n\nTranscript:\n{text}"
    response = await llm_complete(
        messages=[LlmMessage(role="user", content=prompt)],
        max_tokens=1024,
    )

    try:
        # Strip potential markdown code fences
        cleaned = response.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            # Remove first and last lines (``` markers)
            cleaned = "\n".join(lines[1:-1]) if len(lines) > 2 else cleaned
        data = json.loads(cleaned)
        return {
            "attendees": data.get("attendees") or [],
            "mentioned_people": data.get("mentioned_people") or [],
            "topics": data.get("topics") or [],
            "follow_up_tasks": data.get("follow_up_tasks") or [],
        }
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to parse extraction response: %s — response: %r", exc, response)
        return {"attendees": [], "mentioned_people": [], "topics": [], "follow_up_tasks": []}
