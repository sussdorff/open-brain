"""Person dedup domain models.

PersonRecord represents a stored memory containing one or more person entries
(single-person or directory style). MatchCandidate and MatchDecision are
returned by the match_person matcher.
"""

from dataclasses import dataclass, field
from typing import Literal, Protocol, TypedDict, runtime_checkable


class PersonMember(TypedDict, total=False):
    """Typed representation of a person entry within a PersonRecord."""

    name: str
    org: str | None
    linkedin: str | None
    aliases: list[str]


@runtime_checkable
class LLMConfirmCallback(Protocol):
    """Protocol for the optional llm_confirm callback passed to match_person."""

    def __call__(self, decision: "MatchDecision") -> bool: ...


@dataclass(slots=True)
class PersonRecord:
    """A person memory record stored in open-brain.

    Attributes:
        memory_id: The open-brain memory ID.
        style: Whether this is a single-person or directory record.
        members: List of person dicts, each with keys: name, org, linkedin, aliases.
    """

    memory_id: int
    style: Literal["single", "directory"]
    members: list[PersonMember]


@dataclass(slots=True)
class MatchCandidate:
    """A scored match candidate from an existing PersonRecord.

    Attributes:
        memory_id: The open-brain memory ID of the matched record.
        member_name: The canonical name of the matched person within the record.
        member_org: The organisation of the matched person, if any.
        confidence: Score in [0, 1]; higher = more certain it's the same person.
        reasons: Human-readable list describing why this confidence was assigned.
    """

    memory_id: int
    member_name: str
    member_org: str | None
    confidence: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MatchDecision:
    """Final decision returned by match_person.

    Attributes:
        action: One of "new", "auto_merge", "llm_confirm", "ambiguous".
        target: The top-ranked candidate. When action is "new" due to no candidates,
            target is None. When action is "new" because confidence is below
            LLM_CONFIRM_T but candidates exist, target is set to the top candidate
            so callers can inspect the rejected candidate's score.
        runners_up: Up to 2 additional candidates for context.
        rationale: Short human-readable explanation.
    """

    action: Literal["new", "auto_merge", "llm_confirm", "ambiguous"]
    target: MatchCandidate | None
    runners_up: list[MatchCandidate] = field(default_factory=list)
    rationale: str = ""
