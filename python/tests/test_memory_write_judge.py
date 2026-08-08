"""Tests for the OpenBrain memory-write judge."""

from __future__ import annotations

import json
from pathlib import Path

from open_brain.memory_write_judge import (
    JudgeOutcome,
    ProvenanceRef,
    judge_memory_write_proposal,
    memory_metadata_from_judged_proposal,
)


EVAL_SUITE = Path(__file__).resolve().parents[2] / "agents" / "memory-write-judge-eval.json"


def _load_cases() -> list[dict]:
    data = json.loads(EVAL_SUITE.read_text(encoding="utf-8"))
    return data["cases"]


def test_eval_suite_has_minimum_case_coverage() -> None:
    """AK3: paired eval suite has at least 20 cases and covers all outcomes."""
    cases = _load_cases()
    decisions = {case["expected_decision"] for case in cases}

    assert len(cases) >= 20
    assert decisions == {"ALLOW", "BLOCK", "REVISE", "ESCALATE"}


def test_eval_suite_matches_deterministic_judge() -> None:
    """AK2/AK3: default deterministic gate matches the paired eval suite."""
    for case in _load_cases():
        outcome = judge_memory_write_proposal(case["proposal"])

        assert outcome.decision == case["expected_decision"], case["case_id"]
        assert outcome.reason_category == case["expected_reason_category"], case["case_id"]
        assert outcome.policy_version == "memory-write-judge.v1"


def test_allow_outcomes_have_branch_specific_reason_categories() -> None:
    """AK open-brain-ekn.6: ALLOW metrics do not collapse into `other`."""
    allow_categories = {
        judge_memory_write_proposal(case["proposal"]).reason_category
        for case in _load_cases()
        if case["expected_decision"] == "ALLOW"
    }

    assert "other" not in allow_categories
    assert allow_categories == {"authorization", "evidence", "risk"}


def test_revise_returns_full_replacement_proposal() -> None:
    """AK2: REVISE supplies a replacement proposal with instruction downgraded."""
    proposal = {
        "intended_memory_content": "User probably wants all future answers in long form.",
        "category": "preference",
        "source_citation": {"ref": "agent://style-inference", "label": "inferred"},
        "authorization_basis": {
            "ref": "conversation://current",
            "label": "observed",
            "granted_by": "user",
        },
        "expected_use": "instruction",
        "retention_scope": "personal",
        "risk_flags": [],
    }

    outcome = judge_memory_write_proposal(proposal)

    assert outcome.decision == "REVISE"
    assert outcome.revised_proposal is not None
    assert outcome.revised_proposal["expected_use"] == "evidence"


def test_deterministic_gate_blocks_before_reasoned_gate() -> None:
    """AK4: malformed or forbidden proposals do not reach model-reasoned judgment."""
    called = False
    proposal = {
        "intended_memory_content": "API token begins with redacted.",
        "category": "fact",
        "source_citation": {"ref": "terminal://env", "label": "observed"},
        "authorization_basis": {
            "ref": "conversation://current",
            "label": "observed",
            "granted_by": "user",
        },
        "expected_use": "evidence",
        "retention_scope": "personal",
        "risk_flags": ["secret"],
    }

    def reasoned_gate(_proposal):
        nonlocal called
        called = True
        return None

    outcome = judge_memory_write_proposal(proposal, reasoned_gate=reasoned_gate)

    assert outcome.decision == "BLOCK"
    assert called is False


def test_reasoned_gate_can_run_after_deterministic_allow() -> None:
    """AK2: model-reasoned gates are separate and run only after deterministic allow."""
    called = False
    proposal = _load_cases()[0]["proposal"]

    def reasoned_gate(_proposal):
        nonlocal called
        called = True
        return JudgeOutcome(
            decision="ESCALATE",
            reason="human review requested by reasoned policy",
            reason_category="policy",
            provenance_refs=(ProvenanceRef(ref="policy://reasoned", label="observed"),),
            escalation_target="memory-policy-owner",
        )

    outcome = judge_memory_write_proposal(proposal, reasoned_gate=reasoned_gate)

    assert called is True
    assert outcome.decision == "ESCALATE"
    assert outcome.reason_category == "policy"


def test_allowed_proposal_metadata_preserves_provenance() -> None:
    """AK4: allowed writes carry evidence-vs-instruction metadata structurally."""
    proposal = _load_cases()[2]["proposal"]
    outcome = judge_memory_write_proposal(proposal)

    metadata = memory_metadata_from_judged_proposal(proposal, outcome)

    assert metadata["memory_write_judge"]["decision"] == "ALLOW"
    assert metadata["memory_write_judge"]["policy_version"] == "memory-write-judge.v1"
    assert metadata["provenance"]["source_label"] == "generated"
    assert metadata["provenance"]["expected_use"] == "evidence"
    assert metadata["provenance"]["epistemic_version"] == "epistemic-provenance.v1"
