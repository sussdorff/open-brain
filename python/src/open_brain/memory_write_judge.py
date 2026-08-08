"""Runtime-agnostic judge for structured OpenBrain memory writes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal, TypeAlias

from open_brain.epistemic_provenance import (
    EPISTEMIC_LABELS as EPISTEMIC_LABELS,
    EVIDENCE_ONLY_LABELS as SHARED_EVIDENCE_ONLY_LABELS,
    FAILED_EVIDENCE_LABELS as SHARED_FAILED_EVIDENCE_LABELS,
    INSTRUCTION_GRADE_LABELS as SHARED_INSTRUCTION_GRADE_LABELS,
    EpistemicLabel,
    ExpectedUse as ExpectedUse,
    merge_epistemic_into_judged_provenance,
)
from open_brain.memory_write_proposal import (
    EXPECTED_USES as EXPECTED_USES,
    MEMORY_CATEGORIES as MEMORY_CATEGORIES,
    PROVENANCE_LABELS as PROVENANCE_LABELS,
    REQUIRED_PROPOSAL_FIELDS as REQUIRED_PROPOSAL_FIELDS,
    RETENTION_SCOPES as RETENTION_SCOPES,
    RISK_FLAGS as RISK_FLAGS,
    AuthorizationBasis as AuthorizationBasis,
    MemoryCategory as MemoryCategory,
    MemoryWriteProposal as MemoryWriteProposal,
    ProposalValidationIssue as ProposalValidationIssue,
    ProvenanceLabel as ProvenanceLabel,
    ProvenanceRef as ProvenanceRef,
    RetentionScope as RetentionScope,
    RiskFlag as RiskFlag,
    build_memory_write_proposal as build_memory_write_proposal,
    memory_write_proposal_json_schema as memory_write_proposal_json_schema,
    parse_memory_write_proposal as parse_memory_write_proposal,
    proposal_preflight_issues as proposal_preflight_issues,
    raw_proposal_payload as raw_proposal_payload,
)

# ProvenanceLabel is re-exported from the public proposal module, where it is
# an alias of EpistemicLabel from open-brain-ekn.1 (no hand-copied Literal).
assert ProvenanceLabel is EpistemicLabel

MEMORY_WRITE_JUDGE_POLICY_VERSION = "memory-write-judge.v1"

JudgeDecision: TypeAlias = Literal["ALLOW", "BLOCK", "REVISE", "ESCALATE"]
ReasonCategory: TypeAlias = Literal[
    "schema",
    "authorization",
    "evidence",
    "scope",
    "policy",
    "risk",
    "other",
]

INSTRUCTION_GRADE_LABELS = set(SHARED_INSTRUCTION_GRADE_LABELS)
EVIDENCE_ONLY_LABELS = set(SHARED_EVIDENCE_ONLY_LABELS)
FAILED_EVIDENCE_LABELS = set(SHARED_FAILED_EVIDENCE_LABELS)


@dataclass(frozen=True)
class JudgeOutcome:
    """ALLOW, BLOCK, REVISE, or ESCALATE decision for a memory write."""

    decision: JudgeDecision
    reason: str
    reason_category: ReasonCategory
    provenance_refs: tuple[ProvenanceRef, ...]
    policy_version: str = MEMORY_WRITE_JUDGE_POLICY_VERSION
    constraints: Mapping[str, Any] | None = None
    revised_proposal: Mapping[str, Any] | None = None
    escalation_target: str | None = None
    issues: tuple[dict[str, str], ...] | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serializable outcome payload.

        Structured ``issues`` are included for schema failures and omitted
        when absent so non-schema outcomes stay additive-compatible.
        """
        payload = asdict(self)
        if payload.get("issues") is None:
            payload.pop("issues", None)
        return payload


ReasonedGate: TypeAlias = Callable[[MemoryWriteProposal], JudgeOutcome | None]


