#!/usr/bin/env python3
"""Verify that the Second Brain cutover is safe to archive."""

import argparse
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import sys
import tempfile
from typing import Any, Literal

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "python" / "src"))

from open_brain.data_layer.interface import DataLayer, SearchParams  # noqa: E402
from open_brain.digest import generate_daily_review, generate_weekly_briefing  # noqa: E402
from open_brain.paperless import PaperlessClient  # noqa: E402
from open_brain.portable_backup import (  # noqa: E402
    PortableBackupStore,
    _canonical_json,
    export_bundle,
    restore_bundle,
    verify_round_trip,
)
from open_brain.second_brain_import import import_vault  # noqa: E402


SCHEMA_VERSION = "cutover-report.v1"
VERIFIER_VERSION = "20260712.1"
GateStatus = Literal["green", "red"]

GATE_IDS = (
    "open_brain_capabilities",
    "paperless_references",
    "migration_reconciliation",
    "backup_round_trip",
)

# The only meta keys ``_build_report`` is allowed to populate. ``CutoverReport.to_payload``
# rejects any other key so a hand-built report can never leak arbitrary data (PII, tokens,
# secrets) into the durable report through the ``meta`` field.
ALLOWED_META_KEYS = frozenset({"generated_at", "open_brain_git_sha", "verifier_version"})

# Capability mapping for the seven closed prerequisite beads. The checks stay
# cheap and read-only: stats prove persisted aggregate reachability, digest
# smoke calls prove review/capture search paths, callable checks prove the
# Paperless/import/backup primitives are wired, and the live Paperless probes
# are left to the dedicated paperless_references gate.
REQUIRED_CAPABILITY_IDS = (
    "open-brain-ccd",  # canonical entity filtering is wired and returns a valid count
    "open-brain-hws",  # canonical vocabulary: non-empty stats type taxonomy
    "open-brain-slu",  # capture inbox: daily review exposes unresolved capture counts
    "open-brain-5qo",  # agent capture plus daily/weekly review: both digest calls succeed
    "open-brain-amq",  # Paperless resolution capability: resolver method is wired
    "open-brain-brt",  # migration import capability: dry-run importer is callable and vault exists
    "open-brain-jhg",  # portable backup capability: export/restore/verify primitives are callable
)


