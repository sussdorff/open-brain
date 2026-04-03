"""Capture Router: LLM-based classification and structured field extraction.

Classifies incoming memory text into one of the capture templates and extracts
structured fields specific to each template type.

Capture templates:
- decision      : what, context, owner, alternatives, rationale
- meeting       : attendees, topic, key_points, action_items
- person_context: person, relationship, detail
- insight       : realization, trigger, domain
- event         : what, when, who, where, recurrence
- learning      : feedback_type, scope, affected_skills
- observation   : (no special fields — general fallback)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from open_brain.data_layer.llm import LlmMessage, llm_complete

logger = logging.getLogger(__name__)

_CLASSIFICATION_PROMPT_TEMPLATE = """\
Classify the following text into one of these capture templates and extract structured fields.

Templates and their fields:
- decision: what, context, owner, alternatives (list), rationale
- meeting: attendees (list), topic, key_points (list), action_items (list)
- person_context: person, relationship, detail
- insight: realization, trigger, domain
- event: what, when, who, where, recurrence
- learning: feedback_type, scope, affected_skills (list)
- observation: (no extra fields needed)

Rules:
1. Choose the MOST specific matching template based on the text content.
2. Extract all relevant fields for that template from the text.
3. Use null for fields that cannot be determined from the text.
4. For "observation", only output capture_template = observation with no other fields.

Return ONLY a valid JSON object with "capture_template" key and the template's fields.
Do not include markdown fences or any explanation.

Text to classify:
"""


async def classify_and_extract(
    text: str,
    existing_metadata: dict[str, Any] | None = None,
    memory_type: str | None = None,
) -> dict[str, Any]:
    """Classify text and extract structured fields for the matching capture template.

    Args:
        text: The memory text to classify.
        existing_metadata: Existing metadata dict. If it contains 'capture_template',
            classification is skipped and this dict is returned as-is (bypass).
        memory_type: The memory type. If 'session_summary', bypass classification
            and return observation template.

    Returns:
        Dict with 'capture_template' key and extracted fields.
        Returns existing_metadata unchanged if bypass conditions are met.
    """
    # Bypass condition 1: capture_template already set by caller
    if existing_metadata is not None and "capture_template" in existing_metadata:
        return existing_metadata

    # Bypass condition 2: session_summary type — return metadata unchanged (no classification)
    if memory_type == "session_summary":
        return existing_metadata or {}

    # Truncate to prevent context overflow on large inputs
    text = text[:4000]

    prompt = _CLASSIFICATION_PROMPT_TEMPLATE + text

    try:
        response = await llm_complete(
            messages=[LlmMessage(role="user", content=prompt)],
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
        )
        result = _parse_json(response)
        if not isinstance(result, dict) or "capture_template" not in result:
            logger.warning("capture_router: LLM returned unexpected structure, falling back to observation")
            return {"capture_template": "observation"}
        return result
    except Exception:
        logger.exception("capture_router: classification failed, falling back to observation")
        return {"capture_template": "observation"}


def _parse_json(text: str) -> Any:
    """Parse JSON from LLM response, stripping markdown fences if present."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    return json.loads(text)
