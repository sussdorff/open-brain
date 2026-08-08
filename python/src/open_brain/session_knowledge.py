"""Open-Brain-owned session-knowledge capture contract.

Contract URI: ``standard://open-brain/contracts/session-knowledge-capture.v1``

Separates compact observed execution evidence (``what_happened``) from durable
inferred learnings (``what_was_learned``). Unfinished work is classified for the
producer but never persisted and never creates tracker state.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from open_brain.data_layer.interface import (
    Memory,
    SaveMemoryParams,
    SaveMemoryResult,
    SearchParams,
    SearchResult,
)
from open_brain.epistemic_provenance import ensure_epistemic_provenance
from open_brain.memory_write_judge import (
    judge_memory_write_proposal,
    memory_metadata_from_judged_proposal,
)
from open_brain.memory_write_proposal import (
    build_memory_write_proposal,
    raw_proposal_payload,
)

logger = logging.getLogger(__name__)

SESSION_KNOWLEDGE_CAPTURE_SCHEMA_VERSION = "session-knowledge-capture.v1"
SESSION_KNOWLEDGE_CAPTURE_SCHEMA_ID = (
    "standard://open-brain/contracts/session-knowledge-capture.v1"
)
SESSION_KNOWLEDGE_ROLES = frozenset(
    {"session_event", "session_decision", "session_learning"}
)
DERIVED_FROM_LINK_TYPE = "derived_from"
DEFAULT_PRODUCER = "session-knowledge-capture"
DEFAULT_AUTH_REF = "policy://session/evidence-write"

# Compactness / abuse bounds (O1-07 / O1-13).
MAX_WHAT_HAPPENED_CHARS = 2000
MAX_ITEM_TEXT_CHARS = 1000
MAX_DECISIONS = 20
MAX_LEARNINGS = 20
MAX_UNFINISHED = 20
LINEAGE_ANCHOR_PREFIX = "Session-knowledge lineage anchor for session "

CaptureStatus = Literal["captured", "replayed", "rejected", "conflict", "judged"]

# Narrow completed-work narration: sentence-initial past action or first-person
# past action. Bare mid-sentence verbs ("is added", "be verified", "resolved
# symlink") are not treated as completed-work narration (O2-04).
_COMPLETED_NARRATION_RE = re.compile(
    r"(?:"
    r"(?:^|(?<=[.!?]\s))"
    r"(?:(?:I|We|They)\s+)?"
    r"(?:Added|Addressed|Completed|Deployed|Fixed|Implemented|Landed|Merged|"
    r"Released|Resolved|Shipped|Updated|Verified)\b"
    r"|"
    r"\b(?:I|We)\s+"
    r"(?:added|addressed|completed|deployed|fixed|implemented|landed|merged|"
    r"released|resolved|shipped|updated|verified)\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
# First-person / pending markers only — not broad normative "needs to"/"must".
_UNFINISHED_WORK_RE = re.compile(
    r"(?:"
    r"\bstill pending\b|"
    r"\bnot yet\b|"
    r"\bTODO:|"
    r"\bfollow-up (?:needed|required)\b|"
    r"\bmust still\b|"
    r"\bstill need(?:s)?\b|"
    r"\bremains pending\b|"
    r"\bI (?:still )?need\b|"
    r"\bwe (?:still )?need\b|"
    r"\bremains unfinished\b"
    r")",
    re.IGNORECASE,
)
CapacityReserve = Callable[..., Awaitable[str | None]]
CapacityRelease = Callable[[], None]


class CaptureCapacityError(Exception):
    """Raised when a pre-write capacity reservation fails."""

    def __init__(self, payload: str) -> None:
        super().__init__(payload)
        self.payload = payload


def _is_zero_write_result(result: SessionKnowledgeCaptureResult) -> bool:
    """True when the completed result proves no rows were persisted."""
    return (
        result.session_event_id is None
        and not result.decision_ids
        and not result.learning_ids
    )
_SECRET_PATTERNS = (
    re.compile(r"sk-ant-api\d*-[A-Za-z0-9_-]+", re.IGNORECASE),
    re.compile(r"\bANTHROPIC_API_KEY\s*=\s*\S+", re.IGNORECASE),
    re.compile(r"\bAWS_SECRET_ACCESS_KEY\s*=\s*\S+", re.IGNORECASE),
    re.compile(r"\b(?:api[_-]?key|password|secret|token)\s*=\s*\S+", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE),
)


class SessionKnowledgeDataLayer(Protocol):
    """Minimal persistence surface used by session-knowledge capture."""

    async def search(self, params: SearchParams) -> SearchResult: ...

    async def save_memory(self, params: SaveMemoryParams) -> SaveMemoryResult: ...

    async def create_relationship(
        self,
        source_id: int,
        target_id: int,
        link_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> int: ...


@dataclass(frozen=True)
class SessionKnowledgeIssue:
    """Machine-readable capture validation or conflict issue."""

    code: str
    field: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "message": self.message}


@dataclass(frozen=True)
class SessionDecisionItem:
    text: str
    rationale: str | None = None
    risk_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionLearningItem:
    text: str
    evidence: str | None = None
    risk_flags: tuple[str, ...] = ()
    # Always clamped at parse time (O1-01); fields remain for fingerprint stability.
    expected_use: str = "evidence"
    source_label: str = "inferred"


@dataclass(frozen=True)
class UnfinishedWorkItem:
    text: str


@dataclass(frozen=True)
class SessionKnowledgeCaptureRequest:
    schema_version: str
    session_id: str
    producer: str
    source_ref: str
    project: str
    what_happened: str | None
    decisions: tuple[SessionDecisionItem, ...]
    what_was_learned: tuple[SessionLearningItem, ...]
    unfinished_work: tuple[UnfinishedWorkItem, ...]
    risk_flags: tuple[str, ...] = ()
    # Hashes of every submitted learning text (accepted + rejected) for idempotency.
    submitted_learning_hashes: tuple[str, ...] = ()
    classification_issues: tuple[SessionKnowledgeIssue, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SessionKnowledgeCaptureResult:
    status: CaptureStatus
    schema_version: str
    session_event_id: int | None
    decision_ids: tuple[int, ...]
    learning_ids: tuple[int, ...]
    relationship_ids: tuple[int, ...]
    unfinished_work: tuple[dict[str, str], ...]
    replayed: bool
    issues: tuple[SessionKnowledgeIssue, ...]
    judge_outcomes: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "schema_version": self.schema_version,
            "session_event_id": self.session_event_id,
            "decision_ids": list(self.decision_ids),
            "learning_ids": list(self.learning_ids),
            "relationship_ids": list(self.relationship_ids),
            "unfinished_work": list(self.unfinished_work),
            "replayed": self.replayed,
            "issues": [issue.to_dict() for issue in self.issues],
            "judge_outcomes": list(self.judge_outcomes),
        }


def capture_identity(
    actor: str, producer: str, source_ref: str, schema_version: str
) -> str:
    """Stable identity for idempotent capture under one actor/session/source."""
    return f"{actor}|{producer}|{source_ref}|{schema_version}"


def record_identity_for(
    identity: str, role: str, ordinal: int, text: str
) -> str:
    """Deterministic per-record identity used for resume-safe upserts."""
    text_fp = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{identity}|{role}|{ordinal}|{text_fp}"


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_capture_fingerprint(request: SessionKnowledgeCaptureRequest) -> str:
    """SHA-256 of the normalized capture payload (excludes unfinished work).

    Includes hashes of every submitted learning (accepted or rejected) so a
    different rejected learning under the same source identity conflicts.
    Rejected learning text itself is never persisted.
    """
    canonical = {
        "schema_version": request.schema_version,
        "session_id": request.session_id,
        "producer": request.producer,
        "source_ref": request.source_ref,
        "project": request.project,
        "what_happened": request.what_happened or "",
        "risk_flags": list(request.risk_flags),
        "decisions": [
            {
                "text": item.text,
                "rationale": item.rationale or "",
                "risk_flags": list(item.risk_flags),
            }
            for item in request.decisions
        ],
        "what_was_learned": [
            {
                "text": item.text,
                "evidence": item.evidence or "",
                "risk_flags": list(item.risk_flags),
                "expected_use": "evidence",
                "source_label": "inferred",
            }
            for item in request.what_was_learned
        ],
        "submitted_learning_hashes": list(request.submitted_learning_hashes),
    }
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def detect_secret_risk_flags(*texts: str | None) -> tuple[str, ...]:
    """Conservative secret/credential detection over candidate persisted text."""
    for text in texts:
        if not text:
            continue
        for pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                return ("secret", "credential")
    return ()


def safe_judge_receipt(outcome: Any, *, role: str) -> dict[str, Any]:
    """Redacted judge receipt safe to persist/replay (O2-03)."""
    return {
        "decision": getattr(outcome, "decision", None),
        "reason": getattr(outcome, "reason", None),
        "reason_category": getattr(outcome, "reason_category", None),
        "policy_version": getattr(outcome, "policy_version", None),
        "session_knowledge_role": role,
    }


def parse_session_knowledge_capture_request(
    raw: Mapping[str, Any],
) -> tuple[SessionKnowledgeCaptureRequest | None, list[SessionKnowledgeIssue]]:
    """Parse and classify a typed session-knowledge capture request.

    Structural/schema issues are fatal (``request is None``). Per-learning
    classification and authority-raising issues are nonfatal: offending
    learnings are dropped, other fields remain, and issues are returned.
    """
    fatal: list[SessionKnowledgeIssue] = []
    soft: list[SessionKnowledgeIssue] = []

    schema_version = raw.get("schema_version")
    if schema_version != SESSION_KNOWLEDGE_CAPTURE_SCHEMA_VERSION:
        fatal.append(
            SessionKnowledgeIssue(
                code="invalid_schema_version",
                field="schema_version",
                message=(
                    "schema_version must be "
                    f"{SESSION_KNOWLEDGE_CAPTURE_SCHEMA_VERSION!r}"
                ),
            )
        )

    session_id = _require_non_empty_str(raw.get("session_id"), "session_id", fatal)
    producer = _optional_non_empty_str(raw.get("producer")) or DEFAULT_PRODUCER
    source_ref = _require_non_empty_str(raw.get("source_ref"), "source_ref", fatal)
    project = _require_non_empty_str(raw.get("project"), "project", fatal)
    root_risk_flags = _parse_risk_flags(raw.get("risk_flags"), "risk_flags", fatal)

    what_happened_raw = raw.get("what_happened")
    what_happened: str | None
    if what_happened_raw is None or what_happened_raw == "":
        what_happened = None
    elif isinstance(what_happened_raw, str):
        what_happened = what_happened_raw.strip() or None
    else:
        fatal.append(
            SessionKnowledgeIssue(
                code="invalid_type",
                field="what_happened",
                message="what_happened must be a string when provided",
            )
        )
        what_happened = None

    if what_happened is not None and len(what_happened) > MAX_WHAT_HAPPENED_CHARS:
        fatal.append(
            SessionKnowledgeIssue(
                code="what_happened_too_long",
                field="what_happened",
                message=(
                    f"what_happened exceeds compactness bound of "
                    f"{MAX_WHAT_HAPPENED_CHARS} characters"
                ),
            )
        )

    decisions = _parse_decisions(raw.get("decisions"), fatal)
    unfinished = _parse_unfinished(raw.get("unfinished_work"), fatal)
    accepted_learnings, learning_hashes, learning_soft = _parse_and_classify_learnings(
        raw.get("what_was_learned"), fatal
    )
    soft.extend(learning_soft)

    submitted_learning_count = len(learning_hashes)
    if (
        len(decisions) > MAX_DECISIONS
        or submitted_learning_count > MAX_LEARNINGS
        or len(unfinished) > MAX_UNFINISHED
    ):
        fatal.append(
            SessionKnowledgeIssue(
                code="too_many_items",
                field="capture",
                message=(
                    f"capture exceeds item bounds "
                    f"(decisions<={MAX_DECISIONS}, learnings<={MAX_LEARNINGS}, "
                    f"unfinished<={MAX_UNFINISHED})"
                ),
            )
        )

    # Identity fields that land in origin/provenance must not carry secrets.
    if detect_secret_risk_flags(source_ref, producer, project):
        fatal.append(
            SessionKnowledgeIssue(
                code="secret_in_capture_field",
                field="source_ref",
                message=(
                    "credential material must not appear in capture identity "
                    "fields (source_ref/producer/project)"
                ),
            )
        )

    if fatal:
        return None, fatal

    assert session_id is not None
    assert source_ref is not None
    assert project is not None
    return (
        SessionKnowledgeCaptureRequest(
            schema_version=SESSION_KNOWLEDGE_CAPTURE_SCHEMA_VERSION,
            session_id=session_id,
            producer=producer,
            source_ref=source_ref,
            project=project,
            what_happened=what_happened,
            decisions=tuple(decisions),
            what_was_learned=tuple(accepted_learnings),
            unfinished_work=tuple(unfinished),
            risk_flags=root_risk_flags,
            submitted_learning_hashes=tuple(learning_hashes),
            classification_issues=tuple(soft),
        ),
        list(soft),
    )


def filter_by_session_knowledge_role(
    memories: Sequence[Memory],
    roles: Iterable[str],
) -> list[Memory]:
    """Return memories whose session_knowledge.role is in ``roles``."""
    wanted = set(roles)
    selected: list[Memory] = []
    for memory in memories:
        role = _role_from_metadata(memory.metadata)
        if role in wanted:
            selected.append(memory)
    return selected


def estimate_capture_write_slots(request: SessionKnowledgeCaptureRequest) -> int:
    """Rows that may be written for capacity reservation (event + children)."""
    needs_event = bool(
        request.what_happened
        or request.decisions
        or request.what_was_learned
    )
    return (1 if needs_event else 0) + len(request.decisions) + len(
        request.what_was_learned
    )


async def capture_session_knowledge(
    raw: Mapping[str, Any],
    *,
    data_layer: SessionKnowledgeDataLayer,
    actor: str,
    capacity_reserve: CapacityReserve | None = None,
    capacity_release: CapacityRelease | None = None,
) -> SessionKnowledgeCaptureResult:
    """Validate, judge, and idempotently persist a session-knowledge capture.

    Recovery/concurrency invariant:
    - Capture identity is unique for the session_event row and includes actor.
    - Each decision/learning has a deterministic ``record_identity``.
    - A capture is complete only after ``capture_status=complete`` and a
      populated ``capture_result`` are written.
    - Incomplete or raced writers resume children by record identity and never
      report a completed replay with empty child IDs.
    - ``UniqueViolationError`` is translated to resume (same fingerprint) or
      ``session_knowledge_capture_conflict`` (different fingerprint).
    - Capacity is reserved only via ``capacity_reserve`` immediately before the
      first write (replay/conflict/empty/rejected consume none).
    - If a reserved capture completes with zero persisted rows, the rate op is
      released via ``capacity_release``. Never release on exceptions or when any
      event/child id is present.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor must be a non-empty string")
    actor = actor.strip()

    request, issues = parse_session_knowledge_capture_request(raw)
    if request is None:
        return SessionKnowledgeCaptureResult(
            status="rejected",
            schema_version=SESSION_KNOWLEDGE_CAPTURE_SCHEMA_VERSION,
            session_event_id=None,
            decision_ids=(),
            learning_ids=(),
            relationship_ids=(),
            unfinished_work=_echo_unfinished(raw.get("unfinished_work")),
            replayed=False,
            issues=tuple(issues),
            judge_outcomes=(),
        )

    soft_issues = tuple(issues)
    identity = capture_identity(
        actor, request.producer, request.source_ref, request.schema_version
    )
    fingerprint = compute_capture_fingerprint(request)
    unfinished_echo = tuple({"text": item.text} for item in request.unfinished_work)

    # Wholly empty capture: persist nothing (O1-13).
    if (
        not request.what_happened
        and not request.decisions
        and not request.what_was_learned
    ):
        return SessionKnowledgeCaptureResult(
            status="captured",
            schema_version=request.schema_version,
            session_event_id=None,
            decision_ids=(),
            learning_ids=(),
            relationship_ids=(),
            unfinished_work=unfinished_echo,
            replayed=False,
            issues=soft_issues,
            judge_outcomes=(),
        )

    prior = await _find_prior_capture(data_layer, identity)
    if prior is not None:
        branched = _branch_on_prior(
            prior,
            request=request,
            fingerprint=fingerprint,
            unfinished_echo=unfinished_echo,
        )
        if branched is not None:
            return branched
        session_event_id = prior.id
    else:
        session_event_id = None

    # Reserve only when a write path will run (O2-05).
    rate_reserved = False
    if capacity_reserve is not None:
        daily_slots = estimate_capture_write_slots(request)
        # Resume may write fewer rows; over-estimate is the safe direction.
        capacity_error = await capacity_reserve(daily_slots=daily_slots)
        if capacity_error is not None:
            raise CaptureCapacityError(capacity_error)
        rate_reserved = True

    def _finish(
        result: SessionKnowledgeCaptureResult,
    ) -> SessionKnowledgeCaptureResult:
        # Release only the rate op when the completed result proves zero writes.
        # Never release for exceptions (they bypass this helper) or for any
        # result that reports a persisted event/child id.
        if (
            rate_reserved
            and capacity_release is not None
            and _is_zero_write_result(result)
        ):
            capacity_release()
        return result

    judge_outcomes: list[dict[str, Any]] = []

    if session_event_id is None:
        event_text = (request.what_happened or "").strip()
        lineage_only = False
        if not event_text:
            # Decisions/learnings without what_happened: lineage anchor only.
            event_text = (
                f"{LINEAGE_ANCHOR_PREFIX}{request.session_id} "
                f"({len(request.decisions)} decisions, "
                f"{len(request.what_was_learned)} learnings)."
            )
            lineage_only = True
        event_record_id = record_identity_for(identity, "session_event", 0, "")
        event_save = await _judge_and_save(
            data_layer=data_layer,
            request=request,
            actor=actor,
            identity=identity,
            fingerprint=fingerprint,
            record_identity=event_record_id,
            role="session_event",
            memory_type="session_event",
            category="observation",
            text=event_text,
            source_label="observed",
            expected_use="evidence",
            retention_scope="session",
            risk_flags=request.risk_flags,
            evidence_ref=request.source_ref,
            judge_outcomes=judge_outcomes,
            capture_status="incomplete",
            skip_secret_scan=lineage_only,
        )
        if event_save is None and not judge_outcomes:
            prior = await _find_prior_capture(data_layer, identity)
            if prior is None:
                return _finish(
                    SessionKnowledgeCaptureResult(
                        status="judged",
                        schema_version=request.schema_version,
                        session_event_id=None,
                        decision_ids=(),
                        learning_ids=(),
                        relationship_ids=(),
                        unfinished_work=unfinished_echo,
                        replayed=False,
                        issues=(),
                        judge_outcomes=tuple(judge_outcomes),
                    )
                )
            branched = _branch_on_prior(
                prior,
                request=request,
                fingerprint=fingerprint,
                unfinished_echo=unfinished_echo,
            )
            if branched is not None:
                return _finish(branched)
            session_event_id = prior.id
        elif event_save is None:
            return _finish(
                SessionKnowledgeCaptureResult(
                    status="judged",
                    schema_version=request.schema_version,
                    session_event_id=None,
                    decision_ids=(),
                    learning_ids=(),
                    relationship_ids=(),
                    unfinished_work=unfinished_echo,
                    replayed=False,
                    issues=(),
                    judge_outcomes=tuple(judge_outcomes),
                )
            )
        else:
            session_event_id = event_save.id

    assert session_event_id is not None

    decision_ids: list[int] = []
    learning_ids: list[int] = []
    relationship_ids: list[int] = []

    for idx, decision in enumerate(request.decisions):
        record_id = record_identity_for(
            identity, "session_decision", idx, decision.text
        )
        # Rationale is judged with the decision text (O1-02); never a bypass path.
        judged_text = decision.text
        if decision.rationale:
            judged_text = f"{decision.text}\n\nRationale: {decision.rationale}"
        saved = await _judge_and_save(
            data_layer=data_layer,
            request=request,
            actor=actor,
            identity=identity,
            fingerprint=fingerprint,
            record_identity=record_id,
            role="session_decision",
            memory_type="decision",
            category="fact",
            text=judged_text,
            source_label="observed",
            expected_use="evidence",
            retention_scope="project",
            risk_flags=decision.risk_flags,
            evidence_ref=request.source_ref,
            judge_outcomes=judge_outcomes,
            extra_scan_texts=(decision.rationale,),
        )
        if saved is None:
            continue
        decision_ids.append(saved.id)
        rel_id = await data_layer.create_relationship(
            saved.id,
            session_event_id,
            DERIVED_FROM_LINK_TYPE,
            metadata={"session_knowledge_role": "session_decision"},
        )
        relationship_ids.append(rel_id)

    for idx, learning in enumerate(request.what_was_learned):
        record_id = record_identity_for(
            identity, "session_learning", idx, learning.text
        )
        saved = await _judge_and_save(
            data_layer=data_layer,
            request=request,
            actor=actor,
            identity=identity,
            fingerprint=fingerprint,
            record_identity=record_id,
            role="session_learning",
            memory_type="learning",
            category="lesson",
            text=learning.text,
            source_label="inferred",
            expected_use="evidence",
            retention_scope="project",
            risk_flags=learning.risk_flags,
            evidence_ref=learning.evidence or request.source_ref,
            judge_outcomes=judge_outcomes,
        )
        if saved is None:
            continue
        learning_ids.append(saved.id)
        rel_id = await data_layer.create_relationship(
            saved.id,
            session_event_id,
            DERIVED_FROM_LINK_TYPE,
            metadata={"session_knowledge_role": "session_learning"},
        )
        relationship_ids.append(rel_id)

    capture_result = {
        "session_event_id": session_event_id,
        "decision_ids": decision_ids,
        "learning_ids": learning_ids,
        "relationship_ids": relationship_ids,
        "unfinished_work": [dict(item) for item in unfinished_echo],
        "judge_outcomes": list(judge_outcomes),
        "issues": [issue.to_dict() for issue in soft_issues],
    }
    finalized = await _finalize_capture_result(
        data_layer=data_layer,
        session_event_id=session_event_id,
        request=request,
        identity=identity,
        fingerprint=fingerprint,
        capture_result=capture_result,
    )
    if not finalized:
        logger.warning(
            "session_knowledge finalize failed for identity=%s event_id=%s; "
            "leaving capture_status=incomplete for resume",
            identity,
            session_event_id,
        )

    return _finish(
        SessionKnowledgeCaptureResult(
            status="captured",
            schema_version=request.schema_version,
            session_event_id=session_event_id,
            decision_ids=tuple(decision_ids),
            learning_ids=tuple(learning_ids),
            relationship_ids=tuple(relationship_ids),
            unfinished_work=unfinished_echo,
            replayed=False,
            issues=soft_issues,
            judge_outcomes=tuple(judge_outcomes),
        )
    )


