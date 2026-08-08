"""Wake-up pack: categorized, token-budgeted memory injection for session start.

Compilation is gated by ``retrieval-contract.v1``. High-authority sections
require observed/confirmed instruction-grade provenance plus an explicit
promotion/authorization audit reason. Category names and importance alone are
never sufficient.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from open_brain.data_layer.interface import Memory, rank_importance
from open_brain.memory_promotion import PromotionProjection
from open_brain.retrieval_contract import (
    HIGH_AUTHORITY_INFLUENCES,
    RETRIEVAL_CONTRACT_SCHEMA_VERSION,
    RetrievalContract,
    RetrievalResult,
    RetrievalUnit,
    apply_retrieval_contract,
    profile_retrieval_contract,
    resolve_retrieval_contract,
    serialize_evidence_envelope,
)

logger = logging.getLogger(__name__)

CATEGORY_ORDER = ["identity", "constraints", "errors", "project"]
CATEGORY_DISPLAY = {
    "identity": "Identity",
    "decisions": "Decisions",
    "constraints": "Constraints",
    "errors": "Errors",
    "project": "Project",
    "context": "Context",
    "evidence": "Evidence",
    "system_instruction": "System Instruction",
}
# Emit order for contract-aware packs. High-authority first, then context-like,
# with a final Evidence bucket for demoted or explicitly evidential units.
SECTION_ORDER = [
    "identity",
    "constraints",
    "system_instruction",
    "errors",
    "project",
    "context",
    "evidence",
]


def token_estimate(text: str) -> int:
    """Rough token count estimate: len(text) // 4."""
    return len(text) // 4


def classify_memory(memory: Memory) -> str:
    """Classify a memory into one of the legacy wake-up buckets.

    Returns: "identity" | "decisions" | "constraints" | "errors" | "project" | "context"

    Classification is an organizational hint only. It does not grant
    instruction-grade authority; retrieval-contract influence does.
    """
    t = memory.type or ""
    meta_cat = (memory.metadata or {}).get("category", "")
    stability = memory.stability or ""
    project_name = memory.project_name or ""

    if t == "identity" or meta_cat == "identity":
        return "identity"
    if t == "decision" or meta_cat == "decision":
        return "decisions"
    if t == "constraint" or meta_cat == "constraint" or (
        stability == "canonical" and t in ("rule", "policy")
    ):
        return "constraints"
    if t == "error_resolved" or meta_cat == "error":
        return "errors"
    if project_name or meta_cat == "project":
        return "project"
    return "context"


def _sort_key(memory: Memory) -> tuple[int, float, int, float]:
    """Sort key for descending order: importance rank, priority, access_count, updated_at."""
    imp = memory.importance if memory.importance in ("critical", "high", "medium", "low") else "low"
    rank = rank_importance(imp)
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(memory.updated_at.replace("Z", "+00:00"))
        ts = -dt.timestamp()
    except Exception:
        ts = 0.0
    return (-rank, -memory.priority, -memory.access_count, ts)


def _unit_sort_key(unit: RetrievalUnit, memory_by_id: Mapping[int, Memory]) -> tuple[int, float, int, float]:
    memory = memory_by_id.get(unit.memory_id)
    if memory is None:
        return (0, 0.0, 0, 0.0)
    return _sort_key(memory)


def _escape_data_value(value: str) -> str:
    """Keep persisted memory text as quoted data, never structural markdown."""
    return (
        value.replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
        .replace("```", "`\u200b``")
    )


def _format_entry(memory: Memory) -> str:
    """Format a memory as a single markdown list item (legacy helper)."""
    title = memory.title or memory.content[:60]
    importance = memory.importance or "medium"
    content_preview = memory.content[:200] if memory.content else ""
    return (
        f"- **{_escape_data_value(title)}** ({importance}): "
        f"{_escape_data_value(content_preview)}"
    )


def _format_unit_entry(unit: RetrievalUnit, memory: Memory | None) -> str:
    """Format a retrieval unit as quoted evidence/context data."""
    title = unit.title or (unit.content[:60] if unit.content else f"memory:{unit.memory_id}")
    importance = (memory.importance if memory is not None else None) or "medium"
    content_preview = unit.content[:200] if unit.content else ""
    influence = unit.effective_influence
    return (
        f"- [{influence}] **{_escape_data_value(title)}** ({importance}): "
        f"{_escape_data_value(content_preview)}"
    )


def _resolve_wake_up_contract(
    retrieval_contract: Mapping[str, Any] | RetrievalContract | None,
    work_object: Mapping[str, Any] | None,
) -> RetrievalContract:
    if retrieval_contract is None:
        return profile_retrieval_contract(
            "compatibility",
            work_object=work_object or {"kind": "project", "id": "wake-up"},
        )
    return resolve_retrieval_contract(
        retrieval_contract,
        work_object=work_object,
    )


