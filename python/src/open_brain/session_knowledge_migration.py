"""Legacy session-knowledge -> EKN migration contract (open-brain-ekn.8).

Contract URI: ``standard://open-brain/contracts/legacy-session-knowledge-migration.v1``

Repaired for Kimi K1-01..K1-14: persisted embeddings, measured retrieval controls,
archive-after-verify, readonly CLI, stored baselines, resume/cursor, atomic source
units, parser fidelity, promotion-grant authority, conflict reporting, judged lineage.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

from open_brain.data_layer.interface import (
    OriginProvenanceValidationError,
    build_origin_provenance,
    validate_origin_provenance,
)
from open_brain.epistemic_provenance import (
    EPISTEMIC_PROVENANCE_SCHEMA_VERSION,
    ensure_epistemic_provenance,
)
from open_brain.memory_write_judge import MEMORY_WRITE_JUDGE_POLICY_VERSION
from open_brain.session_knowledge import (
    DERIVED_FROM_LINK_TYPE,
    MAX_DECISIONS,
    MAX_ITEM_TEXT_CHARS,
    MAX_LEARNINGS,
    MAX_UNFINISHED,
    MAX_WHAT_HAPPENED_CHARS,
    SESSION_KNOWLEDGE_CAPTURE_SCHEMA_VERSION,
    record_identity_for,
)

logger = logging.getLogger(__name__)

LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_VERSION = (
    "legacy-session-knowledge-migration.v1"
)
LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_ID = (
    "standard://open-brain/contracts/legacy-session-knowledge-migration.v1"
)
RETRIEVAL_CONTROL_SCHEMA_VERSION = (
    "legacy-session-knowledge-retrieval-controls.v1"
)
MIGRATION_JUDGE_POLICY_VERSION = "legacy-session-knowledge-migration-judge.v1"
DEFAULT_APPLY_LIMIT = 50
MAX_APPLY_LIMIT = 200
MAX_UNRESOLVED_EXAMPLES = 20
MAX_CONFLICT_EXAMPLES = 20
MAX_ERROR_CHARS = 500
MIGRATION_PRODUCER = "legacy-session-knowledge-migration"

FIXED_CONTROL_QUERIES: dict[str, str] = {
    "lexical": "session decisions unfinished work inferred learning archival",
    "vector": "compact observed session evidence and durable inferred learning",
    "rerank": "reversible supersession provenance review ledger catch-up",
}

TransitionRoute = Literal[
    "session_event",
    "session_decision",
    "inferred_learning",
    "unfinished_work",
    "unresolved",
    "quarantine",
]
PlanOutcome = Literal["ok", "unresolved", "quarantine"]

_DECISION_HEADER_RE = re.compile(
    r"(?im)^(?:#{1,6}\s*)?(?:key\s+decisions?|decisions?)\s*:?\s*$"
)
_LEARNING_HEADER_RE = re.compile(
    r"(?im)^(?:#{1,6}\s*)?(?:what\s+was\s+learned|learnings?|lessons?(?:\s+learned)?)\s*:?\s*$"
)
_UNFINISHED_HEADER_RE = re.compile(
    r"(?im)^(?:#{1,6}\s*)?(?:still\s+pending|unfinished\s+work|open\s+items?|todo)\s*:?\s*(?P<rest>.*)$"
)
_OBSERVED_HEADER_RE = re.compile(
    r"(?im)^(?:#{1,6}\s*)?(?:observed|what\s+happened|summary)\s*:?\s*$"
)
_BULLET_RE = re.compile(r"^[ \t]*(?:[-*]|\d+[.)])\s+(?P<text>\S.*)$")
_UNFINISHED_INLINE_RE = re.compile(
    r"(?i)\b(?:still pending|not yet|TODO:|follow-up (?:needed|required)|"
    r"must still|still need(?:s)?|remains pending|remains unfinished)\b"
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class MigrationStore(Protocol):
    async def list_eligible_cohort(self) -> Sequence[Any]: ...
    async def count_already_structured(self) -> int: ...
    async def get_memory(self, memory_id: int) -> Any | None: ...
    async def count_review_ledger(self) -> dict[str, Any]: ...
    async def has_accepted_promotion_grant(self, memory_id: int) -> bool: ...
    async def save_derived(
        self,
        *,
        memory_type: str,
        content: str,
        metadata: dict[str, Any],
        title: str = "",
        embedding: list[float] | None = None,
    ) -> int: ...
    async def update_embedding(self, memory_id: int, embedding: list[float]) -> None: ...
    async def create_relationship(
        self,
        source_id: int,
        target_id: int,
        link_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> int: ...
    async def archive_legacy(
        self, memory_id: int, *, operation_id: str, rollback: dict[str, Any]
    ) -> None: ...
    async def upsert_operation(self, record: dict[str, Any]) -> None: ...
    async def get_operation(self, operation_id: str) -> dict[str, Any] | None: ...
    async def upsert_mapping(self, record: dict[str, Any]) -> None: ...
    async def get_mapping(self, operation_id: str, source_id: int) -> dict[str, Any] | None: ...
    async def list_mappings(self, operation_id: str) -> list[dict[str, Any]]: ...
    async def list_mappings_for_source(self, source_id: int) -> list[dict[str, Any]]: ...
    async def schema_ready(self) -> bool: ...


class EmbedAdapter(Protocol):
    async def embed_documents(
        self, texts: list[str]
    ) -> tuple[list[list[float]], dict[str, Any]]: ...


class RerankAdapter(Protocol):
    async def rerank(
        self, query: str, documents: list[str]
    ) -> tuple[list[int], dict[str, Any]]: ...


class ReconcileAdapter(Protocol):
    async def reconcile(self, memory_ids: list[int]) -> dict[str, Any]: ...


class ControlAdapter(Protocol):
    instrument: str

    async def measure(
        self, *, control: str, query: str, documents: list[str]
    ) -> float: ...


@dataclass(frozen=True)
class TransitionOutput:
    route: TransitionRoute
    content: str
    persist: bool
    epistemic: dict[str, str]
    metadata: dict[str, Any] = field(default_factory=dict)
    memory_type: str | None = None
    role: str | None = None
    preserved_authority: bool = False
    ordinal: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "persist": self.persist,
            "content_hash": _sha256(self.content) if self.content else None,
            "epistemic": dict(self.epistemic),
            "memory_type": self.memory_type,
            "role": self.role,
            "preserved_authority": self.preserved_authority,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True)
class TransitionPlan:
    source_id: int
    source_type: str
    outcome: PlanOutcome
    outputs: tuple[TransitionOutput, ...]
    reason: str
    source_content_hash: str
    origin: dict[str, Any]
    origin_inferred: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "outcome": self.outcome,
            "reason": self.reason,
            "source_content_hash": self.source_content_hash,
            "origin": dict(self.origin),
            "origin_inferred": self.origin_inferred,
            "outputs": [o.to_dict() for o in self.outputs],
        }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def coerce_jsonb_object(value: Any) -> dict[str, Any]:
    """Decode JSONB object values, including legacy double-encoded strings."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(loaded, Mapping):
            return dict(loaded)
        if isinstance(loaded, str):
            # Double-encoded JSON string payload.
            try:
                nested = json.loads(loaded)
            except json.JSONDecodeError:
                return {}
            if isinstance(nested, Mapping):
                return dict(nested)
        return {}
    return {}


def coerce_jsonb_array(value: Any) -> list[Any]:
    """Decode JSONB array values, including legacy double-encoded strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(loaded, list):
            return list(loaded)
        if isinstance(loaded, str):
            try:
                nested = json.loads(loaded)
            except json.JSONDecodeError:
                return []
            if isinstance(nested, list):
                return list(nested)
        return []
    return []


def is_migration_derived_metadata(metadata: Mapping[str, Any] | None) -> bool:
    """True when a memory is an EKN migration output, not a legacy source."""
    if not isinstance(metadata, Mapping):
        return False
    if metadata.get("legacy_session_knowledge_migration_output") is True:
        return True
    rid = metadata.get("session_knowledge_record_identity")
    if isinstance(rid, str) and rid.startswith(f"{MIGRATION_PRODUCER}|"):
        return True
    capture_id = metadata.get("session_knowledge_capture_identity")
    if isinstance(capture_id, str) and capture_id.startswith(f"{MIGRATION_PRODUCER}|"):
        return True
    provenance = metadata.get("provenance")
    if isinstance(provenance, Mapping):
        migration = provenance.get("migration")
        if isinstance(migration, Mapping):
            if (
                migration.get("schema_version")
                == LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_VERSION
            ):
                return True
    judge = metadata.get("migration_judge")
    if isinstance(judge, Mapping):
        if judge.get("policy_version") == MIGRATION_JUDGE_POLICY_VERSION:
            return True
    return False


def is_structured_session_knowledge_metadata(
    metadata: Mapping[str, Any] | None,
) -> bool:
    """Return true for records already produced by session-knowledge-capture.v1."""
    if not isinstance(metadata, Mapping) or is_migration_derived_metadata(metadata):
        return False
    capture_identity = metadata.get("session_knowledge_capture_identity")
    session_knowledge = metadata.get("session_knowledge")
    return bool(
        isinstance(capture_identity, str)
        and capture_identity
        and isinstance(session_knowledge, Mapping)
        and session_knowledge.get("schema_version")
        == SESSION_KNOWLEDGE_CAPTURE_SCHEMA_VERSION
        and session_knowledge.get("role")
    )


def control_instrument(control_adapter: ControlAdapter) -> str:
    instrument = getattr(control_adapter, "instrument", None)
    if not isinstance(instrument, str) or not instrument.strip():
        raise ValueError("retrieval control adapter must declare an instrument")
    return instrument.strip()


def approved_sources_from_report(
    dry_run_report: Mapping[str, Any],
    *,
    existing_approved_pack: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Immutable approved source identity list for apply/resume binding."""
    pack = existing_approved_pack or {}
    stored = pack.get("approved_sources")
    if isinstance(stored, list) and stored:
        return [
            {
                "source_id": int(item["source_id"]),
                "source_type": str(item["source_type"]),
                "source_content_hash": str(item["source_content_hash"]),
            }
            for item in stored
            if isinstance(item, Mapping)
        ]
    sources: list[dict[str, Any]] = []
    for plan in dry_run_report.get("plans") or []:
        if not isinstance(plan, Mapping):
            continue
        sources.append(
            {
                "source_id": int(plan["source_id"]),
                "source_type": str(plan["source_type"]),
                "source_content_hash": str(plan["source_content_hash"]),
            }
        )
    return sources


def compute_report_digest(report: Mapping[str, Any]) -> str:
    payload = {k: v for k, v in report.items() if k != "evidence_digest"}
    return _sha256(_stable_json(payload))