def _role_from_metadata(metadata: Mapping[str, Any] | None) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    nested = metadata.get("session_knowledge")
    if isinstance(nested, Mapping):
        role = nested.get("role")
        if isinstance(role, str):
            return role
    role = metadata.get("session_knowledge_role")
    return role if isinstance(role, str) else None


def _require_non_empty_str(
    value: Any, field: str, issues: list[SessionKnowledgeIssue]
) -> str | None:
    if not isinstance(value, str) or not value.strip():
        issues.append(
            SessionKnowledgeIssue(
                code="missing_field",
                field=field,
                message=f"{field} must be a non-empty string",
            )
        )
        return None
    return value.strip()


def _optional_non_empty_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_risk_flags(
    raw: Any, field: str, issues: list[SessionKnowledgeIssue]
) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        issues.append(
            SessionKnowledgeIssue(
                code="invalid_type",
                field=field,
                message=f"{field} must be an array of strings",
            )
        )
        return ()
    flags: list[str] = []
    for flag in raw:
        if isinstance(flag, str) and flag.strip():
            flags.append(flag.strip())
    return tuple(flags)


def _parse_decisions(
    raw: Any, issues: list[SessionKnowledgeIssue]
) -> list[SessionDecisionItem]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        issues.append(
            SessionKnowledgeIssue(
                code="invalid_type",
                field="decisions",
                message="decisions must be an array",
            )
        )
        return []
    items: list[SessionDecisionItem] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            issues.append(
                SessionKnowledgeIssue(
                    code="invalid_type",
                    field=f"decisions[{idx}]",
                    message="decision entries must be objects",
                )
            )
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            issues.append(
                SessionKnowledgeIssue(
                    code="missing_field",
                    field=f"decisions[{idx}].text",
                    message="decision text is required",
                )
            )
            continue
        if len(text.strip()) > MAX_ITEM_TEXT_CHARS:
            issues.append(
                SessionKnowledgeIssue(
                    code="item_too_long",
                    field=f"decisions[{idx}].text",
                    message=(
                        f"decision text exceeds {MAX_ITEM_TEXT_CHARS} characters"
                    ),
                )
            )
            continue
        rationale = entry.get("rationale")
        risk_flags = _parse_risk_flags(
            entry.get("risk_flags"), f"decisions[{idx}].risk_flags", issues
        )
        items.append(
            SessionDecisionItem(
                text=text.strip(),
                rationale=rationale.strip()
                if isinstance(rationale, str) and rationale.strip()
                else None,
                risk_flags=risk_flags,
            )
        )
    return items