def compile_wake_up_units(
    memories: list[Memory],
    *,
    retrieval_contract: Mapping[str, Any] | RetrievalContract | None = None,
    work_object: Mapping[str, Any] | None = None,
    promotion_projection: Mapping[int, PromotionProjection] | None = None,
) -> RetrievalResult:
    """Compile memories into contract-bound retrieval units for wake-up."""
    contract = _resolve_wake_up_contract(retrieval_contract, work_object)
    return apply_retrieval_contract(
        memories,
        contract=contract,
        work_object=work_object,
        promotion_projection=promotion_projection,
    )


def build_wake_up_pack(
    memories: list[Memory],
    token_budget: int = 500,
    *,
    retrieval_contract: Mapping[str, Any] | RetrievalContract | None = None,
    work_object: Mapping[str, Any] | None = None,
    as_envelope: bool = False,
    promotion_projection: Mapping[int, PromotionProjection] | None = None,
) -> str:
    """Build a token-budgeted wake-up pack from memories under a retrieval contract.

    When ``retrieval_contract`` is omitted, the constrained compatibility
    contract is used: searchable evidence/context may be emitted, but identity,
    constraint, policy, and system-instruction authority are denied.

    When ``as_envelope`` is true, returns a deterministic typed evidence envelope
    instead of markdown sections. Token budget applies to the serialized form.
    """
    if token_budget <= 0:
        return ""

    result = compile_wake_up_units(
        memories,
        retrieval_contract=retrieval_contract,
        work_object=work_object,
        promotion_projection=promotion_projection,
    )
    memory_by_id = {memory.id: memory for memory in memories}

    if as_envelope:
        # Trim units against the caller budget. The invariant contract header is
        # emitted by reference (see serialize_evidence_envelope), so fixed
        # overhead does not exhaust the production token_budget=500.
        empty = serialize_evidence_envelope(
            RetrievalResult(contract=result.contract, units=())
        )
        empty_tokens = token_estimate(empty)
        if empty_tokens > token_budget:
            return ""
        selected: list[RetrievalUnit] = []
        encoded = empty
        for unit in sorted(
            result.units, key=lambda item: _unit_sort_key(item, memory_by_id)
        ):
            candidate = RetrievalResult(
                contract=result.contract,
                units=tuple(selected + [unit]),
            )
            candidate_encoded = serialize_evidence_envelope(candidate)
            # Charge only the unit-payload growth against the remaining budget.
            unit_growth = token_estimate(candidate_encoded) - empty_tokens
            if empty_tokens + unit_growth > token_budget:
                break
            selected.append(unit)
            encoded = candidate_encoded
        return encoded

    buckets: dict[str, list[RetrievalUnit]] = {section: [] for section in SECTION_ORDER}
    for unit in result.units:
        section = unit.section if unit.section in buckets else "evidence"
        # Never place demoted units into high-authority section headings.
        if (
            section in {"identity", "constraints", "system_instruction"}
            and unit.effective_influence not in HIGH_AUTHORITY_INFLUENCES
        ):
            section = "evidence"
        buckets[section].append(unit)

    for section in buckets:
        buckets[section].sort(key=lambda item: _unit_sort_key(item, memory_by_id))

    output_parts: list[str] = []
    remaining = token_budget

    # Banner clarifies that markdown headings are organizational, not policy.
    banner = (
        f"# open-brain retrieved data ({RETRIEVAL_CONTRACT_SCHEMA_VERSION})\n"
        "Label: RETRIEVED_DATA_NOT_USER_OR_SYSTEM_POLICY\n\n"
    )
    banner_tokens = token_estimate(banner)
    if banner_tokens > remaining:
        return ""
    output_parts.append(banner)
    remaining -= banner_tokens

    for section in SECTION_ORDER:
        entries = buckets[section]
        if not entries:
            continue

        header = f"## {CATEGORY_DISPLAY.get(section, section.title())}\n"
        header_tokens = token_estimate(header)
        if header_tokens > remaining:
            break

        section_lines: list[str] = [header]
        section_tokens = header_tokens

        for unit in entries:
            line = _format_unit_entry(unit, memory_by_id.get(unit.memory_id)) + "\n"
            line_tokens = token_estimate(line)
            if line_tokens > remaining - section_tokens:
                break
            section_lines.append(line)
            section_tokens += line_tokens

        if len(section_lines) > 1:
            section_text = "".join(section_lines)
            output_parts.append(section_text)
            remaining -= token_estimate(section_text)

    return "".join(output_parts)
