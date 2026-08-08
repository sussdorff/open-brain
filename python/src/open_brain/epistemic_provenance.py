"""Shared epistemic provenance contract for Open Brain memories.

Origin lineage (`metadata.provenance.origin`) answers who produced a memory and
from which source event. Epistemic fields answer how the content may be used.
They remain distinct, versioned, and validated independently.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, MutableMapping, TypeAlias

EPISTEMIC_PROVENANCE_SCHEMA_VERSION = "epistemic-provenance.v1"
EPISTEMIC_BACKFILL_APPLY_DEFAULT_LIMIT = 500
EPISTEMIC_BACKFILL_APPLY_MAX_LIMIT = 1000
EPISTEMIC_AMBIGUOUS_IDS_CAP = 100

EpistemicLabel: TypeAlias = Literal[
    "observed",
    "inferred",
    "generated",
    "confirmed",
    "disputed",
    "superseded",
]
ExpectedUse: TypeAlias = Literal["evidence", "instruction"]

EPISTEMIC_LABELS: set[str] = {
    "observed",
    "inferred",
    "generated",
    "confirmed",
    "disputed",
    "superseded",
}
EXPECTED_USES: set[str] = {"evidence", "instruction"}
INSTRUCTION_GRADE_LABELS: set[str] = {"observed", "confirmed"}
EVIDENCE_ONLY_LABELS: set[str] = {"inferred", "generated"}
FAILED_EVIDENCE_LABELS: set[str] = {"disputed", "superseded"}

_LEGAL_EXPECTED_USE: dict[str, set[str]] = {
    "observed": {"evidence", "instruction"},
    "inferred": {"evidence"},
    "generated": {"evidence"},
    "confirmed": {"evidence", "instruction"},
    "disputed": {"evidence"},
    "superseded": {"evidence"},
}


class EpistemicProvenanceValidationError(ValueError):
    """Raised when epistemic provenance is missing, incomplete, or illegal."""

    code = "invalid_epistemic_provenance"


def is_legal_epistemic_combination(source_label: str, expected_use: str) -> bool:
    """Return True when label and expected_use form a permitted pair."""
    return expected_use in _LEGAL_EXPECTED_USE.get(source_label, set())


def default_epistemic_classification() -> dict[str, str]:
    """Conservative defaults for unlabeled agent/model writes.

    Absence of a judged proposal never implies confirmation or instruction-grade
    use. New writes default to inferred evidence-only authority.
    """
    return {
        "source_label": "inferred",
        "expected_use": "evidence",
        "epistemic_version": EPISTEMIC_PROVENANCE_SCHEMA_VERSION,
    }


def validate_epistemic_provenance(
    provenance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate epistemic fields without mutating canonical origin lineage."""
    if provenance is None:
        raise EpistemicProvenanceValidationError(
            "provenance must be an object carrying epistemic source_label and expected_use"
        )
    if not isinstance(provenance, Mapping):
        raise EpistemicProvenanceValidationError(
            "provenance must be an object carrying epistemic source_label and expected_use"
        )

    source_label = provenance.get("source_label")
    expected_use = provenance.get("expected_use")
    if source_label not in EPISTEMIC_LABELS:
        raise EpistemicProvenanceValidationError(
            f"provenance.source_label: invalid epistemic label {source_label!r}"
        )
    if expected_use not in EXPECTED_USES:
        raise EpistemicProvenanceValidationError(
            f"provenance.expected_use: invalid value {expected_use!r}"
        )
    if not is_legal_epistemic_combination(source_label, expected_use):
        raise EpistemicProvenanceValidationError(
            f"provenance combination source_label={source_label!r} "
            f"expected_use={expected_use!r} is not instruction-safe"
        )

    version = provenance.get("epistemic_version", EPISTEMIC_PROVENANCE_SCHEMA_VERSION)
    if version != EPISTEMIC_PROVENANCE_SCHEMA_VERSION:
        raise EpistemicProvenanceValidationError(
            f"provenance.epistemic_version: unsupported version {version!r}"
        )

    validated = dict(provenance)
    validated["source_label"] = source_label
    validated["expected_use"] = expected_use
    validated["epistemic_version"] = EPISTEMIC_PROVENANCE_SCHEMA_VERSION
    return validated


def _previous_expected_use(previous: Mapping[str, Any] | None) -> str | None:
    if not isinstance(previous, Mapping):
        return None
    provenance = previous.get("provenance")
    if not isinstance(provenance, Mapping):
        return None
    expected_use = provenance.get("expected_use")
    return expected_use if isinstance(expected_use, str) else None