def _parse_and_classify_learnings(
    raw: Any, fatal: list[SessionKnowledgeIssue]
) -> tuple[list[SessionLearningItem], list[str], list[SessionKnowledgeIssue]]:
    """Parse learnings; return accepted items, all submitted hashes, soft issues."""
    if raw is None:
        return [], [], []
    if not isinstance(raw, list):
        fatal.append(
            SessionKnowledgeIssue(
                code="invalid_type",
                field="what_was_learned",
                message="what_was_learned must be an array",
            )
        )
        return [], [], []
    accepted: list[SessionLearningItem] = []
    hashes: list[str] = []
    soft: list[SessionKnowledgeIssue] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            fatal.append(
                SessionKnowledgeIssue(
                    code="invalid_type",
                    field=f"what_was_learned[{idx}]",
                    message="learning entries must be objects",
                )
            )
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            fatal.append(
                SessionKnowledgeIssue(
                    code="missing_field",
                    field=f"what_was_learned[{idx}].text",
                    message="learning text is required",
                )
            )
            continue
        stripped = text.strip()
        if len(stripped) > MAX_ITEM_TEXT_CHARS:
            fatal.append(
                SessionKnowledgeIssue(
                    code="item_too_long",
                    field=f"what_was_learned[{idx}].text",
                    message=(
                        f"learning text exceeds {MAX_ITEM_TEXT_CHARS} characters"
                    ),
                )
            )
            continue
        # Hash every submitted learning for idempotency (including rejected ones).
        hashes.append(_content_hash(stripped))
        caller_expected = entry.get("expected_use")
        caller_source = entry.get("source_label")
        if caller_expected not in (None, "evidence") or caller_source not in (
            None,
            "inferred",
        ):
            soft.append(
                SessionKnowledgeIssue(
                    code="authority_raising_learning",
                    field=f"what_was_learned[{idx}]",
                    message=(
                        "session learnings are always inferred evidence; "
                        "callers cannot raise source_label or expected_use"
                    ),
                )
            )
            continue
        evidence = entry.get("evidence")
        risk_flags = _parse_risk_flags(
            entry.get("risk_flags"), f"what_was_learned[{idx}].risk_flags", fatal
        )
        item = SessionLearningItem(
            text=stripped,
            evidence=evidence.strip()
            if isinstance(evidence, str) and evidence.strip()
            else None,
            risk_flags=risk_flags,
            expected_use="evidence",
            source_label="inferred",
        )
        class_issues = _classify_learning(item, idx)
        if class_issues:
            soft.extend(class_issues)
            continue
        accepted.append(item)
    return accepted, hashes, soft


