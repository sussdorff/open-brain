"""Versioned memory promotion contract and append-only authority ledger.

Contract URI: ``standard://open-brain/promotion/memory-promotion.v1``

Epistemic authority may move only through explicitly enumerated transitions.
Every allowed transition requires an authenticated allowlisted OAuth admin
actor and a cryptographically signed, short-lived (max 10 minute),
audience-bound promotion grant that also binds origin attestation. Actor-
authored metadata, Judge ALLOW, learning-review acceptance, repetition,
migration origin, and model output never raise authority.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeAlias

import jwt

from open_brain.epistemic_provenance import EPISTEMIC_LABELS

logger = logging.getLogger(__name__)

MEMORY_PROMOTION_SCHEMA_VERSION = "memory-promotion.v1"
PROMOTION_GRANT_AUDIENCE = "open-brain.promotion-grant"
PROMOTION_GRANT_ISSUER = "open-brain.promotion-grant.v1"
PROMOTION_GRANT_ALGORITHMS: tuple[str, ...] = ("HS256",)
PROMOTION_GRANT_SECRET_MIN_LENGTH = 32
MAX_PROMOTION_GRANT_TTL = timedelta(minutes=10)
FUTURE_IAT_SKEW_SECONDS = 30
MAX_GRANT_JTI_CHARS = 128
MAX_EVIDENCE_REFS = 32
MAX_EVIDENCE_REF_CHARS = 256
MAX_REASON_CHARS = 2000
SUPERSEDES_LINK_TYPE = "supersedes"
ACCEPTED_GRANT_JTI_INDEX = "memory_promotion_events_accepted_grant_jti_uidx"
ORIGIN_ATTESTATION_DOMAIN = "open-brain.promotion-origin-attestation.v1"

EpistemicState: TypeAlias = Literal[
    "observed",
    "inferred",
    "generated",
    "confirmed",
    "disputed",
    "superseded",
]
PromotionDecision: TypeAlias = Literal["accepted", "rejected"]
AuthorizationMode: TypeAlias = Literal["signed_grant", "automatic_rule"]

ALLOWED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("inferred", "confirmed"),
        ("generated", "confirmed"),
        ("observed", "confirmed"),
        ("disputed", "confirmed"),
        ("inferred", "disputed"),
        ("generated", "disputed"),
        ("observed", "disputed"),
        ("confirmed", "disputed"),
        ("inferred", "superseded"),
        ("generated", "superseded"),
        ("observed", "superseded"),
        ("confirmed", "superseded"),
        ("disputed", "superseded"),
    }
)

# Every allowed transition requires a signed grant (dispute/supersession included).
GRANT_REQUIRED_TRANSITIONS: frozenset[tuple[str, str]] = ALLOWED_TRANSITIONS

_SECRET_RE = re.compile(
    r"(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
    r"|((?:promotion[_-]?grant[_-]?secret|jwt[_-]?secret|secret)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)


class PromotionError(ValueError):
    """Base error for promotion contract failures."""

    code = "promotion_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class PromotionGrantError(PromotionError):
    """Raised when a promotion grant cannot be verified."""

    code = "grant_invalid"


class PromotionTransitionError(PromotionError):
    """Raised when a requested epistemic transition is illegal."""

    code = "invalid_transition"


class OriginAttestationError(PromotionError):
    """Raised when origin/ingestion attestation cannot be computed."""

    code = "origin_attestation_invalid"


def is_transition_allowed(source_state: str, target_state: str) -> bool:
    """Return True when the explicit transition matrix permits the edge."""
    return (source_state, target_state) in ALLOWED_TRANSITIONS


def requires_signed_grant(source_state: str, target_state: str) -> bool:
    """Return True when the default policy requires a signed promotion grant."""
    return (source_state, target_state) in GRANT_REQUIRED_TRANSITIONS


def redact_secrets(value: str) -> str:
    """Remove grant tokens and secret material from loggable strings."""
    return _SECRET_RE.sub("[REDACTED]", value)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json_for_digest(value: Any) -> str:
    """Serialize digest payloads as UTF-8 JSON with stable dict key order.

    Rules for out-of-band signers (prefer importing the public helpers instead):
    - ``json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=True)``
    - UTF-8 bytes of that string are hashed with SHA-256 (hex digest)
    - list/array element order is preserved (not sorted)
    - non-ASCII characters are escaped via ``ensure_ascii=True``
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def compute_reason_digest(reason: str) -> str:
    """SHA-256 hex digest of the UTF-8 reason string (no JSON wrapping)."""
    if not isinstance(reason, str):
        raise PromotionGrantError("reason must be a string", code="grant_invalid")
    return _sha256_hex(reason)


def compute_evidence_digest(evidence_refs: Sequence[str]) -> str:
    """SHA-256 hex digest of canonical JSON for the evidence refs list.

    Array order is significant and preserved. Dict keys inside nested objects
    (if ever present) are sorted; separators are compact ``(",", ":")``.
    """
    refs = _normalize_evidence_refs(list(evidence_refs))
    return _sha256_hex(_canonical_json_for_digest(refs))


def parse_promotion_admin_users() -> frozenset[str]:
    """Return the server-controlled promotion admin allowlist (exact subjects)."""
    from open_brain.config import get_config

    raw = getattr(get_config(), "PROMOTION_ADMIN_USERS", "") or ""
    if not isinstance(raw, str) or not raw.strip():
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def is_promotion_admin_actor(actor: str | None) -> bool:
    """Fail closed unless actor is an exact allowlisted promotion admin subject."""
    if not isinstance(actor, str) or not actor.strip():
        return False
    return actor.strip() in parse_promotion_admin_users()


