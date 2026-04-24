"""People deduplication subpackage for open-brain.

Public API:
    match_person         — match an incoming person against existing records
    PersonRecord         — stored person memory (single or directory style)
    PersonMember         — typed dict for a single person entry within a record
    MatchCandidate       — scored match result for one existing person
    MatchDecision        — final decision from match_person
    LLMConfirmCallback   — Protocol for the optional llm_confirm callback
"""

from open_brain.people.dedup import match_person
from open_brain.people.models import (
    LLMConfirmCallback,
    MatchCandidate,
    MatchDecision,
    PersonMember,
    PersonRecord,
)

__all__ = [
    "match_person",
    "LLMConfirmCallback",
    "MatchCandidate",
    "MatchDecision",
    "PersonMember",
    "PersonRecord",
]