def ensure_epistemic_provenance(
    metadata: Mapping[str, Any] | None,
    *,
    allow_instruction: bool = False,
    previous: Mapping[str, Any] | None = None,
    repair_orphaned_use: bool = False,
) -> dict[str, Any]:
    """Return metadata with validated epistemic fields, applying defaults when absent.

    Instruction-grade use requires either ``allow_instruction`` (allowed judge
    outcome on this write) or preservation of an already-instruction previous
    state. Raw no-proposal callers cannot newly raise authority to instruction.

    Canonical ``origin`` is preserved unchanged when present.
    """
    merged: dict[str, Any] = dict(metadata) if metadata else {}
    raw_provenance = merged.get("provenance")
    if raw_provenance is not None and not isinstance(raw_provenance, Mapping):
        # Preserve non-object provenance summaries elsewhere; start a fresh object.
        merged.setdefault("provenance_summary", str(raw_provenance))
        provenance: dict[str, Any] = {}
    else:
        provenance = dict(raw_provenance) if isinstance(raw_provenance, Mapping) else {}

    has_label = provenance.get("source_label") is not None
    has_use = provenance.get("expected_use") is not None

    if has_use and not has_label:
        if repair_orphaned_use:
            provenance.pop("expected_use", None)
            has_use = False
        else:
            raise EpistemicProvenanceValidationError(
                "provenance.expected_use without source_label is ambiguous"
            )

    if has_label and has_use:
        validated = validate_epistemic_provenance(provenance)
    elif has_label and not has_use:
        # Complete legacy partial rows conservatively: evidence is always legal.
        provenance["expected_use"] = "evidence"
        validated = validate_epistemic_provenance(provenance)
    else:
        defaults = default_epistemic_classification()
        provenance.update(defaults)
        validated = validate_epistemic_provenance(provenance)

    if validated["expected_use"] == "instruction":
        previous_use = _previous_expected_use(previous)
        if not allow_instruction and previous_use != "instruction":
            raise EpistemicProvenanceValidationError(
                "expected_use=instruction requires an allowed memory-write judge "
                "outcome or prior instruction-grade state"
            )

    merged["provenance"] = validated
    return merged


def merge_update_metadata(
    existing: Mapping[str, Any] | None,
    incoming: Mapping[str, Any] | None,
    *,
    repair_orphaned_use: bool = False,
) -> dict[str, Any]:
    """Merge update metadata, preserving origin and validating epistemic fields.

    When ``incoming`` does not touch ``provenance``, existing provenance is kept
    untouched. When provenance is touched, origin cannot be stripped or silently
    replaced with a conflicting lineage.
    """
    base = dict(existing) if isinstance(existing, Mapping) else {}
    patch = dict(incoming) if isinstance(incoming, Mapping) else {}
    if not patch:
        return base

    provenance_touched = "provenance" in patch
    merged = {**base, **patch}

    if not provenance_touched:
        if "provenance" in base:
            merged["provenance"] = base["provenance"]
        return merged

    existing_prov = base.get("provenance")
    incoming_prov = patch.get("provenance")
    if incoming_prov is not None and not isinstance(incoming_prov, Mapping):
        raise EpistemicProvenanceValidationError(
            "provenance must be an object when provided on update"
        )

    combined: dict[str, Any] = {}
    if isinstance(existing_prov, Mapping):
        combined.update(existing_prov)
    elif existing_prov is not None:
        merged.setdefault("provenance_summary", str(existing_prov))

    if isinstance(incoming_prov, Mapping):
        for key, value in incoming_prov.items():
            if key == "origin":
                continue
            combined[key] = value

    from open_brain.data_layer.interface import (
        OriginProvenanceConflictError,
        validate_origin_provenance,
    )

    existing_origin = (
        existing_prov.get("origin") if isinstance(existing_prov, Mapping) else None
    )
    incoming_origin = (
        incoming_prov.get("origin") if isinstance(incoming_prov, Mapping) else None
    )
    if existing_origin is not None:
        if incoming_origin is None:
            combined["origin"] = existing_origin
        else:
            normalized_existing = validate_origin_provenance(existing_origin)
            normalized_incoming = validate_origin_provenance(incoming_origin)
            if normalized_incoming != normalized_existing:
                raise OriginProvenanceConflictError(
                    "origin provenance conflicts with metadata.provenance.origin"
                )
            combined["origin"] = normalized_existing
    elif incoming_origin is not None:
        combined["origin"] = validate_origin_provenance(incoming_origin)

    ensured = ensure_epistemic_provenance(
        {**merged, "provenance": combined},
        previous=base,
        repair_orphaned_use=repair_orphaned_use,
    )
    merged["provenance"] = ensured["provenance"]
    return merged


def classify_epistemic_row(metadata: Mapping[str, Any] | None) -> str:
    """Classify one memory into an epistemic coverage cohort."""
    if not isinstance(metadata, Mapping):
        return "unlabeled"
    provenance = metadata.get("provenance")
    if not isinstance(provenance, Mapping):
        return "unlabeled"

    source_label = provenance.get("source_label")
    expected_use = provenance.get("expected_use")
    has_label = source_label is not None
    has_use = expected_use is not None
    if not has_label and not has_use:
        return "unlabeled"
    if has_label and not has_use:
        # Only a valid label without use is completable partial.
        # Invalid labels are ambiguous and must never be treated as partial.
        if source_label in EPISTEMIC_LABELS:
            return "partial"
        return "ambiguous"
    if has_use and not has_label:
        # Use-only rows are ambiguous and must not be promoted by backfill.
        return "ambiguous"
    try:
        validate_epistemic_provenance(provenance)
    except EpistemicProvenanceValidationError:
        return "ambiguous"
    return "labeled"