def compute_origin_attestation_digest(
    *,
    memory_id: int,
    producer: str | None,
    source_ref: str | None,
    ingestion_route: str | None,
) -> str:
    """Compute the server-issued origin attestation digest bound into grants.

    Digests cover memory id, canonical origin producer/source_ref, ingestion
    route, and a fixed domain version. Missing or ``unknown`` fields fail closed.
    Out-of-band signers must recompute this exact digest from current memory
    metadata before minting a grant.
    """
    if isinstance(memory_id, bool) or not isinstance(memory_id, int) or memory_id < 1:
        raise OriginAttestationError(
            "memory_id must be a positive integer",
            code="origin_attestation_invalid",
        )
    cleaned: dict[str, str] = {}
    for label, value in (
        ("producer", producer),
        ("source_ref", source_ref),
        ("ingestion_route", ingestion_route),
    ):
        if not isinstance(value, str) or not value.strip():
            raise OriginAttestationError(
                f"{label} is required for origin attestation",
                code="origin_attestation_invalid",
            )
        normalized = value.strip()
        if normalized.lower() == "unknown":
            raise OriginAttestationError(
                f"{label} must not be unknown for origin attestation",
                code="origin_attestation_invalid",
            )
        cleaned[label] = normalized
    payload = {
        "domain": ORIGIN_ATTESTATION_DOMAIN,
        "memory_id": memory_id,
        "producer": cleaned["producer"],
        "source_ref": cleaned["source_ref"],
        "ingestion_route": cleaned["ingestion_route"],
        "policy_version": MEMORY_PROMOTION_SCHEMA_VERSION,
    }
    return _sha256_hex(_canonical_json_for_digest(payload))


def origin_attestation_digest_from_metadata(
    memory_id: int, metadata: Mapping[str, Any] | None
) -> str:
    """Recompute attestation digest from current memory metadata."""
    meta = dict(metadata) if isinstance(metadata, Mapping) else {}
    route = meta.get("ingestion_route")
    provenance = meta.get("provenance")
    origin = (
        provenance.get("origin")
        if isinstance(provenance, Mapping)
        else None
    )
    producer = origin.get("producer") if isinstance(origin, Mapping) else None
    source_ref = origin.get("source_ref") if isinstance(origin, Mapping) else None
    return compute_origin_attestation_digest(
        memory_id=memory_id,
        producer=producer if isinstance(producer, str) else None,
        source_ref=source_ref if isinstance(source_ref, str) else None,
        ingestion_route=route if isinstance(route, str) else None,
    )


def grant_digest(token: str) -> str:
    """Return a non-reversible digest of a raw grant token."""
    return _sha256_hex(token)


def get_promotion_grant_secret() -> str:
    """Load PROMOTION_GRANT_SECRET; fail closed when absent or too short.

    Never falls back to JWT_SECRET.
    """
    from open_brain.config import get_config

    config = get_config()
    secret = getattr(config, "PROMOTION_GRANT_SECRET", "") or ""
    if not isinstance(secret, str) or len(secret) < PROMOTION_GRANT_SECRET_MIN_LENGTH:
        raise PromotionGrantError(
            "PROMOTION_GRANT_SECRET is missing or shorter than "
            f"{PROMOTION_GRANT_SECRET_MIN_LENGTH} characters",
            code="missing_grant_secret",
        )
    return secret