def _parse_unfinished(
    raw: Any, issues: list[SessionKnowledgeIssue]
) -> list[UnfinishedWorkItem]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        issues.append(
            SessionKnowledgeIssue(
                code="invalid_type",
                field="unfinished_work",
                message="unfinished_work must be an array",
            )
        )
        return []
    items: list[UnfinishedWorkItem] = []
    for idx, entry in enumerate(raw):
        if isinstance(entry, str) and entry.strip():
            items.append(UnfinishedWorkItem(text=entry.strip()))
            continue
        if not isinstance(entry, Mapping):
            issues.append(
                SessionKnowledgeIssue(
                    code="invalid_type",
                    field=f"unfinished_work[{idx}]",
                    message="unfinished_work entries must be objects or strings",
                )
            )
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            issues.append(
                SessionKnowledgeIssue(
                    code="missing_field",
                    field=f"unfinished_work[{idx}].text",
                    message="unfinished_work text is required",
                )
            )
            continue
        items.append(UnfinishedWorkItem(text=text.strip()))
    return items


def _classify_learning(
    learning: SessionLearningItem, idx: int
) -> list[SessionKnowledgeIssue]:
    text = learning.text
    completed = bool(_COMPLETED_NARRATION_RE.search(text))
    unfinished = bool(_UNFINISHED_WORK_RE.search(text))
    field_name = f"what_was_learned[{idx}]"

    if completed and unfinished:
        return [
            SessionKnowledgeIssue(
                code="conflation",
                field=field_name,
                message=(
                    "learning conflates completed-work narration with unfinished work"
                ),
            )
        ]
    if unfinished:
        return [
            SessionKnowledgeIssue(
                code="unfinished_work_as_learning",
                field=field_name,
                message="unfinished work cannot be stored as durable learning",
            )
        ]
    if completed:
        return [
            SessionKnowledgeIssue(
                code="completed_work_as_learning",
                field=field_name,
                message="completed-work narration cannot be stored as durable learning",
            )
        ]
    return []