def normalize_operation_id(value: str | None) -> str:
    if value is None:
        raise ValueError("operation_id is required")
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("operation_id must be a UUID") from exc


def _memory_id(memory: Any) -> int:
    return int(getattr(memory, "id"))


def _memory_type(memory: Any) -> str:
    return str(getattr(memory, "type"))


def _memory_content(memory: Any) -> str:
    return str(getattr(memory, "content") or "")


def _memory_metadata(memory: Any) -> dict[str, Any]:
    raw = getattr(memory, "metadata", None)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _bounded_error(message: str, *, code: str) -> dict[str, str]:
    return {"code": code, "message": message[:MAX_ERROR_CHARS]}


def _origin_from_metadata(
    metadata: Mapping[str, Any], memory: Any
) -> tuple[dict[str, Any], bool]:
    provenance = metadata.get("provenance")
    if isinstance(provenance, Mapping):
        origin = provenance.get("origin")
        if isinstance(origin, Mapping):
            producer = origin.get("producer")
            source_ref = origin.get("source_ref")
            if isinstance(producer, str) and isinstance(source_ref, str):
                try:
                    return dict(
                        validate_origin_provenance(
                            {"producer": producer, "source_ref": source_ref}
                        )
                    ), False
                except OriginProvenanceValidationError:
                    return dict(
                        build_origin_provenance(
                            producer,
                            source_ref,
                            default_namespace="legacy-session",
                        )
                    ), True
    source = metadata.get("source")
    session_ref = getattr(memory, "session_ref", None) or metadata.get("session_ref")
    producer = (
        source
        if isinstance(source, str) and source
        else f"{MIGRATION_PRODUCER}:legacy-fallback"
    )
    ref = (
        session_ref
        if isinstance(session_ref, str) and session_ref
        else f"memory:{_memory_id(memory)}"
    )
    return dict(
        build_origin_provenance(
            producer,
            str(ref),
            default_namespace="legacy-session",
        )
    ), True


def _project_from_memory(memory: Any, metadata: Mapping[str, Any]) -> str:
    # Historical records commonly carried their logical project only in
    # metadata while remaining physically attached to the default index.
    # Prefer that explicit legacy value; fall back to the joined index name.
    project = metadata.get("project") or getattr(memory, "project", None)
    if isinstance(project, str) and project.strip():
        return project.strip()
    return "unknown"


def _section_blocks(content: str) -> dict[str, list[str]]:
    lines = content.splitlines()
    buckets: dict[str, list[str]] = {
        "observed": [],
        "decisions": [],
        "learnings": [],
        "unfinished": [],
    }
    current = "observed"
    saw_header = False
    for line in lines:
        stripped = line.strip()
        if _DECISION_HEADER_RE.match(stripped):
            current = "decisions"
            saw_header = True
            continue
        if _LEARNING_HEADER_RE.match(stripped):
            current = "learnings"
            saw_header = True
            continue
        unfinished_header = _UNFINISHED_HEADER_RE.match(stripped)
        if unfinished_header:
            current = "unfinished"
            saw_header = True
            rest = (unfinished_header.group("rest") or "").strip()
            if rest:
                buckets["unfinished"].append(rest)
            continue
        if _OBSERVED_HEADER_RE.match(stripped):
            current = "observed"
            saw_header = True
            continue
        buckets[current].append(line)
    if not saw_header and content.strip():
        buckets["observed"] = lines
    return buckets


def _bullet_or_lines(lines: list[str]) -> list[str]:
    items: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        match = _BULLET_RE.match(line)
        if match:
            items.append(match.group("text").strip())
        elif items:
            items[-1] = f"{items[-1]} {stripped}".strip()
        else:
            items.append(stripped)
    return [item[:MAX_ITEM_TEXT_CHARS] for item in items if item]


def _compact_observed(text: str) -> str:
    return " ".join(text.split())


def _is_unsupported(content: str) -> bool:
    return (not content.strip()) or bool(_CONTROL_CHARS_RE.search(content))


def _epistemic_inferred() -> dict[str, str]:
    return {
        "source_label": "inferred",
        "expected_use": "evidence",
        "epistemic_version": EPISTEMIC_PROVENANCE_SCHEMA_VERSION,
    }


def _epistemic_observed() -> dict[str, str]:
    return {
        "source_label": "observed",
        "expected_use": "evidence",
        "epistemic_version": EPISTEMIC_PROVENANCE_SCHEMA_VERSION,
    }


def _judge_receipt(content: str, *, role: str) -> dict[str, Any]:
    return {
        "receipt": _sha256(f"{MIGRATION_JUDGE_POLICY_VERSION}|{role}|{_sha256(content)}"),
        "policy_version": MIGRATION_JUDGE_POLICY_VERSION,
        "judge_policy_version": MEMORY_WRITE_JUDGE_POLICY_VERSION,
        "content_hash": _sha256(content),
        "decision": "ALLOW",
        "session_knowledge_role": role,
    }


def _base_output_metadata(
    *,
    origin: Mapping[str, Any],
    project: str,
    source_id: int,
    source_type: str,
    role: str,
    epistemic: Mapping[str, str],
    operation_identity: str,
    content: str,
    session_ref: str | None,
    origin_inferred: bool,
) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "origin": dict(origin),
        "source_label": epistemic["source_label"],
        "expected_use": epistemic["expected_use"],
        "epistemic_version": epistemic["epistemic_version"],
        "migration": {
            "schema_version": LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_VERSION,
            "source_memory_id": source_id,
            "source_type": source_type,
            "origin_inferred": origin_inferred,
        },
    }
    metadata: dict[str, Any] = {
        "project": project,
        "provenance": provenance,
        "category": role,
        "session_knowledge": {
            "role": role,
            "schema_version": SESSION_KNOWLEDGE_CAPTURE_SCHEMA_VERSION,
            "capture_identity": operation_identity,
        },
        "session_knowledge_capture_identity": operation_identity,
        "legacy_session_knowledge_migration_output": True,
        "migration_judge": _judge_receipt(content, role=role),
        "content_hash": _sha256(content),
    }
    if session_ref:
        metadata["session_ref"] = session_ref
    return ensure_epistemic_provenance(metadata)