def judge_memory_write_proposal(
    raw_proposal: Mapping[str, Any],
    *,
    reasoned_gate: ReasonedGate | None = None,
) -> JudgeOutcome:
    """Judge a raw memory-write proposal.

    Deterministic schema, authorization, provenance, and risk gates always run
    first. A model-reasoned gate can only run after those deterministic gates
    return ALLOW.

    Risk-first fail-closed: raw ``secret`` / ``credential`` flags BLOCK even
    when the envelope is schema-invalid. Structured schema ``issues`` are still
    attached; proposal content is never echoed in the reason.
    """
    proposal, errors = parse_memory_write_proposal(raw_proposal)
    issue_payload = tuple(error.to_dict() for error in errors) if errors else None

    if _raw_risk_flags_include_secret_or_credential(raw_proposal):
        refs: tuple[ProvenanceRef, ...] = ()
        if proposal is not None:
            refs = _outcome_refs(proposal)
        return JudgeOutcome(
            decision="BLOCK",
            reason=(
                "secrets and credentials are ephemeral and must not be "
                "persisted to memory"
            ),
            reason_category="risk",
            provenance_refs=refs,
            issues=issue_payload,
        )

    if errors or proposal is None:
        return JudgeOutcome(
            decision="ESCALATE",
            reason="proposal schema violation: "
            + "; ".join(str(error) for error in errors),
            reason_category="schema",
            provenance_refs=(),
            escalation_target="memory-policy-owner",
            issues=issue_payload,
        )

    deterministic = deterministic_memory_write_gate(proposal)
    if deterministic.decision != "ALLOW" or reasoned_gate is None:
        return deterministic

    reasoned = reasoned_gate(proposal)
    return reasoned or deterministic


def _raw_risk_flags_include_secret_or_credential(raw_proposal: Mapping[str, Any] | Any) -> bool:
    """Return True when raw risk_flags claims secret/credential material."""
    if not isinstance(raw_proposal, Mapping):
        return False
    flags = raw_proposal.get("risk_flags")
    if isinstance(flags, (str, bytes)) or not isinstance(flags, (list, tuple)):
        # Malformed non-sequence flags are schema issues, not risk claims.
        if isinstance(flags, str):
            return flags in {"secret", "credential"}
        return False
    return any(flag in {"secret", "credential"} for flag in flags)


def deterministic_memory_write_gate(proposal: MemoryWriteProposal) -> JudgeOutcome:
    """Apply deterministic memory-write policy without model reasoning."""
    refs = _outcome_refs(proposal)
    risk_flags = set(proposal.risk_flags)

    if risk_flags.intersection({"secret", "credential"}):
        return JudgeOutcome(
            decision="BLOCK",
            reason="secrets and credentials are ephemeral and must not be persisted to memory",
            reason_category="risk",
            provenance_refs=refs,
        )

    if proposal.source_citation.label in FAILED_EVIDENCE_LABELS:
        return JudgeOutcome(
            decision="ESCALATE",
            reason=f"source citation is {proposal.source_citation.label} and cannot support a new memory write",
            reason_category="evidence",
            provenance_refs=(proposal.source_citation,),
            escalation_target="memory-policy-owner",
        )

    auth = proposal.authorization_basis
    if auth is None:
        return JudgeOutcome(
            decision="BLOCK",
            reason="memory write lacks an authorization basis",
            reason_category="authorization",
            provenance_refs=(proposal.source_citation,),
        )

    if auth.label in FAILED_EVIDENCE_LABELS:
        return JudgeOutcome(
            decision="ESCALATE",
            reason=f"authorization basis is {auth.label} and must be refreshed before writing memory",
            reason_category="authorization",
            provenance_refs=refs,
            escalation_target="memory-policy-owner",
        )

    if proposal.category == "policy" and auth.label not in INSTRUCTION_GRADE_LABELS:
        return JudgeOutcome(
            decision="BLOCK",
            reason="policy memories require observed or confirmed authorization",
            reason_category="authorization",
            provenance_refs=refs,
        )

    if proposal.expected_use == "instruction":
        if proposal.source_citation.label in EVIDENCE_ONLY_LABELS:
            return _revise_expected_use_to_evidence(
                proposal,
                "inferred or generated source citations can only be saved as evidence",
                "evidence",
            )
        if auth.label not in INSTRUCTION_GRADE_LABELS:
            return _revise_expected_use_to_evidence(
                proposal,
                "instruction-grade memory requires observed or confirmed authorization",
                "authorization",
            )

    if "policy-sensitive" in risk_flags:
        if proposal.expected_use == "instruction" or proposal.category == "policy":
            if proposal.source_citation.label != "confirmed" or auth.label != "confirmed":
                return JudgeOutcome(
                    decision="ESCALATE",
                    reason="policy-sensitive instruction requires confirmed source and confirmed authorization",
                    reason_category="policy",
                    provenance_refs=refs,
                    escalation_target="memory-policy-owner",
                )

    if "pii" in risk_flags and proposal.retention_scope == "team":
        revised = dict(raw_proposal_payload(proposal))
        revised["retention_scope"] = "personal"
        return JudgeOutcome(
            decision="REVISE",
            reason="PII defaults to personal retention unless a confirmed team-sharing mandate exists",
            reason_category="risk",
            provenance_refs=refs,
            revised_proposal=revised,
        )

    if proposal.retention_scope == "team" and auth.label != "confirmed":
        return JudgeOutcome(
            decision="ESCALATE",
            reason="team-scoped memories require confirmed sharing authorization",
            reason_category="scope",
            provenance_refs=refs,
            escalation_target="memory-policy-owner",
        )

    constraints: dict[str, Any] = {}
    if proposal.source_citation.label in EVIDENCE_ONLY_LABELS:
        constraints["expected_use"] = "evidence"
        constraints["instruction_grade"] = False

    return JudgeOutcome(
        decision="ALLOW",
        reason="proposal is authorized, sufficiently evidenced, and within retention/risk policy",
        reason_category=_allow_reason_category(proposal),
        provenance_refs=refs,
        constraints=constraints or None,
    )


