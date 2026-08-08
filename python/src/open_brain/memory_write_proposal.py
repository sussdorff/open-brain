"""Public Memory Write Proposal contract for Open Brain.

Contract URI: ``standard://open-brain/proposals/memory-write-proposal.v1``

This module is the sole structured claim surface for judged memory writes.
Producers construct and validate proposals here without importing judge
internals. The memory-write judge consumes the same types and parser.

Owned documentation lives at ``docs/standards/memory-write-proposal.md``
(repository pre-migration standards owner).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from open_brain.epistemic_provenance import (
    EPISTEMIC_LABELS,
    EXPECTED_USES as SHARED_EXPECTED_USES,
    EpistemicLabel,
    ExpectedUse,
    is_legal_epistemic_combination,
)

MEMORY_WRITE_PROPOSAL_SCHEMA_VERSION = "memory-write-proposal.v1"
MEMORY_WRITE_PROPOSAL_SCHEMA_ID = (
    "standard://open-brain/proposals/memory-write-proposal.v1"
)

MemoryCategory: TypeAlias = Literal[
    "preference",
    "fact",
    "policy",
    "lesson",
    "observation",
]
RetentionScope: TypeAlias = Literal["session", "project", "personal", "team"]
RiskFlag: TypeAlias = Literal[
    "pii",
    "secret",
    "credential",
    "policy-sensitive",
    "external-confidential",
]
ProvenanceLabel: TypeAlias = EpistemicLabel

MEMORY_CATEGORIES: set[str] = {
    "preference",
    "fact",
    "policy",
    "lesson",
    "observation",
}
EXPECTED_USES: set[str] = set(SHARED_EXPECTED_USES)
RETENTION_SCOPES: set[str] = {"session", "project", "personal", "team"}
PROVENANCE_LABELS: set[str] = set(EPISTEMIC_LABELS)
RISK_FLAGS: set[str] = {
    "pii",
    "secret",
    "credential",
    "policy-sensitive",
    "external-confidential",
}
REQUIRED_PROPOSAL_FIELDS = {
    "intended_memory_content",
    "category",
    "source_citation",
    "authorization_basis",
    "expected_use",
    "retention_scope",
    "risk_flags",
}

# Shared source-locator grammar for Python runtime, Python jsonschema, and
# ECMA-262. Path segments are ASCII-only; non-ASCII path characters are rejected.
# End anchor is (?![\s\S]) so a trailing newline cannot sneak past `$`.
#
# Accepted forms:
# 1) scheme-qualified refs: conversation://current/preference
# 2) namespaced identifiers: agent-session:codex:session-123
# 3) path-like locators, including multi-word ASCII segments:
#    docs/meeting notes/decision.md
_PATH_SEGMENT = r"[A-Za-z0-9._-]+(?: [A-Za-z0-9._-]+)*"
SOURCE_LOCATOR_PATTERN = (
    r"^(?:"
    r"[a-z][a-z0-9+.-]*://\S+"
    r"|[a-z][a-z0-9-]*(?::[^\s:/]+)+"
    r"|(?:" + _PATH_SEGMENT + r"/)+" + _PATH_SEGMENT + r"\.[A-Za-z0-9]+"
    r"|(?:" + _PATH_SEGMENT + r"/){2,}" + _PATH_SEGMENT +
    r"|[A-Za-z0-9._-]{4,}(?: [A-Za-z0-9._-]+)?(?:/" + _PATH_SEGMENT + r")+"
    r")(?![\s\S])"
)
SOURCE_LOCATOR_REGEX = re.compile(SOURCE_LOCATOR_PATTERN)
_NON_EMPTY_STRING_PATTERN = r"^(?=.*\S).+(?![\s\S])"


@dataclass(frozen=True)
class ProposalValidationIssue:
    """Deterministic, machine-readable proposal validation failure."""

    code: str
    field: str
    message: str

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "field": self.field,
            "message": self.message,
        }


@dataclass(frozen=True)
class ProvenanceRef:
    """Evidence or authority reference cited by a proposal."""

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


class MemoryWriteProposalValidationError(ValueError):
    """Raised when constructing or requiring a valid proposal fails."""

    code = "invalid_memory_write_proposal"

    def __init__(self, issues: Sequence[ProposalValidationIssue]):
        self.issues = tuple(issues)
        super().__init__("; ".join(issue.message for issue in self.issues))


def is_source_locator(value: str) -> bool:
    """Return True when value matches the shared source-locator grammar."""
    if not isinstance(value, str) or value != value.strip():
        return False
    return SOURCE_LOCATOR_REGEX.fullmatch(value) is not None


def memory_write_proposal_json_schema() -> dict[str, Any]:
    """Return a JSON Schema producers can use without importing judge internals."""
    non_empty_string = {
        "type": "string",
        "minLength": 1,
        "pattern": _NON_EMPTY_STRING_PATTERN,
    }
    source_locator_string = {
        "type": "string",
        "minLength": 1,
        "pattern": SOURCE_LOCATOR_PATTERN,
        "description": (
            "Stable source locator: scheme-qualified, namespaced, or path-like."
        ),
    }
    provenance_ref_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ref", "label"],
        "properties": {
            "ref": source_locator_string,
            "label": {
                "type": "string",
                "enum": sorted(PROVENANCE_LABELS),
            },
        },
    }
    authorization_object = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ref", "label"],
        "properties": {
            "ref": source_locator_string,
            "label": {
                "type": "string",
                "enum": sorted(PROVENANCE_LABELS),
            },
            "granted_by": {
                "anyOf": [non_empty_string, {"type": "null"}],
            },
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": MEMORY_WRITE_PROPOSAL_SCHEMA_ID,
        "title": "Open Brain Memory Write Proposal",
        "type": "object",
        "additionalProperties": False,
        "required": sorted(REQUIRED_PROPOSAL_FIELDS),
        "properties": {
            "intended_memory_content": {
                **non_empty_string,
                "description": "Exact memory content proposed for persistence.",
            },
            "category": {
                "type": "string",
                "enum": sorted(MEMORY_CATEGORIES),
            },
            "source_citation": provenance_ref_schema,
            "authorization_basis": {
                "anyOf": [authorization_object, {"type": "null"}],
            },
            "expected_use": {
                "type": "string",
                "enum": sorted(EXPECTED_USES),
            },
            "retention_scope": {
                "type": "string",
                "enum": sorted(RETENTION_SCOPES),
            },
            "risk_flags": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(RISK_FLAGS)},
                "uniqueItems": True,
            },
        },
        "x-open-brain-schema-version": MEMORY_WRITE_PROPOSAL_SCHEMA_VERSION,
        "x-open-brain-source-locator-pattern": SOURCE_LOCATOR_PATTERN,
    }


def parse_memory_write_proposal(
    raw_proposal: Mapping[str, Any] | Any,
) -> tuple[MemoryWriteProposal | None, list[ProposalValidationIssue]]:
    """Parse and validate the seven-field memory-write proposal schema.

    Returns structured, deterministic issues. Actor prose never substitutes for
    missing evidence or authorization fields. Authorization may be JSON ``null``
    for wire compatibility; producers should run :func:`proposal_preflight_issues`
    before judge submission.

    Never returns ``(None, [])``: failure always includes at least one issue.
    """
    errors: list[ProposalValidationIssue] = []
    if not isinstance(raw_proposal, Mapping):
        return None, [
            ProposalValidationIssue(
                code="invalid_proposal_type",
                field="proposal",
                message="proposal: expected object",
            )
        ]

    unknown = sorted(set(raw_proposal) - REQUIRED_PROPOSAL_FIELDS)
    for field in unknown:
        errors.append(
            ProposalValidationIssue(
                code="unexpected_field",
                field=field,
                message=f"{field}: unexpected field",
            )
        )

    missing = sorted(REQUIRED_PROPOSAL_FIELDS - set(raw_proposal))
    for field in missing:
        errors.append(
            ProposalValidationIssue(
                code="missing_required_field",
                field=field,
                message=f"missing required field: {field}",
            )
        )

    content = raw_proposal.get("intended_memory_content")
    if "intended_memory_content" in raw_proposal and not _is_non_empty_string(content):
        errors.append(
            ProposalValidationIssue(
                code="invalid_intended_memory_content",
                field="intended_memory_content",
                message="intended_memory_content: expected non-empty string",
            )
        )

    category = raw_proposal.get("category")
    if "category" in raw_proposal and category not in MEMORY_CATEGORIES:
        errors.append(
            ProposalValidationIssue(
                code="invalid_category",
                field="category",
                message=f"category: invalid value {category!r}",
            )
        )

    expected_use = raw_proposal.get("expected_use")
    if "expected_use" in raw_proposal and expected_use not in EXPECTED_USES:
        errors.append(
            ProposalValidationIssue(
                code="invalid_expected_use",
                field="expected_use",
                message=f"expected_use: invalid value {expected_use!r}",
            )
        )

    retention_scope = raw_proposal.get("retention_scope")
    if "retention_scope" in raw_proposal and retention_scope not in RETENTION_SCOPES:
        errors.append(
            ProposalValidationIssue(
                code="unclear_retention",
                field="retention_scope",
                message=f"retention_scope: invalid value {retention_scope!r}",
            )
        )

    source = None
    if "source_citation" in raw_proposal:
        source = _parse_provenance_ref(
            raw_proposal.get("source_citation"),
            "source_citation",
            errors,
        )

    authorization = None
    if "authorization_basis" in raw_proposal:
        authorization = _parse_authorization_basis(
            raw_proposal.get("authorization_basis"),
            errors,
        )

    risk_flags: tuple[RiskFlag, ...] = ()
    if "risk_flags" in raw_proposal:
        risk_flags = _parse_risk_flags(raw_proposal.get("risk_flags"), errors)

    if errors:
        return None, errors

    if (
        source is None
        or not _is_non_empty_string(content)
        or category not in MEMORY_CATEGORIES
        or expected_use not in EXPECTED_USES
        or retention_scope not in RETENTION_SCOPES
    ):
        # Defensive: every failure path must carry a machine-readable issue.
        return None, [
            ProposalValidationIssue(
                code="invalid_proposal_type",
                field="proposal",
                message="proposal: incomplete after validation",
            )
        ]

    assert isinstance(content, str)
    assert isinstance(category, str)
    assert isinstance(expected_use, str)
    assert isinstance(retention_scope, str)

    return MemoryWriteProposal(
        intended_memory_content=content,
        category=category,  # type: ignore[arg-type]
        source_citation=source,
        authorization_basis=authorization,
        expected_use=expected_use,  # type: ignore[arg-type]
        retention_scope=retention_scope,  # type: ignore[arg-type]
        risk_flags=risk_flags,
    ), []


def proposal_preflight_issues(
    raw_proposal: Mapping[str, Any] | Any,
) -> list[ProposalValidationIssue]:
    """Return schema issues plus producer preflight advisories.

    Always includes any :func:`parse_memory_write_proposal` schema failures.
    When the proposal is schema-valid, also reports ``missing_authorization`` and
    ``illegal_epistemic_combination`` advisories. Does not apply judge
    ALLOW/BLOCK/REVISE/ESCALATE outcome policy.
    """
    proposal, errors = parse_memory_write_proposal(raw_proposal)
    if errors:
        return list(errors)
    if proposal is None:
        return [
            ProposalValidationIssue(
                code="invalid_proposal_type",
                field="proposal",
                message="proposal: expected object",
            )
        ]

    issues: list[ProposalValidationIssue] = []
    if proposal.authorization_basis is None:
        issues.append(
            ProposalValidationIssue(
                code="missing_authorization",
                field="authorization_basis",
                message="authorization_basis: required before judged persistence",
            )
        )

    if not is_legal_epistemic_combination(
        proposal.source_citation.label,
        proposal.expected_use,
    ):
        issues.append(
            ProposalValidationIssue(
                code="illegal_epistemic_combination",
                field="expected_use",
                message=(
                    "source_citation.label="
                    f"{proposal.source_citation.label!r} cannot request "
                    f"expected_use={proposal.expected_use!r}"
                ),
            )
        )

    return issues


def build_memory_write_proposal(
    *,
    intended_memory_content: str,
    category: MemoryCategory | str,
    source_citation: Mapping[str, Any] | ProvenanceRef,
    authorization_basis: Mapping[str, Any] | AuthorizationBasis | None,
    expected_use: ExpectedUse | str,
    retention_scope: RetentionScope | str,
    risk_flags: Sequence[str] | None = None,
) -> MemoryWriteProposal:
    """Construct a validated proposal for structured producers."""
    raw: dict[str, Any] = {
        "intended_memory_content": intended_memory_content,
        "category": category,
        "source_citation": (
            {"ref": source_citation.ref, "label": source_citation.label}
            if isinstance(source_citation, ProvenanceRef)
            else dict(source_citation)
        ),
        "authorization_basis": _authorization_payload(authorization_basis),
        "expected_use": expected_use,
        "retention_scope": retention_scope,
        "risk_flags": list(risk_flags or ()),
    }
    proposal, errors = parse_memory_write_proposal(raw)
    if errors or proposal is None:
        raise MemoryWriteProposalValidationError(errors)
    return proposal


def raw_proposal_payload(proposal: MemoryWriteProposal) -> dict[str, Any]:
    """Return the canonical JSON-compatible seven-field proposal shape.

    Optional ``granted_by`` is omitted when absent so the payload validates
    against :func:`memory_write_proposal_json_schema`.
    """
    return {
        "intended_memory_content": proposal.intended_memory_content,
        "category": proposal.category,
        "source_citation": {
            "ref": proposal.source_citation.ref,
            "label": proposal.source_citation.label,
        },
        "authorization_basis": _authorization_payload(proposal.authorization_basis),
        "expected_use": proposal.expected_use,
        "retention_scope": proposal.retention_scope,
        "risk_flags": list(proposal.risk_flags),
    }


def _authorization_payload(
    authorization_basis: Mapping[str, Any] | AuthorizationBasis | None,
) -> dict[str, Any] | None:
    if authorization_basis is None:
        return None
    if isinstance(authorization_basis, AuthorizationBasis):
        payload: dict[str, Any] = {
            "ref": authorization_basis.ref,
            "label": authorization_basis.label,
        }
        if authorization_basis.granted_by is not None:
            payload["granted_by"] = authorization_basis.granted_by
        return payload

    # Preserve producer-supplied mappings so missing/unknown fields reach parser.
    return dict(authorization_basis)


def _parse_provenance_ref(
    value: Any,
    field_name: str,
    errors: list[ProposalValidationIssue],
) -> ProvenanceRef | None:
    if not isinstance(value, Mapping):
        errors.append(
            ProposalValidationIssue(
                code="invalid_source_citation",
                field=field_name,
                message=f"{field_name}: expected object",
            )
        )
        return None

    for key in sorted(set(value) - {"ref", "label"}):
        errors.append(
            ProposalValidationIssue(
                code="unexpected_field",
                field=f"{field_name}.{key}",
                message=f"{field_name}.{key}: unexpected field",
            )
        )

    ref = value.get("ref")
    label = value.get("label")
    local_error = False

    if not _is_non_empty_string(ref):
        errors.append(
            ProposalValidationIssue(
                code="invalid_source_citation",
                field=f"{field_name}.ref",
                message=f"{field_name}.ref: expected non-empty string",
            )
        )
        local_error = True
    elif isinstance(ref, str) and not is_source_locator(ref):
        errors.append(
            ProposalValidationIssue(
                code="vague_source",
                field=f"{field_name}.ref",
                message=(
                    f"{field_name}.ref: {ref!r} is not a source locator; "
                    "use scheme-qualified, namespaced, or path-like form"
                ),
            )
        )
        local_error = True

    if label not in PROVENANCE_LABELS:
        errors.append(
            ProposalValidationIssue(
                code="invalid_source_citation",
                field=f"{field_name}.label",
                message=f"{field_name}.label: invalid provenance label {label!r}",
            )
        )
        local_error = True

    if local_error:
        return None

    assert isinstance(ref, str)
    assert isinstance(label, str)
    return ProvenanceRef(ref=ref, label=label)  # type: ignore[arg-type]


def _parse_authorization_basis(
    value: Any,
    errors: list[ProposalValidationIssue],
) -> AuthorizationBasis | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        errors.append(
            ProposalValidationIssue(
                code="invalid_authorization_basis",
                field="authorization_basis",
                message="authorization_basis: expected object or null",
            )
        )
        return None

    for key in sorted(set(value) - {"ref", "label", "granted_by"}):
        errors.append(
            ProposalValidationIssue(
                code="unexpected_field",
                field=f"authorization_basis.{key}",
                message=f"authorization_basis.{key}: unexpected field",
            )
        )

    ref = value.get("ref")
    label = value.get("label")
    granted_by = value.get("granted_by")
    local_error = False

    if not _is_non_empty_string(ref):
        errors.append(
            ProposalValidationIssue(
                code="invalid_authorization_basis",
                field="authorization_basis.ref",
                message="authorization_basis.ref: expected non-empty string",
            )
        )
        local_error = True
    elif isinstance(ref, str) and not is_source_locator(ref):
        errors.append(
            ProposalValidationIssue(
                code="vague_source",
                field="authorization_basis.ref",
                message=(
                    f"authorization_basis.ref: {ref!r} is not a source locator"
                ),
            )
        )
        local_error = True

    if label not in PROVENANCE_LABELS:
        errors.append(
            ProposalValidationIssue(
                code="invalid_authorization_basis",
                field="authorization_basis.label",
                message=f"authorization_basis.label: invalid provenance label {label!r}",
            )
        )
        local_error = True

    if "granted_by" in value and granted_by is not None and not _is_non_empty_string(
        granted_by
    ):
        errors.append(
            ProposalValidationIssue(
                code="invalid_authorization_basis",
                field="authorization_basis.granted_by",
                message=(
                    "authorization_basis.granted_by: expected non-empty string "
                    "when present"
                ),
            )
        )
        local_error = True

    if local_error:
        return None

    assert isinstance(ref, str)
    assert isinstance(label, str)
    granted: str | None
    if "granted_by" not in value or granted_by is None:
        granted = None
    else:
        assert isinstance(granted_by, str)
        granted = granted_by
    return AuthorizationBasis(ref=ref, label=label, granted_by=granted)  # type: ignore[arg-type]


def _parse_risk_flags(
    value: Any,
    errors: list[ProposalValidationIssue],
) -> tuple[RiskFlag, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        errors.append(
            ProposalValidationIssue(
                code="invalid_risk_flags",
                field="risk_flags",
                message="risk_flags: expected array",
            )
        )
        return ()

    parsed: list[RiskFlag] = []
    seen: set[str] = set()
    for index, flag in enumerate(value):
        if flag not in RISK_FLAGS:
            errors.append(
                ProposalValidationIssue(
                    code="invalid_risk_flags",
                    field=f"risk_flags[{index}]",
                    message=f"risk_flags[{index}]: invalid value {flag!r}",
                )
            )
            continue
        if flag in seen:
            errors.append(
                ProposalValidationIssue(
                    code="invalid_risk_flags",
                    field=f"risk_flags[{index}]",
                    message=f"risk_flags[{index}]: duplicate value {flag!r}",
                )
            )
            continue
        seen.add(flag)
        parsed.append(flag)
    return tuple(parsed)


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