def transform_legacy_memory(
    memory: Any,
    *,
    promotion_grant_accepted: bool = False,
) -> TransitionPlan:
    """Deterministically map one supported legacy memory to EKN routes."""
    source_id = _memory_id(memory)
    source_type = _memory_type(memory)
    content = _memory_content(memory)
    metadata = _memory_metadata(memory)
    origin, origin_inferred = _origin_from_metadata(metadata, memory)
    project = _project_from_memory(memory, metadata)
    content_hash = _sha256(content)
    session_ref = getattr(memory, "session_ref", None) or metadata.get("session_ref")
    if not isinstance(session_ref, str):
        session_ref = None
    identity = (
        f"{MIGRATION_PRODUCER}|legacy:{source_type}:{source_id}|"
        f"{LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_VERSION}"
    )

    if source_type not in {"session_summary", "learning"}:
        return TransitionPlan(
            source_id=source_id,
            source_type=source_type,
            outcome="quarantine",
            outputs=(
                TransitionOutput(
                    route="quarantine",
                    content="",
                    persist=False,
                    epistemic=_epistemic_inferred(),
                ),
            ),
            reason="unsupported_memory_type",
            source_content_hash=content_hash,
            origin=origin,
            origin_inferred=origin_inferred,
        )

    if _is_unsupported(content):
        if _CONTROL_CHARS_RE.search(content):
            unsupported_route: TransitionRoute = "quarantine"
            unsupported_outcome: PlanOutcome = "quarantine"
        else:
            unsupported_route = "unresolved"
            unsupported_outcome = "unresolved"
        return TransitionPlan(
            source_id=source_id,
            source_type=source_type,
            outcome=unsupported_outcome,
            outputs=(
                TransitionOutput(
                    route=unsupported_route,
                    content="",
                    persist=False,
                    epistemic=_epistemic_inferred(),
                ),
            ),
            reason="unsupported_or_empty_shape",
            source_content_hash=content_hash,
            origin=origin,
            origin_inferred=origin_inferred,
        )

    outputs: list[TransitionOutput] = []

    if source_type == "learning":
        prior = metadata.get("provenance")
        preserved = False
        epistemic = _epistemic_inferred()
        downgrade_reason: str | None = None
        if isinstance(prior, Mapping) and prior.get("source_label") == "confirmed":
            if promotion_grant_accepted:
                epistemic = {
                    "source_label": "confirmed",
                    "expected_use": "evidence",
                    "epistemic_version": EPISTEMIC_PROVENANCE_SCHEMA_VERSION,
                }
                preserved = True
            else:
                downgrade_reason = "missing_promotion_grant"
        text = content.strip()[:MAX_ITEM_TEXT_CHARS]
        meta = _base_output_metadata(
            origin=origin,
            project=project,
            source_id=source_id,
            source_type=source_type,
            role="session_learning",
            epistemic=epistemic,
            operation_identity=identity,
            content=text,
            session_ref=session_ref,
            origin_inferred=origin_inferred,
        )
        if downgrade_reason:
            meta["authority_downgrade_reason"] = downgrade_reason
        record_id = record_identity_for(identity, "session_learning", 0, text)
        meta["session_knowledge_record_identity"] = record_id
        outputs.append(
            TransitionOutput(
                route="inferred_learning",
                content=text,
                persist=True,
                epistemic=epistemic,
                metadata=meta,
                memory_type="learning",
                role="session_learning",
                preserved_authority=preserved,
                ordinal=0,
            )
        )
        return TransitionPlan(
            source_id=source_id,
            source_type=source_type,
            outcome="ok",
            outputs=tuple(outputs),
            reason="legacy_learning_normalized",
            source_content_hash=content_hash,
            origin=origin,
            origin_inferred=origin_inferred,
        )

    blocks = _section_blocks(content)
    observed_lines: list[str] = []
    unfinished: list[str] = list(_bullet_or_lines(blocks["unfinished"]))
    for line in blocks["observed"]:
        if _UNFINISHED_INLINE_RE.search(line):
            unfinished.append(line.strip())
        else:
            observed_lines.append(line)
    observed_text = _compact_observed("\n".join(observed_lines).strip())
    decisions = _bullet_or_lines(blocks["decisions"])
    learnings = _bullet_or_lines(blocks["learnings"])

    overflow = False
    if len(observed_text) > MAX_WHAT_HAPPENED_CHARS:
        overflow = True
        observed_text = ""
    if len(decisions) > MAX_DECISIONS:
        overflow = True
        decisions = decisions[:MAX_DECISIONS]
    if len(learnings) > MAX_LEARNINGS:
        overflow = True
        learnings = learnings[:MAX_LEARNINGS]
    if len(unfinished) > MAX_UNFINISHED:
        overflow = True
        unfinished = unfinished[:MAX_UNFINISHED]

    if overflow and not observed_text and not decisions and not learnings:
        return TransitionPlan(
            source_id=source_id,
            source_type=source_type,
            outcome="unresolved",
            outputs=(
                TransitionOutput(
                    route="unresolved",
                    content="",
                    persist=False,
                    epistemic=_epistemic_inferred(),
                ),
            ),
            reason="overflow_or_ambiguous_residue",
            source_content_hash=content_hash,
            origin=origin,
            origin_inferred=origin_inferred,
        )

    if not observed_text and not decisions and not learnings:
        return TransitionPlan(
            source_id=source_id,
            source_type=source_type,
            outcome="unresolved",
            outputs=(
                TransitionOutput(
                    route="unresolved",
                    content="",
                    persist=False,
                    epistemic=_epistemic_inferred(),
                ),
            ),
            reason="no_supported_extractable_evidence",
            source_content_hash=content_hash,
            origin=origin,
            origin_inferred=origin_inferred,
        )

    if overflow and observed_text == "":
        # Overflow of observed body with no other extractables already handled;
        # when decisions/learnings remain, mark unresolved residue without body.
        pass

    if observed_text:
        epistemic = _epistemic_observed()
        meta = _base_output_metadata(
            origin=origin,
            project=project,
            source_id=source_id,
            source_type=source_type,
            role="session_event",
            epistemic=epistemic,
            operation_identity=identity,
            content=observed_text,
            session_ref=session_ref,
            origin_inferred=origin_inferred,
        )
        record_id = record_identity_for(identity, "session_event", 0, observed_text)
        meta["session_knowledge_record_identity"] = record_id
        outputs.append(
            TransitionOutput(
                route="session_event",
                content=observed_text,
                persist=True,
                epistemic=epistemic,
                metadata=meta,
                memory_type="session_event",
                role="session_event",
                ordinal=0,
            )
        )
    elif decisions or learnings:
        anchor = f"Session-knowledge lineage anchor for session legacy:{source_id}"
        epistemic = _epistemic_observed()
        meta = _base_output_metadata(
            origin=origin,
            project=project,
            source_id=source_id,
            source_type=source_type,
            role="session_event",
            epistemic=epistemic,
            operation_identity=identity,
            content=anchor,
            session_ref=session_ref,
            origin_inferred=origin_inferred,
        )
        record_id = record_identity_for(identity, "session_event", 0, anchor)
        meta["session_knowledge_record_identity"] = record_id
        meta["session_knowledge"]["lineage_anchor"] = True
        outputs.append(
            TransitionOutput(
                route="session_event",
                content=anchor,
                persist=True,
                epistemic=epistemic,
                metadata=meta,
                memory_type="session_event",
                role="session_event",
                ordinal=0,
            )
        )

    for idx, text in enumerate(decisions):
        epistemic = _epistemic_observed()
        meta = _base_output_metadata(
            origin=origin,
            project=project,
            source_id=source_id,
            source_type=source_type,
            role="session_decision",
            epistemic=epistemic,
            operation_identity=identity,
            content=text,
            session_ref=session_ref,
            origin_inferred=origin_inferred,
        )
        record_id = record_identity_for(identity, "session_decision", idx, text)
        meta["session_knowledge_record_identity"] = record_id
        outputs.append(
            TransitionOutput(
                route="session_decision",
                content=text,
                persist=True,
                epistemic=epistemic,
                metadata=meta,
                memory_type="decision",
                role="session_decision",
                ordinal=idx,
            )
        )

    for idx, text in enumerate(learnings):
        epistemic = _epistemic_inferred()
        meta = _base_output_metadata(
            origin=origin,
            project=project,
            source_id=source_id,
            source_type=source_type,
            role="session_learning",
            epistemic=epistemic,
            operation_identity=identity,
            content=text,
            session_ref=session_ref,
            origin_inferred=origin_inferred,
        )
        record_id = record_identity_for(identity, "session_learning", idx, text)
        meta["session_knowledge_record_identity"] = record_id
        outputs.append(
            TransitionOutput(
                route="inferred_learning",
                content=text,
                persist=True,
                epistemic=epistemic,
                metadata=meta,
                memory_type="learning",
                role="session_learning",
                ordinal=idx,
            )
        )

    for idx, text in enumerate(unfinished):
        outputs.append(
            TransitionOutput(
                route="unfinished_work",
                content=text,
                persist=False,
                epistemic=_epistemic_inferred(),
                ordinal=idx,
            )
        )

    if overflow:
        outputs.append(
            TransitionOutput(
                route="unresolved",
                content="",
                persist=False,
                epistemic=_epistemic_inferred(),
            )
        )

    outcome: PlanOutcome = "ok"
    reason = "session_summary_transformed"
    if overflow and not any(o.persist for o in outputs):
        outcome = "unresolved"
        reason = "overflow_or_ambiguous_residue"

    return TransitionPlan(
        source_id=source_id,
        source_type=source_type,
        outcome=outcome,
        outputs=tuple(outputs),
        reason=reason,
        source_content_hash=content_hash,
        origin=origin,
        origin_inferred=origin_inferred,
    )


