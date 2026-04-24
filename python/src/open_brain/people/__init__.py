"""People deduplication subpackage for open-brain.

Public API:
    match_person  — match an incoming person against existing records
    PersonRecord  — stored person memory (single or directory style)
    MatchCandidate — scored match result for one existing person
    MatchDecision  — final decision from match_person
"""

from open_brain.people.dedup import match_person
from open_brain.people.models import MatchCandidate, MatchDecision, PersonRecord

__all__ = [
    "match_person",
    "MatchCandidate",
    "MatchDecision",
    "PersonRecord",
]