def _as_utc_datetime(value: Any) -> datetime | None:
    """Parse a JWT time claim as UTC, or return None for malformed/non-finite.

    NaN, Infinity, and overflow timestamps must not escape as ValueError /
    OverflowError — callers map ``None`` to ``grant_time_invalid``.
    """
    # bool is a subclass of int; reject it as a time claim.
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, (int, float)):
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return None
        try:
            return datetime.fromtimestamp(numeric, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _normalize_optional_successor_id(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PromotionGrantError(
            "successor_memory_id claim invalid",
            code="successor_mismatch",
        )
    return value


def _normalize_attestation_digest_claim(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PromotionGrantError(
            "origin_attestation_digest claim missing",
            code="origin_attestation_invalid",
        )
    digest = value.strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise PromotionGrantError(
            "origin_attestation_digest claim malformed",
            code="origin_attestation_invalid",
        )
    return digest


def _is_accepted_grant_jti_unique_violation(exc: BaseException) -> bool:
    """Map only the accepted-jti unique index to grant_replay."""
    name = getattr(exc, "constraint_name", None) or ""
    diag = getattr(exc, "diag", None)
    if diag is not None:
        name = name or (getattr(diag, "constraint_name", None) or "")
    return ACCEPTED_GRANT_JTI_INDEX in f"{name} {exc}"


def _recover_verified_grant_jti(token: str | None) -> str | None:
    """Recover a bounded jti from a signature-valid grant after a race."""
    if not isinstance(token, str) or not token.strip():
        return None
    try:
        secret = get_promotion_grant_secret()
        payload = jwt.decode(
            token,
            secret,
            algorithms=list(PROMOTION_GRANT_ALGORITHMS),
            audience=PROMOTION_GRANT_AUDIENCE,
            issuer=PROMOTION_GRANT_ISSUER,
            options={
                "require": ["jti"],
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": False,
                "verify_iat": False,
            },
        )
    except Exception:
        return None
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti.strip():
        return None
    cleaned = jti.strip()
    if len(cleaned) > MAX_GRANT_JTI_CHARS:
        return None
    return cleaned


def _normalize_evidence_refs(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PromotionGrantError(
            "evidence_refs must be a list of strings",
            code="malformed_evidence",
        )
    if len(value) > MAX_EVIDENCE_REFS:
        raise PromotionGrantError(
            f"evidence_refs exceeds max of {MAX_EVIDENCE_REFS}",
            code="malformed_evidence",
        )
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PromotionGrantError(
                "evidence_refs entries must be non-empty strings",
                code="malformed_evidence",
            )
        cleaned = item.strip()
        if len(cleaned) > MAX_EVIDENCE_REF_CHARS:
            raise PromotionGrantError(
                "evidence_refs entry exceeds max length",
                code="malformed_evidence",
            )
        normalized.append(cleaned)
    return normalized


@dataclass(frozen=True)
class AuthenticatedPromotionGrant:
    """Signature/time/issuer/audience-authenticated grant (bounded jti safe)."""

    payload: Mapping[str, Any]
    jti: str
    iat: int
    exp: int
    origin_attestation_digest: str | None
    sub: str


@dataclass(frozen=True)
class VerifiedPromotionGrant:
    """Claims from a fully bound promotion grant (never includes the raw token)."""

    memory_id: int
    from_label: str
    to_label: str
    sub: str
    actor: str
    reason_digest: str
    evidence_refs: tuple[str, ...]
    evidence_digest: str
    policy_version: str
    jti: str
    iat: int
    exp: int
    successor_memory_id: int | None
    origin_attestation_digest: str


def authenticate_promotion_grant(token: str) -> AuthenticatedPromotionGrant:
    """Phase 1: verify signature, algorithm, iss/aud, and ordered time claims.

    Returns a bounded verified ``jti`` only after cryptographic authentication.
    Tampered/invalid tokens never contribute an unverified jti.

    Manual ordered time checks (not PyJWT auto-exp):
    1. typed finite ``iat``/``exp`` (``grant_time_invalid``)
    2. ``exp > iat`` (``grant_time_invalid``)
    3. ``exp - iat <= MAX_PROMOTION_GRANT_TTL`` (``grant_ttl_exceeded``)
    4. future ``iat`` within skew (``grant_future_iat``)
    5. current-time expiry ``exp <= now`` (``grant_expired``)
    """
    if not isinstance(token, str) or not token.strip():
        raise PromotionGrantError("promotion grant is required", code="grant_invalid")

    secret = get_promotion_grant_secret()
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=list(PROMOTION_GRANT_ALGORITHMS),
            audience=PROMOTION_GRANT_AUDIENCE,
            issuer=PROMOTION_GRANT_ISSUER,
            options={
                "require": ["exp", "iat", "iss", "aud", "sub", "jti"],
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": False,
                "verify_iat": False,
            },
        )
    except jwt.InvalidTokenError as exc:
        raise PromotionGrantError("promotion grant invalid", code="grant_invalid") from exc

    now = datetime.now(UTC)
    iat_raw = payload.get("iat")
    exp_raw = payload.get("exp")
    if isinstance(iat_raw, bool) or isinstance(exp_raw, bool):
        raise PromotionGrantError(
            "promotion grant iat/exp must be numeric timestamps",
            code="grant_time_invalid",
        )
    iat_dt = _as_utc_datetime(iat_raw)
    exp_dt = _as_utc_datetime(exp_raw)
    if iat_dt is None or exp_dt is None:
        raise PromotionGrantError(
            "promotion grant missing or malformed iat/exp",
            code="grant_time_invalid",
        )
    if exp_dt <= iat_dt:
        raise PromotionGrantError(
            "promotion grant requires exp > iat",
            code="grant_time_invalid",
        )
    if exp_dt - iat_dt > MAX_PROMOTION_GRANT_TTL:
        raise PromotionGrantError(
            f"promotion grant TTL exceeds {MAX_PROMOTION_GRANT_TTL.total_seconds():.0f}s",
            code="grant_ttl_exceeded",
        )
    if iat_dt > now + timedelta(seconds=FUTURE_IAT_SKEW_SECONDS):
        raise PromotionGrantError(
            "promotion grant issued in the future",
            code="grant_future_iat",
        )
    if exp_dt <= now:
        raise PromotionGrantError("promotion grant expired", code="grant_expired")

    claim_jti = payload.get("jti")
    if not isinstance(claim_jti, str) or not claim_jti.strip():
        raise PromotionGrantError("jti missing", code="grant_jti_invalid")
    if len(claim_jti.strip()) > MAX_GRANT_JTI_CHARS:
        raise PromotionGrantError("jti exceeds max length", code="grant_jti_invalid")
    claim_sub = payload.get("sub")
    if not isinstance(claim_sub, str) or not claim_sub.strip():
        raise PromotionGrantError("subject missing", code="grant_invalid")

    attestation: str | None = None
    raw_attestation = payload.get("origin_attestation_digest")
    if raw_attestation is not None:
        attestation = _normalize_attestation_digest_claim(raw_attestation)

    return AuthenticatedPromotionGrant(
        payload=payload,
        jti=claim_jti.strip(),
        iat=int(iat_dt.timestamp()),
        exp=int(exp_dt.timestamp()),
        origin_attestation_digest=attestation,
        sub=claim_sub.strip(),
    )


def bind_promotion_grant(
    authenticated: AuthenticatedPromotionGrant,
    *,
    memory_id: int,
    from_label: str,
    to_label: str,
    actor: str,
    reason: str,
    evidence_refs: Sequence[str],
    successor_memory_id: int | None = None,
    origin_attestation_digest: str | None = None,
) -> VerifiedPromotionGrant:
    """Phase 2: exact request-binding validation against an authenticated grant."""
    payload = authenticated.payload
    claim_memory_id = payload.get("memory_id")
    claim_from = payload.get("from_label")
    claim_to = payload.get("to_label")
    claim_sub = payload.get("sub")
    claim_actor = payload.get("actor")
    claim_policy = payload.get("policy_version")
    claim_reason_digest = payload.get("reason_digest")
    claim_evidence_digest = payload.get("evidence_digest")
    claim_successor = _normalize_optional_successor_id(
        payload.get("successor_memory_id")
    )
    claim_attestation = _normalize_attestation_digest_claim(
        payload.get("origin_attestation_digest")
    )
    try:
        claim_evidence_refs = _normalize_evidence_refs(payload.get("evidence_refs"))
    except PromotionGrantError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise PromotionGrantError(
            "promotion grant evidence malformed",
            code="malformed_evidence",
        ) from exc

    if not isinstance(claim_memory_id, int) or isinstance(claim_memory_id, bool):
        raise PromotionGrantError("memory_id claim invalid", code="grant_invalid")
    if claim_memory_id != memory_id:
        raise PromotionGrantError("memory_id mismatch", code="transition_mismatch")
    if claim_from != from_label or claim_to != to_label:
        raise PromotionGrantError(
            "transition labels mismatch grant claims",
            code="transition_mismatch",
        )
    if not isinstance(claim_sub, str) or claim_sub != actor:
        raise PromotionGrantError("subject mismatch", code="subject_mismatch")
    if not isinstance(claim_actor, str) or claim_actor != actor:
        raise PromotionGrantError("actor mismatch", code="actor_mismatch")
    if claim_policy != MEMORY_PROMOTION_SCHEMA_VERSION:
        raise PromotionGrantError("policy_version mismatch", code="grant_invalid")
    if (
        not isinstance(claim_reason_digest, str)
        or claim_reason_digest != compute_reason_digest(reason)
    ):
        raise PromotionGrantError("reason digest mismatch", code="grant_invalid")

    expected_evidence = _normalize_evidence_refs(list(evidence_refs))
    if claim_evidence_refs != expected_evidence:
        raise PromotionGrantError("evidence refs mismatch", code="malformed_evidence")
    expected_evidence_digest = compute_evidence_digest(expected_evidence)
    if (
        not isinstance(claim_evidence_digest, str)
        or claim_evidence_digest != expected_evidence_digest
    ):
        raise PromotionGrantError("evidence digest mismatch", code="malformed_evidence")

    expected_successor = successor_memory_id
    if (
        expected_successor is not None
        and (
            isinstance(expected_successor, bool)
            or not isinstance(expected_successor, int)
            or expected_successor < 1
        )
    ):
        raise PromotionGrantError(
            "successor_memory_id invalid",
            code="successor_mismatch",
        )
    if claim_successor != expected_successor:
        raise PromotionGrantError(
            "successor_memory_id mismatch",
            code="successor_mismatch",
        )
    if (
        origin_attestation_digest is not None
        and claim_attestation != origin_attestation_digest.strip().lower()
    ):
        raise PromotionGrantError(
            "origin_attestation_digest mismatch",
            code="origin_attestation_invalid",
        )

    return VerifiedPromotionGrant(
        memory_id=claim_memory_id,
        from_label=str(claim_from),
        to_label=str(claim_to),
        sub=claim_sub,
        actor=claim_actor,
        reason_digest=claim_reason_digest,
        evidence_refs=tuple(claim_evidence_refs),
        evidence_digest=claim_evidence_digest,
        policy_version=MEMORY_PROMOTION_SCHEMA_VERSION,
        jti=authenticated.jti,
        iat=authenticated.iat,
        exp=authenticated.exp,
        successor_memory_id=claim_successor,
        origin_attestation_digest=claim_attestation,
    )


def verify_promotion_grant(
    token: str,
    *,
    memory_id: int,
    from_label: str,
    to_label: str,
    actor: str,
    reason: str,
    evidence_refs: Sequence[str],
    successor_memory_id: int | None = None,
    origin_attestation_digest: str | None = None,
) -> VerifiedPromotionGrant:
    """Authenticate then bind a promotion grant to the exact request."""
    authenticated = authenticate_promotion_grant(token)
    return bind_promotion_grant(
        authenticated,
        memory_id=memory_id,
        from_label=from_label,
        to_label=to_label,
        actor=actor,
        reason=reason,
        evidence_refs=evidence_refs,
        successor_memory_id=successor_memory_id,
        origin_attestation_digest=origin_attestation_digest,
    )


@dataclass(frozen=True)
class PromotionProjection:
    """Server-supplied, ledger-backed read-time promotion authority."""

    memory_id: int
    event_id: int
    decision: PromotionDecision
    source_state: str
    target_state: str
    outcome: str
    is_current: bool
    policy_version: str
    grant_jti_digest: str | None
    audit_reason: str
    audit_source: str = "memory_promotion_events"
    origin_attestation_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "event_id": self.event_id,
            "decision": self.decision,
            "source_state": self.source_state,
            "target_state": self.target_state,
            "outcome": self.outcome,
            "is_current": self.is_current,
            "policy_version": self.policy_version,
            "grant_jti_digest": self.grant_jti_digest,
            "audit_reason": self.audit_reason,
            "audit_source": self.audit_source,
            "origin_attestation_digest": self.origin_attestation_digest,
        }


@dataclass(frozen=True)
class PromotionEvent:
    """One immutable promotion ledger row."""

    id: int
    memory_id: int
    actor: str
    source_state: str
    target_state: str
    reason: str
    evidence_refs: tuple[str, ...]
    policy_version: str
    rule_version: str | None
    grant_jti: str | None
    grant_digest: str | None
    decision: PromotionDecision
    outcome: str
    rejection_code: str | None
    relationship_id: int | None
    created_at: str
    origin_attestation_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "memory_id": self.memory_id,
            "actor": self.actor,
            "source_state": self.source_state,
            "target_state": self.target_state,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "policy_version": self.policy_version,
            "rule_version": self.rule_version,
            "grant_jti": self.grant_jti,
            "grant_digest": self.grant_digest,
            "origin_attestation_digest": self.origin_attestation_digest,
            "decision": self.decision,
            "outcome": self.outcome,
            "rejection_code": self.rejection_code,
            "relationship_id": self.relationship_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class PromotionAttemptParams:
    """Validated input for one promotion, dispute, or supersession attempt."""

    memory_id: int
    target_state: str
    reason: str
    evidence_refs: list[str]
    actor: str
    promotion_grant: str | None = None
    successor_memory_id: int | None = None
    authorization_mode: AuthorizationMode = "signed_grant"
    rule_version: str | None = None
    policy_version: str = MEMORY_PROMOTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.memory_id, bool)
            or not isinstance(self.memory_id, int)
            or self.memory_id < 1
        ):
            raise PromotionError("memory_id must be a positive integer", code="invalid_request")
        if self.target_state not in EPISTEMIC_LABELS:
            raise PromotionError(
                f"target_state invalid: {self.target_state!r}",
                code="invalid_transition",
            )
        reason = self.reason.strip() if isinstance(self.reason, str) else ""
        if not reason or len(reason) > MAX_REASON_CHARS:
            raise PromotionError("reason must be a non-empty bounded string", code="invalid_request")
        actor = self.actor.strip() if isinstance(self.actor, str) else ""
        if not actor:
            raise PromotionError("actor is required", code="invalid_request")
        refs = _normalize_evidence_refs(self.evidence_refs)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "evidence_refs", refs)
        if self.policy_version != MEMORY_PROMOTION_SCHEMA_VERSION:
            raise PromotionError(
                "unsupported policy_version",
                code="invalid_request",
            )
        if self.authorization_mode not in {"signed_grant", "automatic_rule"}:
            raise PromotionError(
                "authorization_mode must be signed_grant or automatic_rule",
                code="invalid_request",
            )


