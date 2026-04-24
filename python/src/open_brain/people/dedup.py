"""Person deduplication matcher.

Implements a 3-stage scoring stack to match an incoming person record against
a list of existing PersonRecords:

    Stage 1 — LinkedIn URL exact match  → confidence 0.99
    Stage 2 — Alias exact match         → confidence 0.96
    Stage 3 — Fuzzy name similarity (SequenceMatcher)
                + token-subset bonus    (+0.25)
                + org-boost             (+0.05 / +0.10 with subset)

Thresholds: auto_merge >= 0.92, llm_confirm >= 0.85.

Critical rule: if a subset bonus was applied AND name_similarity < 1.0, cap
confidence below auto_merge (0.92). Partial references must always go through
the LLM-confirm gate.
"""

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Literal

from open_brain.people.models import (
    LLMConfirmCallback,
    MatchCandidate,
    MatchDecision,
    PersonRecord,
)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

AUTO_MERGE_T: float = 0.92
LLM_CONFIRM_T: float = 0.85

# Maximum confidence allowed for a subset-capped candidate (must stay below AUTO_MERGE_T)
SUBSET_CAP_MAX: float = AUTO_MERGE_T - 0.01

# Stage-specific scores
LINKEDIN_EXACT_SCORE: float = 0.99
ALIAS_MATCH_SCORE: float = 0.96

# Fuzzy scoring constants
SUBSET_BONUS: float = 0.25
ORG_BOOST_BASE: float = 0.05    # used without subset
ORG_BOOST_SUBSET: float = 0.10  # used combined with subset

# Minimum score to even keep a fuzzy candidate
MIN_FUZZY_SCORE: float = 0.4

# Title prefixes to strip during normalisation.
# Dotless forms: punctuation is stripped before tokenization,
# so "dr." becomes "dr" — but dotted forms listed for clarity.
_TITLE_PREFIXES: frozenset[str] = frozenset(
    {"dr.", "dr", "prof.", "prof", "herr", "frau", "mr.", "mr", "ms.", "ms"}
)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    """Strip titles, diacritics, punctuation, lower-case, collapse whitespace.

    Args:
        name: Raw name string, may contain titles and diacritics.

    Returns:
        Normalised name string suitable for comparison.
    """
    # NFD decomposition strips combining marks (ä→a, ü→u, etc.)
    result = unicodedata.normalize("NFD", name)
    result = "".join(c for c in result if not unicodedata.combining(c))
    result = result.lower()
    # Remove punctuation except hyphen and apostrophe
    result = re.sub(r"[^\w\s\-']", " ", result)
    tokens = [t for t in result.split() if t not in _TITLE_PREFIXES]
    return " ".join(tokens)


def _normalize_org(org: str | None) -> str:
    """Normalise an organisation string for token-overlap comparison.

    Args:
        org: Raw org string or None.

    Returns:
        Lower-cased, diacritic-stripped, punctuation-normalised string.
        Empty string if *org* is None or empty.
    """
    if not org:
        return ""
    result = unicodedata.normalize("NFD", org)
    result = "".join(c for c in result if not unicodedata.combining(c))
    result = result.lower()
    result = re.sub(r"[^\w\s/]", " ", result)
    return " ".join(result.split())