@dataclass(frozen=True, slots=True)
class GateResult:
    """Redacted result for a single verifier gate."""

    id: str
    status: GateStatus
    counts: dict[str, int]
    detail: str

    def to_payload(self) -> dict[str, Any]:
        """Return the report-safe gate payload."""
        return {
            "id": self.id,
            "status": self.status,
            "counts": dict(self.counts),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class CutoverReport:
    """Machine-readable cutover verifier report."""

    schema_version: str
    overall_status: GateStatus
    gates: list[GateResult]
    meta: dict[str, str]

    def to_payload(self) -> dict[str, Any]:
        """Return the durable report payload."""
        gates: list[dict[str, Any]] = []
        for gate in self.gates:
            if isinstance(gate, GateResult):
                gates.append(gate.to_payload())
            else:
                raise TypeError(f"unsupported gate result type: {gate.__class__.__name__}")
        unexpected_meta = set(self.meta) - ALLOWED_META_KEYS
        if unexpected_meta:
            raise ValueError(f"unsupported meta keys: {sorted(unexpected_meta)}")
        return {
            "schema_version": self.schema_version,
            "overall_status": self.overall_status,
            "gates": gates,
            "meta": dict(self.meta),
        }


def _green(gate_id: str, counts: dict[str, int], detail: str) -> GateResult:
    """Build a green gate result."""
    return GateResult(id=gate_id, status="green", counts=counts, detail=detail)


def _red(gate_id: str, counts: dict[str, int], detail: str) -> GateResult:
    """Build a red gate result."""
    return GateResult(id=gate_id, status="red", counts=counts, detail=detail)


def _exception_detail(exc: Exception) -> str:
    """Return a redacted exception detail string."""
    return f"exception:{exc.__class__.__name__}"


def _safe_int(value: Any, default: int = 0) -> int:
    """Convert a count-like value to an int without raising."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sum_counts(counts: dict[str, int]) -> int:
    """Return the sum of non-negative count values."""
    return sum(value for value in counts.values() if value > 0)


def _stats_types(stats: dict[str, Any]) -> dict[str, int]:
    """Return the stats type taxonomy across current and legacy field names."""
    raw = stats.get("types")
    if raw is None:
        raw = stats.get("by_type")
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): _safe_int(value)
        for key, value in raw.items()
        if isinstance(key, str)
    }


def _git_dir() -> Path | None:
    """Resolve this worktree's git directory without invoking git."""
    marker = REPO_ROOT / ".git"
    try:
        if marker.is_dir():
            return marker
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "gitdir: "
    if not text.startswith(prefix):
        return None
    git_dir = Path(text[len(prefix):])
    if not git_dir.is_absolute():
        git_dir = REPO_ROOT / git_dir
    return git_dir.resolve()


def _common_git_dir(git_dir: Path) -> Path:
    """Return the common git directory for linked worktrees."""
    common_dir_file = git_dir / "commondir"
    try:
        raw = common_dir_file.read_text(encoding="utf-8").strip()
    except OSError:
        return git_dir
    common_dir = Path(raw)
    if not common_dir.is_absolute():
        common_dir = git_dir / common_dir
    return common_dir.resolve()


def _read_git_sha() -> str:
    """Read the current commit SHA without shelling out."""
    git_dir = _git_dir()
    if git_dir is None:
        return "unknown"
    common_dir = _common_git_dir(git_dir)
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    if not head.startswith("ref: "):
        return head if head else "unknown"

    ref_name = head.removeprefix("ref: ").strip()
    for base in (git_dir, common_dir):
        ref_path = base / ref_name
        try:
            value = ref_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value

    packed_refs = common_dir / "packed-refs"
    try:
        for line in packed_refs.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or line.startswith("^"):
                continue
            sha, _, ref = line.partition(" ")
            if ref == ref_name and sha:
                return sha
    except OSError:
        pass
    return "unknown"


def _write_report(report: CutoverReport, report_path: Path) -> None:
    """Write the report via the deterministic portable-backup serializer."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _canonical_json(report.to_payload(), pretty=True) + "\n",
        encoding="utf-8",
    )


def _build_report(gates: list[GateResult]) -> CutoverReport:
    """Build the aggregate cutover report."""
    overall_status: GateStatus = "green" if all(gate.status == "green" for gate in gates) else "red"
    return CutoverReport(
        schema_version=SCHEMA_VERSION,
        overall_status=overall_status,
        gates=gates,
        meta={
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "open_brain_git_sha": _read_git_sha(),
            "verifier_version": VERIFIER_VERSION,
        },
    )


def _default_data_layer() -> DataLayer:
    """Return the production data layer."""
    from open_brain.data_layer.postgres import PostgresDataLayer

    return PostgresDataLayer()


async def _evaluate_open_brain_capabilities(
    *,
    data_layer: DataLayer,
    paperless_client: PaperlessClient,
    vault_path: str | Path,
    required_capabilities: list[str],
) -> GateResult:
    """Evaluate required Open Brain capabilities without writing data."""
    try:
        stats = await data_layer.stats()
        if not isinstance(stats, dict):
            return _red(
                "open_brain_capabilities",
                {"required": len(required_capabilities), "satisfied": 0, "missing": len(required_capabilities)},
                "stats unavailable",
            )

        required = set(required_capabilities)
        canonical_count = 0
        canonical_query_succeeded = False
        if "open-brain-ccd" in required:
            try:
                canonical_result = await data_layer.search(
                    SearchParams(metadata_filter={"canonical_entity": True}, limit=1)
                )
            except Exception:
                pass
            else:
                canonical_total = getattr(canonical_result, "total", None)
                canonical_query_succeeded = (
                    isinstance(canonical_total, int)
                    and not isinstance(canonical_total, bool)
                    and canonical_total >= 0
                )
                if canonical_query_succeeded:
                    canonical_count = canonical_total

        today = datetime.now(tz=UTC).date().isoformat()
        daily_review = None
        if required & {"open-brain-slu", "open-brain-5qo"}:
            daily_review = await generate_daily_review(data_layer, date=today, max_items=5)
        weekly_briefing = None
        if "open-brain-5qo" in required:
            weekly_briefing = await generate_weekly_briefing(
                data_layer, weeks_back=1, max_memories=5
            )

        stats_memories = _safe_int(stats.get("memories"))
        stats_relationships = _safe_int(stats.get("relationships"))
        stats_sessions = _safe_int(stats.get("sessions"))
        type_counts = _stats_types(stats)
        capability_results = {
            "open-brain-ccd": canonical_query_succeeded,
            "open-brain-hws": stats_memories > 0 and bool(type_counts),
            "open-brain-slu": (
                daily_review is not None
                and stats_memories > 0
                and isinstance(daily_review.counts, dict)
                and "unresolved" in daily_review.counts
            ),
            "open-brain-5qo": (
                daily_review is not None
                and weekly_briefing is not None
                and isinstance(daily_review.counts, dict)
                and bool(daily_review.counts)
                and isinstance(weekly_briefing.memory_counts, dict)
                and bool(weekly_briefing.memory_counts)
                and isinstance(weekly_briefing.inbox_state, dict)
                and bool(weekly_briefing.inbox_state)
            ),
            "open-brain-amq": callable(getattr(paperless_client, "resolve_reference", None)),
            "open-brain-brt": callable(import_vault) and Path(vault_path).expanduser().exists(),
            "open-brain-jhg": (
                callable(export_bundle)
                and callable(restore_bundle)
                and callable(verify_round_trip)
            ),
        }
        unknown = [
            capability_id
            for capability_id in required_capabilities
            if capability_id not in REQUIRED_CAPABILITY_IDS
        ]
        missing = [
            capability_id
            for capability_id in required_capabilities
            if (
                capability_id in REQUIRED_CAPABILITY_IDS
                and not capability_results.get(capability_id, False)
            )
        ]
        counts = {
            "required": len(required_capabilities),
            "satisfied": len(required_capabilities) - len(missing) - len(unknown),
            "missing": len(missing),
            "unknown": len(unknown),
            "stats_memories": stats_memories,
            "stats_sessions": stats_sessions,
            "stats_relationships": stats_relationships,
            "stats_types": len(type_counts),
            "canonical_entities": canonical_count,
        }
        if not required_capabilities:
            return _red("open_brain_capabilities", counts, "no required capabilities configured")
        if missing or unknown:
            return _red("open_brain_capabilities", counts, "required capabilities incomplete")
        return _green("open_brain_capabilities", counts, "required capabilities verified")
    except Exception as exc:
        return _red("open_brain_capabilities", {}, _exception_detail(exc))


async def _evaluate_paperless_references(
    *,
    paperless_client: PaperlessClient,
    paperless_probe_ids: list[int],
) -> GateResult:
    """Evaluate live Paperless reference probes."""
    if not paperless_probe_ids:
        return _red(
            "paperless_references",
            {"probes": 0, "found": 0, "failed": 0},
            "no paperless probes configured",
        )

    status_counts = {
        "found": 0,
        "not_found": 0,
        "unauthorized": 0,
        "malformed": 0,
        "transport_error": 0,
        "not_configured": 0,
        "exceptions": 0,
    }
    for document_id in paperless_probe_ids:
        try:
            result = await paperless_client.resolve_reference(document_id)
        except Exception:
            status_counts["exceptions"] += 1
            continue
        status = getattr(result, "status", "malformed")
        if status in status_counts:
            status_counts[status] += 1
        else:
            status_counts["malformed"] += 1

    failed = len(paperless_probe_ids) - status_counts["found"]
    counts = {
        "probes": len(paperless_probe_ids),
        "found": status_counts["found"],
        "failed": failed,
        "not_found": status_counts["not_found"],
        "unauthorized": status_counts["unauthorized"],
        "malformed": status_counts["malformed"],
        "transport_error": status_counts["transport_error"],
        "not_configured": status_counts["not_configured"],
        "exceptions": status_counts["exceptions"],
    }
    if failed:
        return _red("paperless_references", counts, "paperless probes not all found")
    return _green("paperless_references", counts, "paperless probes found")


async def _evaluate_migration_reconciliation(
    *,
    vault_path: str | Path,
    paperless_mapping_path: str | Path | None,
    data_layer: DataLayer,
    paperless_client: PaperlessClient,
) -> GateResult:
    """Evaluate dry-run Second Brain migration reconciliation."""
    try:
        report = await import_vault(
            vault_path=vault_path,
            paperless_mapping_path=paperless_mapping_path,
            data_layer=data_layer,
            paperless_client=paperless_client,
            apply=False,
        )
        summary = report.get("summary") if isinstance(report, dict) else None
        if not isinstance(summary, dict):
            return _red("migration_reconciliation", {}, "migration summary unavailable")

        counts = {
            "importable": _safe_int(summary.get("importable"), -1),
            "unresolved_links": _safe_int(summary.get("unresolved_links"), -1),
            "unresolved_attachments": _safe_int(summary.get("unresolved_attachments"), -1),
            "duplicate": _safe_int(summary.get("duplicate")),
            "skipped": _safe_int(summary.get("skipped")),
        }
        if (
            counts["importable"] == 0
            and counts["unresolved_links"] == 0
            and counts["unresolved_attachments"] == 0
            and counts["skipped"] == 0
        ):
            return _green("migration_reconciliation", counts, "migration reconciliation complete")
        return _red("migration_reconciliation", counts, "migration reconciliation incomplete")
    except Exception as exc:
        return _red("migration_reconciliation", {}, _exception_detail(exc))


async def _evaluate_backup_round_trip(
    *,
    backup_store_factory: Callable[[], PortableBackupStore] | None,
) -> GateResult:
    """Evaluate portable backup export, restore, and round-trip verification."""
    if backup_store_factory is None:
        return _red(
            "backup_round_trip",
            {"closure_total": 0},
            "restore-safe backup store factory required",
        )

    try:
        source_store = backup_store_factory()
        source_counts = {
            key: _safe_int(value)
            for key, value in (await source_store.portable_closure_counts()).items()
        }
        closure_total = _sum_counts(source_counts)
        counts = {
            "closure_total": closure_total,
            "indexes": source_counts.get("indexes", 0),
            "memories": source_counts.get("memories", 0),
            "relationships": source_counts.get("relationships", 0),
            "round_trip_ok": 0,
        }
        if closure_total <= 0:
            return _red("backup_round_trip", counts, "backup closure empty")

        restore_store = backup_store_factory()
        with tempfile.TemporaryDirectory(prefix="open-brain-cutover-") as temp_dir:
            bundle_path = Path(temp_dir) / "bundle"
            await export_bundle(
                bundle_path,
                source_store,
                source_label="second-brain-cutover-verifier",
            )
            await restore_bundle(
                bundle_path,
                restore_store,
                regenerate_embeddings=False,
            )
            verification = await verify_round_trip(bundle_path, restore_store)

        round_trip_ok = verification.get("ok") is True if isinstance(verification, dict) else False
        counts["round_trip_ok"] = int(round_trip_ok)
        if round_trip_ok:
            return _green("backup_round_trip", counts, "backup round trip verified")
        return _red("backup_round_trip", counts, "backup round trip failed")
    except Exception as exc:
        return _red("backup_round_trip", {}, _exception_detail(exc))


async def run_cutover(
    *,
    data_layer: DataLayer | None = None,
    paperless_client: PaperlessClient | None = None,
    backup_store_factory: Callable[[], PortableBackupStore] | None = None,
    vault_path: str | Path,
    paperless_mapping_path: str | Path | None = None,
    paperless_probe_ids: list[int],
    required_capabilities: list[str],
    report_path: Path,
) -> CutoverReport:
    """Run all Second Brain cutover gates and write a redacted report."""
    resolved_data_layer = data_layer or _default_data_layer()
    resolved_paperless_client = paperless_client or PaperlessClient()

    gates = [
        await _evaluate_open_brain_capabilities(
            data_layer=resolved_data_layer,
            paperless_client=resolved_paperless_client,
            vault_path=vault_path,
            required_capabilities=required_capabilities,
        ),
        await _evaluate_paperless_references(
            paperless_client=resolved_paperless_client,
            paperless_probe_ids=paperless_probe_ids,
        ),
        await _evaluate_migration_reconciliation(
            vault_path=vault_path,
            paperless_mapping_path=paperless_mapping_path,
            data_layer=resolved_data_layer,
            paperless_client=resolved_paperless_client,
        ),
        await _evaluate_backup_round_trip(
            backup_store_factory=backup_store_factory,
        ),
    ]

    report = _build_report(gates)
    _write_report(report, report_path)
    return report


def build_parser() -> argparse.ArgumentParser:
    """Build the verifier CLI parser."""
    parser = argparse.ArgumentParser(description="Verify Second Brain cutover readiness.")
    parser.add_argument("--vault-path", required=True, help="Second Brain vault path to dry-run")
    parser.add_argument(
        "--paperless-mapping-path",
        default=None,
        help="Optional Second Brain Paperless mapping JSON path",
    )
    parser.add_argument(
        "--paperless-probe-id",
        action="append",
        type=int,
        required=True,
        help="Paperless document id that must resolve; repeat for multiple probes",
    )
    parser.add_argument(
        "--required-capability",
        action="append",
        choices=REQUIRED_CAPABILITY_IDS,
        default=None,
        help="Required Open Brain capability id; defaults to all prerequisite ids",
    )
    parser.add_argument(
        "--report-path",
        required=True,
        type=Path,
        help="Path for the redacted cutover report JSON",
    )
    return parser


async def main(argv: list[str] | None = None) -> int:
    """Run the verifier CLI."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2

    try:
        report = await run_cutover(
            vault_path=args.vault_path,
            paperless_mapping_path=args.paperless_mapping_path,
            paperless_probe_ids=list(args.paperless_probe_id),
            required_capabilities=args.required_capability or list(REQUIRED_CAPABILITY_IDS),
            report_path=args.report_path,
        )
    except Exception as exc:
        print(f"verifier error: {exc.__class__.__name__}", file=sys.stderr)
        return 2

    return 0 if report.overall_status == "green" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