@dataclass(frozen=True)
class PromotionResult:
    """Outcome of one promotion attempt, always accompanied by a ledger event."""

    decision: PromotionDecision
    rejection_code: str | None
    event: PromotionEvent | None
    memory_id: int
    source_state: str | None
    target_state: str
    relationship_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "rejection_code": self.rejection_code,
            "event": self.event.to_dict() if self.event else None,
            "memory_id": self.memory_id,
            "source_state": self.source_state,
            "target_state": self.target_state,
            "relationship_id": self.relationship_id,
        }


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _row_to_event(row: Mapping[str, Any]) -> PromotionEvent:
    refs = row.get("evidence_refs") or []
    if isinstance(refs, str):
        refs = json.loads(refs)
    attestation = row.get("origin_attestation_digest")
    return PromotionEvent(
        id=int(row["id"]),
        memory_id=int(row["memory_id"]),
        actor=str(row["actor"]),
        source_state=str(row["source_state"]),
        target_state=str(row["target_state"]),
        reason=str(row["reason"]),
        evidence_refs=tuple(str(item) for item in refs),
        policy_version=str(row["policy_version"]),
        rule_version=str(row["rule_version"]) if row.get("rule_version") else None,
        grant_jti=str(row["grant_jti"]) if row.get("grant_jti") else None,
        grant_digest=str(row["grant_digest"]) if row.get("grant_digest") else None,
        decision=row["decision"],  # type: ignore[arg-type]
        outcome=str(row["outcome"]),
        rejection_code=(
            str(row["rejection_code"]) if row.get("rejection_code") else None
        ),
        relationship_id=(
            int(row["relationship_id"]) if row.get("relationship_id") is not None else None
        ),
        created_at=_timestamp(row["created_at"]),
        origin_attestation_digest=str(attestation) if attestation else None,
    )