def validate_backfill_apply_limit(limit: int | None) -> int:
    """Return a conservative apply batch limit or raise a stable validation error."""
    if limit is None:
        return EPISTEMIC_BACKFILL_APPLY_DEFAULT_LIMIT
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise EpistemicProvenanceValidationError(
            "backfill apply limit must be an integer"
        )
    if limit < 1 or limit > EPISTEMIC_BACKFILL_APPLY_MAX_LIMIT:
        raise EpistemicProvenanceValidationError(
            "backfill apply limit must be between 1 and "
            f"{EPISTEMIC_BACKFILL_APPLY_MAX_LIMIT}"
        )
    return limit


def plan_epistemic_backfill(
    rows: list[Mapping[str, Any]],
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Build a conservative, idempotent epistemic backfill plan.

    Dry-run is the default. Cohort counters are mutually exclusive and sum to
    ``total``. Ambiguous rows are counted (with capped ids) and never silently
    promoted. Canonical origin is never rewritten. Malformed rows never abort
    the planner.
    """
    updates: list[dict[str, Any]] = []
    already_labeled = 0
    ambiguous = 0
    unlabeled = 0
    partial = 0
    ambiguous_ids: list[int] = []
    ambiguous_ids_truncated = False

    def _record_ambiguous(memory_id: Any) -> None:
        nonlocal ambiguous, ambiguous_ids_truncated
        ambiguous += 1
        if isinstance(memory_id, int) and not isinstance(memory_id, bool):
            if len(ambiguous_ids) < EPISTEMIC_AMBIGUOUS_IDS_CAP:
                ambiguous_ids.append(memory_id)
            else:
                ambiguous_ids_truncated = True

    for row in rows:
        memory_id = row.get("id")
        metadata = row.get("metadata")
        if isinstance(metadata, str):
            cohort = "unlabeled"
            parsed_metadata: dict[str, Any] = {}
        elif isinstance(metadata, Mapping):
            cohort = classify_epistemic_row(metadata)
            parsed_metadata = dict(metadata)
        else:
            cohort = "unlabeled"
            parsed_metadata = {}

        if cohort == "labeled":
            already_labeled += 1
            continue
        if cohort == "ambiguous":
            _record_ambiguous(memory_id)
            continue
        if cohort == "partial":
            # Valid label-only rows may be completed with expected_use=evidence.
            partial += 1
            try:
                ensured = ensure_epistemic_provenance(parsed_metadata)
            except EpistemicProvenanceValidationError:
                # Illegal legacy state must not abort inventory/apply planning.
                partial -= 1
                _record_ambiguous(memory_id)
                continue
        elif cohort == "unlabeled":
            unlabeled += 1
            try:
                ensured = ensure_epistemic_provenance(parsed_metadata)
            except EpistemicProvenanceValidationError:
                unlabeled -= 1
                _record_ambiguous(memory_id)
                continue
        else:
            _record_ambiguous(memory_id)
            continue

        provenance = dict(ensured["provenance"])
        existing_prov = parsed_metadata.get("provenance")
        if isinstance(existing_prov, Mapping) and "origin" in existing_prov:
            provenance["origin"] = existing_prov["origin"]
        updates.append(
            {
                "id": memory_id,
                "source_label": provenance["source_label"],
                "expected_use": provenance["expected_use"],
                "epistemic_version": provenance["epistemic_version"],
                "metadata": {**parsed_metadata, "provenance": provenance},
            }
        )

    total = len(rows)
    plan = {
        "mode": "apply" if apply else "dry_run",
        "total": total,
        "would_update": len(updates),
        "updated": len(updates) if apply else 0,
        "already_labeled": already_labeled,
        "unlabeled": unlabeled,
        "partial": partial,
        "ambiguous": ambiguous,
        "ambiguous_ids": ambiguous_ids,
        "ambiguous_ids_truncated": ambiguous_ids_truncated,
        "ambiguous_ids_cap": EPISTEMIC_AMBIGUOUS_IDS_CAP,
        "updates": updates,
    }
    cohort_sum = already_labeled + unlabeled + partial + ambiguous
    if cohort_sum != total:
        raise EpistemicProvenanceValidationError(
            f"backfill cohort counters do not reconcile: {cohort_sum} != {total}"
        )
    return plan


def merge_epistemic_into_judged_provenance(
    provenance: MutableMapping[str, Any],
) -> dict[str, Any]:
    """Attach the epistemic schema version to judge-written provenance fields."""
    merged = dict(provenance)
    if "source_label" in merged and "expected_use" in merged:
        return validate_epistemic_provenance(merged)
    defaults = default_epistemic_classification()
    merged.setdefault("source_label", defaults["source_label"])
    merged.setdefault("expected_use", defaults["expected_use"])
    return validate_epistemic_provenance(merged)
