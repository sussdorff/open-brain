"""Read-time retrieval contract for Open Brain consumers.

Contract URI: ``standard://open-brain/retrieval/retrieval-contract.v1``

Every consumer declares what retrieved memory may influence. Missing provenance,
legacy unlabeled rows, external ingestion, and evidence-only epistemic labels
never become identity, constraint, policy, or system-instruction authority.

High-authority elevation requires a server-supplied, ledger-backed promotion
projection from ``memory_promotion_events``. Actor-authored
``metadata.retrieval_promotion`` and write-time Judge ``ALLOW`` are never
trusted as read-time promotion. Direct calls without a projection fail closed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from open_brain.epistemic_provenance import (
    EPISTEMIC_LABELS,
    EXPECTED_USES,
)
from open_brain.memory_promotion import PromotionProjection

RETRIEVAL_CONTRACT_SCHEMA_VERSION = "retrieval-contract.v1"
RETRIEVAL_CONTRACT_SCHEMA_ID = (
    "standard://open-brain/retrieval/retrieval-contract.v1"
)

HIGH_AUTHORITY_DISABLED_REASON = (
    "high_authority_disabled_pending_open_brain_ekn_5_promotion_ledger"
)

PROVENANCE_FLOOR_MIN_LABELS: frozenset[str] = frozenset({"observed", "confirmed"})
PROVENANCE_FLOOR_EXCLUDE_LABELS: frozenset[str] = frozenset(
    {"disputed", "superseded", "inferred", "generated"}
)
# Caller-authored authoritative source strings must not impersonate trust.
# Server-constructed profiles may still declare reserved descriptive names
# without passing through parse_retrieval_contract.
RESERVED_AUTHORITATIVE_SOURCE_PREFIXES: tuple[str, ...] = (
    "user-confirmed-",
    "server-issued-",
)

Influence: TypeAlias = Literal[
    "evidence",
    "context",
    "identity",
    "constraint",
    "policy",
    "system_instruction",
]
PromotionState: TypeAlias = Literal[
    "unpromoted",
    "promoted",
    "disputed",
    "superseded",
    "legacy_unlabeled",
]
EpistemicStatus: TypeAlias = Literal["declared", "missing", "invalid"]
ProfileName: TypeAlias = Literal[
    "bead-orchestrator",
    "claude-wake-up",
    "ios-mobile-readonly",
    "compatibility",
]

INFLUENCES: set[str] = {
    "evidence",
    "context",
    "identity",
    "constraint",
    "policy",
    "system_instruction",
}
HIGH_AUTHORITY_INFLUENCES: set[str] = {
    "identity",
    "constraint",
    "policy",
    "system_instruction",
}
EVIDENCE_INFLUENCES: set[str] = {"evidence", "context"}
PROMOTION_STATES: set[str] = {
    "unpromoted",
    "promoted",
    "disputed",
    "superseded",
    "legacy_unlabeled",
}
PROFILE_NAMES: set[str] = {
    "bead-orchestrator",
    "claude-wake-up",
    "ios-mobile-readonly",
    "compatibility",
}
WORK_OBJECT_KINDS: set[str] = {
    "project",
    "bead",
    "session",
    "agent",
    "mobile_client",
    "legacy_caller",
}
EXTERNAL_INGESTION_ROUTES: set[str] = {
    "url",
    "external",
    "web",
    "web-fetch",
    "url-ingest",
}
EXTERNAL_PRODUCERS: set[str] = {
    "url-ingest",
    "web-fetch",
    "external",
    "http-ingest",
}

_METADATA_EXCERPT_MAX_BYTES = 1024
_METADATA_SCALAR_MAX_CHARS = 200
_METADATA_RISK_FLAG_MAX_ITEMS = 8
_METADATA_RISK_FLAG_MAX_CHARS = 64
_METADATA_PROMOTION_FIELD_MAX_CHARS = 128

_NON_EMPTY_STRING_PATTERN = r"^(?=\S)(?!.*\s$)(?!.*[\r\n]).+$"

_INFLUENCE_RANK: dict[str, int] = {
    "evidence": 0,
    "context": 1,
    "identity": 2,
    "constraint": 2,
    "policy": 2,
    "system_instruction": 3,
}


class RetrievalContractValidationError(ValueError):
    """Raised when a retrieval contract or unit fails deterministic validation."""

    code = "invalid_retrieval_contract"

    def __init__(self, message: str, *, field: str | None = None):
        self.field = field
        super().__init__(message)


@dataclass(frozen=True)
class WorkObject:
    """What the consumer is currently working on."""

    kind: str
    id: str
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind, "id": self.id}
        if self.label is not None:
            payload["label"] = self.label
        return payload


@dataclass(frozen=True)
class AuthoritativeSourceDecl:
    """Declared authoritative source for one retrieval unit kind."""

    unit_kind: str
    source: str

    def to_dict(self) -> dict[str, str]:
        return {"unit_kind": self.unit_kind, "source": self.source}


@dataclass(frozen=True)
class Permissions:
    """Consumer permissions at the retrieval boundary."""

    read: bool = True
    write_back: bool = False
    allow_high_authority: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "read": self.read,
            "write_back": self.write_back,
            "allow_high_authority": self.allow_high_authority,
        }


@dataclass(frozen=True)
class ProvenanceRequirements:
    """Minimum provenance needed for high-authority influence.

    Callers may tighten relative to the module provenance floor; relaxation is
    rejected at parse time.
    """

    min_labels_for_high_authority: frozenset[str] = PROVENANCE_FLOOR_MIN_LABELS
    require_instruction_expected_use: bool = True
    require_promotion_audit_reason: bool = True
    exclude_labels: frozenset[str] = PROVENANCE_FLOOR_EXCLUDE_LABELS

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_labels_for_high_authority": sorted(
                self.min_labels_for_high_authority
            ),
            "require_instruction_expected_use": (
                self.require_instruction_expected_use
            ),
            "require_promotion_audit_reason": self.require_promotion_audit_reason,
            "exclude_labels": sorted(self.exclude_labels),
        }


@dataclass(frozen=True)
class CompiledContextCandidate:
    """A section that may be compiled into consumer context."""

    section: str
    max_influence: Influence
    require_promotion: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "max_influence": self.max_influence,
            "require_promotion": self.require_promotion,
        }


@dataclass(frozen=True)
class WriteBackContract:
    """Whether and how a consumer may write memory back."""

    allowed: bool = False
    requires_memory_write_proposal: bool = True
    allowed_expected_uses: frozenset[str] = frozenset({"evidence"})

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "requires_memory_write_proposal": self.requires_memory_write_proposal,
            "allowed_expected_uses": sorted(self.allowed_expected_uses),
        }


@dataclass(frozen=True)
class RetrievalContract:
    """Versioned seven-dimension retrieval contract."""

    version: str
    work_object: WorkObject
    retrieval_units: tuple[str, ...]
    authoritative_sources: tuple[AuthoritativeSourceDecl, ...]
    permissions: Permissions
    provenance_requirements: ProvenanceRequirements
    compiled_context_candidates: tuple[CompiledContextCandidate, ...]
    write_back: WriteBackContract
    profile: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": self.version,
            "work_object": self.work_object.to_dict(),
            "retrieval_units": list(self.retrieval_units),
            "authoritative_sources": [
                item.to_dict() for item in self.authoritative_sources
            ],
            "permissions": self.permissions.to_dict(),
            "provenance_requirements": self.provenance_requirements.to_dict(),
            "compiled_context_candidates": [
                item.to_dict() for item in self.compiled_context_candidates
            ],
            "write_back": self.write_back.to_dict(),
        }
        if self.profile is not None:
            payload["profile"] = self.profile
        return payload

    def source_for_unit_kind(self, unit_kind: str) -> str:
        for decl in self.authoritative_sources:
            if decl.unit_kind == unit_kind:
                return decl.source
        raise RetrievalContractValidationError(
            f"authoritative_sources: no declaration for unit kind {unit_kind!r}",
            field="authoritative_sources",
        )


@dataclass(frozen=True)
class RetrievalUnit:
    """One provenance-preserving retrieved memory unit."""

    memory_id: int
    content: str
    title: str | None
    subtitle: str | None
    narrative: str | None
    memory_type: str | None
    category: str | None
    origin_producer: str | None
    origin_source_ref: str | None
    ingestion_route: str | None
    content_type: str | None
    source_label: str
    expected_use: str
    epistemic_status: EpistemicStatus
    promotion_state: PromotionState
    requested_influence: Influence
    effective_influence: Influence
    contract_version: str
    audit_reason: str
    authoritative_source: str
    section: str
    metadata_excerpt: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "title": self.title,
            "subtitle": self.subtitle,
            "narrative": self.narrative,
            "memory_type": self.memory_type,
            "category": self.category,
            "origin_producer": self.origin_producer,
            "origin_source_ref": self.origin_source_ref,
            "ingestion_route": self.ingestion_route,
            "content_type": self.content_type,
            "source_label": self.source_label,
            "expected_use": self.expected_use,
            "epistemic_status": self.epistemic_status,
            "promotion_state": self.promotion_state,
            "requested_influence": self.requested_influence,
            "effective_influence": self.effective_influence,
            "contract_version": self.contract_version,
            "audit_reason": self.audit_reason,
            "authoritative_source": self.authoritative_source,
            "section": self.section,
            "metadata_excerpt": dict(self.metadata_excerpt),
        }


@dataclass(frozen=True)
class RetrievalResult:
    """Contract-bound retrieval output."""

    contract: RetrievalContract
    units: tuple[RetrievalUnit, ...]
    contract_version: str = RETRIEVAL_CONTRACT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "contract": self.contract.to_dict(),
            "units": [unit.to_dict() for unit in self.units],
            "high_authority_units": [
                unit.to_dict()
                for unit in self.units
                if unit.effective_influence in HIGH_AUTHORITY_INFLUENCES
            ],
        }


def retrieval_contract_json_schema() -> dict[str, Any]:
    """Machine-readable JSON Schema for retrieval-contract.v1.

    Expresses every constraint that JSON Schema 2020-12 can encode, including
    the provenance exclude floor and per-profile security semantics. Unique
    ``authoritative_sources[].unit_kind`` and unique
    ``compiled_context_candidates[].section`` are enforced by
    :func:`retrieval_contract_schema_is_valid` (draft cannot unique-by-key).
    """
    non_empty = {
        "type": "string",
        "minLength": 1,
        "pattern": _NON_EMPTY_STRING_PATTERN,
    }
    # Descriptive sources only; reserved trust namespaces are rejected.
    caller_source = {
        "type": "string",
        "minLength": 1,
        "not": {
            "anyOf": [
                {"pattern": "^user-confirmed-"},
                {"pattern": "^server-issued-"},
            ]
        },
        "pattern": _NON_EMPTY_STRING_PATTERN,
    }
    ha_influence = {
        "type": "string",
        "enum": sorted(HIGH_AUTHORITY_INFLUENCES),
    }
    exclude_floor_contains = [
        {"contains": {"const": label}}
        for label in sorted(PROVENANCE_FLOOR_EXCLUDE_LABELS)
    ]
    no_ha_permissions = {
        "type": "object",
        "additionalProperties": False,
        "required": ["read", "write_back", "allow_high_authority"],
        "properties": {
            "read": {"const": True},
            "write_back": {"const": False},
            "allow_high_authority": {"const": False},
        },
    }
    write_back_denied = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "allowed",
            "requires_memory_write_proposal",
            "allowed_expected_uses",
        ],
        "properties": {
            "allowed": {"const": False},
            "requires_memory_write_proposal": {"type": "boolean"},
            "allowed_expected_uses": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": sorted(EXPECTED_USES)},
            },
        },
    }

    def _profile_rule(
        profile: str,
        *,
        permissions: dict[str, Any],
        write_back: dict[str, Any],
        require_ha_section: bool = False,
        require_promotion_identity: bool = False,
    ) -> dict[str, Any]:
        then_props: dict[str, Any] = {
            "permissions": permissions,
            "write_back": write_back,
        }
        then: dict[str, Any] = {"properties": then_props}
        if require_ha_section:
            then_props["compiled_context_candidates"] = {
                "contains": {
                    "type": "object",
                    "required": ["max_influence", "require_promotion"],
                    "properties": {
                        "max_influence": ha_influence,
                        "require_promotion": {"const": True},
                    },
                }
            }
        if require_promotion_identity:
            then_props["compiled_context_candidates"] = {
                "allOf": [
                    {
                        "contains": {
                            "type": "object",
                            "required": ["section", "max_influence", "require_promotion"],
                            "properties": {
                                "section": {"const": "identity"},
                                "max_influence": {"const": "identity"},
                                "require_promotion": {"const": True},
                            },
                        }
                    },
                    {
                        "contains": {
                            "type": "object",
                            "required": ["section", "max_influence", "require_promotion"],
                            "properties": {
                                "section": {"const": "constraints"},
                                "max_influence": {"const": "constraint"},
                                "require_promotion": {"const": True},
                            },
                        }
                    },
                ]
            }
        return {
            "if": {
                "properties": {"profile": {"const": profile}},
                "required": ["profile"],
            },
            "then": then,
        }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": RETRIEVAL_CONTRACT_SCHEMA_ID,
        "title": "Open Brain Retrieval Contract v1",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "version",
            "work_object",
            "retrieval_units",
            "authoritative_sources",
            "permissions",
            "provenance_requirements",
            "compiled_context_candidates",
            "write_back",
        ],
        "properties": {
            "version": {
                "type": "string",
                "const": RETRIEVAL_CONTRACT_SCHEMA_VERSION,
            },
            "profile": {
                "type": "string",
                "enum": sorted(PROFILE_NAMES),
            },
            "work_object": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "id"],
                "properties": {
                    "kind": {"type": "string", "enum": sorted(WORK_OBJECT_KINDS)},
                    "id": non_empty,
                    "label": non_empty,
                },
            },
            "retrieval_units": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": non_empty,
            },
            "authoritative_sources": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["unit_kind", "source"],
                    "properties": {
                        "unit_kind": non_empty,
                        "source": caller_source,
                    },
                },
            },
            "permissions": {
                "type": "object",
                "additionalProperties": False,
                "required": ["read", "write_back", "allow_high_authority"],
                "properties": {
                    "read": {"const": True},
                    "write_back": {"type": "boolean"},
                    "allow_high_authority": {"type": "boolean"},
                },
            },
            "provenance_requirements": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "min_labels_for_high_authority",
                    "require_instruction_expected_use",
                    "require_promotion_audit_reason",
                ],
                "properties": {
                    "min_labels_for_high_authority": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {
                            "type": "string",
                            "enum": sorted(PROVENANCE_FLOOR_MIN_LABELS),
                        },
                    },
                    "require_instruction_expected_use": {"const": True},
                    "require_promotion_audit_reason": {"const": True},
                    "exclude_labels": {
                        "type": "array",
                        "uniqueItems": True,
                        "minItems": len(PROVENANCE_FLOOR_EXCLUDE_LABELS),
                        "items": {
                            "type": "string",
                            "enum": sorted(EPISTEMIC_LABELS),
                        },
                        "allOf": exclude_floor_contains,
                    },
                },
            },
            "compiled_context_candidates": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["section", "max_influence", "require_promotion"],
                    "properties": {
                        "section": non_empty,
                        "max_influence": {"$ref": "#/$defs/influence"},
                        "require_promotion": {"type": "boolean"},
                    },
                },
            },
            "write_back": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "allowed",
                    "requires_memory_write_proposal",
                    "allowed_expected_uses",
                ],
                "properties": {
                    "allowed": {"type": "boolean"},
                    "requires_memory_write_proposal": {"type": "boolean"},
                    "allowed_expected_uses": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {
                            "type": "string",
                            "enum": sorted(EXPECTED_USES),
                        },
                    },
                },
            },
        },
        "allOf": [
            {
                "if": {
                    "properties": {
                        "permissions": {
                            "properties": {
                                "allow_high_authority": {"const": True},
                            },
                            "required": ["allow_high_authority"],
                        }
                    },
                    "required": ["permissions"],
                },
                "then": {
                    "properties": {
                        "compiled_context_candidates": {
                            "contains": {
                                "type": "object",
                                "properties": {
                                    "max_influence": ha_influence,
                                },
                                "required": ["max_influence"],
                            }
                        }
                    }
                },
            },
            {
                "if": {
                    "properties": {
                        "compiled_context_candidates": {
                            "contains": {
                                "type": "object",
                                "properties": {
                                    "max_influence": ha_influence,
                                },
                                "required": ["max_influence"],
                            }
                        }
                    },
                    "required": ["compiled_context_candidates"],
                },
                "then": {
                    "properties": {
                        "permissions": {
                            "properties": {
                                "allow_high_authority": {"const": True},
                            },
                            "required": ["allow_high_authority"],
                        }
                    }
                },
            },
            _profile_rule(
                "compatibility",
                permissions=no_ha_permissions,
                write_back=write_back_denied,
            ),
            _profile_rule(
                "ios-mobile-readonly",
                permissions=no_ha_permissions,
                write_back=write_back_denied,
            ),
            _profile_rule(
                "bead-orchestrator",
                permissions={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["read", "write_back", "allow_high_authority"],
                    "properties": {
                        "read": {"const": True},
                        "write_back": {"const": True},
                        "allow_high_authority": {"const": False},
                    },
                },
                write_back={
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "allowed",
                        "requires_memory_write_proposal",
                        "allowed_expected_uses",
                    ],
                    "properties": {
                        "allowed": {"const": True},
                        "requires_memory_write_proposal": {"const": True},
                        "allowed_expected_uses": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "string",
                                "enum": sorted(EXPECTED_USES),
                            },
                            "contains": {"const": "evidence"},
                        },
                    },
                },
            ),
            _profile_rule(
                "claude-wake-up",
                permissions={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["read", "write_back", "allow_high_authority"],
                    "properties": {
                        "read": {"const": True},
                        "write_back": {"const": False},
                        "allow_high_authority": {"const": True},
                    },
                },
                write_back=write_back_denied,
                require_promotion_identity=True,
            ),
        ],
        "$defs": {
            "influence": {
                "type": "string",
                "enum": sorted(INFLUENCES),
            }
        },
    }


def retrieval_contract_schema_is_valid(raw: Mapping[str, Any] | Any) -> bool:
    """Return True when ``raw`` satisfies the published schema + uniqueness.

    JSON Schema 2020-12 cannot unique-by-key, so unique
    ``authoritative_sources[].unit_kind`` and unique
    ``compiled_context_candidates[].section`` are checked here and must agree
    with :func:`parse_retrieval_contract`.
    """
    try:
        from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - test/runtime dependency
        return False
    if not isinstance(raw, Mapping):
        return False
    if not Draft202012Validator(retrieval_contract_json_schema()).is_valid(raw):
        return False
    sources = raw.get("authoritative_sources")
    if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)):
        kinds: list[str] = []
        for item in sources:
            if isinstance(item, Mapping) and isinstance(item.get("unit_kind"), str):
                kinds.append(item["unit_kind"])
        if len(kinds) != len(set(kinds)):
            return False
    candidates = raw.get("compiled_context_candidates")
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
        sections: list[str] = []
        for item in candidates:
            if isinstance(item, Mapping) and isinstance(item.get("section"), str):
                sections.append(item["section"])
        if len(sections) != len(set(sections)):
            return False
    # Profile security parity (exact) — schema if/then approximates; enforce exact.
    profile = raw.get("profile")
    if isinstance(profile, str) and profile in PROFILE_NAMES:
        try:
            parsed = parse_retrieval_contract(raw)
        except RetrievalContractValidationError:
            return False
        expected = profile_retrieval_contract(
            profile, work_object=parsed.work_object.to_dict()
        )
        if contract_security_semantics(parsed) != contract_security_semantics(
            expected
        ):
            return False
    return True


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RetrievalContractValidationError(
            f"{field} must be an object", field=field
        )
    return value


def _reject_unknown_keys(
    data: Mapping[str, Any],
    allowed: set[str] | frozenset[str],
    field: str,
) -> None:
    unknown = set(data) - set(allowed)
    if unknown:
        raise RetrievalContractValidationError(
            f"{field}: unexpected fields {sorted(unknown)}",
            field=field,
        )


def _reject_reserved_authoritative_source(source: str, field: str) -> None:
    for prefix in RESERVED_AUTHORITATIVE_SOURCE_PREFIXES:
        if source.startswith(prefix):
            raise RetrievalContractValidationError(
                f"{field}: source must not use reserved trust namespace "
                f"prefix {prefix!r}",
                field=field,
            )


def _require_non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RetrievalContractValidationError(
            f"{field} must be a non-empty string", field=field
        )
    if value != value.strip() or "\n" in value or "\r" in value:
        raise RetrievalContractValidationError(
            f"{field} must not contain leading/trailing whitespace or newlines",
            field=field,
        )
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise RetrievalContractValidationError(
            f"{field} must be a boolean", field=field
        )
    return value


def _parse_unique_string_list(
    raw: Any,
    field: str,
    *,
    allowed: set[str] | frozenset[str],
) -> list[str]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise RetrievalContractValidationError(
            f"{field} must be an array of strings",
            field=field,
        )
    items: list[str] = []
    for idx, item in enumerate(raw):
        items.append(_require_non_empty_string(item, f"{field}[{idx}]"))
    if len(items) != len(set(items)):
        raise RetrievalContractValidationError(
            f"{field} must not contain duplicate entries",
            field=field,
        )
    invalid = [item for item in items if item not in allowed]
    if invalid:
        raise RetrievalContractValidationError(
            f"{field} has invalid values {invalid}",
            field=field,
        )
    return items


def contract_security_semantics(contract: RetrievalContract) -> dict[str, Any]:
    """Return the security-relevant subset used for profile parity checks."""
    return {
        "permissions": contract.permissions.to_dict(),
        "write_back": contract.write_back.to_dict(),
        "provenance_requirements": contract.provenance_requirements.to_dict(),
        "high_authority_sections": sorted(
            candidate.section
            for candidate in contract.compiled_context_candidates
            if candidate.max_influence in HIGH_AUTHORITY_INFLUENCES
        ),
        "require_promotion_sections": sorted(
            candidate.section
            for candidate in contract.compiled_context_candidates
            if candidate.require_promotion
        ),
    }


def parse_work_object(raw: Mapping[str, Any] | None) -> WorkObject:
    """Parse and validate a work object declaration."""
    data = _require_mapping(raw, "work_object")
    kind = _require_non_empty_string(data.get("kind"), "work_object.kind")
    if kind not in WORK_OBJECT_KINDS:
        raise RetrievalContractValidationError(
            f"work_object.kind: unsupported kind {kind!r}",
            field="work_object.kind",
        )
    object_id = _require_non_empty_string(data.get("id"), "work_object.id")
    label_raw = data.get("label")
    label = (
        _require_non_empty_string(label_raw, "work_object.label")
        if label_raw is not None
        else None
    )
    unknown = set(data) - {"kind", "id", "label"}
    if unknown:
        raise RetrievalContractValidationError(
            f"work_object: unexpected fields {sorted(unknown)}",
            field="work_object",
        )
    return WorkObject(kind=kind, id=object_id, label=label)


def parse_retrieval_contract(raw: Mapping[str, Any] | None) -> RetrievalContract:
    """Validate and parse a retrieval-contract.v1 object."""
    data = _require_mapping(raw, "retrieval_contract")
    version = _require_non_empty_string(data.get("version"), "version")
    if version != RETRIEVAL_CONTRACT_SCHEMA_VERSION:
        raise RetrievalContractValidationError(
            f"version: unsupported contract version {version!r}",
            field="version",
        )

    work_object = parse_work_object(data.get("work_object"))

    units_raw = data.get("retrieval_units")
    if not isinstance(units_raw, Sequence) or isinstance(units_raw, (str, bytes)):
        raise RetrievalContractValidationError(
            "retrieval_units must be a non-empty array of strings",
            field="retrieval_units",
        )
    retrieval_units = tuple(
        _require_non_empty_string(item, f"retrieval_units[{idx}]")
        for idx, item in enumerate(units_raw)
    )
    if not retrieval_units:
        raise RetrievalContractValidationError(
            "retrieval_units must be a non-empty array of strings",
            field="retrieval_units",
        )
    if len(retrieval_units) != len(set(retrieval_units)):
        raise RetrievalContractValidationError(
            "retrieval_units must not contain duplicate entries",
            field="retrieval_units",
        )

    sources_raw = data.get("authoritative_sources")
    if not isinstance(sources_raw, Sequence) or isinstance(sources_raw, (str, bytes)):
        raise RetrievalContractValidationError(
            "authoritative_sources must be a non-empty array",
            field="authoritative_sources",
        )
    authoritative_sources: list[AuthoritativeSourceDecl] = []
    seen_unit_kinds: set[str] = set()
    for idx, item in enumerate(sources_raw):
        mapping = _require_mapping(item, f"authoritative_sources[{idx}]")
        _reject_unknown_keys(
            mapping,
            {"unit_kind", "source"},
            f"authoritative_sources[{idx}]",
        )
        unit_kind = _require_non_empty_string(
            mapping.get("unit_kind"),
            f"authoritative_sources[{idx}].unit_kind",
        )
        if unit_kind in seen_unit_kinds:
            raise RetrievalContractValidationError(
                f"authoritative_sources: duplicate unit_kind {unit_kind!r}",
                field="authoritative_sources",
            )
        seen_unit_kinds.add(unit_kind)
        source = _require_non_empty_string(
            mapping.get("source"),
            f"authoritative_sources[{idx}].source",
        )
        _reject_reserved_authoritative_source(
            source, f"authoritative_sources[{idx}].source"
        )
        authoritative_sources.append(
            AuthoritativeSourceDecl(unit_kind=unit_kind, source=source)
        )
    if not authoritative_sources:
        raise RetrievalContractValidationError(
            "authoritative_sources must be a non-empty array",
            field="authoritative_sources",
        )

    perms_raw = _require_mapping(data.get("permissions"), "permissions")
    _reject_unknown_keys(
        perms_raw,
        {"read", "write_back", "allow_high_authority"},
        "permissions",
    )
    permissions = Permissions(
        read=_require_bool(perms_raw.get("read"), "permissions.read"),
        write_back=_require_bool(
            perms_raw.get("write_back"), "permissions.write_back"
        ),
        allow_high_authority=_require_bool(
            perms_raw.get("allow_high_authority"),
            "permissions.allow_high_authority",
        ),
    )
    if not permissions.read:
        raise RetrievalContractValidationError(
            "permissions.read must be true for retrieval",
            field="permissions.read",
        )

    prov_raw = _require_mapping(
        data.get("provenance_requirements"), "provenance_requirements"
    )
    _reject_unknown_keys(
        prov_raw,
        {
            "min_labels_for_high_authority",
            "require_instruction_expected_use",
            "require_promotion_audit_reason",
            "exclude_labels",
        },
        "provenance_requirements",
    )
    min_labels_list = _parse_unique_string_list(
        prov_raw.get("min_labels_for_high_authority"),
        "provenance_requirements.min_labels_for_high_authority",
        allowed=EPISTEMIC_LABELS,
    )
    min_labels = frozenset(min_labels_list)
    if not min_labels or not min_labels <= PROVENANCE_FLOOR_MIN_LABELS:
        raise RetrievalContractValidationError(
            "provenance_requirements.min_labels_for_high_authority must be a "
            "non-empty subset of the provenance floor "
            f"{sorted(PROVENANCE_FLOOR_MIN_LABELS)}",
            field="provenance_requirements.min_labels_for_high_authority",
        )

    require_instruction = _require_bool(
        prov_raw.get("require_instruction_expected_use"),
        "provenance_requirements.require_instruction_expected_use",
    )
    if require_instruction is not True:
        raise RetrievalContractValidationError(
            "provenance_requirements.require_instruction_expected_use must be true",
            field="provenance_requirements.require_instruction_expected_use",
        )
    require_promotion_audit = _require_bool(
        prov_raw.get("require_promotion_audit_reason"),
        "provenance_requirements.require_promotion_audit_reason",
    )
    if require_promotion_audit is not True:
        raise RetrievalContractValidationError(
            "provenance_requirements.require_promotion_audit_reason must be true",
            field="provenance_requirements.require_promotion_audit_reason",
        )

    if "exclude_labels" not in prov_raw:
        exclude_labels = PROVENANCE_FLOOR_EXCLUDE_LABELS
    else:
        exclude_list = _parse_unique_string_list(
            prov_raw.get("exclude_labels"),
            "provenance_requirements.exclude_labels",
            allowed=EPISTEMIC_LABELS,
        )
        exclude_labels = frozenset(exclude_list)
        missing_floor = PROVENANCE_FLOOR_EXCLUDE_LABELS - exclude_labels
        if missing_floor:
            raise RetrievalContractValidationError(
                "provenance_requirements.exclude_labels must be a superset of "
                f"the provenance floor excludes {sorted(PROVENANCE_FLOOR_EXCLUDE_LABELS)}; "
                f"missing {sorted(missing_floor)}",
                field="provenance_requirements.exclude_labels",
            )

    provenance_requirements = ProvenanceRequirements(
        min_labels_for_high_authority=min_labels,
        require_instruction_expected_use=True,
        require_promotion_audit_reason=True,
        exclude_labels=exclude_labels,
    )

    candidates_raw = data.get("compiled_context_candidates")
    if not isinstance(candidates_raw, Sequence) or isinstance(
        candidates_raw, (str, bytes)
    ):
        raise RetrievalContractValidationError(
            "compiled_context_candidates must be a non-empty array",
            field="compiled_context_candidates",
        )
    candidates: list[CompiledContextCandidate] = []
    seen_sections: set[str] = set()
    for idx, item in enumerate(candidates_raw):
        mapping = _require_mapping(item, f"compiled_context_candidates[{idx}]")
        _reject_unknown_keys(
            mapping,
            {"section", "max_influence", "require_promotion"},
            f"compiled_context_candidates[{idx}]",
        )
        max_influence = _require_non_empty_string(
            mapping.get("max_influence"),
            f"compiled_context_candidates[{idx}].max_influence",
        )
        if max_influence not in INFLUENCES:
            raise RetrievalContractValidationError(
                f"compiled_context_candidates[{idx}].max_influence: "
                f"invalid influence {max_influence!r}",
                field=f"compiled_context_candidates[{idx}].max_influence",
            )
        section = _require_non_empty_string(
            mapping.get("section"),
            f"compiled_context_candidates[{idx}].section",
        )
        if section in seen_sections:
            raise RetrievalContractValidationError(
                f"compiled_context_candidates: duplicate section {section!r}",
                field="compiled_context_candidates",
            )
        seen_sections.add(section)
        candidates.append(
            CompiledContextCandidate(
                section=section,
                max_influence=max_influence,  # type: ignore[arg-type]
                require_promotion=_require_bool(
                    mapping.get("require_promotion"),
                    f"compiled_context_candidates[{idx}].require_promotion",
                ),
            )
        )
    if not candidates:
        raise RetrievalContractValidationError(
            "compiled_context_candidates must be a non-empty array",
            field="compiled_context_candidates",
        )

    write_raw = _require_mapping(data.get("write_back"), "write_back")
    _reject_unknown_keys(
        write_raw,
        {"allowed", "requires_memory_write_proposal", "allowed_expected_uses"},
        "write_back",
    )
    uses_raw = write_raw.get("allowed_expected_uses")
    if not isinstance(uses_raw, Sequence) or isinstance(uses_raw, (str, bytes)):
        raise RetrievalContractValidationError(
            "write_back.allowed_expected_uses must be a non-empty array",
            field="write_back.allowed_expected_uses",
        )
    allowed_uses_list = [
        _require_non_empty_string(item, f"write_back.allowed_expected_uses[{idx}]")
        for idx, item in enumerate(uses_raw)
    ]
    if len(allowed_uses_list) != len(set(allowed_uses_list)):
        raise RetrievalContractValidationError(
            "write_back.allowed_expected_uses must not contain duplicates",
            field="write_back.allowed_expected_uses",
        )
    allowed_uses = frozenset(allowed_uses_list)
    if not allowed_uses or not allowed_uses <= EXPECTED_USES:
        raise RetrievalContractValidationError(
            "write_back.allowed_expected_uses has invalid values",
            field="write_back.allowed_expected_uses",
        )
    write_back = WriteBackContract(
        allowed=_require_bool(write_raw.get("allowed"), "write_back.allowed"),
        requires_memory_write_proposal=_require_bool(
            write_raw.get("requires_memory_write_proposal"),
            "write_back.requires_memory_write_proposal",
        ),
        allowed_expected_uses=allowed_uses,
    )

    profile_raw = data.get("profile")
    profile = (
        _require_non_empty_string(profile_raw, "profile")
        if profile_raw is not None
        else None
    )
    if profile is not None and profile not in PROFILE_NAMES:
        raise RetrievalContractValidationError(
            f"profile: unsupported profile {profile!r}", field="profile"
        )

    if permissions.allow_high_authority:
        if not any(
            candidate.max_influence in HIGH_AUTHORITY_INFLUENCES
            for candidate in candidates
        ):
            raise RetrievalContractValidationError(
                "allow_high_authority requires at least one high-authority "
                "compiled_context_candidate",
                field="permissions.allow_high_authority",
            )
    else:
        for candidate in candidates:
            if candidate.max_influence in HIGH_AUTHORITY_INFLUENCES:
                raise RetrievalContractValidationError(
                    "compiled_context_candidates cannot request high authority "
                    "when permissions.allow_high_authority is false",
                    field="compiled_context_candidates",
                )

    allowed_keys = {
        "version",
        "work_object",
        "retrieval_units",
        "authoritative_sources",
        "permissions",
        "provenance_requirements",
        "compiled_context_candidates",
        "write_back",
        "profile",
    }
    unknown = set(data) - allowed_keys
    if unknown:
        raise RetrievalContractValidationError(
            f"unexpected fields {sorted(unknown)}", field="retrieval_contract"
        )

    contract = RetrievalContract(
        version=RETRIEVAL_CONTRACT_SCHEMA_VERSION,
        work_object=work_object,
        retrieval_units=retrieval_units,
        authoritative_sources=tuple(authoritative_sources),
        permissions=permissions,
        provenance_requirements=provenance_requirements,
        compiled_context_candidates=tuple(candidates),
        write_back=write_back,
        profile=profile,
    )

    if profile is not None:
        expected = profile_retrieval_contract(profile, work_object=work_object)
        if contract_security_semantics(contract) != contract_security_semantics(
            expected
        ):
            raise RetrievalContractValidationError(
                "profile_security_mismatch: full contract security semantics "
                f"do not match profile {profile!r}",
                field="profile",
            )

    return contract


def compatibility_retrieval_contract(
    *,
    work_object: Mapping[str, Any] | WorkObject | None = None,
) -> RetrievalContract:
    """Constrained path for consumers that omit an explicit contract.

    Searchable evidence is still returned, but cannot become identity,
    constraint, policy, or system-instruction authority.
    """
    resolved = _coerce_work_object(
        work_object, default_kind="legacy_caller", default_id="unspecified"
    )
    return RetrievalContract(
        version=RETRIEVAL_CONTRACT_SCHEMA_VERSION,
        work_object=resolved,
        retrieval_units=("memory",),
        authoritative_sources=(
            AuthoritativeSourceDecl(
                unit_kind="memory",
                source="open-brain.memories",
            ),
        ),
        permissions=Permissions(
            read=True,
            write_back=False,
            allow_high_authority=False,
        ),
        provenance_requirements=ProvenanceRequirements(),
        compiled_context_candidates=(
            CompiledContextCandidate(
                section="errors",
                max_influence="context",
                require_promotion=False,
            ),
            CompiledContextCandidate(
                section="project",
                max_influence="context",
                require_promotion=False,
            ),
            CompiledContextCandidate(
                section="context",
                max_influence="context",
                require_promotion=False,
            ),
            CompiledContextCandidate(
                section="evidence",
                max_influence="evidence",
                require_promotion=False,
            ),
        ),
        write_back=WriteBackContract(allowed=False),
        profile="compatibility",
    )


def profile_retrieval_contract(
    profile: ProfileName | str,
    *,
    work_object: Mapping[str, Any] | WorkObject | None = None,
) -> RetrievalContract:
    """Return a concrete consumer profile contract."""
    if profile == "compatibility":
        return compatibility_retrieval_contract(work_object=work_object)
    if profile == "ios-mobile-readonly":
        resolved = _coerce_work_object(
            work_object, default_kind="mobile_client", default_id="ios-app"
        )
        return RetrievalContract(
            version=RETRIEVAL_CONTRACT_SCHEMA_VERSION,
            work_object=resolved,
            retrieval_units=("memory",),
            authoritative_sources=(
                AuthoritativeSourceDecl(
                    unit_kind="memory",
                    source="open-brain.memories",
                ),
            ),
            permissions=Permissions(
                read=True,
                write_back=False,
                allow_high_authority=False,
            ),
            provenance_requirements=ProvenanceRequirements(),
            compiled_context_candidates=(
                CompiledContextCandidate(
                    section="evidence",
                    max_influence="evidence",
                    require_promotion=False,
                ),
            ),
            write_back=WriteBackContract(allowed=False),
            profile="ios-mobile-readonly",
        )
    if profile == "bead-orchestrator":
        resolved = _coerce_work_object(
            work_object, default_kind="bead", default_id="unspecified-bead"
        )
        return RetrievalContract(
            version=RETRIEVAL_CONTRACT_SCHEMA_VERSION,
            work_object=resolved,
            retrieval_units=("memory", "session_summary"),
            authoritative_sources=(
                AuthoritativeSourceDecl(
                    unit_kind="memory",
                    source="open-brain.memories",
                ),
                AuthoritativeSourceDecl(
                    unit_kind="session_summary",
                    source="open-brain.session_summaries",
                ),
            ),
            permissions=Permissions(
                read=True,
                write_back=True,
                allow_high_authority=False,
            ),
            provenance_requirements=ProvenanceRequirements(),
            compiled_context_candidates=(
                CompiledContextCandidate(
                    section="evidence",
                    max_influence="evidence",
                    require_promotion=False,
                ),
                CompiledContextCandidate(
                    section="context",
                    max_influence="context",
                    require_promotion=False,
                ),
            ),
            write_back=WriteBackContract(
                allowed=True,
                requires_memory_write_proposal=True,
                allowed_expected_uses=frozenset({"evidence"}),
            ),
            profile="bead-orchestrator",
        )
    if profile == "claude-wake-up":
        # Identity/constraint may elevate only through a ledger projection.
        # Policy/system-instruction sections are intentionally undeclared.
        resolved = _coerce_work_object(
            work_object, default_kind="project", default_id="unspecified-project"
        )
        return RetrievalContract(
            version=RETRIEVAL_CONTRACT_SCHEMA_VERSION,
            work_object=resolved,
            retrieval_units=("memory",),
            authoritative_sources=(
                AuthoritativeSourceDecl(
                    unit_kind="memory",
                    source="open-brain.memories",
                ),
                AuthoritativeSourceDecl(
                    unit_kind="promoted_identity",
                    source="open-brain.promoted-identity",
                ),
                AuthoritativeSourceDecl(
                    unit_kind="promoted_constraint",
                    source="open-brain.promoted-constraints",
                ),
            ),
            permissions=Permissions(
                read=True,
                write_back=False,
                allow_high_authority=True,
            ),
            provenance_requirements=ProvenanceRequirements(),
            compiled_context_candidates=(
                CompiledContextCandidate(
                    section="identity",
                    max_influence="identity",
                    require_promotion=True,
                ),
                CompiledContextCandidate(
                    section="constraints",
                    max_influence="constraint",
                    require_promotion=True,
                ),
                CompiledContextCandidate(
                    section="errors",
                    max_influence="context",
                    require_promotion=False,
                ),
                CompiledContextCandidate(
                    section="project",
                    max_influence="context",
                    require_promotion=False,
                ),
                CompiledContextCandidate(
                    section="context",
                    max_influence="context",
                    require_promotion=False,
                ),
                CompiledContextCandidate(
                    section="evidence",
                    max_influence="evidence",
                    require_promotion=False,
                ),
            ),
            write_back=WriteBackContract(allowed=False),
            profile="claude-wake-up",
        )
    raise RetrievalContractValidationError(
        f"profile: unsupported profile {profile!r}", field="profile"
    )


def _coerce_work_object(
    value: Mapping[str, Any] | WorkObject | None,
    *,
    default_kind: str,
    default_id: str,
) -> WorkObject:
    if isinstance(value, WorkObject):
        return value
    if value is None:
        return WorkObject(kind=default_kind, id=default_id)
    return parse_work_object(value)


def resolve_retrieval_contract(
    raw: Mapping[str, Any] | RetrievalContract | None,
    *,
    work_object: Mapping[str, Any] | WorkObject | None = None,
) -> RetrievalContract:
    """Resolve an optional contract, defaulting to the compatibility path."""
    if raw is None:
        return compatibility_retrieval_contract(work_object=work_object)
    if isinstance(raw, RetrievalContract):
        return raw
    if "profile" in raw and set(raw.keys()) <= {"profile", "work_object"}:
        profile = raw["profile"]
        wo = raw.get("work_object", work_object)
        return profile_retrieval_contract(str(profile), work_object=wo)
    return parse_retrieval_contract(raw)


def _metadata_mapping(memory: Any) -> dict[str, Any]:
    metadata = getattr(memory, "metadata", None)
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def _provenance_mapping(metadata: Mapping[str, Any]) -> dict[str, Any]:
    provenance = metadata.get("provenance")
    return dict(provenance) if isinstance(provenance, Mapping) else {}


def _origin_fields(provenance: Mapping[str, Any]) -> tuple[str | None, str | None]:
    origin = provenance.get("origin")
    if not isinstance(origin, Mapping):
        return None, None
    producer = origin.get("producer")
    source_ref = origin.get("source_ref")
    return (
        producer if isinstance(producer, str) and producer.strip() else None,
        source_ref if isinstance(source_ref, str) and source_ref.strip() else None,
    )


def is_external_untrusted(metadata: Mapping[str, Any]) -> bool:
    """Return True when ingestion marks the memory as external/untrusted."""
    route = metadata.get("ingestion_route")
    if isinstance(route, str) and route.strip().lower() in EXTERNAL_INGESTION_ROUTES:
        return True
    provenance = _provenance_mapping(metadata)
    producer, _source_ref = _origin_fields(provenance)
    if producer is not None and producer.strip().lower() in EXTERNAL_PRODUCERS:
        return True
    if metadata.get("external") is True:
        return True
    return False


def is_external_unattested_for_high_authority(metadata: Mapping[str, Any]) -> bool:
    """Return True when route/producer attestation is absent or unknown.

    This does not invent a trusted-route allowlist that enables high authority.
    Absence or an explicitly unknown route/producer blocks HA elevation.
    """
    route = metadata.get("ingestion_route")
    if not isinstance(route, str) or not route.strip():
        return True
    if route.strip().lower() == "unknown":
        return True
    provenance = _provenance_mapping(metadata)
    producer, _source_ref = _origin_fields(provenance)
    if producer is None:
        return True
    if producer.strip().lower() == "unknown":
        return True
    return False


def _promotion_origin_attestation_matches(
    memory_id: int,
    metadata: Mapping[str, Any],
    projection: PromotionProjection,
) -> bool:
    """Require exact recomputation of the ledger-bound origin attestation digest."""
    stored = getattr(projection, "origin_attestation_digest", None)
    if not isinstance(stored, str) or not stored.strip():
        return False
    try:
        from open_brain.memory_promotion import origin_attestation_digest_from_metadata

        recomputed = origin_attestation_digest_from_metadata(memory_id, metadata)
    except Exception:
        return False
    return recomputed == stored.strip().lower()


def inspect_promotion(
    metadata: Mapping[str, Any],
    *,
    projection: PromotionProjection | None = None,
) -> tuple[PromotionState, str]:
    """Derive confirmation/promotion state and an audit reason.

    The ledger projection is the sole read-time promotion authority.
    Actor-authored promotion metadata and write-time Judge ALLOW are never
    treated as server-issued read-time promotion.
    """
    provenance = _provenance_mapping(metadata)
    source_label = provenance.get("source_label")
    if source_label is not None and source_label not in EPISTEMIC_LABELS:
        return "legacy_unlabeled", "invalid_epistemic_label"
    if source_label is None and provenance.get("expected_use") is None:
        return "legacy_unlabeled", "missing_epistemic_provenance_defaults_to_evidence"
    if source_label is None:
        return "legacy_unlabeled", "missing_epistemic_source_label"

    if (
        projection is not None
        and projection.is_current
        and projection.decision == "accepted"
    ):
        if projection.target_state == "disputed" or source_label == "disputed":
            return "disputed", projection.audit_reason or "ledger_disputed"
        if projection.target_state == "superseded" or source_label == "superseded":
            return "superseded", projection.audit_reason or "ledger_superseded"
        if projection.outcome == "promoted" and projection.target_state == "confirmed":
            if source_label != "confirmed":
                return "unpromoted", "ledger_metadata_state_mismatch"
            return "promoted", projection.audit_reason or "ledger_promotion_grant"

    if source_label == "disputed":
        return "disputed", "epistemic_label_disputed"
    if source_label == "superseded":
        return "superseded", "epistemic_label_superseded"
    if isinstance(metadata.get("retrieval_promotion"), Mapping):
        return "unpromoted", "promotion_record_not_server_issued"
    judge = metadata.get("memory_write_judge")
    if isinstance(judge, Mapping) and judge.get("decision") == "ALLOW":
        return "unpromoted", "judge_allow_is_not_read_time_promotion"
    return "unpromoted", "no_server_issued_promotion_record"


def classify_requested_influence(memory: Any) -> Influence:
    """Map a memory's type/category cues to a requested influence."""
    memory_type = getattr(memory, "type", None) or ""
    metadata = _metadata_mapping(memory)
    meta_cat = metadata.get("category") or ""
    stability = getattr(memory, "stability", None) or ""

    # Observed/inferred session-knowledge roles stay evidence-only by default.
    # Actor-authored category cues (including "policy") cannot raise them.
    session_knowledge = metadata.get("session_knowledge")
    session_role = None
    if isinstance(session_knowledge, Mapping):
        role_value = session_knowledge.get("role")
        if isinstance(role_value, str):
            session_role = role_value
    if session_role is None:
        role_value = metadata.get("session_knowledge_role")
        if isinstance(role_value, str):
            session_role = role_value
    # Session-knowledge roles and session_event stay evidence-only.
    # Legacy session_summary is NOT forced to evidence here (O1-09): it keeps
    # the normal type/project classification path (often "context").
    if session_role in {
        "session_event",
        "session_decision",
        "session_learning",
    } or memory_type == "session_event":
        return "evidence"

    if memory_type == "identity" or meta_cat == "identity":
        return "identity"
    if memory_type == "constraint" or meta_cat == "constraint":
        return "constraint"
    if memory_type == "policy" or meta_cat == "policy":
        return "policy"
    if stability == "canonical" and memory_type in ("rule", "policy"):
        return "constraint"
    if memory_type == "system_instruction" or meta_cat == "system_instruction":
        return "system_instruction"
    if getattr(memory, "project_name", None) or meta_cat == "project":
        return "context"
    if memory_type == "error_resolved" or meta_cat == "error":
        return "context"
    return "evidence"


