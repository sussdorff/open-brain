"""Tests for the people dedup library (open-brain-cr3.2).

All 10 spike scenarios are covered via parametrize.
Verifies: 3-stage scoring, directory iteration, subset-cap, llm_confirm invocation.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
    ("Dr. Alamouti", "Dental-Now", None, "auto_merge"),              # 4: containment → auto_merge (single Alamouti)
    ("Stephan Weihe", "ICRD", None, "auto_merge"),                  # 5: alias match singleton
    ("Reza Mollaei", "HeyDonto", None, "new"),                      # 6: new person
    ("Siamak", "Dental-Now", None, "auto_merge"),                   # 7: containment → auto_merge (single Siamak)
    ("J. Jungbluth", None, "jochen-jungbluth-a5a412152", "auto_merge"),  # 8: linkedin beats name diff
    ("Philipp", "Sonia", None, "auto_merge"),                        # 9: containment → auto_merge (single Philipp@Sonia)
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
    """Containment path supersedes the old subset-cap rule for unambiguous single matches.

    With the name-containment fix, 'Siamak' vs 'Siamak Ghasemi' (the only Siamak
    in the fixture) is now auto_merge at CONTAINMENT_SCORE (0.93).
    The subset-cap rule still applies to fuzzy-path candidates (non-containment).
    """
    decision = match_person("Siamak", "Dental-Now", None, existing_records)
    # Single unambiguous Siamak → containment path → auto_merge
    assert decision.action == "auto_merge", (
        "Unambiguous containment match should auto_merge; got: "
        f"{decision.action} (rationale: {decision.rationale})"
    )
    assert decision.target is not None
    assert "name-containment" in decision.target.reasons


# ---------------------------------------------------------------------------
# Criterion 4: llm_confirm callable invoked when decision=llm_confirm
# ---------------------------------------------------------------------------


def test_llm_confirm_callable_invoked_and_returns_true(
    existing_records: list[PersonRecord],
) -> None:
    """When decision is llm_confirm and llm_confirm callable returns True → auto_merge."""
    mock_llm = MagicMock(return_value=True)
    # "Stefan Weihe" (slight name variation, no org) scores in the llm_confirm band
    # vs "Stephan Weihe" — sim ~0.88, no containment, no org boost
    existing = [
        PersonRecord(
            memory_id=999,
            style="single",
            members=[{"name": "Stephan Weihe", "org": None, "linkedin": None, "aliases": []}],
        )
    ]
    decision = match_person("Stefan Weihe", None, None, existing, llm_confirm=mock_llm)
    mock_llm.assert_called_once()
    assert decision.action == "auto_merge"


def test_llm_confirm_callable_invoked_and_returns_false(
    existing_records: list[PersonRecord],
) -> None:
    """When decision is llm_confirm and llm_confirm callable returns False → new."""
    mock_llm = MagicMock(return_value=False)
    existing = [
        PersonRecord(
            memory_id=999,
            style="single",
            members=[{"name": "Stephan Weihe", "org": None, "linkedin": None, "aliases": []}],
        )
    ]
    decision = match_person("Stefan Weihe", None, None, existing, llm_confirm=mock_llm)
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


# ---------------------------------------------------------------------------
# AK tests: name-containment / first-name-vs-full-name (open-brain-3bm)
# ---------------------------------------------------------------------------


def test_first_name_matches_full_name_auto_merge() -> None:
    """AK2: 'Malte' matches existing 'Malte Sussdorff' → auto_merge."""
    existing = [
        PersonRecord(
            memory_id=100,
            style="single",
            members=[{"name": "Malte Sussdorff", "org": None, "linkedin": None, "aliases": []}],
        )
    ]
    decision = match_person("Malte", None, None, existing)
    assert decision.action == "auto_merge"
    assert decision.target is not None
    assert decision.target.memory_id == 100


def test_first_name_umlauts_auto_merge() -> None:
    """AK3: 'Andreas' matches 'Andreas Müller' → auto_merge."""
    existing = [
        PersonRecord(
            memory_id=200,
            style="single",
            members=[{"name": "Andreas Müller", "org": None, "linkedin": None, "aliases": []}],
        )
    ]
    decision = match_person("Andreas", None, None, existing)
    assert decision.action == "auto_merge"
    assert decision.target is not None
    assert decision.target.memory_id == 200


def test_ambiguous_first_name_two_records() -> None:
    """AK4: 'Anna' matches both 'Anna Schmidt' and 'Anna Meyer' → ambiguous or llm_confirm."""
    existing = [
        PersonRecord(
            memory_id=300,
            style="single",
            members=[{"name": "Anna Schmidt", "org": None, "linkedin": None, "aliases": []}],
        ),
        PersonRecord(
            memory_id=301,
            style="single",
            members=[{"name": "Anna Meyer", "org": None, "linkedin": None, "aliases": []}],
        ),
    ]
    decision = match_person("Anna", None, None, existing)
    assert decision.action in {"ambiguous", "llm_confirm"}


def test_conflicting_org_no_containment_merge() -> None:
    """Org conflict prevents name-containment auto_merge."""
    existing = [
        PersonRecord(
            memory_id=400,
            style="single",
            members=[{"name": "Anna Schmidt", "org": "Acme Corp", "linkedin": None, "aliases": []}],
        )
    ]
    # "Widget Inc" shares no tokens with "Acme Corp" → org conflict guard fires
    decision = match_person("Anna", "Widget Inc", None, existing)
    assert decision.action != "auto_merge"


def test_no_overlap_new() -> None:
    """No token overlap → new."""
    existing = [
        PersonRecord(
            memory_id=500,
            style="single",
            members=[{"name": "Thomas Müller", "org": None, "linkedin": None, "aliases": []}],
        )
    ]
    decision = match_person("Anna Schmidt", None, None, existing)
    assert decision.action == "new"


def test_superset_new_name_auto_merge() -> None:
    """'Malte Sussdorff' (new) vs existing 'Malte' → also auto_merge."""
    existing = [
        PersonRecord(
            memory_id=600,
            style="single",
            members=[{"name": "Malte", "org": None, "linkedin": None, "aliases": []}],
        )
    ]
    decision = match_person("Malte Sussdorff", None, None, existing)
    assert decision.action == "auto_merge"
    assert decision.target is not None
    assert decision.target.memory_id == 600


# ---------------------------------------------------------------------------
# Regression tests (open-brain-3bm adversarial review)
# ---------------------------------------------------------------------------


def test_org_conflict_fuzzy_not_auto_merge() -> None:
    """Regression 1: org conflict + subset bonus must NOT produce auto_merge.

    'John Smith' vs 'John Smith Jr @ Widget' — new_tokens ⊂ member_tokens,
    but orgs conflict → containment fast path is skipped.  In the fuzzy path
    the subset bonus must be capped to SUBSET_CAP_MAX so confidence stays
    below AUTO_MERGE_T.
    """
    from open_brain.people.dedup import SUBSET_CAP_MAX

    existing = [
        PersonRecord(
            memory_id=1,
            style="single",
            members=[{"name": "John Smith Jr", "org": "Widget", "linkedin": None, "aliases": []}],
        )
    ]
    decision = match_person("John Smith", "Acme", None, existing)
    assert decision.action != "auto_merge", (
        f"Org-conflict fuzzy match must not auto_merge; got: {decision.action} "
        f"(confidence={decision.target.confidence if decision.target else 'N/A'}, "
        f"reasons={decision.target.reasons if decision.target else 'N/A'})"
    )
    if decision.target is not None:
        assert decision.target.confidence < SUBSET_CAP_MAX + 0.001, (
            f"Confidence {decision.target.confidence} should be <= SUBSET_CAP_MAX {SUBSET_CAP_MAX}"
        )


def test_exact_name_auto_merge_with_containment_runner_up() -> None:
    """Regression 2: a perfect fuzzy match (sim=1.0) must auto_merge even when
    a containment runner-up (confidence=0.93) exists.

    'Malte' exact match vs PersonRecord(1,'Malte') should be auto_merge;
    the runner-up PersonRecord(2,'Malte Sussdorff') at containment-score must
    not trigger the ambiguity gate.
    """
    existing = [
        PersonRecord(
            memory_id=1,
            style="single",
            members=[{"name": "Malte", "org": None, "linkedin": None, "aliases": []}],
        ),
        PersonRecord(
            memory_id=2,
            style="single",
            members=[{"name": "Malte Sussdorff", "org": None, "linkedin": None, "aliases": []}],
        ),
    ]
    decision = match_person("Malte", None, None, existing)
    assert decision.action == "auto_merge", (
        f"Perfect name match must auto_merge even with containment runner-up; "
        f"got: {decision.action} (rationale: {decision.rationale})"
    )
    assert decision.target is not None
    assert decision.target.memory_id == 1, (
        f"Should match the exact 'Malte' record (id=1), got id={decision.target.memory_id}"
    )


# ---------------------------------------------------------------------------
# AK4 alias-persistence: update_memory called with new alias after containment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alias_persisted_after_containment_auto_merge() -> None:
    """AK4: _resolve_person() calls update_memory with the short name as alias.

    When 'Malte' matches existing 'Malte Sussdorff' via name-containment, the
    ingestor must persist 'Malte' as an alias on memory_id=100.
    """
    from open_brain.data_layer.interface import SaveMemoryResult, SearchResult
    from open_brain.ingest.adapters.transcript import TranscriptIngestor

    # Pre-load an existing person record for "Malte Sussdorff"
    existing_person_record = PersonRecord(
        memory_id=100,
        style="single",
        members=[{"name": "Malte Sussdorff", "org": None, "linkedin": None, "aliases": []}],
    )

    mock_dl = AsyncMock()
    mock_dl.update_memory.return_value = SaveMemoryResult(id=100, message="ok")

    ingestor = TranscriptIngestor(data_layer=mock_dl)

    await ingestor._resolve_person(
        name="Malte",
        existing_records=[existing_person_record],
        run_id="test-run-id",
    )

    # update_memory must have been called exactly once with the alias list
    mock_dl.update_memory.assert_called_once()
    call_kwargs = mock_dl.update_memory.call_args[0][0]  # UpdateMemoryParams positional arg
    assert call_kwargs.id == 100
    assert call_kwargs.metadata == {"aliases": ["Malte"]}
