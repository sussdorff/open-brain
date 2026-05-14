"""Runtime-agnostic judge for structured OpenBrain memory writes."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal, TypeAlias

MEMORY_WRITE_JUDGE_POLICY_VERSION = "memory-write-judge.v1"

MemoryCategory: TypeAlias = Literal["preference", "fact", "policy", "lesson", "observation"]
ExpectedUse: TypeAlias = Literal["evidence", "instruction"]
RetentionScope: TypeAlias = Literal["session", "project", "personal", "team"]
ProvenanceLabel: TypeAlias = Literal[
    "observed",
    "inferred",
    "generated",
    "confirmed",
    "disputed",
    "superseded",
]
RiskFlag: TypeAlias = Literal[
    "pii",
    "secret",
    "credential",
    "policy-sensitive",
    "external-confidential",
]
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

MEMORY_CATEGORIES: set[str] = {"preference", "fact", "policy", "lesson", "observation"}
EXPECTED_USES: set[str] = {"evidence", "instruction"}
RETENTION_SCOPES: set[str] = {"session", "project", "personal", "team"}
PROVENANCE_LABELS: set[str] = {
    "observed",
    "inferred",
    "generated",
    "confirmed",
    "disputed",
    "superseded",
}
RISK_FLAGS: set[str] = {
    "pii",
    "secret",
    "credential",
    "policy-sensitive",
    "external-confidential",
}
INSTRUCTION_GRADE_LABELS = {"observed", "confirmed"}
EVIDENCE_ONLY_LABELS = {"inferred", "generated"}
FAILED_EVIDENCE_LABELS = {"disputed", "superseded"}
REQUIRED_PROPOSAL_FIELDS = {
    "intended_memory_content",
    "category",
    "source_citation",
    "authorization_basis",
    "expected_use",
    "retention_scope",
    "risk_flags",
}


@dataclass(frozen=True)
class ProvenanceRef:
    """Evidence or authority reference used by the judge."""

    ref: str
    label: ProvenanceLabel


@dataclass(frozen=True)
class AuthorizationBasis:
    """Who or what authorized the proposed memory write."""

    ref: str
    label: ProvenanceLabel
    granted_by: str | None = None


@dataclass(frozen=True)
class MemoryWriteProposal:
    """The seven-field proposal surface for an OpenBrain memory write."""

    intended_memory_content: str
    category: MemoryCategory
    source_citation: ProvenanceRef
    authorization_basis: AuthorizationBasis | None
    expected_use: ExpectedUse
    retention_scope: RetentionScope
    risk_flags: tuple[RiskFlag, ...] = ()


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

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serializable outcome payload."""
        return asdict(self)


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
    """
    proposal, errors = parse_memory_write_proposal(raw_proposal)
    if errors or proposal is None:
        return JudgeOutcome(
            decision="ESCALATE",
            reason="proposal schema violation: " + "; ".join(errors),
            reason_category="schema",
            provenance_refs=(),
            escalation_target="memory-policy-owner",
        )

    deterministic = deterministic_memory_write_gate(proposal)
    if deterministic.decision != "ALLOW" or reasoned_gate is None:
        return deterministic

    reasoned = reasoned_gate(proposal)
    return reasoned or deterministic


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
        reason_category="other",
        provenance_refs=refs,
        constraints=constraints or None,
    )


def parse_memory_write_proposal(
    raw_proposal: Mapping[str, Any],
) -> tuple[MemoryWriteProposal | None, list[str]]:
    """Parse and validate the seven-field memory-write proposal schema."""
    errors: list[str] = []
    if not isinstance(raw_proposal, Mapping):
        return None, ["proposal: expected object"]

    missing = sorted(REQUIRED_PROPOSAL_FIELDS - set(raw_proposal))
    if missing:
        errors.append("missing required field(s): " + ", ".join(missing))

    content = raw_proposal.get("intended_memory_content")
    if not _is_non_empty_string(content):
        errors.append("intended_memory_content: expected non-empty string")

    category = raw_proposal.get("category")
    if category not in MEMORY_CATEGORIES:
        errors.append(f"category: invalid value {category!r}")

    expected_use = raw_proposal.get("expected_use")
    if expected_use not in EXPECTED_USES:
        errors.append(f"expected_use: invalid value {expected_use!r}")

    retention_scope = raw_proposal.get("retention_scope")
    if retention_scope not in RETENTION_SCOPES:
        errors.append(f"retention_scope: invalid value {retention_scope!r}")

    source = _parse_provenance_ref(raw_proposal.get("source_citation"), "source_citation", errors)
    authorization = _parse_authorization_basis(raw_proposal.get("authorization_basis"), errors)
    risk_flags = _parse_risk_flags(raw_proposal.get("risk_flags"), errors)

    if errors or source is None:
        return None, errors

    return MemoryWriteProposal(
        intended_memory_content=content,
        category=category,
        source_citation=source,
        authorization_basis=authorization,
        expected_use=expected_use,
        retention_scope=retention_scope,
        risk_flags=risk_flags,
    ), []


def raw_proposal_payload(proposal: MemoryWriteProposal) -> dict[str, Any]:
    """Return the canonical JSON-compatible proposal shape."""
    return {
        "intended_memory_content": proposal.intended_memory_content,
        "category": proposal.category,
        "source_citation": asdict(proposal.source_citation),
        "authorization_basis": (
            asdict(proposal.authorization_basis)
            if proposal.authorization_basis is not None
            else None
        ),
        "expected_use": proposal.expected_use,
        "retention_scope": proposal.retention_scope,
        "risk_flags": list(proposal.risk_flags),
    }


def memory_metadata_from_judged_proposal(
    raw_proposal: Mapping[str, Any],
    outcome: JudgeOutcome,
) -> dict[str, Any]:
    """Build metadata patch applied to allowed memory writes."""
    proposal, errors = parse_memory_write_proposal(raw_proposal)
    if errors or proposal is None:
        return {}

    return {
        "memory_write_judge": {
            "decision": outcome.decision,
            "policy_version": outcome.policy_version,
            "reason_category": outcome.reason_category,
        },
        "provenance": {
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
        },
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
        refs.append(ProvenanceRef(
            ref=proposal.authorization_basis.ref,
            label=proposal.authorization_basis.label,
        ))
    return tuple(refs)


def _parse_provenance_ref(
    value: Any,
    field_name: str,
    errors: list[str],
) -> ProvenanceRef | None:
    if not isinstance(value, Mapping):
        errors.append(f"{field_name}: expected object")
        return None

    ref = value.get("ref")
    if not _is_non_empty_string(ref):
        errors.append(f"{field_name}.ref: expected non-empty string")

    label = value.get("label")
    if label not in PROVENANCE_LABELS:
        errors.append(f"{field_name}.label: invalid provenance label {label!r}")

    if errors:
        return None

    return ProvenanceRef(ref=ref, label=label)


def _parse_authorization_basis(
    value: Any,
    errors: list[str],
) -> AuthorizationBasis | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        errors.append("authorization_basis: expected object or null")
        return None

    ref = value.get("ref")
    if not _is_non_empty_string(ref):
        errors.append("authorization_basis.ref: expected non-empty string")

    label = value.get("label")
    if label not in PROVENANCE_LABELS:
        errors.append(f"authorization_basis.label: invalid provenance label {label!r}")

    granted_by = value.get("granted_by")
    if granted_by is not None and not _is_non_empty_string(granted_by):
        errors.append("authorization_basis.granted_by: expected non-empty string when present")

    if errors:
        return None

    return AuthorizationBasis(ref=ref, label=label, granted_by=granted_by)


def _parse_risk_flags(value: Any, errors: list[str]) -> tuple[RiskFlag, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        errors.append("risk_flags: expected array")
        return ()

    parsed: list[RiskFlag] = []
    for index, flag in enumerate(value):
        if flag not in RISK_FLAGS:
            errors.append(f"risk_flags[{index}]: invalid value {flag!r}")
        elif flag not in parsed:
            parsed.append(flag)
    return tuple(parsed)


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
