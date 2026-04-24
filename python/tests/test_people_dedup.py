"""Tests for the people dedup library (open-brain-cr3.2).

All 10 spike scenarios are covered via parametrize.
Verifies: 3-stage scoring, directory iteration, subset-cap, llm_confirm invocation.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from open_brain.people.dedup import match_person
from open_brain.people.models import MatchDecision, PersonRecord

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "people"


def _load_record(filename: str) -> PersonRecord:
    data = json.loads((FIXTURES_DIR / filename).read_text())
    return PersonRecord(
        memory_id=data["memory_id"],
        style=data["style"],
        members=data["members"],
    )


@pytest.fixture(scope="module")
def existing_records() -> list[PersonRecord]:
    return [
        _load_record("directory_polaris.json"),
        _load_record("singleton_weihe.json"),
    ]


# ---------------------------------------------------------------------------
# 10 spike scenarios
# ---------------------------------------------------------------------------

SCENARIOS: list[tuple[str, str | None, str | None, str]] = [
    # (name, org, linkedin, expected_action)
    ("Jochen Jungbluth", "Dental-Now", None, "auto_merge"),         # 1: exact name
    ("Cyrus Amadi", "Dental-Now", None, "auto_merge"),              # 2: alias match
    ("Jochen Jungblut", "Dental-Now", None, "auto_merge"),          # 3: alias (no h)
    ("Dr. Alamouti", "Dental-Now", None, "llm_confirm"),            # 4: last-name only + org
    ("Stephan Weihe", "ICRD", None, "auto_merge"),                  # 5: alias match singleton
    ("Reza Mollaei", "HeyDonto", None, "new"),                      # 6: new person
    ("Siamak", "Dental-Now", None, "llm_confirm"),                  # 7: first-name + org boost
    ("J. Jungbluth", None, "jochen-jungbluth-a5a412152", "auto_merge"),  # 8: linkedin beats name diff
    ("Philipp", "Sonia", None, "llm_confirm"),                      # 9: single first name + org
    ("Thomas Müller", None, None, "new"),                           # 10: unknown person with diacritic
]


@pytest.mark.parametrize(
    "name,org,linkedin,expected_action",
    SCENARIOS,
    ids=[s[0] for s in SCENARIOS],
)
def test_match_person_scenarios(
    name: str,
    org: str | None,
    linkedin: str | None,
    expected_action: str,
    existing_records: list[PersonRecord],
) -> None:
    """Each of the 10 spike scenarios must yield the correct action."""
    decision = match_person(name, org, linkedin, existing_records)
    assert decision.action == expected_action, (
        f"name={name!r}, org={org!r}, linkedin={linkedin!r}\n"
        f"  expected: {expected_action}\n"
        f"  got:      {decision.action}\n"
        f"  rationale: {decision.rationale}"
    )


# ---------------------------------------------------------------------------
# Criterion 2: directory records iterated per-member
# ---------------------------------------------------------------------------


def test_directory_members_iterated(existing_records: list[PersonRecord]) -> None:
    """match_person must consider all members of a directory-style record."""
    # Siamak is member index 2 inside directory_polaris — must still be found
    decision = match_person("Siamak Ghasemi", "Dental-Now", None, existing_records)
    assert decision.action in {"auto_merge", "llm_confirm"}
    assert decision.target is not None
    assert "Siamak" in decision.target.member_name


# ---------------------------------------------------------------------------
# Criterion 3: subset-bonus cap enforced
# ---------------------------------------------------------------------------


def test_subset_cap_below_auto_merge(existing_records: list[PersonRecord]) -> None:
    """Subset-bonus candidates must be capped below AUTO_MERGE_T when sim < 1.0."""
    # "Siamak" is a strict subset of "Siamak Ghasemi" tokens — should never auto_merge
    decision = match_person("Siamak", "Dental-Now", None, existing_records)
    assert decision.action != "auto_merge", (
        "Subset partial-name match must not auto_merge; must go through llm_confirm"
    )
    if decision.target is not None:
        assert decision.target.confidence < 0.92, (
            f"Subset-match confidence {decision.target.confidence} must be < 0.92"
        )


# ---------------------------------------------------------------------------
# Criterion 4: llm_confirm callable invoked when decision=llm_confirm
# ---------------------------------------------------------------------------


def test_llm_confirm_callable_invoked_and_returns_true(
    existing_records: list[PersonRecord],
) -> None:
    """When decision is llm_confirm and llm_confirm callable returns True → auto_merge."""
    mock_llm = MagicMock(return_value=True)
    decision = match_person("Siamak", "Dental-Now", None, existing_records, llm_confirm=mock_llm)
    mock_llm.assert_called_once()
    assert decision.action == "auto_merge"


def test_llm_confirm_callable_invoked_and_returns_false(
    existing_records: list[PersonRecord],
) -> None:
    """When decision is llm_confirm and llm_confirm callable returns False → new."""
    mock_llm = MagicMock(return_value=False)
    decision = match_person("Siamak", "Dental-Now", None, existing_records, llm_confirm=mock_llm)
    mock_llm.assert_called_once()
    assert decision.action == "new"


def test_llm_confirm_not_called_for_auto_merge(
    existing_records: list[PersonRecord],
) -> None:
    """llm_confirm callable must NOT be invoked for auto_merge decisions."""
    mock_llm = MagicMock(return_value=True)
    decision = match_person("Jochen Jungbluth", "Dental-Now", None, existing_records, llm_confirm=mock_llm)
    assert decision.action == "auto_merge"
    mock_llm.assert_not_called()


def test_llm_confirm_not_called_for_new(
    existing_records: list[PersonRecord],
) -> None:
    """llm_confirm callable must NOT be invoked for new decisions."""
    mock_llm = MagicMock(return_value=True)
    decision = match_person("Reza Mollaei", "HeyDonto", None, existing_records, llm_confirm=mock_llm)
    assert decision.action == "new"
    mock_llm.assert_not_called()