def _echo_unfinished(raw: Any) -> tuple[dict[str, str], ...]:
    if not isinstance(raw, list):
        return ()
    items: list[dict[str, str]] = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            items.append({"text": entry.strip()})
        elif isinstance(entry, Mapping):
            text = entry.get("text")
            if isinstance(text, str) and text.strip():
                items.append({"text": text.strip()})
    return tuple(items)


def _is_capture_complete(session_knowledge: Mapping[str, Any]) -> bool:
    if session_knowledge.get("capture_status") != "complete":
        return False
    result = session_knowledge.get("capture_result")
    return isinstance(result, Mapping) and result.get("session_event_id") is not None


def _conflict_result(
    *,
    request: SessionKnowledgeCaptureRequest,
    session_event_id: int | None,
    unfinished_echo: tuple[dict[str, str], ...],
) -> SessionKnowledgeCaptureResult:
    return SessionKnowledgeCaptureResult(
        status="conflict",
        schema_version=request.schema_version,
        session_event_id=session_event_id,
        decision_ids=(),
        learning_ids=(),
        relationship_ids=(),
        unfinished_work=unfinished_echo,
        replayed=False,
        issues=(
            SessionKnowledgeIssue(
                code="session_knowledge_capture_conflict",
                field="source_ref",
                message=(
                    "A semantically different capture already exists for "
                    "this session/source identity"
                ),
            ),
        ),
        judge_outcomes=(),
    )