def _expected_use_for_target(target_state: str) -> str:
    if target_state in {"confirmed", "observed"}:
        return "instruction"
    return "evidence"


def _outcome_for(target_state: str, *, accepted: bool) -> str:
    if not accepted:
        return "rejected"
    if target_state == "confirmed":
        return "promoted"
    return target_state


async def get_pool() -> Any:
    """Indirection for tests that patch pool acquisition."""
    from open_brain.data_layer.postgres import get_pool as _get_pool

    return await _get_pool()


async def _insert_event(
    conn: Any,
    *,
    memory_id: int,
    actor: str,
    source_state: str,
    target_state: str,
    reason: str,
    evidence_refs: Sequence[str],
    policy_version: str,
    rule_version: str | None,
    grant_jti: str | None,
    grant_digest: str | None,
    decision: PromotionDecision,
    outcome: str,
    rejection_code: str | None,
    relationship_id: int | None,
    origin_attestation_digest: str | None = None,
) -> PromotionEvent:
    row = await conn.fetchrow(
        """
        INSERT INTO memory_promotion_events (
            memory_id, actor, source_state, target_state, reason,
            evidence_refs, policy_version, rule_version,
            grant_jti, grant_digest, origin_attestation_digest,
            decision, outcome, rejection_code, relationship_id
        ) VALUES (
            $1, $2, $3, $4, $5,
            $6::jsonb, $7, $8,
            $9, $10, $11,
            $12, $13, $14, $15
        )
        RETURNING id, memory_id, actor, source_state, target_state, reason,
                  evidence_refs, policy_version, rule_version, grant_jti,
                  grant_digest, origin_attestation_digest, decision, outcome,
                  rejection_code, relationship_id, created_at
        """,
        memory_id,
        actor,
        source_state,
        target_state,
        reason,
        list(evidence_refs),
        policy_version,
        rule_version,
        grant_jti,
        grant_digest,
        origin_attestation_digest,
        decision,
        outcome,
        rejection_code,
        relationship_id,
    )
    if row is None:
        raise RuntimeError("memory_promotion_events insert returned no row")
    return _row_to_event(row)


