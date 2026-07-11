"""Capture Router: LLM-based classification and structured field extraction.

Classifies incoming memory text into one of the capture templates and extracts
structured fields specific to each template type.

Capture templates:
- project       : name, status, owner, goals, next_actions, repository
- resource      : title, url, source_type, author, summary, published_at
- concept       : name, domain, summary, related_concepts
- journal       : entry_date, mood, themes, reflection
- correspondence: with, channel, direction, subject, summary, occurred_at, follow_up_needed
- prompt        : purpose, prompt_text, target_model, variables, constraints
- decision      : what, context, owner, alternatives, rationale
- meeting       : attendees, topic, key_points, action_items
- person_context: person, relationship, detail
- insight       : realization, trigger, domain
- event         : what, when, who, where, recurrence
- learning      : feedback_type, scope, affected_skills
- observation   : (no special fields — general fallback)
"""

from __future__ import annotations

import logging
from typing import Any

from open_brain.data_layer.llm import LlmMessage, llm_complete
from open_brain.utils import parse_llm_json

logger = logging.getLogger(__name__)

_CLASSIFICATION_PROMPT_TEMPLATE = """\
Classify the following text into one of these capture templates and extract structured fields.

Templates and their fields:
- project: name, status, owner, goals (list), next_actions (list), repository
- resource: title, url, source_type, author, summary, published_at
- concept: name, domain, summary, related_concepts (list)
- journal: entry_date, mood, themes (list), reflection
- correspondence: with (list), channel, direction, subject, summary, occurred_at, follow_up_needed
- prompt: purpose, prompt_text, target_model, variables (list), constraints (list)
- decision: what, context, owner, alternatives (list), rationale
- meeting: attendees (list), topic, key_points (list), action_items (list)
- person_context: person, relationship, detail (use for person knowledge)
- insight: realization, trigger, domain
- event: what, when, who, where, recurrence
- learning: feedback_type, scope, affected_skills (list)
- observation: (no extra fields needed)

Rules:
1. Choose the MOST specific matching template based on the text content.
2. Extract all relevant fields for that template from the text.
3. Use null for fields that cannot be determined from the text.
4. Use person_context for person knowledge to preserve existing callers.
5. For ambiguous or unclassifiable text, choose observation.
6. For "observation", only output capture_template = observation with no other fields.

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

    Assumes text is trusted user input. Not suitable for multi-tenant use
    without prompt sanitization.

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
            # 2048 (was 512): the per-template field set plus list values can
            # exceed 512 output tokens, which truncated the JSON mid-string and
            # raised JSONDecodeError. json_object enforces a complete object;
            # reasoning disabled so thinking tokens don't reclaim the budget.
            max_tokens=2048,
            response_format={"type": "json_object"},
            disable_reasoning=True,
        )
        result = parse_llm_json(response)
        if not isinstance(result, dict) or "capture_template" not in result:
            logger.warning("capture_router: LLM returned unexpected structure, falling back to observation")
            return {"capture_template": "observation"}
        return result
    except Exception:
        logger.exception("capture_router: classification failed, falling back to observation")
        return {"capture_template": "observation"}