def estimate_provider_usage(
    plans: Sequence[TransitionPlan],
    provider_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    chars_per_token = float(provider_metadata.get("chars_per_token") or 4.0)
    cost_per_1k = float(provider_metadata.get("cost_per_1k_tokens") or 0.0)
    model = str(provider_metadata.get("embedding_model") or "configured")
    dimension = int(provider_metadata.get("embedding_dimension") or 0)
    docs = 0
    chars = 0
    for plan in plans:
        for out in plan.outputs:
            if out.persist and out.content:
                docs += 1
                chars += len(out.content)
    documents = docs * 2
    tokens = int(chars * 2 / chars_per_token) if chars_per_token else 0
    cost = round((tokens / 1000.0) * cost_per_1k, 6)
    return {
        "model": model,
        "dimension": dimension,
        "documents": documents,
        "tokens": tokens,
        "estimated_cost": cost,
        "currency_unit": "configured",
        "scope": "batch_plans",
        # The dry-run records both a full-cohort diagnostic and one control
        # measurement per source with persistable output. The latter keeps
        # bounded apply batches comparable without an unrelated cohort MAX.
        "retrieval_control_measurements": (
            (1 if docs else 0)
            + sum(
                any(out.persist and out.content for out in plan.outputs)
                for plan in plans
            )
        ),
    }


def _cohort_watermark(memories: Sequence[Any]) -> dict[str, Any]:
    ids = sorted(int(getattr(m, "id")) for m in memories)
    return {
        "count": len(ids),
        "min_id": ids[0] if ids else None,
        "max_id": ids[-1] if ids else None,
        "ids_digest": _sha256(_stable_json(ids)),
    }


def _cohort_digest(plans: Sequence[TransitionPlan]) -> str:
    payload = [
        {
            "source_id": p.source_id,
            "source_type": p.source_type,
            "source_content_hash": p.source_content_hash,
            "outcome": p.outcome,
            "routes": [o.route for o in p.outputs],
        }
        for p in plans
    ]
    return _sha256(_stable_json(payload))


async def measure_retrieval_controls(
    store: MigrationStore,
    *,
    plans: Sequence[Any],
    control_adapter: ControlAdapter,
) -> dict[str, Any]:
    """Measure fixed lexical/vector/rerank controls; never trust caller scores."""
    documents: list[str] = []
    for plan in plans:
        if isinstance(plan, TransitionPlan):
            for out in plan.outputs:
                if out.persist and out.content:
                    documents.append(out.content)
        elif isinstance(plan, Mapping):
            for out in plan.get("outputs") or []:
                if out.get("persist") and out.get("content_hash"):
                    # Dry-run plan dicts omit bodies; use hash as opaque stand-in.
                    documents.append(str(out["content_hash"]))
    scores: dict[str, float] = {}
    if documents:
        for name, query in FIXED_CONTROL_QUERIES.items():
            scores[name] = float(
                await control_adapter.measure(
                    control=name, query=query, documents=documents
                )
            )
    else:
        scores = {name: 0.0 for name in FIXED_CONTROL_QUERIES}
    return {
        "schema_version": RETRIEVAL_CONTROL_SCHEMA_VERSION,
        "instrument": control_instrument(control_adapter),
        "queries": dict(FIXED_CONTROL_QUERIES),
        "lexical": scores["lexical"],
        "vector": scores["vector"],
        "rerank": scores["rerank"],
        "digest": _sha256(_stable_json(scores)),
    }


async def dry_run_session_knowledge_migration(
    store: MigrationStore,
    *,
    provider_metadata: Mapping[str, Any],
    control_adapter: ControlAdapter | None = None,
) -> dict[str, Any]:
    """Full-cohort inventory/plan with zero persistent side effects."""
    memories = list(await store.list_eligible_cohort())
    already_structured_count = 0
    if hasattr(store, "count_already_structured"):
        already_structured_count = int(await store.count_already_structured())
    plans: list[TransitionPlan] = []
    for mem in memories:
        grant = False
        if hasattr(store, "has_accepted_promotion_grant"):
            grant = bool(await store.has_accepted_promotion_grant(_memory_id(mem)))
        plans.append(transform_legacy_memory(mem, promotion_grant_accepted=grant))

    route_counts: dict[str, int] = {
        "session_event": 0,
        "session_decision": 0,
        "inferred_learning": 0,
        "unfinished_work": 0,
        "unresolved": 0,
        "quarantine": 0,
    }
    unresolved_examples: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for plan in plans:
        prior = []
        if hasattr(store, "list_mappings_for_source"):
            prior = await store.list_mappings_for_source(plan.source_id)
        for mapping in prior:
            if len(conflicts) < MAX_CONFLICT_EXAMPLES:
                conflicts.append(
                    {
                        "source_id": plan.source_id,
                        "kind": "prior_mapping",
                        "operation_id": mapping.get("operation_id"),
                        "status": mapping.get("status"),
                    }
                )
        if plan.outcome in {"unresolved", "quarantine"}:
            route_counts[plan.outcome] += 1
            if len(unresolved_examples) < MAX_UNRESOLVED_EXAMPLES:
                unresolved_examples.append(
                    {
                        "source_id": plan.source_id,
                        "source_type": plan.source_type,
                        "outcome": plan.outcome,
                        "reason": plan.reason,
                        "source_content_hash": plan.source_content_hash,
                    }
                )
            continue
        for out in plan.outputs:
            route_counts[out.route] = route_counts.get(out.route, 0) + 1

    review_before = await store.count_review_ledger()
    watermark = _cohort_watermark(memories)
    cohort_digest = _cohort_digest(plans)
    proposed_operation_id = str(
        UUID(bytes=hashlib.sha256(cohort_digest.encode()).digest()[:16])
    )
    provider_estimate = estimate_provider_usage(plans, provider_metadata)

    retrieval_baseline: dict[str, Any]
    retrieval_source_baselines: dict[str, dict[str, Any]] = {}
    if control_adapter is not None:
        retrieval_baseline = await measure_retrieval_controls(
            store, plans=plans, control_adapter=control_adapter
        )
        for plan in plans:
            retrieval_source_baselines[str(plan.source_id)] = (
                await measure_retrieval_controls(
                    store,
                    plans=[plan],
                    control_adapter=control_adapter,
                )
            )
    else:
        retrieval_baseline = {
            "schema_version": RETRIEVAL_CONTROL_SCHEMA_VERSION,
            "instrument": None,
            "queries": dict(FIXED_CONTROL_QUERIES),
            "lexical": 0.0,
            "vector": 0.0,
            "rerank": 0.0,
            "digest": _sha256("unmeasured"),
            "unmeasured": True,
        }

    report: dict[str, Any] = {
        "schema_version": LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_VERSION,
        "schema_id": LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_ID,
        "mode": "dry_run",
        "route_counts": route_counts,
        "already_structured_count": already_structured_count,
        "conflicts": conflicts,
        "conflict_count": len(conflicts),
        "unresolved_count": route_counts["unresolved"],
        "quarantine_count": route_counts["quarantine"],
        "unresolved_examples": unresolved_examples,
        "provider_estimate": provider_estimate,
        "cohort_watermark": watermark,
        "cohort_digest": cohort_digest,
        "catch_up_plan": {
            "strategy": "watermark_delta",
            "watermark": watermark,
            "note": "Re-run dry-run after apply to capture rows with id > max_id",
        },
        "proposed_operation_id": proposed_operation_id,
        "review_ledger_before": {
            "count": int(review_before.get("count") or 0),
            "digest": str(review_before.get("digest") or ""),
        },
        "retrieval_control_baseline": retrieval_baseline,
        "retrieval_control_source_baselines": retrieval_source_baselines,
        "plans": [p.to_dict() for p in plans],
    }
    report["evidence_digest"] = compute_report_digest(report)
    return report


def evaluate_migration_gate(
    *,
    decision: str | None,
    dry_run_report: Mapping[str, Any],
    evidence: Mapping[str, Any],
    configured_provider_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = (decision or evidence.get("decision") or "BLOCK")
    if not isinstance(normalized, str):
        normalized = "BLOCK"
    normalized = normalized.upper()
    if normalized not in {"ALLOW", "BLOCK", "REVISE", "ESCALATE"}:
        normalized = "BLOCK"
    if normalized != "ALLOW":
        return {
            "outcome": normalized,
            "writes_authorized": False,
            "reasons": ["decision_is_not_allow"],
        }

    reasons: list[str] = []
    expected_digest = compute_report_digest(dry_run_report)
    provided_digest = evidence.get("dry_run_report_digest")
    if provided_digest != expected_digest and provided_digest != dry_run_report.get(
        "evidence_digest"
    ):
        reasons.append("dry_run_report_digest_mismatch")
    if evidence.get("cohort_digest") != dry_run_report.get("cohort_digest"):
        reasons.append("cohort_digest_mismatch")
    if evidence.get("cohort_watermark") != dry_run_report.get("cohort_watermark"):
        reasons.append("cohort_watermark_mismatch")

    evidence_op = evidence.get("operation_id")
    proposed = dry_run_report.get("proposed_operation_id")
    if not evidence_op:
        reasons.append("operation_id_missing")
    elif proposed and str(evidence_op) != str(proposed):
        # Allow resume with previously approved op id equal to stored; mismatch vs proposed blocks new ops.
        reasons.append("operation_id_mismatch")

    batch_scope = evidence.get("batch_scope")
    if not isinstance(batch_scope, Mapping):
        reasons.append("batch_scope_missing")
    else:
        limit = batch_scope.get("limit")
        if not isinstance(limit, int) or limit < 1 or limit > MAX_APPLY_LIMIT:
            reasons.append("batch_scope_limit_invalid")

    receipt = evidence.get("backup_restore_receipt")
    if not isinstance(receipt, Mapping) or receipt.get("verified") is not True:
        reasons.append("backup_restore_receipt_unverified")

    baseline = evidence.get("retrieval_control_baseline")
    report_baseline = dry_run_report.get("retrieval_control_baseline")
    if not isinstance(baseline, Mapping):
        reasons.append("retrieval_control_baseline_missing")
    elif isinstance(report_baseline, Mapping):
        if report_baseline.get("unmeasured") is True or baseline.get("unmeasured") is True:
            reasons.append("retrieval_control_baseline_unmeasured")
        if not report_baseline.get("instrument") or (
            baseline.get("instrument") != report_baseline.get("instrument")
        ):
            reasons.append("retrieval_control_instrument_mismatch")
        for key in ("lexical", "vector", "rerank"):
            if key in report_baseline and baseline.get(key) != report_baseline.get(key):
                # Allow evidence to carry the report baseline object.
                if baseline.get("digest") != report_baseline.get("digest"):
                    if float(baseline.get(key) or -1) != float(
                        report_baseline.get(key) or -2
                    ):
                        reasons.append("retrieval_control_baseline_mismatch")
                        break

    source_baselines = dry_run_report.get("retrieval_control_source_baselines")
    plans = dry_run_report.get("plans")
    if not isinstance(source_baselines, Mapping) or not isinstance(plans, list):
        reasons.append("retrieval_control_source_baselines_missing")
    else:
        expected_source_ids = {
            str(plan.get("source_id"))
            for plan in plans
            if isinstance(plan, Mapping) and plan.get("source_id") is not None
        }
        if set(source_baselines) != expected_source_ids:
            reasons.append("retrieval_control_source_baselines_mismatch")
        elif isinstance(report_baseline, Mapping):
            expected_instrument = report_baseline.get("instrument")
            if any(
                not isinstance(source_baseline, Mapping)
                or source_baseline.get("unmeasured") is True
                or source_baseline.get("instrument") != expected_instrument
                for source_baseline in source_baselines.values()
            ):
                reasons.append("retrieval_control_source_baselines_invalid")

    if evidence.get("unresolved_acknowledgement") is not True:
        reasons.append("unresolved_acknowledgement_missing")

    conflict_count = int(dry_run_report.get("conflict_count") or 0)
    if conflict_count > 0 and evidence.get("conflict_acknowledgement") is not True:
        reasons.append("conflict_acknowledgement_missing")

    provided_provider = evidence.get("provider_metadata")
    if not isinstance(provided_provider, Mapping):
        reasons.append("provider_metadata_missing")
    elif configured_provider_metadata is not None:
        for key in ("embedding_model", "embedding_dimension", "rerank_model"):
            if provided_provider.get(key) != configured_provider_metadata.get(key):
                reasons.append(f"provider_metadata_mismatch:{key}")

    if reasons:
        return {"outcome": "BLOCK", "writes_authorized": False, "reasons": reasons}
    return {
        "outcome": "ALLOW",
        "writes_authorized": True,
        "reasons": [],
        "batch_scope": dict(batch_scope) if isinstance(batch_scope, Mapping) else {},
        "operation_id": str(evidence_op),
    }


def _source_payload_fingerprint(plan: TransitionPlan) -> str:
    return _sha256(
        _stable_json(
            {
                "source_id": plan.source_id,
                "source_content_hash": plan.source_content_hash,
                "outputs": [o.to_dict() for o in plan.outputs],
            }
        )
    )


async def persist_source_unit(
    store: MigrationStore,
    *,
    operation_id: str,
    plan: TransitionPlan,
    outputs: Sequence[TransitionOutput],
    embeddings: Mapping[int, list[float]],
) -> dict[str, Any]:
    """Persist derived rows, typed lineage, and pre-archive mapping for one source.

    Does not archive the legacy source. Callers archive only after verification.
    """
    source_output_ids: list[int] = []
    event_id: int | None = None
    identity_to_id: dict[str, int] = {}
    for ordinal, out in enumerate(outputs):
        if not out.persist:
            continue
        metadata = dict(out.metadata)
        provenance = metadata.get("provenance")
        if not isinstance(provenance, Mapping):
            raise OriginProvenanceValidationError(
                "derived migration output lacks provenance"
            )
        normalized_origin = validate_origin_provenance(provenance.get("origin"))
        metadata["provenance"] = {
            **dict(provenance),
            "origin": dict(normalized_origin),
        }
        rid = out.metadata.get("session_knowledge_record_identity")
        if isinstance(rid, str) and rid in identity_to_id:
            mid = identity_to_id[rid]
        else:
            mid = await store.save_derived(
                memory_type=out.memory_type or "note",
                content=out.content,
                metadata=metadata,
                title=f"migrated:{out.route}:{plan.source_id}",
                embedding=embeddings.get(ordinal),
            )
            if isinstance(rid, str):
                identity_to_id[rid] = mid
        source_output_ids.append(mid)
        if out.role == "session_event":
            event_id = mid
        # Always link derived output -> legacy source.
        await store.create_relationship(
            mid,
            plan.source_id,
            DERIVED_FROM_LINK_TYPE,
            metadata={
                "migration_operation_id": operation_id,
                "source_memory_id": plan.source_id,
                "edge_role": "legacy_source",
            },
        )
    if event_id is not None:
        for mid, out in zip(
            [i for i, o in zip(source_output_ids, [o for o in outputs if o.persist], strict=False)],
            [o for o in outputs if o.persist],
            strict=False,
        ):
            if out.role in {"session_decision", "session_learning"} and mid != event_id:
                await store.create_relationship(
                    mid,
                    event_id,
                    DERIVED_FROM_LINK_TYPE,
                    metadata={
                        "migration_operation_id": operation_id,
                        "source_memory_id": plan.source_id,
                        "edge_role": "session_event",
                    },
                )
    await store.upsert_mapping(
        {
            "operation_id": operation_id,
            "source_id": plan.source_id,
            "source_type": plan.source_type,
            "source_content_hash": plan.source_content_hash,
            "output_ids": source_output_ids,
            "routes": [o.route for o in outputs],
            "status": "derived_ready",
        }
    )
    return {"output_ids": source_output_ids, "event_id": event_id}


async def apply_session_knowledge_migration_batch(
    store: MigrationStore,
    *,
    dry_run_report: Mapping[str, Any],
    gate_evidence: Mapping[str, Any],
    embed_adapter: EmbedAdapter,
    rerank_adapter: RerankAdapter,
    reconcile_adapter: ReconcileAdapter,
    provider_metadata: Mapping[str, Any],
    control_adapter: ControlAdapter | None = None,
    retrieval_controls: Mapping[str, Any] | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Bounded gated apply. Archives only after verification; never hard-deletes."""
    _ = retrieval_controls  # K1-02: caller-supplied scores are ignored.

    if hasattr(store, "schema_ready") and not await store.schema_ready():
        return {
            "status": "blocked",
            "writes": 0,
            "operation_id": None,
            "output_ids": [],
            "errors": [
                _bounded_error(
                    "migration schema missing; deploy schema before apply",
                    code="schema_missing",
                )
            ],
        }

    if control_adapter is None:
        return {
            "status": "blocked",
            "writes": 0,
            "operation_id": None,
            "output_ids": [],
            "errors": [
                _bounded_error(
                    "operational control_adapter required for apply",
                    code="adapters_unavailable",
                )
            ],
        }

    if embed_adapter is None:
        return {
            "status": "blocked",
            "writes": 0,
            "operation_id": None,
            "output_ids": [],
            "errors": [
                _bounded_error(
                    "operational embed_adapter required for apply",
                    code="adapters_unavailable",
                )
            ],
        }

    evidence_op = gate_evidence.get("operation_id") or operation_id
    try:
        op_id = normalize_operation_id(
            str(evidence_op) if evidence_op else dry_run_report.get("proposed_operation_id")
        )
    except ValueError as exc:
        return {
            "status": "blocked",
            "writes": 0,
            "operation_id": None,
            "output_ids": [],
            "errors": [_bounded_error(str(exc), code="operation_id_invalid")],
        }

    existing = await store.get_operation(op_id)
    evidence_digest = str(
        gate_evidence.get("dry_run_report_digest")
        or dry_run_report.get("evidence_digest")
    )

    if existing and existing.get("evidence_digest") not in {None, evidence_digest}:
        if existing.get("status") in {"running", "completed", "completed_with_errors", "replayed", "failed"}:
            return {
                "status": "conflict",
                "operation_id": op_id,
                "writes": 0,
                "output_ids": list(
                    (existing.get("counters") or {}).get("output_ids") or []
                ),
                "errors": [
                    _bounded_error(
                        "evidence digest mismatch for stable operation id",
                        code="evidence_digest_conflict",
                    )
                ],
            }

    gate = evaluate_migration_gate(
        decision=str(gate_evidence.get("decision") or "BLOCK"),
        dry_run_report=dry_run_report,
        evidence=gate_evidence,
        configured_provider_metadata=provider_metadata,
    )
    # Resume may use stored approved operation id that matches evidence even if
    # proposed_operation_id from a fresh dry-run differs; allow when existing.
    if (
        not gate["writes_authorized"]
        and "operation_id_mismatch" in (gate.get("reasons") or [])
        and existing
        and str(gate_evidence.get("operation_id")) == op_id
    ):
        gate = {
            **gate,
            "writes_authorized": True,
            "outcome": "ALLOW",
            "reasons": [r for r in (gate.get("reasons") or []) if r != "operation_id_mismatch"],
            "batch_scope": gate_evidence.get("batch_scope") or {},
            "operation_id": op_id,
        }
        if gate["reasons"]:
            gate["writes_authorized"] = False
            gate["outcome"] = "BLOCK"

    if not gate["writes_authorized"]:
        return {
            "status": "blocked",
            "gate": gate,
            "writes": 0,
            "operation_id": None,
            "output_ids": [],
            "errors": [
                _bounded_error(
                    ",".join(gate.get("reasons") or ["blocked"]),
                    code="human_decision_gate_blocked",
                )
            ],
        }

    batch_scope = dict(gate.get("batch_scope") or gate_evidence.get("batch_scope") or {})
    limit = int(batch_scope.get("limit") or DEFAULT_APPLY_LIMIT)
    requested_after_id = int(batch_scope.get("after_id") or 0)
    stored_after_id = (
        int(existing.get("cursor") or 0)
        if existing and existing.get("cursor") is not None
        else 0
    )
    after_id = max(requested_after_id, stored_after_id)

    async def _source_hash(source_id: int) -> str | None:
        mem = await store.get_memory(source_id)
        if mem is None:
            return None
        return _sha256(_memory_content(mem))

    if existing and existing.get("status") in {
        "completed",
        "replayed",
        "completed_with_errors",
    }:
        mappings = await store.list_mappings(op_id)
        existing_params = coerce_jsonb_object(existing.get("parameters"))
        existing_pack = coerce_jsonb_object(existing_params.get("approved_pack"))
        terminal_approved_sources = approved_sources_from_report(
            dry_run_report,
            existing_approved_pack=existing_pack,
        )
        terminal_source_ids = {
            int(mapping["source_id"])
            for mapping in mappings
            if mapping.get("status") in {"completed", "unresolved", "quarantine"}
        }
        approved_source_ids = {
            int(source["source_id"]) for source in terminal_approved_sources
        }
        if not approved_source_ids.issubset(terminal_source_ids):
            mappings = []
        if mappings:
            for mapping in mappings:
                current_hash = await _source_hash(int(mapping["source_id"]))
                if (
                    current_hash is not None
                    and mapping.get("source_content_hash")
                    and current_hash != mapping.get("source_content_hash")
                ):
                    return {
                        "status": "conflict",
                        "operation_id": op_id,
                        "writes": 0,
                        "output_ids": list(
                            (existing.get("counters") or {}).get("output_ids") or []
                        ),
                        "errors": [
                            _bounded_error(
                                "payload changed under stable operation/evidence id",
                                code="operation_payload_conflict",
                            )
                        ],
                    }
                for oid in mapping.get("output_ids") or []:
                    if await store.get_memory(int(oid)) is None:
                        return {
                            "status": "conflict",
                            "operation_id": op_id,
                            "writes": 0,
                            "output_ids": [],
                            "errors": [
                                _bounded_error(
                                    "replay output missing",
                                    code="replay_output_missing",
                                )
                            ],
                        }
            replayed_output_ids = sorted(
                {
                    int(oid)
                    for mapping in mappings
                    for oid in (mapping.get("output_ids") or [])
                }
            )
            return {
                "status": "replayed",
                "operation_id": op_id,
                "writes": 0,
                "output_ids": replayed_output_ids,
                "cursor": existing.get("cursor"),
                "counters": existing.get("counters") or {},
                "errors": [],
            }

    existing_params = coerce_jsonb_object((existing or {}).get("parameters"))
    existing_pack = coerce_jsonb_object(existing_params.get("approved_pack"))
    approved_sources = approved_sources_from_report(
        dry_run_report, existing_approved_pack=existing_pack
    )
    if not approved_sources:
        return {
            "status": "blocked",
            "operation_id": op_id,
            "writes": 0,
            "output_ids": [],
            "errors": [
                _bounded_error(
                    "approved dry-run sources missing from report/pack",
                    code="approved_sources_missing",
                )
            ],
        }

    scoped_sources = [
        s for s in approved_sources if int(s["source_id"]) > after_id
    ]
    scoped_sources = sorted(scoped_sources, key=lambda s: int(s["source_id"]))[:limit]

    plans: list[TransitionPlan] = []
    for approved in scoped_sources:
        mem = await store.get_memory(int(approved["source_id"]))
        if mem is None:
            return {
                "status": "conflict",
                "operation_id": op_id,
                "writes": 0,
                "output_ids": [],
                "errors": [
                    _bounded_error(
                        f"approved source {approved['source_id']} missing",
                        code="approved_source_missing",
                    )
                ],
            }
        if _memory_type(mem) != approved["source_type"]:
            return {
                "status": "conflict",
                "operation_id": op_id,
                "writes": 0,
                "output_ids": [],
                "errors": [
                    _bounded_error(
                        f"approved source {approved['source_id']} type changed",
                        code="approved_source_type_conflict",
                    )
                ],
            }
        live_hash = _sha256(_memory_content(mem))
        if live_hash != approved["source_content_hash"]:
            return {
                "status": "conflict",
                "operation_id": op_id,
                "writes": 0,
                "output_ids": [],
                "errors": [
                    _bounded_error(
                        f"source {approved['source_id']} payload changed under operation",
                        code="source_payload_conflict",
                    )
                ],
            }
        grant = False
        if hasattr(store, "has_accepted_promotion_grant"):
            grant = bool(await store.has_accepted_promotion_grant(_memory_id(mem)))
        plan = transform_legacy_memory(mem, promotion_grant_accepted=grant)
        if plan.source_content_hash != approved["source_content_hash"]:
            return {
                "status": "conflict",
                "operation_id": op_id,
                "writes": 0,
                "output_ids": [],
                "errors": [
                    _bounded_error(
                        f"source {approved['source_id']} transform hash drift",
                        code="source_payload_conflict",
                    )
                ],
            }
        plans.append(plan)

    approved_ids = {int(s["source_id"]) for s in approved_sources}
    live_cohort = list(await store.list_eligible_cohort())
    catch_up_ids = sorted(
        int(getattr(m, "id"))
        for m in live_cohort
        if int(getattr(m, "id")) not in approved_ids
    )
    catch_up_delta = {
        "remaining_count": len(catch_up_ids),
        "remaining_ids_digest": _sha256(_stable_json(catch_up_ids)),
        "ignored_under_operation": True,
    }

    approved_baseline = gate_evidence.get("retrieval_control_baseline") or dry_run_report.get(
        "retrieval_control_baseline"
    )
    approved_source_baselines = dry_run_report.get(
        "retrieval_control_source_baselines"
    )
    approved_pack = {
        "cohort_digest": dry_run_report.get("cohort_digest"),
        "cohort_watermark": dry_run_report.get("cohort_watermark"),
        "review_ledger_before": dry_run_report.get("review_ledger_before"),
        "retrieval_control_baseline": approved_baseline,
        "retrieval_control_source_baselines": approved_source_baselines,
        "evidence_digest": evidence_digest,
        "approved_sources": approved_sources,
        "catch_up_delta": catch_up_delta,
    }

    prior_errors = list(existing_params.get("prior_errors") or [])
    if existing and existing.get("error"):
        prior_errors.append(
            {
                "status": existing.get("status"),
                "error": existing.get("error"),
            }
        )
    # Preserve cumulative counters from the prior attempt (including failures).
    resume_counters = dict((existing or {}).get("counters") or {})

    await store.upsert_operation(
        {
            "operation_id": op_id,
            "status": "running",
            "parameters": {
                "batch_scope": {"limit": limit, "after_id": after_id},
                "cohort_digest": dry_run_report.get("cohort_digest"),
                "schema_version": LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_VERSION,
                "approved_pack": approved_pack,
                "prior_errors": prior_errors,
            },
            "evidence_digest": evidence_digest,
            "cursor": str(after_id),
            "counters": resume_counters,
            "provider_metadata": dict(provider_metadata),
            "approved_baseline": approved_baseline,
            "error": None,
        }
    )

    output_ids: list[int] = []
    source_ids: list[int] = []
    batch_source_ids: list[int] = []
    errors: list[dict[str, str]] = []
    prior_provider_metrics = resume_counters.get("provider_metrics")
    if not isinstance(prior_provider_metrics, Mapping):
        prior_provider_metrics = {}
    phase_metrics: dict[str, Any] = {
        "tokens": int(prior_provider_metrics.get("tokens") or 0),
        "documents": int(prior_provider_metrics.get("documents") or 0),
        "phases": list(prior_provider_metrics.get("phases") or []),
    }
    created_ids: list[int] = []
    pending_archive: list[tuple[TransitionPlan, list[int]]] = []

    try:
        if not isinstance(approved_source_baselines, Mapping):
            raise ValueError("retrieval_control_source_baselines_missing")
        source_baselines = []
        for plan in plans:
            source_baseline = approved_source_baselines.get(str(plan.source_id))
            if not isinstance(source_baseline, Mapping):
                raise ValueError(
                    f"retrieval_control_source_baseline_missing:{plan.source_id}"
                )
            source_baselines.append(source_baseline)
        batch_baseline = {
            "instrument": (approved_baseline or {}).get("instrument"),
            **{
                name: max(
                    (float(baseline.get(name) or 0.0) for baseline in source_baselines),
                    default=0.0,
                )
                for name in ("lexical", "vector", "rerank")
            },
        }
        if any(
            baseline.get("instrument") != batch_baseline["instrument"]
            for baseline in source_baselines
        ):
            raise RuntimeError("retrieval_control_instrument_mismatch")
        if batch_baseline.get("instrument") != (
            approved_baseline or {}
        ).get("instrument"):
            raise RuntimeError("retrieval_control_instrument_mismatch")

        for plan in plans:
            prior = await store.get_mapping(op_id, plan.source_id)
            if prior and prior.get("status") == "completed":
                for oid in prior.get("output_ids") or []:
                    output_ids.append(int(oid))
                source_ids.append(plan.source_id)
                continue
            if prior and prior.get("status") == "derived_ready":
                oids = [int(x) for x in prior.get("output_ids") or []]
                output_ids.extend(oids)
                source_ids.append(plan.source_id)
                batch_source_ids.append(plan.source_id)
                pending_archive.append((plan, oids))
                continue
            if plan.outcome != "ok":
                await store.upsert_mapping(
                    {
                        "operation_id": op_id,
                        "source_id": plan.source_id,
                        "source_type": plan.source_type,
                        "source_content_hash": plan.source_content_hash,
                        "output_ids": [],
                        "routes": [plan.outcome],
                        "status": plan.outcome,
                    }
                )
                continue

            persistable = [o for o in plan.outputs if o.persist]
            texts = [o.content for o in persistable]
            vectors: list[list[float]] = []
            if texts:
                vectors, usage = await embed_adapter.embed_documents(texts)
                phase_metrics["phases"].append("embed")
                phase_metrics["tokens"] += int(usage.get("tokens") or 0)
                phase_metrics["documents"] += int(usage.get("documents") or 0)
                if len(vectors) != len(texts):
                    raise RuntimeError("embed adapter arity mismatch")
            emb_map = {i: vectors[i] for i in range(len(vectors))}
            unit = await persist_source_unit(
                store,
                operation_id=op_id,
                plan=plan,
                outputs=persistable,
                embeddings=emb_map,
            )
            oids = [int(x) for x in unit["output_ids"]]
            created_ids.extend(oids)
            output_ids.extend(oids)
            source_ids.append(plan.source_id)
            batch_source_ids.append(plan.source_id)
            pending_archive.append((plan, oids))

        if created_ids or pending_archive:
            scoped = sorted(set(created_ids + [i for _, ids in pending_archive for i in ids]))
            await reconcile_adapter.reconcile(scoped)
            phase_metrics["phases"].append("reconcile")

            # Final embed: actual current content of derived rows.
            contents: list[str] = []
            ids_for_embed: list[int] = []
            for mid in scoped:
                mem = await store.get_memory(mid)
                if mem is None:
                    continue
                contents.append(_memory_content(mem))
                ids_for_embed.append(mid)
            if contents:
                vectors2, usage2 = await embed_adapter.embed_documents(contents)
                if len(vectors2) != len(ids_for_embed):
                    raise RuntimeError("final embed adapter arity mismatch")
                phase_metrics["phases"].append("embed")
                phase_metrics["tokens"] += int(usage2.get("tokens") or 0)
                phase_metrics["documents"] += int(usage2.get("documents") or 0)
                for mid, vec in zip(ids_for_embed, vectors2, strict=False):
                    if hasattr(store, "update_embedding"):
                        await store.update_embedding(mid, vec)

            # Measured retrieval controls vs approved baseline (ignore caller scores).
            measured = await measure_retrieval_controls(
                store,
                plans=[p for p, _ in pending_archive],
                control_adapter=control_adapter,
            )
            derived_docs = contents or ["empty"]
            phase_metrics["phases"].append("rerank")
            await rerank_adapter.rerank(
                FIXED_CONTROL_QUERIES["rerank"], derived_docs
            )

            baseline = approved_baseline or {}
            if measured.get("instrument") != baseline.get("instrument"):
                raise RuntimeError("retrieval_control_instrument_mismatch")
            for name in ("lexical", "vector", "rerank"):
                base = float(batch_baseline.get(name) or 0.0)
                score = float(measured.get(name) or 0.0)
                if score < base:
                    raise RuntimeError(
                        f"retrieval_control_failed:{name}:{score}<{base}"
                    )

            # Archive only after successful verification.
            for plan, oids in pending_archive:
                mem = await store.get_memory(plan.source_id)
                prev_status = _memory_metadata(mem).get("status") if mem else None
                await store.archive_legacy(
                    plan.source_id,
                    operation_id=op_id,
                    rollback={
                        "previous_status": prev_status,
                        "source_content_hash": plan.source_content_hash,
                        "output_ids": list(oids),
                        "reversible": True,
                        "owning_operation_id": op_id,
                    },
                )
                await store.upsert_mapping(
                    {
                        "operation_id": op_id,
                        "source_id": plan.source_id,
                        "source_type": plan.source_type,
                        "source_content_hash": plan.source_content_hash,
                        "output_ids": oids,
                        "routes": [o.route for o in plan.outputs],
                        "status": "completed",
                    }
                )

        status = "running"
    except Exception as exc:  # noqa: BLE001 - bounded terminal failure
        msg = f"{type(exc).__name__}: {exc}"
        code = "apply_phase_failed"
        if "retrieval_control_failed" in str(exc):
            code = "retrieval_control_failed"
            status = "failed"
        else:
            status = "failed"
        errors.append(_bounded_error(msg, code=code))
        await store.upsert_operation(
            {
                "operation_id": op_id,
                "status": "failed",
                "parameters": {
                    "batch_scope": {"limit": limit, "after_id": after_id},
                    "cohort_digest": dry_run_report.get("cohort_digest"),
                    "schema_version": LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_VERSION,
                    "approved_pack": approved_pack,
                    "prior_errors": prior_errors,
                },
                "evidence_digest": evidence_digest,
                "cursor": str(after_id),
                "counters": {
                    "source_ids": source_ids,
                    "output_ids": sorted(set(output_ids)),
                    "source_count": len(set(source_ids)),
                    "output_count": len(set(output_ids)),
                    "error_count": len(errors),
                    "provider_metrics": phase_metrics,
                },
                "provider_metadata": dict(provider_metadata),
                "approved_baseline": approved_baseline,
                "error": errors[0]["message"],
            }
        )
        return {
            "status": "failed",
            "operation_id": op_id,
            "writes": len(created_ids),
            "output_ids": sorted(set(output_ids)),
            "source_ids": source_ids,
            "cursor": str(after_id),
            "catch_up_delta": catch_up_delta,
            "errors": errors,
            "gate": gate,
        }

    # Cumulative counters from mappings.
    mappings = await store.list_mappings(op_id)
    all_outputs = sorted(
        {int(oid) for m in mappings for oid in (m.get("output_ids") or [])}
    )
    all_sources = sorted({int(m["source_id"]) for m in mappings})
    terminal_source_ids = {
        int(mapping["source_id"])
        for mapping in mappings
        if mapping.get("status") in {"completed", "unresolved", "quarantine"}
    }
    status = "completed" if approved_ids.issubset(terminal_source_ids) else "running"
    cursor = str(
        max((int(p.source_id) for p in plans), default=after_id)
    )
    counters = {
        "source_ids": all_sources,
        "output_ids": all_outputs,
        "source_count": len(all_sources),
        "output_count": len(all_outputs),
        "error_count": len(errors),
        "payload_fingerprint": _sha256(
            _stable_json([_source_payload_fingerprint(p) for p in plans])
        ),
        "provider_metrics": phase_metrics,
        "provider_metadata": {
            "embedding_model": provider_metadata.get("embedding_model"),
            "rerank_model": provider_metadata.get("rerank_model"),
            "embedding_dimension": provider_metadata.get("embedding_dimension"),
        },
    }
    await store.upsert_operation(
        {
            "operation_id": op_id,
            "status": status,
            "parameters": {
                "batch_scope": {"limit": limit, "after_id": after_id},
                "cohort_digest": dry_run_report.get("cohort_digest"),
                "schema_version": LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_VERSION,
                "approved_pack": approved_pack,
                "prior_errors": prior_errors,
            },
            "evidence_digest": evidence_digest,
            "cursor": cursor,
            "counters": counters,
            "provider_metadata": dict(provider_metadata),
            "approved_baseline": approved_baseline,
            "error": None,
        }
    )
    return {
        "status": status,
        "operation_id": op_id,
        "writes": len(created_ids),
        # Include resumed derived_ready outputs, not only newly inserted rows.
        "output_ids": all_outputs,
        "source_ids": sorted(set(source_ids) | set(batch_source_ids)),
        "cursor": cursor,
        "catch_up_delta": catch_up_delta,
        "counters": counters,
        "errors": errors,
        "gate": gate,
    }


async def reconcile_session_knowledge_migration(
    store: MigrationStore,
    *,
    operation_id: str,
    dry_run_report: Mapping[str, Any] | None = None,
    report_file_digest: str | None = None,
) -> dict[str, Any]:
    op_id = normalize_operation_id(operation_id)
    operation = await store.get_operation(op_id)
    if operation is None:
        return {
            "schema_version": LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_VERSION,
            "operation_id": op_id,
            "baseline_missing": True,
            "rollback_ready": False,
        }

    params = operation.get("parameters") or {}
    approved_pack = params.get("approved_pack") or {}
    stored_baseline = operation.get("approved_baseline") or approved_pack.get(
        "retrieval_control_baseline"
    )
    if not stored_baseline:
        return {
            "schema_version": LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_VERSION,
            "operation_id": op_id,
            "baseline_missing": True,
            "rollback_ready": False,
        }

    if dry_run_report is not None and report_file_digest is not None:
        if compute_report_digest(dry_run_report) != report_file_digest:
            return {
                "schema_version": LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_VERSION,
                "operation_id": op_id,
                "baseline_mismatch": True,
                "rollback_ready": False,
            }

    review_before = approved_pack.get("review_ledger_before") or (
        (dry_run_report or {}).get("review_ledger_before") or {}
    )
    review_after = await store.count_review_ledger()
    mappings = await store.list_mappings(op_id)
    output_ids: list[int] = []
    source_ids: list[int] = []
    lineage_closed = True
    for mapping in mappings:
        source_ids.append(int(mapping["source_id"]))
        outs = [int(x) for x in (mapping.get("output_ids") or [])]
        output_ids.extend(outs)
        for oid in outs:
            mem = await store.get_memory(oid)
            if mem is None:
                lineage_closed = False

    missing_embeddings = 0
    for oid in set(output_ids):
        mem = await store.get_memory(oid)
        if mem is None or getattr(mem, "embedding", None) is None:
            missing_embeddings += 1

    watermark = approved_pack.get("cohort_watermark") or {}
    max_id = watermark.get("max_id")
    remaining = [
        m
        for m in await store.list_eligible_cohort()
        if max_id is None or int(getattr(m, "id")) > int(max_id)
    ]
    review_preserved = (
        int(review_after.get("count") or 0) == int(review_before.get("count") or 0)
        and str(review_after.get("digest") or "")
        == str(review_before.get("digest") or "")
    )
    rollback_ready = bool(
        operation is not None
        and operation.get("status") in {"completed", "replayed"}
        and lineage_closed
        and review_preserved
        and missing_embeddings == 0
    )
    return {
        "schema_version": LEGACY_SESSION_KNOWLEDGE_MIGRATION_SCHEMA_VERSION,
        "operation_id": op_id,
        "operation_status": operation.get("status"),
        "baseline_missing": False,
        "baseline_used": stored_baseline,
        "source_count": len(set(source_ids)),
        "output_count": len(set(output_ids)),
        "lineage_closed": lineage_closed,
        "review_ledger": {
            "count": int(review_after.get("count") or 0),
            "digest": str(review_after.get("digest") or ""),
            "preserved": review_preserved,
            "delta_count": int(review_after.get("count") or 0)
            - int(review_before.get("count") or 0),
        },
        "embedding_coverage": {
            "output_count": len(set(output_ids)),
            "missing": missing_embeddings,
        },
        "catch_up_delta": {
            "remaining_count": len(remaining),
            "remaining_ids_digest": _sha256(
                _stable_json(sorted(int(getattr(m, "id")) for m in remaining))
            ),
        },
        "rollback_ready": rollback_ready,
    }


async def migration_operation_status(
    store: MigrationStore, operation_id: str
) -> dict[str, Any]:
    op_id = normalize_operation_id(operation_id)
    operation = await store.get_operation(op_id)
    if operation is None:
        return {"status": "not_found", "operation_id": op_id}
    mappings = await store.list_mappings(op_id)
    return {
        "status": operation.get("status"),
        "operation_id": op_id,
        "cursor": operation.get("cursor"),
        "counters": operation.get("counters") or {},
        "evidence_digest": operation.get("evidence_digest"),
        "approved_baseline": operation.get("approved_baseline"),
        "mapping_count": len(mappings),
        "error": operation.get("error"),
    }


class PostgresMigrationStore:
    """Runtime store; CLI paths must construct with run_migrations=False."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @property
    def pool(self) -> Any:
        return self._pool

    async def schema_ready(self) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                """
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_schema='public'
                  AND table_name IN (
                    'session_knowledge_migration_operations',
                    'session_knowledge_migration_mappings'
                  )
                """
            )
        return int(row or 0) == 2

    async def list_eligible_cohort(self) -> list[Any]:
        from types import SimpleNamespace

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT m.id, m.type, m.content, m.title, m.metadata,
                       m.session_ref, m.embedding, mi.name AS project
                  FROM memories AS m
                  LEFT JOIN memory_indexes AS mi ON mi.id = m.index_id
                 WHERE m.type IN ('session_summary', 'learning')
                   AND COALESCE(m.metadata->>'status', '') <> 'archived'
                   AND m.metadata #>> '{session_knowledge_migration,superseded_by_operation}'
                       IS NULL
                   AND COALESCE(
                         (m.metadata->>'legacy_session_knowledge_migration_output')::boolean,
                         false
                       ) IS NOT TRUE
                   AND m.metadata #>> '{provenance,migration,schema_version}' IS NULL
                   AND COALESCE(m.metadata->>'session_knowledge_record_identity', '')
                       NOT LIKE $1
                   AND m.metadata->>'session_knowledge_capture_identity' IS NULL
                   AND m.metadata #>> '{session_knowledge,role}' IS NULL
                 ORDER BY m.id ASC
                """,
                f"{MIGRATION_PRODUCER}|%",
            )
        results: list[Any] = []
        for r in rows:
            metadata = coerce_jsonb_object(r["metadata"])
            if is_migration_derived_metadata(metadata) or is_structured_session_knowledge_metadata(metadata):
                continue
            results.append(
                SimpleNamespace(
                    id=int(r["id"]),
                    type=str(r["type"]),
                    content=str(r["content"] or ""),
                    title=str(r["title"] or ""),
                    metadata=metadata,
                    session_ref=r["session_ref"],
                    embedding=r["embedding"],
                    project=r["project"],
                )
            )
        return results

    async def count_already_structured(self) -> int:
        async with self._pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*)
                  FROM memories
                 WHERE type IN ('session_summary', 'learning')
                   AND COALESCE(metadata->>'status', '') <> 'archived'
                   AND metadata->>'session_knowledge_capture_identity' IS NOT NULL
                   AND metadata #>> '{session_knowledge,schema_version}' = $1
                   AND metadata #>> '{session_knowledge,role}' IS NOT NULL
                   AND COALESCE(
                         (metadata->>'legacy_session_knowledge_migration_output')::boolean,
                         false
                       ) IS NOT TRUE
                """,
                SESSION_KNOWLEDGE_CAPTURE_SCHEMA_VERSION,
            )
        return int(count or 0)

    async def get_memory(self, memory_id: int) -> Any | None:
        from types import SimpleNamespace

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT m.id, m.type, m.content, m.title, m.metadata,
                       m.session_ref, m.embedding, mi.name AS project
                  FROM memories AS m
                  LEFT JOIN memory_indexes AS mi ON mi.id = m.index_id
                 WHERE m.id = $1
                """,
                memory_id,
            )
        if row is None:
            return None
        return SimpleNamespace(
            id=int(row["id"]),
            type=str(row["type"]),
            content=str(row["content"] or ""),
            title=str(row["title"] or ""),
            metadata=coerce_jsonb_object(row["metadata"]),
            session_ref=row["session_ref"],
            embedding=row["embedding"],
            project=row["project"],
        )

    async def count_review_ledger(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, review_key, decision, created_at
                  FROM session_learning_reviews
                 ORDER BY id ASC
                """
            )
        payload = [
            {
                "id": int(r["id"]),
                "review_key": str(r["review_key"]),
                "decision": str(r["decision"]),
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]
        return {"count": len(payload), "digest": _sha256(_stable_json(payload))}

    async def has_accepted_promotion_grant(self, memory_id: int) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                """
                SELECT 1 FROM memory_promotion_events
                 WHERE memory_id = $1
                   AND decision = 'accepted'
                   AND target_state = 'confirmed'
                   AND grant_jti IS NOT NULL
                 LIMIT 1
                """,
                memory_id,
            )
        return row is not None

    async def save_derived(
        self,
        *,
        memory_type: str,
        content: str,
        metadata: dict[str, Any],
        title: str = "",
        embedding: list[float] | None = None,
    ) -> int:
        from open_brain.data_layer.postgres import to_pg_vector

        async with self._pool.acquire() as conn:
            project = metadata.get("project")
            index_id = 1
            if isinstance(project, str) and project.strip() and project != "unknown":
                project_row = await conn.fetchrow(
                    "SELECT id FROM memory_indexes WHERE name = $1",
                    project.strip(),
                )
                if project_row is None:
                    project_row = await conn.fetchrow(
                        "INSERT INTO memory_indexes (name) VALUES ($1) RETURNING id",
                        project.strip(),
                    )
                index_id = int(project_row["id"])
            rid = metadata.get("session_knowledge_record_identity")
            if isinstance(rid, str) and rid:
                existing = await conn.fetchval(
                    """
                    SELECT id FROM memories
                     WHERE metadata->>'session_knowledge_record_identity' = $1
                     LIMIT 1
                    """,
                    rid,
                )
                if existing is not None:
                    if embedding is not None:
                        await conn.execute(
                            """
                            UPDATE memories
                               SET index_id = $2,
                                   embedding = $3::vector,
                                   updated_at = NOW()
                             WHERE id = $1
                            """,
                            int(existing),
                            index_id,
                            to_pg_vector(embedding),
                        )
                    else:
                        await conn.execute(
                            """
                            UPDATE memories
                               SET index_id = $2, updated_at = NOW()
                             WHERE id = $1
                            """,
                            int(existing),
                            index_id,
                        )
                    return int(existing)
            row = await conn.fetchrow(
                """
                INSERT INTO memories (
                    index_id, type, title, content, metadata, priority, embedding
                )
                VALUES ($6, $1, $2, $3, $4::jsonb, 0.5, $5::vector)
                RETURNING id
                """,
                memory_type,
                title,
                content,
                dict(metadata),
                to_pg_vector(embedding) if embedding is not None else None,
                index_id,
            )
        return int(row["id"])

    async def update_embedding(self, memory_id: int, embedding: list[float]) -> None:
        from open_brain.data_layer.postgres import to_pg_vector

        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE memories SET embedding = $2::vector, updated_at = NOW() WHERE id = $1",
                memory_id,
                to_pg_vector(embedding),
            )

    async def create_relationship(
        self,
        source_id: int,
        target_id: int,
        link_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO memory_relationships (
                    source_id, target_id, relation_type, link_type, metadata
                ) VALUES ($1, $2, $3, $3, $4::jsonb)
                ON CONFLICT (source_id, target_id, relation_type) DO UPDATE
                  SET metadata = EXCLUDED.metadata
                RETURNING id
                """,
                source_id,
                target_id,
                link_type,
                dict(metadata or {}),
            )
        return int(row["id"])

    async def archive_legacy(
        self, memory_id: int, *, operation_id: str, rollback: dict[str, Any]
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE memories
                   SET metadata = jsonb_set(
                         jsonb_set(
                           COALESCE(metadata, '{}'::jsonb),
                           '{status}',
                           '"archived"'::jsonb,
                           true
                         ),
                         '{session_knowledge_migration}',
                         $2::jsonb,
                         true
                       ),
                       updated_at = NOW()
                 WHERE id = $1
                """,
                memory_id,
                {
                    "superseded_by_operation": operation_id,
                    "rollback": dict(rollback),
                },
            )

    async def upsert_operation(self, record: dict[str, Any]) -> None:
        parameters = {
            **dict(record.get("parameters") or {}),
            "approved_baseline": record.get("approved_baseline"),
        }
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO session_knowledge_migration_operations (
                    operation_id, status, parameters, evidence_digest,
                    cursor, counters, provider_metadata, error, updated_at,
                    completed_at
                ) VALUES (
                    $1::uuid, $2, $3::jsonb, $4, $5, $6::jsonb, $7::jsonb, $8,
                    NOW(),
                    CASE WHEN $2 IN (
                        'completed','failed','replayed','completed_with_errors'
                    ) THEN NOW() ELSE NULL END
                )
                ON CONFLICT (operation_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    parameters = EXCLUDED.parameters,
                    evidence_digest = EXCLUDED.evidence_digest,
                    cursor = EXCLUDED.cursor,
                    counters = EXCLUDED.counters,
                    provider_metadata = EXCLUDED.provider_metadata,
                    error = EXCLUDED.error,
                    updated_at = NOW(),
                    completed_at = EXCLUDED.completed_at
                """,
                record["operation_id"],
                record["status"],
                parameters,
                record.get("evidence_digest"),
                record.get("cursor"),
                dict(record.get("counters") or {}),
                dict(record.get("provider_metadata") or {}),
                record.get("error"),
            )

    async def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT operation_id, status, parameters, evidence_digest,
                       cursor, counters, provider_metadata, error
                  FROM session_knowledge_migration_operations
                 WHERE operation_id = $1::uuid
                """,
                operation_id,
            )
        if row is None:
            return None
        parameters = coerce_jsonb_object(row["parameters"])
        counters = coerce_jsonb_object(row["counters"])
        provider_metadata = coerce_jsonb_object(row["provider_metadata"])
        return {
            "operation_id": str(row["operation_id"]),
            "status": str(row["status"]),
            "parameters": parameters,
            "evidence_digest": row["evidence_digest"],
            "cursor": row["cursor"],
            "counters": counters,
            "provider_metadata": provider_metadata,
            "approved_baseline": parameters.get("approved_baseline"),
            "error": row["error"],
        }

    async def upsert_mapping(self, record: dict[str, Any]) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO session_knowledge_migration_mappings (
                    operation_id, source_id, source_type, source_content_hash,
                    output_ids, routes, status, error, updated_at
                ) VALUES (
                    $1::uuid, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, NOW()
                )
                ON CONFLICT (operation_id, source_id) DO UPDATE SET
                    source_type = EXCLUDED.source_type,
                    source_content_hash = EXCLUDED.source_content_hash,
                    output_ids = EXCLUDED.output_ids,
                    routes = EXCLUDED.routes,
                    status = EXCLUDED.status,
                    error = EXCLUDED.error,
                    updated_at = NOW()
                """,
                record["operation_id"],
                int(record["source_id"]),
                record.get("source_type"),
                record.get("source_content_hash"),
                list(record.get("output_ids") or []),
                list(record.get("routes") or []),
                record.get("status") or "completed",
                record.get("error"),
            )

    async def get_mapping(
        self, operation_id: str, source_id: int
    ) -> dict[str, Any] | None:
        rows = await self.list_mappings(operation_id)
        for row in rows:
            if int(row["source_id"]) == int(source_id):
                return row
        return None

    async def list_mappings(self, operation_id: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT operation_id, source_id, source_type, source_content_hash,
                       output_ids, routes, status, error
                  FROM session_knowledge_migration_mappings
                 WHERE operation_id = $1::uuid
                 ORDER BY source_id ASC
                """,
                operation_id,
            )
        results: list[dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "operation_id": str(row["operation_id"]),
                    "source_id": int(row["source_id"]),
                    "source_type": row["source_type"],
                    "source_content_hash": row["source_content_hash"],
                    "output_ids": coerce_jsonb_array(row["output_ids"]),
                    "routes": coerce_jsonb_array(row["routes"]),
                    "status": row["status"],
                    "error": row["error"],
                }
            )
        return results

    async def list_mappings_for_source(self, source_id: int) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT operation_id, source_id, source_type, source_content_hash,
                       output_ids, routes, status, error
                  FROM session_knowledge_migration_mappings
                 WHERE source_id = $1
                 ORDER BY operation_id ASC
                """,
                source_id,
            )
        results: list[dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "operation_id": str(row["operation_id"]),
                    "source_id": int(row["source_id"]),
                    "source_type": row["source_type"],
                    "source_content_hash": row["source_content_hash"],
                    "output_ids": coerce_jsonb_array(row["output_ids"]),
                    "routes": coerce_jsonb_array(row["routes"]),
                    "status": row["status"],
                    "error": row["error"],
                }
            )
        return results


async def build_postgres_migration_store(
    *, readonly: bool = False
) -> PostgresMigrationStore:
    """Build a store. Always disables runtime DDL for CLI safety (K1-04)."""
    from open_brain.data_layer.postgres import get_pool, suppress_migrations

    suppress_migrations()
    pool = await get_pool(run_migrations=False)
    _ = readonly
    return PostgresMigrationStore(pool)


def configured_provider_metadata_from_config(
    *, readonly: bool = False
) -> dict[str, Any]:
    """Provider metadata. Readonly path never requires a provider API key."""
    from open_brain.data_layer.embedding import EMBEDDING_DIM

    if readonly:
        return {
            "embedding_model": (
                os.environ.get("OPEN_BRAIN_EMBEDDING_MODEL")
                or os.environ.get("VOYAGE_MODEL")
                or "configured"
            ),
            "rerank_model": (
                os.environ.get("OPEN_BRAIN_RERANK_MODEL")
                or os.environ.get("RERANK_MODEL")
                or "configured-rerank"
            ),
            "embedding_dimension": EMBEDDING_DIM,
            "cost_per_1k_tokens": 0.0,
            "chars_per_token": 4.0,
        }
    from open_brain.config import get_config

    config = get_config()
    return {
        "embedding_model": config.VOYAGE_MODEL,
        "rerank_model": config.RERANK_MODEL,
        "embedding_dimension": EMBEDDING_DIM,
        "cost_per_1k_tokens": float(
            getattr(config, "EMBEDDING_COST_PER_1K_TOKENS", 0.0) or 0.0
        ),
        "chars_per_token": 4.0,
    }


class DeterministicLexicalControlAdapter:
    """Local control adapter for dry-run without paid providers."""

    instrument = "deterministic-lexical-proxy.v1"

    async def measure(
        self, *, control: str, query: str, documents: list[str]
    ) -> float:
        q = set(query.lower().split())
        if not q:
            return 0.0
        scores = []
        for doc in documents:
            tokens = set(str(doc).lower().split())
            scores.append(len(q & tokens) / len(q))
        return float(sum(scores) / max(1, len(scores)))


class ConfiguredEmbeddingAdapter:
    """Operational document embeddings through the configured provider."""

    async def embed_documents(
        self, texts: list[str]
    ) -> tuple[list[list[float]], dict[str, Any]]:
        from open_brain.data_layer.embedding import embed_batch_with_usage

        vectors, tokens = await embed_batch_with_usage(texts)
        return vectors, {"documents": len(texts), "tokens": tokens}


class ConfiguredRerankAdapter:
    """Operational reranking through the configured provider."""

    def __init__(self, model: str) -> None:
        self._model = model

    async def rerank(
        self, query: str, documents: list[str]
    ) -> tuple[list[int], dict[str, Any]]:
        from open_brain.data_layer.reranker import rerank as configured_rerank

        results = await configured_rerank(query, documents, self._model)
        return [item.index for item in results], {
            "model": self._model,
            "documents": len(documents),
            "max_relevance": max(
                (item.relevance_score for item in results), default=0.0
            ),
        }


class ConfiguredRetrievalControlAdapter:
    """Cohort-scoped lexical, vector, and rerank measurement instrument."""

    def __init__(self, pool: Any, provider_metadata: Mapping[str, Any]) -> None:
        self._pool = pool
        self._provider_metadata = dict(provider_metadata)
        self.instrument = (
            "configured-retrieval-controls.v1:"
            f"{self._provider_metadata.get('embedding_model')}:"
            f"{self._provider_metadata.get('embedding_dimension')}:"
            f"{self._provider_metadata.get('rerank_model')}"
        )

    async def measure(
        self, *, control: str, query: str, documents: list[str]
    ) -> float:
        if not documents:
            return 0.0
        if control == "lexical":
            async with self._pool.acquire() as conn:
                value = await conn.fetchval(
                    """
                    SELECT COALESCE(MAX(ts_rank_cd(
                        to_tsvector('english', document),
                        plainto_tsquery('english', $2)
                    )), 0.0)
                    FROM unnest($1::text[]) AS document
                    """,
                    documents,
                    query,
                )
            return float(value or 0.0)
        if control == "vector":
            from open_brain.data_layer.embedding import (
                embed_batch_with_usage,
                embed_query_with_usage,
            )

            query_vector, _ = await embed_query_with_usage(query)
            document_vectors, _ = await embed_batch_with_usage(documents)
            query_norm = math.sqrt(sum(value * value for value in query_vector))
            scores: list[float] = []
            for vector in document_vectors:
                vector_norm = math.sqrt(sum(value * value for value in vector))
                denominator = query_norm * vector_norm
                scores.append(
                    sum(a * b for a, b in zip(query_vector, vector, strict=True))
                    / denominator
                    if denominator
                    else 0.0
                )
            return max(scores, default=0.0)
        if control == "rerank":
            adapter = ConfiguredRerankAdapter(
                str(self._provider_metadata.get("rerank_model") or "")
            )
            _, metrics = await adapter.rerank(query, documents)
            return float(metrics.get("max_relevance") or 0.0)
        raise ValueError(f"unknown retrieval control: {control}")


class ScopedReconcileAdapter:
    """Verify the exact migrated scope and report duplicate candidates without mutation."""

    def __init__(self, store: MigrationStore) -> None:
        self._store = store

    async def reconcile(self, memory_ids: list[int]) -> dict[str, Any]:
        existing: list[int] = []
        for memory_id in sorted(set(memory_ids)):
            if await self._store.get_memory(memory_id) is not None:
                existing.append(memory_id)
        if existing != sorted(set(memory_ids)):
            raise RuntimeError("scoped reconcile found missing migrated outputs")
        return {"scoped_ids": existing, "refined": 0, "mutation": "none"}