async def _reject(
    conn: Any,
    *,
    params: PromotionAttemptParams,
    source_state: str | None,
    rejection_code: str,
    grant_jti: str | None = None,
    grant_digest_value: str | None = None,
    origin_attestation_digest: str | None = None,
) -> PromotionResult:
    source = source_state or "unknown"
    event = await _insert_event(
        conn,
        memory_id=params.memory_id,
        actor=params.actor,
        source_state=source,
        target_state=params.target_state,
        reason=params.reason,
        evidence_refs=params.evidence_refs,
        policy_version=params.policy_version,
        rule_version=params.rule_version,
        grant_jti=grant_jti,
        grant_digest=grant_digest_value,
        decision="rejected",
        outcome="rejected",
        rejection_code=rejection_code,
        relationship_id=None,
        origin_attestation_digest=origin_attestation_digest,
    )
    return PromotionResult(
        decision="rejected",
        rejection_code=rejection_code,
        event=event,
        memory_id=params.memory_id,
        source_state=source_state,
        target_state=params.target_state,
    )


async def _active_successor_id(conn: Any, memory_id: int) -> int | None:
    """Return the newer memory that currently supersedes ``memory_id``, if any."""
    return await conn.fetchval(
        """
        SELECT source_id
        FROM memory_relationships
        WHERE target_id = $1
          AND relation_type = $2
        ORDER BY id ASC
        LIMIT 1
        """,
        memory_id,
        SUPERSEDES_LINK_TYPE,
    )


async def _would_create_cycle(
    conn: Any, *, older_id: int, newer_id: int
) -> bool:
    """Return True if newer already sits in older's supersession ancestor chain."""
    current = newer_id
    seen: set[int] = {older_id}
    for _ in range(64):
        parent = await conn.fetchval(
            """
            SELECT target_id
            FROM memory_relationships
            WHERE source_id = $1
              AND relation_type = $2
            ORDER BY id ASC
            LIMIT 1
            """,
            current,
            SUPERSEDES_LINK_TYPE,
        )
        if parent is None:
            return False
        parent_id = int(parent)
        if parent_id in seen or parent_id == older_id:
            return True
        seen.add(parent_id)
        current = parent_id
    return True


async def attempt_memory_promotion(
    params: PromotionAttemptParams,
) -> PromotionResult:
    """Attempt a validated promotion/dispute/supersession with immutable audit."""
    import asyncpg  # type: ignore[import-untyped]

    pool = await get_pool()
    try:
        return await _attempt_memory_promotion_inner(params, pool)
    except asyncpg.UniqueViolationError as exc:
        if not _is_accepted_grant_jti_unique_violation(exc):
            raise
        # Race-safe replay: unique accepted-jti index aborted the accept txn.
        async with pool.acquire() as conn:
            async with conn.transaction():
                digest = (
                    grant_digest(params.promotion_grant)
                    if params.promotion_grant
                    else None
                )
                recovered_jti = _recover_verified_grant_jti(params.promotion_grant)
                return await _reject(
                    conn,
                    params=params,
                    source_state=None,
                    rejection_code="grant_replay",
                    grant_jti=recovered_jti,
                    grant_digest_value=digest,
                )