def _allow_reason_category(proposal: MemoryWriteProposal) -> ReasonCategory:
    """Return the dominant metrics category for an allowed write."""
    if proposal.risk_flags or proposal.retention_scope == "team":
        return "risk"
    if (
        proposal.expected_use == "evidence"
        or proposal.source_citation.label in EVIDENCE_ONLY_LABELS
        or proposal.source_citation.label == "confirmed"
    ):
        return "evidence"
    return "authorization"


def memory_metadata_from_judged_proposal(
    raw_proposal: Mapping[str, Any],
    outcome: JudgeOutcome,
) -> dict[str, Any]:
    """Build metadata patch applied to allowed memory writes.

    Persists the judge decision, policy version, provenance references,
    constraints, and expected-use decision. Epistemic fields are written under
    ``metadata.provenance`` without an ``origin`` key so the separate origin
    lineage from ``open-brain-ekn.1`` remains authoritative.
    """
    proposal, errors = parse_memory_write_proposal(raw_proposal)
    if errors or proposal is None:
        return {}

    provenance = merge_epistemic_into_judged_provenance(
        {
            "source_ref": proposal.source_citation.ref,
            "source_label": proposal.source_citation.label,
            "authorization_ref": (
                proposal.authorization_basis.ref
                if proposal.authorization_basis is not None
                else None
            ),
            "authorization_label": (
                proposal.authorization_basis.label
                if proposal.authorization_basis is not None
                else None
            ),
            "expected_use": proposal.expected_use,
            "retention_scope": proposal.retention_scope,
        }
    )
    judge_meta: dict[str, Any] = {
        "decision": outcome.decision,
        "policy_version": outcome.policy_version,
        "reason_category": outcome.reason_category,
        "provenance_refs": [
            {"ref": ref.ref, "label": ref.label} for ref in outcome.provenance_refs
        ],
    }
    if outcome.constraints is not None:
        judge_meta["constraints"] = dict(outcome.constraints)
    return {
        "memory_write_judge": judge_meta,
        "provenance": provenance,
        "risk_flags": list(proposal.risk_flags),
    }


def _revise_expected_use_to_evidence(
    proposal: MemoryWriteProposal,
    reason: str,
    reason_category: ReasonCategory,
) -> JudgeOutcome:
    revised = raw_proposal_payload(proposal)
    revised["expected_use"] = "evidence"
    return JudgeOutcome(
        decision="REVISE",
        reason=reason,
        reason_category=reason_category,
        provenance_refs=_outcome_refs(proposal),
        revised_proposal=revised,
    )


def _outcome_refs(proposal: MemoryWriteProposal) -> tuple[ProvenanceRef, ...]:
    refs = [proposal.source_citation]
    if proposal.authorization_basis is not None:
        refs.append(
            ProvenanceRef(
                ref=proposal.authorization_basis.ref,
                label=proposal.authorization_basis.label,
            )
        )
    return tuple(refs)
