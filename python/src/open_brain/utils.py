"""Shared utility functions for open-brain."""

from __future__ import annotations

import json
from typing import Any


def parse_llm_json(text: str) -> Any:
    """Parse JSON from LLM response, stripping markdown fences if present."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    return json.loads(text)