def _cap_influence(requested: str, maximum: str) -> Influence:
    if requested not in INFLUENCES:
        requested = "evidence"
    if maximum not in INFLUENCES:
        maximum = "evidence"
    if requested == maximum:
        return requested  # type: ignore[return-value]
    if _INFLUENCE_RANK[requested] < _INFLUENCE_RANK[maximum]:
        return requested  # type: ignore[return-value]
    # Equal-rank different HA label, or requested stronger than maximum.
    return "evidence"


def _section_for_influence(influence: Influence, requested: Influence) -> str:
    if influence == "identity":
        return "identity"
    if influence in {"constraint", "policy"}:
        return "constraints"
    if influence == "system_instruction":
        return "system_instruction"
    if requested == "context":
        return "context"
    return "evidence"


def _candidate_for_section(
    contract: RetrievalContract, section: str
) -> CompiledContextCandidate | None:
    for candidate in contract.compiled_context_candidates:
        if candidate.section == section:
            return candidate
    return None


def _max_influence_for_section(contract: RetrievalContract, section: str) -> Influence:
    candidate = _candidate_for_section(contract, section)
    if candidate is None:
        return "evidence"
    return candidate.max_influence


def _truncate_str(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars]


def _metadata_excerpt(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Build a bounded, shallow metadata excerpt for audit/display."""
    excerpt: dict[str, Any] = {}

    for key in ("category", "ingestion_route", "content_type"):
        value = metadata.get(key)
        if isinstance(value, str):
            excerpt[key] = _truncate_str(value, _METADATA_SCALAR_MAX_CHARS)

    risk_flags = metadata.get("risk_flags")
    if isinstance(risk_flags, list):
        cleaned: list[str] = []
        for item in risk_flags[:_METADATA_RISK_FLAG_MAX_ITEMS]:
            if isinstance(item, str):
                cleaned.append(_truncate_str(item, _METADATA_RISK_FLAG_MAX_CHARS))
        if cleaned:
            excerpt["risk_flags"] = cleaned

    promotion = metadata.get("retrieval_promotion")
    if isinstance(promotion, Mapping):
        summary: dict[str, str] = {}
        state = promotion.get("state")
        if isinstance(state, str):
            summary["state"] = _truncate_str(
                state, _METADATA_PROMOTION_FIELD_MAX_CHARS
            )
        audit_reason = promotion.get("audit_reason")
        if isinstance(audit_reason, str):
            summary["audit_reason"] = _truncate_str(
                audit_reason, _METADATA_PROMOTION_FIELD_MAX_CHARS
            )
        if summary:
            # Untrusted actor-authored summary only; never a promotion grant.
            excerpt["retrieval_promotion"] = summary

    # Drop keys newest-first until the JSON encoding fits the bound.
    keys = list(excerpt.keys())
    while keys:
        encoded = json.dumps(excerpt, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= _METADATA_EXCERPT_MAX_BYTES:
            break
        drop = keys.pop()
        excerpt.pop(drop, None)
    return excerpt


def _epistemic_fields(
    provenance: Mapping[str, Any],
) -> tuple[str, str, EpistemicStatus]:
    source_label_raw = provenance.get("source_label")
    expected_use_raw = provenance.get("expected_use")

    if source_label_raw is None:
        epistemic_status: EpistemicStatus = "missing"
        source_label = ""
    elif source_label_raw in EPISTEMIC_LABELS:
        epistemic_status = "declared"
        source_label = str(source_label_raw)
    else:
        epistemic_status = "invalid"
        source_label = (
            str(source_label_raw)
            if isinstance(source_label_raw, str)
            else repr(source_label_raw)
        )

    if epistemic_status == "declared" and expected_use_raw in EXPECTED_USES:
        expected_use = str(expected_use_raw)
    else:
        expected_use = "evidence"

    return source_label, expected_use, epistemic_status


def _high_authority_denial_reason(
    metadata: Mapping[str, Any],
    *,
    allow_high_authority: bool,
    promotion_reason: str,
) -> str:
    if promotion_reason == "ledger_metadata_state_mismatch":
        return promotion_reason
    if isinstance(metadata.get("retrieval_promotion"), Mapping):
        return "promotion_record_not_server_issued"
    judge = metadata.get("memory_write_judge")
    if isinstance(judge, Mapping) and judge.get("decision") == "ALLOW":
        return "judge_allow_is_not_read_time_promotion"
    if is_external_unattested_for_high_authority(metadata):
        return "external_trust_unattested_pending_trusted_issuer"
    if not allow_high_authority:
        return HIGH_AUTHORITY_DISABLED_REASON
    if promotion_reason and promotion_reason != "no_server_issued_promotion_record":
        return promotion_reason
    return "no_server_issued_promotion_record"


def _unit_kind_for_influence(influence: Influence) -> str:
    if influence == "identity":
        return "promoted_identity"
    if influence in {"constraint", "policy"}:
        return "promoted_constraint"
    return "memory"


def memory_to_retrieval_unit(
    memory: Any,
    contract: RetrievalContract,
    *,
    requested_influence: Influence | str | None = None,
    promotion_projection: Mapping[int, PromotionProjection] | None = None,
) -> RetrievalUnit:
    """Convert a Memory into a provenance-preserving retrieval unit."""
    metadata = _metadata_mapping(memory)
    provenance = _provenance_mapping(metadata)
    origin_producer, origin_source_ref = _origin_fields(provenance)

    source_label, expected_use, epistemic_status = _epistemic_fields(provenance)
    memory_id = int(getattr(memory, "id"))
    projection = None
    if promotion_projection is not None:
        projection = promotion_projection.get(memory_id)
    promotion_state, promotion_reason = inspect_promotion(
        metadata, projection=projection
    )

    requested = requested_influence or classify_requested_influence(memory)
    if requested not in INFLUENCES:
        raise RetrievalContractValidationError(
            f"requested_influence: invalid value {requested!r}",
            field="requested_influence",
        )
    requested_typed: Influence = requested  # type: ignore[assignment]

    section_guess = _section_for_influence(requested_typed, requested_typed)
    candidate = _candidate_for_section(contract, section_guess)
    section_max = _max_influence_for_section(contract, section_guess)
    capped = _cap_influence(requested_typed, section_max)

    # Section require_promotion: without a server-issued promotion, HA is denied.
    if (
        candidate is not None
        and candidate.require_promotion
        and promotion_state != "promoted"
        and capped in HIGH_AUTHORITY_INFLUENCES
    ):
        capped = "evidence"

    external = is_external_untrusted(metadata)
    effective: Influence = "evidence"
    audit_reason = promotion_reason
    floor_labels = contract.provenance_requirements.min_labels_for_high_authority
    requires_instruction = (
        contract.provenance_requirements.require_instruction_expected_use
    )

    can_consider_ha = (
        capped in HIGH_AUTHORITY_INFLUENCES
        and contract.permissions.allow_high_authority
        and promotion_state == "promoted"
        and epistemic_status == "declared"
        and source_label in floor_labels
        and (not requires_instruction or expected_use == "instruction")
        and source_label not in contract.provenance_requirements.exclude_labels
        and not is_external_unattested_for_high_authority(metadata)
        and candidate is not None
        and candidate.require_promotion
        and projection is not None
        and projection.is_current
        and projection.decision == "accepted"
        and projection.outcome == "promoted"
        and projection.target_state == "confirmed"
        and source_label == "confirmed"
        and bool(projection.audit_reason.strip())
        and bool(projection.audit_source.strip())
        and _promotion_origin_attestation_matches(
            memory_id, metadata, projection
        )
    )

    if can_consider_ha:
        effective = capped
        audit_reason = projection.audit_reason if projection else promotion_reason
    elif capped in HIGH_AUTHORITY_INFLUENCES:
        effective = "evidence"
        audit_reason = _high_authority_denial_reason(
            metadata,
            allow_high_authority=contract.permissions.allow_high_authority,
            promotion_reason=promotion_reason,
        )
    elif capped in EVIDENCE_INFLUENCES:
        effective = capped
        if external:
            audit_reason = "external_or_url_ingestion_quoted_as_evidence"
        elif (
            promotion_state == "unpromoted"
            and requested_typed in HIGH_AUTHORITY_INFLUENCES
        ):
            audit_reason = _high_authority_denial_reason(
                metadata,
                allow_high_authority=contract.permissions.allow_high_authority,
                promotion_reason=promotion_reason,
            )
        elif (
            epistemic_status == "declared"
            and source_label in contract.provenance_requirements.exclude_labels
        ):
            audit_reason = f"epistemic_label_{source_label}_not_high_authority"
        elif promotion_state in {"disputed", "superseded", "legacy_unlabeled"}:
            audit_reason = promotion_reason
        else:
            audit_reason = promotion_reason
    else:
        effective = "evidence"
        audit_reason = _high_authority_denial_reason(
            metadata,
            allow_high_authority=contract.permissions.allow_high_authority,
            promotion_reason=promotion_reason,
        )

    section = _section_for_influence(effective, requested_typed)
    memory_type = getattr(memory, "type", None)
    meta_cat = metadata.get("category")
    if effective in EVIDENCE_INFLUENCES:
        if memory_type == "error_resolved" or meta_cat == "error":
            section = "errors"
        elif getattr(memory, "project_name", None) or meta_cat == "project":
            section = "project"
        elif requested_typed == "context":
            section = "context"
        else:
            section = "evidence"
        # Keep section within declared candidates when possible.
        if _candidate_for_section(contract, section) is None:
            if _candidate_for_section(contract, "evidence") is not None:
                section = "evidence"
            elif contract.compiled_context_candidates:
                section = contract.compiled_context_candidates[0].section

    # Exact per-section max again after section reassignment.
    section_candidate = _candidate_for_section(contract, section)
    if section_candidate is not None:
        effective = _cap_influence(effective, section_candidate.max_influence)
        if (
            section_candidate.require_promotion
            and promotion_state != "promoted"
            and effective in HIGH_AUTHORITY_INFLUENCES
        ):
            effective = "evidence"
            audit_reason = _high_authority_denial_reason(
                metadata,
                allow_high_authority=contract.permissions.allow_high_authority,
                promotion_reason=promotion_reason,
            )
    else:
        # Undeclared sections (e.g. policy/system_instruction on wake-up) fail closed.
        if effective in HIGH_AUTHORITY_INFLUENCES:
            effective = "evidence"
            audit_reason = "section_not_declared_for_high_authority"

    if effective in HIGH_AUTHORITY_INFLUENCES and not can_consider_ha:
        effective = "evidence"
        audit_reason = _high_authority_denial_reason(
            metadata,
            allow_high_authority=contract.permissions.allow_high_authority,
            promotion_reason=promotion_reason,
        )

    ingestion_route = metadata.get("ingestion_route")
    content_type = metadata.get("content_type")
    category = meta_cat if isinstance(meta_cat, str) else None

    unit_kind = (
        _unit_kind_for_influence(effective)
        if effective in HIGH_AUTHORITY_INFLUENCES
        else "memory"
    )
    try:
        authoritative_source = contract.source_for_unit_kind(unit_kind)
    except RetrievalContractValidationError:
        # Profile declared HA influence but not the promoted unit kind.
        effective = "evidence"
        audit_reason = "authoritative_source_missing_for_promoted_unit"
        unit_kind = "memory"
        authoritative_source = contract.source_for_unit_kind(unit_kind)
        section = "evidence"

    return RetrievalUnit(
        memory_id=memory_id,
        content=str(getattr(memory, "content", "") or ""),
        title=getattr(memory, "title", None),
        subtitle=getattr(memory, "subtitle", None),
        narrative=getattr(memory, "narrative", None),
        memory_type=memory_type if isinstance(memory_type, str) else None,
        category=category,
        origin_producer=origin_producer,
        origin_source_ref=origin_source_ref,
        ingestion_route=(
            ingestion_route if isinstance(ingestion_route, str) else None
        ),
        content_type=content_type if isinstance(content_type, str) else None,
        source_label=source_label,
        expected_use=expected_use,
        epistemic_status=epistemic_status,
        promotion_state=promotion_state,
        requested_influence=requested_typed,
        effective_influence=effective,
        contract_version=RETRIEVAL_CONTRACT_SCHEMA_VERSION,
        audit_reason=audit_reason,
        authoritative_source=authoritative_source,
        section=section,
        metadata_excerpt=_metadata_excerpt(metadata),
    )


def authorize_memory_write_back(
    contract: RetrievalContract | None,
    *,
    proposal: Mapping[str, Any] | None = None,
) -> None:
    """Authorize a memory write against an optional retrieval write-back contract.

    ``contract is None`` is a no-op so current write behavior is preserved when
    callers omit a retrieval contract.
    """
    if contract is None:
        return
    if not contract.write_back.allowed:
        raise RetrievalContractValidationError(
            "write_back is not allowed by retrieval contract",
            field="write_back.allowed",
        )
    if contract.write_back.requires_memory_write_proposal:
        if not isinstance(proposal, Mapping):
            raise RetrievalContractValidationError(
                "write_back requires a memory_write_proposal mapping",
                field="write_back.requires_memory_write_proposal",
            )
        expected_use = proposal.get("expected_use")
        if expected_use not in contract.write_back.allowed_expected_uses:
            raise RetrievalContractValidationError(
                "proposal expected_use is not in write_back.allowed_expected_uses",
                field="write_back.allowed_expected_uses",
            )


def apply_retrieval_contract(
    memories: Sequence[Any],
    *,
    contract: Mapping[str, Any] | RetrievalContract | None = None,
    work_object: Mapping[str, Any] | WorkObject | None = None,
    query_project: str | None = None,
    promotion_projection: Mapping[int, PromotionProjection] | None = None,
) -> RetrievalResult:
    """Apply a retrieval contract (or compatibility path) to memories.

    ``promotion_projection`` must be supplied by server seams that load the
    ledger. Direct calls with no projection remain fail-closed for HA.
    """
    resolved = resolve_retrieval_contract(contract, work_object=work_object)

    if (
        resolved.work_object.kind == "project"
        and query_project is not None
        and str(query_project).strip() != ""
        and query_project != resolved.work_object.id
    ):
        raise RetrievalContractValidationError(
            f"query_project {query_project!r} does not match "
            f"work_object.id {resolved.work_object.id!r}",
            field="work_object.id",
        )

    units_list: list[RetrievalUnit] = []
    if "memory" in resolved.retrieval_units:
        for memory in memories:
            units_list.append(
                memory_to_retrieval_unit(
                    memory,
                    resolved,
                    promotion_projection=promotion_projection,
                )
            )

    units = tuple(units_list)
    for unit in units:
        if (
            unit.effective_influence in HIGH_AUTHORITY_INFLUENCES
            and not unit.audit_reason.strip()
        ):
            raise RetrievalContractValidationError(
                "high-authority units require a non-empty audit_reason",
                field="audit_reason",
            )
        if (
            unit.effective_influence in HIGH_AUTHORITY_INFLUENCES
            and unit.promotion_state != "promoted"
        ):
            raise RetrievalContractValidationError(
                "high-authority units require a current ledger promotion",
                field="promotion_state",
            )
    return RetrievalResult(contract=resolved, units=units)


def serialize_evidence_envelope(
    result: RetrievalResult,
    *,
    label: str = "RETRIEVED_DATA_NOT_USER_OR_SYSTEM_POLICY",
) -> str:
    """Deterministic typed envelope for SessionStart injection.

    The invariant contract is emitted by reference (``contract_ref`` +
    ``profile`` + ``contract_version``), not as a full inline contract object,
    so fixed header overhead does not consume the per-session unit payload
    budget.
    """
    profile = result.contract.profile
    if profile:
        contract_ref = f"{RETRIEVAL_CONTRACT_SCHEMA_ID}#profile={profile}"
    else:
        contract_ref = RETRIEVAL_CONTRACT_SCHEMA_ID
    payload = {
        "envelope_type": "open-brain.retrieved-evidence.v1",
        "label": label,
        "contract_version": result.contract_version,
        "contract_ref": contract_ref,
        "profile": profile,
        "work_object": result.contract.work_object.to_dict(),
        "units": [unit.to_dict() for unit in result.units],
        "high_authority_units": [
            unit.to_dict()
            for unit in result.units
            if unit.effective_influence in HIGH_AUTHORITY_INFLUENCES
        ],
    }
    body = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    # Neutralize accidental delimiter collisions inside serialized JSON text.
    body = body.replace(
        "<<<OPEN_BRAIN_RETRIEVED_EVIDENCE_V1>>>",
        "<<\\u003cOPEN_BRAIN_RETRIEVED_EVIDENCE_V1>>>",
    ).replace(
        "<<<END_OPEN_BRAIN_RETRIEVED_EVIDENCE_V1>>>",
        "<<\\u003cEND_OPEN_BRAIN_RETRIEVED_EVIDENCE_V1>>>",
    )
    return (
        "<<<OPEN_BRAIN_RETRIEVED_EVIDENCE_V1>>>\n"
        f"{body}\n"
        "<<<END_OPEN_BRAIN_RETRIEVED_EVIDENCE_V1>>>"
    )