def _replay_result(
    *,
    request: SessionKnowledgeCaptureRequest,
    prior: Memory,
    unfinished_echo: tuple[dict[str, str], ...],
) -> SessionKnowledgeCaptureResult:
    prior_sk = (prior.metadata or {}).get("session_knowledge") or {}
    stored = prior_sk.get("capture_result") or {}
    stored_outcomes = stored.get("judge_outcomes") or ()
    stored_issues = stored.get("issues") or ()
    replay_issues: list[SessionKnowledgeIssue] = []
    for item in stored_issues:
        if isinstance(item, Mapping) and isinstance(item.get("code"), str):
            replay_issues.append(
                SessionKnowledgeIssue(
                    code=str(item.get("code")),
                    field=str(item.get("field") or ""),
                    message=str(item.get("message") or ""),
                )
            )
    if not replay_issues:
        replay_issues.extend(request.classification_issues)
    return SessionKnowledgeCaptureResult(
        status="replayed",
        schema_version=request.schema_version,
        session_event_id=int(stored.get("session_event_id") or prior.id),
        decision_ids=tuple(int(x) for x in stored.get("decision_ids") or ()),
        learning_ids=tuple(int(x) for x in stored.get("learning_ids") or ()),
        relationship_ids=tuple(int(x) for x in stored.get("relationship_ids") or ()),
        unfinished_work=tuple(
            {"text": str(item.get("text", ""))}
            for item in (stored.get("unfinished_work") or unfinished_echo)
            if isinstance(item, Mapping)
        ),
        replayed=True,
        issues=tuple(replay_issues),
        judge_outcomes=tuple(
            dict(outcome)
            for outcome in stored_outcomes
            if isinstance(outcome, Mapping)
        ),
    )