def _name_similarity(a: str, b: str) -> float:
    """Compute similarity between two raw name strings.

    Args:
        a: First name (raw, will be normalised internally).
        b: Second name (raw, will be normalised internally).

    Returns:
        Similarity score in [0, 1].
    """
    na, nb = _normalize_name(a), _normalize_name(b)
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _org_overlap(a: str | None, b: str | None) -> float:
    """Compute token overlap between two organisation strings.

    Args:
        a: First org string (raw or None).
        b: Second org string (raw or None).

    Returns:
        Overlap fraction [0, 1]: proportion of shorter-org tokens found in
        longer org. 0.0 if either org is missing.
    """
    na, nb = _normalize_org(a), _normalize_org(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return 0.0
    shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(shorter & longer) / len(shorter)


# ---------------------------------------------------------------------------
# Core matcher
# ---------------------------------------------------------------------------


def match_person(
    new_name: str,
    new_org: str | None,
    new_linkedin: str | None,
    existing: list[PersonRecord],
    llm_confirm: LLMConfirmCallback | None = None,
) -> MatchDecision:
    """Match an incoming person against existing records.

    Iterates every member of every PersonRecord in *existing* (supports both
    "single" and "directory" styles). Applies a 3-stage scoring stack:

    1. LinkedIn URL exact match (confidence 0.99)
    2. Alias exact match on normalised name (confidence 0.96)
    3. Fuzzy name similarity with optional subset bonus and org boost.

    If a subset bonus was applied and name similarity < 1.0, confidence is
    capped below *AUTO_MERGE_T* to force the LLM-confirm gate.

    If *llm_confirm* is provided and the decision is "llm_confirm", it is
    invoked once. A True return changes action to "auto_merge"; False to "new".

    Args:
        new_name: Incoming person's name.
        new_org: Incoming person's organisation (optional).
        new_linkedin: Incoming person's LinkedIn handle/URL (optional).
        existing: List of existing PersonRecords to match against.
        llm_confirm: Optional callable invoked for llm_confirm decisions.

    Returns:
        MatchDecision describing the recommended action and top candidate.
    """
    candidates: list[MatchCandidate] = []

    for record in existing:
        for member in record.members:
            member_linkedin: str | None = member.get("linkedin")
            member_name: str = member["name"]
            member_org: str | None = member.get("org")
            aliases: list[str] = member.get("aliases") or []

            # --- Stage 1: LinkedIn exact match ---
            if new_linkedin and member_linkedin and new_linkedin == member_linkedin:
                candidates.append(
                    MatchCandidate(
                        memory_id=record.memory_id,
                        member_name=member_name,
                        member_org=member_org,
                        confidence=LINKEDIN_EXACT_SCORE,
                        reasons=["linkedin-exact"],
                    )
                )
                continue  # no need to score further for this member

            # --- Stage 2: Alias exact match ---
            alias_matched = False
            for alias in aliases:
                if _normalize_name(alias) == _normalize_name(new_name):
                    candidates.append(
                        MatchCandidate(
                            memory_id=record.memory_id,
                            member_name=member_name,
                            member_org=member_org,
                            confidence=ALIAS_MATCH_SCORE,
                            reasons=[f"alias-match:{alias}"],
                        )
                    )
                    alias_matched = True
                    break

            if alias_matched:
                continue

            # --- Stage 3: Fuzzy name similarity ---
            sim = _name_similarity(new_name, member_name)
            new_tokens = set(_normalize_name(new_name).split())
            member_tokens = set(_normalize_name(member_name).split())

            subset_applied = False
            subset_bonus_val = 0.0
            if new_tokens and new_tokens.issubset(member_tokens):
                subset_bonus_val = SUBSET_BONUS
                subset_applied = True

            reasons: list[str] = [f"name-sim:{sim:.2f}"]
            if subset_applied:
                reasons.append(f"token-subset:+{subset_bonus_val:.2f}")

            score = sim + subset_bonus_val
            if score < MIN_FUZZY_SCORE:
                continue

            # Org boost
            if new_org and member_org:
                ov = _org_overlap(new_org, member_org)
                if ov > 0:
                    boost = ORG_BOOST_SUBSET if subset_applied else ORG_BOOST_BASE
                    score += boost * ov
                    reasons.append(f"org-overlap:{ov:.2f}")

            confidence = min(1.0, score)

            # Subset-cap rule: partial references must not auto_merge
            if subset_applied and sim < 1.0 and confidence >= AUTO_MERGE_T:
                confidence = SUBSET_CAP_MAX
                reasons.append("capped:subset-below-auto-merge")

            candidates.append(
                MatchCandidate(
                    memory_id=record.memory_id,
                    member_name=member_name,
                    member_org=member_org,
                    confidence=confidence,
                    reasons=reasons,
                )
            )

    # --- Decision logic ---
    candidates.sort(key=lambda c: c.confidence, reverse=True)

    if not candidates:
        return MatchDecision(action="new", target=None, rationale="no candidates")

    top = candidates[0]
    runners_up = candidates[1:3]

    # Ambiguity: two high-confidence matches for DIFFERENT people.
    # If the top candidate has a hard signal (linkedin-exact or alias-match),
    # skip the ambiguity gate — hard signals are unambiguous.
    has_hard_signal = top.reasons and any(
        r.startswith("linkedin-exact") or r.startswith("alias-match:")
        for r in top.reasons
    )
    if (
        not has_hard_signal
        and len(candidates) >= 2
        and candidates[1].confidence >= LLM_CONFIRM_T
        and candidates[0].member_name != candidates[1].member_name
    ):
        return MatchDecision(
            action="ambiguous",
            target=top,
            runners_up=runners_up,
            rationale=(
                f"multiple candidates >= {LLM_CONFIRM_T}: "
                f"{top.member_name} vs {candidates[1].member_name}"
            ),
        )

    if top.confidence >= AUTO_MERGE_T:
        return MatchDecision(
            action="auto_merge",
            target=top,
            runners_up=runners_up,
            rationale=f"confidence {top.confidence:.2f} >= {AUTO_MERGE_T}",
        )

    if top.confidence >= LLM_CONFIRM_T:
        decision = MatchDecision(
            action="llm_confirm",
            target=top,
            runners_up=runners_up,
            rationale=f"confidence {top.confidence:.2f} in LLM-confirm band",
        )
        if llm_confirm is not None:
            confirmed = llm_confirm(decision)
            action: Literal["auto_merge", "new"] = "auto_merge" if confirmed else "new"
            return MatchDecision(
                action=action,
                target=decision.target,
                runners_up=decision.runners_up,
                rationale=f"llm_confirm decided: {action}",
            )
        return decision

    # Confidence below LLM_CONFIRM_T: set target to top so callers can inspect
    # the rejected candidate's score.
    return MatchDecision(
        action="new",
        target=top,
        runners_up=runners_up,
        rationale=f"top confidence {top.confidence:.2f} below {LLM_CONFIRM_T}",
    )