async def _attempt_memory_promotion_inner(
    params: PromotionAttemptParams,
    pool: Any,
) -> PromotionResult:
    async with pool.acquire() as conn:
        async with conn.transaction():
            memory = await conn.fetchrow(
                "SELECT id, metadata FROM memories WHERE id = $1 FOR UPDATE",
                params.memory_id,
            )
            source_state: str | None = None
            metadata: dict[str, Any] = {}
            if memory is None:
                return await _reject(
                    conn,
                    params=params,
                    source_state=None,
                    rejection_code="memory_not_found",
                )

            raw_meta = memory["metadata"]
            if isinstance(raw_meta, str):
                metadata = json.loads(raw_meta)
            elif isinstance(raw_meta, Mapping):
                metadata = dict(raw_meta)
            provenance = metadata.get("provenance")
            if isinstance(provenance, Mapping):
                label = provenance.get("source_label")
                if isinstance(label, str):
                    source_state = label

            grant_jti: str | None = None
            grant_digest_value: str | None = None
            origin_attestation: str | None = None
            relationship_id: int | None = None

            if params.authorization_mode == "automatic_rule":
                configured = ""
                try:
                    from open_brain.config import get_config

                    configured = (
                        getattr(get_config(), "PROMOTION_AUTOMATIC_RULE_VERSION", "")
                        or ""
                    )
                except Exception:
                    configured = ""
                if (
                    not configured
                    or not params.rule_version
                    or params.rule_version != configured
                    or not params.evidence_refs
                ):
                    return await _reject(
                        conn,
                        params=params,
                        source_state=source_state,
                        rejection_code="automatic_rule_disabled",
                    )
                # v1 ships with no built-in rule implementations.
                return await _reject(
                    conn,
                    params=params,
                    source_state=source_state,
                    rejection_code="automatic_rule_disabled",
                )

            # Every allowed transition requires a signed grant (dispute/supersession
            # included). Automatic rule remains disabled above.
            if not params.promotion_grant:
                return await _reject(
                    conn,
                    params=params,
                    source_state=source_state,
                    rejection_code="grant_invalid",
                )
            grant_digest_value = grant_digest(params.promotion_grant)

            # Phase 1: authenticate enough to obtain a cryptographically verified
            # jti, then check accepted-jti ledger BEFORE transition validation so
            # replays are not masked as invalid_transition.
            try:
                authenticated = authenticate_promotion_grant(params.promotion_grant)
            except PromotionGrantError as exc:
                logger.info(
                    "promotion grant rejected memory_id=%s code=%s",
                    params.memory_id,
                    exc.code,
                )
                return await _reject(
                    conn,
                    params=params,
                    source_state=source_state,
                    rejection_code=exc.code,
                    grant_digest_value=grant_digest_value,
                )
            existing = await conn.fetchval(
                """
                SELECT id FROM memory_promotion_events
                WHERE grant_jti = $1 AND decision = 'accepted'
                LIMIT 1
                """,
                authenticated.jti,
            )
            if existing is not None:
                return await _reject(
                    conn,
                    params=params,
                    source_state=source_state,
                    rejection_code="grant_replay",
                    grant_jti=authenticated.jti,
                    grant_digest_value=grant_digest_value,
                    origin_attestation_digest=authenticated.origin_attestation_digest,
                )

            if source_state is None or source_state not in EPISTEMIC_LABELS:
                return await _reject(
                    conn,
                    params=params,
                    source_state=source_state,
                    rejection_code="invalid_transition",
                    grant_jti=authenticated.jti,
                    grant_digest_value=grant_digest_value,
                    origin_attestation_digest=authenticated.origin_attestation_digest,
                )

            if not is_transition_allowed(source_state, params.target_state):
                return await _reject(
                    conn,
                    params=params,
                    source_state=source_state,
                    rejection_code="invalid_transition",
                    grant_jti=authenticated.jti,
                    grant_digest_value=grant_digest_value,
                    origin_attestation_digest=authenticated.origin_attestation_digest,
                )

            try:
                expected_attestation = origin_attestation_digest_from_metadata(
                    params.memory_id, metadata
                )
            except OriginAttestationError as exc:
                return await _reject(
                    conn,
                    params=params,
                    source_state=source_state,
                    rejection_code=exc.code,
                    grant_jti=authenticated.jti,
                    grant_digest_value=grant_digest_value,
                    origin_attestation_digest=authenticated.origin_attestation_digest,
                )
            try:
                verified = bind_promotion_grant(
                    authenticated,
                    memory_id=params.memory_id,
                    from_label=source_state,
                    to_label=params.target_state,
                    actor=params.actor,
                    reason=params.reason,
                    evidence_refs=params.evidence_refs,
                    successor_memory_id=params.successor_memory_id,
                    origin_attestation_digest=expected_attestation,
                )
            except PromotionGrantError as exc:
                logger.info(
                    "promotion grant binding rejected memory_id=%s code=%s",
                    params.memory_id,
                    exc.code,
                )
                return await _reject(
                    conn,
                    params=params,
                    source_state=source_state,
                    rejection_code=exc.code,
                    grant_jti=authenticated.jti,
                    grant_digest_value=grant_digest_value,
                    origin_attestation_digest=authenticated.origin_attestation_digest,
                )
            grant_jti = verified.jti
            origin_attestation = verified.origin_attestation_digest

            if params.target_state == "superseded":
                successor = params.successor_memory_id
                if successor is None:
                    return await _reject(
                        conn,
                        params=params,
                        source_state=source_state,
                        rejection_code="missing_successor",
                        grant_jti=grant_jti,
                        grant_digest_value=grant_digest_value,
                        origin_attestation_digest=origin_attestation,
                    )
                if (
                    isinstance(successor, bool)
                    or not isinstance(successor, int)
                    or successor < 1
                ):
                    return await _reject(
                        conn,
                        params=params,
                        source_state=source_state,
                        rejection_code="missing_successor",
                        grant_jti=grant_jti,
                        grant_digest_value=grant_digest_value,
                        origin_attestation_digest=origin_attestation,
                    )
                if successor == params.memory_id:
                    return await _reject(
                        conn,
                        params=params,
                        source_state=source_state,
                        rejection_code="self_supersession",
                        grant_jti=grant_jti,
                        grant_digest_value=grant_digest_value,
                        origin_attestation_digest=origin_attestation,
                    )
                successor_row = await conn.fetchrow(
                    "SELECT id, metadata FROM memories WHERE id = $1",
                    successor,
                )
                if successor_row is None:
                    return await _reject(
                        conn,
                        params=params,
                        source_state=source_state,
                        rejection_code="missing_successor",
                        grant_jti=grant_jti,
                        grant_digest_value=grant_digest_value,
                        origin_attestation_digest=origin_attestation,
                    )
                succ_meta_raw = successor_row["metadata"]
                succ_meta: dict[str, Any] = {}
                if isinstance(succ_meta_raw, str):
                    succ_meta = json.loads(succ_meta_raw)
                elif isinstance(succ_meta_raw, Mapping):
                    succ_meta = dict(succ_meta_raw)
                succ_prov = succ_meta.get("provenance")
                succ_label = (
                    succ_prov.get("source_label")
                    if isinstance(succ_prov, Mapping)
                    else None
                )
                if succ_label in {"disputed", "superseded"}:
                    return await _reject(
                        conn,
                        params=params,
                        source_state=source_state,
                        rejection_code="successor_inactive",
                        grant_jti=grant_jti,
                        grant_digest_value=grant_digest_value,
                        origin_attestation_digest=origin_attestation,
                    )
                existing_successor = await _active_successor_id(conn, params.memory_id)
                if existing_successor is not None and int(existing_successor) != successor:
                    return await _reject(
                        conn,
                        params=params,
                        source_state=source_state,
                        rejection_code="duplicate_active_successor",
                        grant_jti=grant_jti,
                        grant_digest_value=grant_digest_value,
                        origin_attestation_digest=origin_attestation,
                    )
                if await _would_create_cycle(
                    conn, older_id=params.memory_id, newer_id=successor
                ):
                    return await _reject(
                        conn,
                        params=params,
                        source_state=source_state,
                        rejection_code="supersession_cycle",
                        grant_jti=grant_jti,
                        grant_digest_value=grant_digest_value,
                        origin_attestation_digest=origin_attestation,
                    )
            elif params.successor_memory_id is not None:
                return await _reject(
                    conn,
                    params=params,
                    source_state=source_state,
                    rejection_code="successor_mismatch",
                    grant_jti=grant_jti,
                    grant_digest_value=grant_digest_value,
                    origin_attestation_digest=origin_attestation,
                )

            # Accepted mutation path: metadata + optional edge + ledger event.
            provenance_obj = dict(provenance) if isinstance(provenance, Mapping) else {}
            # Preserve canonical origin lineage untouched.
            provenance_obj["source_label"] = params.target_state
            provenance_obj["expected_use"] = _expected_use_for_target(params.target_state)
            provenance_obj["epistemic_version"] = provenance_obj.get(
                "epistemic_version", "epistemic-provenance.v1"
            )
            metadata["provenance"] = provenance_obj

            await conn.execute(
                """
                UPDATE memories
                SET metadata = $2::jsonb, updated_at = NOW()
                WHERE id = $1
                """,
                params.memory_id,
                metadata,
            )

            if params.target_state == "superseded":
                assert params.successor_memory_id is not None
                relationship_id = await conn.fetchval(
                    """
                    INSERT INTO memory_relationships (
                        source_id, target_id, relation_type, link_type, confidence, metadata
                    ) VALUES ($1, $2, $3, $3, 1.0, $4::jsonb)
                    ON CONFLICT (source_id, target_id, relation_type) DO UPDATE
                        SET link_type = EXCLUDED.link_type,
                            confidence = 1.0,
                            metadata = COALESCE(EXCLUDED.metadata, memory_relationships.metadata)
                    RETURNING id
                    """,
                    params.successor_memory_id,
                    params.memory_id,
                    SUPERSEDES_LINK_TYPE,
                    {
                        "promotion_policy_version": MEMORY_PROMOTION_SCHEMA_VERSION,
                        "actor": params.actor,
                    },
                )

            event = await _insert_event(
                conn,
                memory_id=params.memory_id,
                actor=params.actor,
                source_state=source_state,
                target_state=params.target_state,
                reason=params.reason,
                evidence_refs=params.evidence_refs,
                policy_version=params.policy_version,
                rule_version=params.rule_version,
                grant_jti=grant_jti,
                grant_digest=grant_digest_value,
                decision="accepted",
                outcome=_outcome_for(params.target_state, accepted=True),
                rejection_code=None,
                relationship_id=relationship_id,
                origin_attestation_digest=origin_attestation,
            )

            return PromotionResult(
                decision="accepted",
                rejection_code=None,
                event=event,
                memory_id=params.memory_id,
                source_state=source_state,
                target_state=params.target_state,
                relationship_id=relationship_id,
            )