def _branch_on_prior(
    prior: Memory,
    *,
    request: SessionKnowledgeCaptureRequest,
    fingerprint: str,
    unfinished_echo: tuple[dict[str, str], ...],
) -> SessionKnowledgeCaptureResult | None:
    """Return replay/conflict result, or None when the prior must be resumed."""
    prior_sk = (prior.metadata or {}).get("session_knowledge") or {}
    prior_fp = prior_sk.get("payload_fingerprint")
    if prior_fp != fingerprint:
        return _conflict_result(
            request=request,
            session_event_id=prior.id,
            unfinished_echo=unfinished_echo,
        )
    if _is_capture_complete(prior_sk):
        return _replay_result(
            request=request, prior=prior, unfinished_echo=unfinished_echo
        )
    return None


def _is_unique_violation(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name == "UniqueViolationError":
        return True
    message = str(exc).lower()
    return "unique" in message and "violat" in message


def _is_session_knowledge_identity_violation(exc: BaseException) -> bool:
    """True only for capture/record identity races, not project-index races."""
    if not _is_unique_violation(exc):
        return False
    message = str(exc).lower()
    return "session_knowledge" in message


async def _find_prior_capture(
    data_layer: SessionKnowledgeDataLayer, identity: str
) -> Memory | None:
    result = await data_layer.search(
        SearchParams(
            type="session_event",
            metadata_filter={"session_knowledge_capture_identity": identity},
            limit=5,
        )
    )
    if not result.results:
        return None
    for memory in result.results:
        if _role_from_metadata(memory.metadata) == "session_event":
            return memory
    return result.results[0]


async def _find_by_record_identity(
    data_layer: SessionKnowledgeDataLayer, record_identity: str
) -> Memory | None:
    result = await data_layer.search(
        SearchParams(
            metadata_filter={"session_knowledge_record_identity": record_identity},
            limit=5,
        )
    )
    if not result.results:
        return None
    return result.results[0]


async def _finalize_capture_result(
    *,
    data_layer: SessionKnowledgeDataLayer,
    session_event_id: int,
    request: SessionKnowledgeCaptureRequest,
    identity: str,
    fingerprint: str,
    capture_result: Mapping[str, Any],
) -> bool:
    update = getattr(data_layer, "update_memory", None)
    if not callable(update):
        logger.debug("session_knowledge: data layer lacks update_memory")
        return False
    from open_brain.data_layer.interface import UpdateMemoryParams

    try:
        maybe = update(
            UpdateMemoryParams(
                id=session_event_id,
                metadata={
                    "session_knowledge": {
                        "role": "session_event",
                        "schema_version": request.schema_version,
                        "capture_identity": identity,
                        "payload_fingerprint": fingerprint,
                        "capture_status": "complete",
                        "capture_result": dict(capture_result),
                    },
                    "session_knowledge_capture_identity": identity,
                    "session_knowledge_payload_fingerprint": fingerprint,
                    "session_knowledge_role": "session_event",
                    "session_knowledge_record_identity": record_identity_for(
                        identity, "session_event", 0, ""
                    ),
                },
            )
        )
        if inspect.isawaitable(maybe):
            await maybe
        return True
    except Exception:
        logger.exception(
            "session_knowledge finalize failed event_id=%s", session_event_id
        )
        return False


async def _judge_and_save(
    *,
    data_layer: SessionKnowledgeDataLayer,
    request: SessionKnowledgeCaptureRequest,
    actor: str,
    identity: str,
    fingerprint: str,
    record_identity: str,
    role: str,
    memory_type: str,
    category: str,
    text: str,
    source_label: str,
    expected_use: str,
    retention_scope: str,
    risk_flags: tuple[str, ...],
    evidence_ref: str,
    judge_outcomes: list[dict[str, Any]],
    narrative: str | None = None,
    capture_status: str | None = None,
    extra_scan_texts: tuple[str | None, ...] = (),
    skip_secret_scan: bool = False,
) -> SaveMemoryResult | None:
    existing = await _find_by_record_identity(data_layer, record_identity)
    if existing is not None:
        return SaveMemoryResult(id=existing.id, message="session_knowledge_record_replay")

    # Scan every caller-controlled string that may persist (O2-03).
    detected = () if skip_secret_scan else detect_secret_risk_flags(
        text,
        narrative,
        evidence_ref,
        request.source_ref,
        request.producer,
        request.project,
        *extra_scan_texts,
    )
    merged_flags = tuple(dict.fromkeys((*risk_flags, *detected)))

    # Learnings are always inferred evidence; never raise authority (O1-01).
    if role == "session_learning":
        source_label = "inferred"
        expected_use = "evidence"

    # Never put credential-bearing refs into the proposal citation.
    citation_ref = evidence_ref
    if detect_secret_risk_flags(citation_ref):
        citation_ref = f"conversation://session/{request.session_id}/redacted-evidence"
    elif not (
        citation_ref.startswith(("agent-session:", "policy://", "conversation://"))
        or "://" in citation_ref
        or ":" in citation_ref
    ):
        citation_ref = f"conversation://session/{request.session_id}/evidence"

    proposal = build_memory_write_proposal(
        intended_memory_content=text,
        category=category,  # type: ignore[arg-type]
        source_citation={
            "ref": citation_ref,
            "label": source_label,  # type: ignore[arg-type]
        },
        authorization_basis={
            "ref": DEFAULT_AUTH_REF,
            "label": "observed",
            "granted_by": "system",
        },
        expected_use=expected_use,  # type: ignore[arg-type]
        retention_scope=retention_scope,  # type: ignore[arg-type]
        risk_flags=list(merged_flags),  # type: ignore[arg-type]
    )
    raw_proposal = raw_proposal_payload(proposal)
    outcome = judge_memory_write_proposal(raw_proposal)
    if outcome.decision != "ALLOW":
        judge_outcomes.append(safe_judge_receipt(outcome, role=role))
        return None

    judged_meta = memory_metadata_from_judged_proposal(raw_proposal, outcome)
    session_knowledge: dict[str, Any] = {
        "role": role,
        "schema_version": request.schema_version,
        "capture_identity": identity,
        "payload_fingerprint": fingerprint,
        "record_identity": record_identity,
    }
    if role == "session_event":
        session_knowledge["capture_status"] = capture_status or "incomplete"

    # Keep content-hash uniqueness across sibling records with identical prose.
    storage_text = text
    if role != "session_event":
        storage_text = f"{text}\n\n[session-knowledge-record:{record_identity}]"

    metadata: dict[str, Any] = {
        **judged_meta,
        "category": category,
        "session_knowledge_role": role,
        "session_knowledge_capture_identity": identity,
        "session_knowledge_payload_fingerprint": fingerprint,
        "session_knowledge_record_identity": record_identity,
        "session_knowledge": session_knowledge,
        "capture_template": "session_knowledge",
    }
    metadata = ensure_epistemic_provenance(metadata, allow_instruction=False)

    save_params = SaveMemoryParams(
        text=storage_text,
        type=memory_type,
        project=request.project,
        narrative=None,  # Rationale is already inside judged text when present.
        session_ref=request.session_id,
        metadata=metadata,
        provenance={
            "producer": request.producer,
            "source_ref": request.source_ref,
        },
        user_id=actor,
        instruction_authorized=False,
    )
    result: SaveMemoryResult | None = None
    for attempt in range(3):
        try:
            result = await data_layer.save_memory(save_params)
            break
        except Exception as exc:
            if _is_session_knowledge_identity_violation(exc):
                if role == "session_event":
                    # Caller re-finds the winner and branches on fingerprint.
                    return None
                raced = await _find_by_record_identity(data_layer, record_identity)
                if raced is None:
                    return None
                return SaveMemoryResult(
                    id=raced.id, message="session_knowledge_unique_race"
                )
            if _is_unique_violation(exc) and attempt < 2:
                # Likely concurrent memory_indexes(name) creation; retry.
                continue
            raise
    if result is None:
        return None

    if result.duplicate_of is not None:
        # Content-hash short-circuit: accept only when it is our record.
        dup = await _find_by_record_identity(data_layer, record_identity)
        if dup is not None:
            return SaveMemoryResult(id=dup.id, message="session_knowledge_record_replay")
        if role == "session_event":
            prior = await _find_prior_capture(data_layer, identity)
            if prior is not None:
                return None
        # False content-hash collision with an unrelated memory: disambiguate.
        result = await data_layer.save_memory(
            SaveMemoryParams(
                text=f"{storage_text}\n[sk-disambiguate:{record_identity}]",
                type=memory_type,
                project=request.project,
                narrative=None,
                session_ref=request.session_id,
                metadata=metadata,
                provenance={
                    "producer": request.producer,
                    "source_ref": request.source_ref,
                },
                user_id=actor,
                instruction_authorized=False,
            )
        )
    return result