async def list_memory_promotion_history(memory_id: int) -> list[dict[str, Any]]:
    """Return complete promotion/dispute/supersession history for a memory."""
    if isinstance(memory_id, bool) or not isinstance(memory_id, int) or memory_id < 1:
        raise PromotionError("memory_id must be a positive integer", code="invalid_request")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, memory_id, actor, source_state, target_state, reason,
                   evidence_refs, policy_version, rule_version, grant_jti,
                   grant_digest, origin_attestation_digest, decision, outcome,
                   rejection_code, relationship_id, created_at
            FROM memory_promotion_events
            WHERE memory_id = $1
            ORDER BY created_at ASC, id ASC
            """,
            memory_id,
        )
    return [_row_to_event(row).to_dict() for row in rows]


async def fetch_promotion_projections(
    memory_ids: Sequence[int],
    *,
    allow_missing_table: bool = True,
) -> dict[int, PromotionProjection]:
    """Fetch current ledger-backed promotion projections keyed by memory id.

    Missing ledger table fails closed to an empty projection map so retrieval
    seams remain available and high-authority elevation stays denied.
    """
    import asyncpg  # type: ignore[import-untyped]

    ids = sorted(
        {
            int(memory_id)
            for memory_id in memory_ids
            if isinstance(memory_id, int)
            and not isinstance(memory_id, bool)
            and memory_id > 0
        }
    )
    if not ids:
        return {}
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (memory_id)
                       id, memory_id, actor, source_state, target_state, reason,
                       evidence_refs, policy_version, rule_version, grant_jti,
                       grant_digest, origin_attestation_digest, decision, outcome,
                       rejection_code, relationship_id, created_at
                FROM memory_promotion_events
                WHERE memory_id = ANY($1::int[])
                  AND decision = 'accepted'
                ORDER BY memory_id, created_at DESC, id DESC
                """,
                ids,
            )
    except asyncpg.UndefinedTableError:
        if not allow_missing_table:
            raise
        return {}
    except Exception:
        # Fail closed for retrieval seams: no projection => no HA elevation.
        if not allow_missing_table:
            raise
        logger.warning(
            "promotion projection lookup failed; failing closed without elevation",
            exc_info=True,
        )
        return {}
    projections: dict[int, PromotionProjection] = {}
    for row in rows:
        event = _row_to_event(row)
        jti_digest = (
            _sha256_hex(event.grant_jti) if event.grant_jti else event.grant_digest
        )
        projections[event.memory_id] = PromotionProjection(
            memory_id=event.memory_id,
            event_id=event.id,
            decision=event.decision,
            source_state=event.source_state,
            target_state=event.target_state,
            outcome=event.outcome,
            is_current=True,
            policy_version=event.policy_version,
            grant_jti_digest=jti_digest,
            audit_reason=(
                "ledger_promotion_grant"
                if event.outcome == "promoted"
                else f"ledger_{event.outcome}"
            ),
            audit_source="memory_promotion_events",
            origin_attestation_digest=event.origin_attestation_digest,
        )
    return projections
