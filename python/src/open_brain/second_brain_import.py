"""Deterministic bulk importer for historical Second Brain vault notes.

This module intentionally does not implement the ingest adapter registry
Protocol. It is a standalone migration module for one-shot historical vault
imports, while ``open_brain.migrate`` keeps its Markdown/Obsidian parsing note
scoped to the interactive single-note capture skill. Bulk migration needs a
deterministic, fixture-tested path so dry runs and repeated applies are
auditable.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from open_brain.data_layer.interface import (
    DataLayer,
    SaveMemoryParams,
    paperless_reference_binary_keys,
)
from open_brain.ingest.runs import ingest_run
from open_brain.paperless import PaperlessClient, PaperlessResolveResult

ImportMode = Literal["dry_run", "apply"]
ItemAction = Literal["import", "duplicate", "skip"]

_WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]]+)\]\]")
_EMBED_RE = re.compile(r"!\[\[([^\]]+)\]\]")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_EXTERNAL_HTTP_REFERENCE_RE = re.compile(r"^https?:/{1,2}", re.IGNORECASE)
_DATE_STEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TEMPLATE_DIR_RE = re.compile(r"^(?:\d+-)?templates$", re.IGNORECASE)
_ATTACHMENT_SUFFIXES = frozenset({
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".tif",
    ".tiff",
    ".heic",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
})


@dataclass(slots=True)
class ParsedWikilink:
    """A wikilink found in a note body."""

    raw: str
    target: str


@dataclass(slots=True)
class ParsedNote:
    """A parsed Markdown note ready for reconciliation."""

    source_ref: str
    path: Path
    title: str
    body: str
    frontmatter: dict[str, Any]
    memory_type: str
    metadata: dict[str, Any]
    wikilinks: list[ParsedWikilink]
    attachments: list[str]
    paperless_references: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class SkippedNote:
    """A note that could not be parsed safely."""

    source_ref: str
    reason: str


@dataclass(slots=True)
class NoteResolution:
    """Result of resolving a wikilink target against the parsed vault."""

    note: ParsedNote | None
    reason: str | None


def _to_posix(path: Path) -> str:
    """Return a stable POSIX path for source_ref and matching."""
    return path.as_posix()


def _iso_from_timestamp(timestamp: float) -> str:
    """Convert a filesystem timestamp to an ISO UTC string."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _json_safe(value: Any) -> Any:
    """Recursively normalize date/datetime leaves to ISO strings for JSON storage.

    YAML 1.1 implicit typing parses unquoted date-like scalars (e.g. ``date: 2026-07-10``,
    common in daily notes) into ``datetime.date``/``datetime.datetime`` objects. The
    Postgres jsonb codec encodes metadata with plain ``json.dumps`` and no ``default=``
    fallback, so such objects would raise ``TypeError`` in ``save_memory``. Convert them
    to ISO strings while leaving every other value unchanged.
    """
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split leading YAML frontmatter from a Markdown document."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text

    closing_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing_index = index
            break

    if closing_index is None:
        raise ValueError("unterminated frontmatter fence")

    frontmatter_text = "".join(lines[1:closing_index])
    body = "".join(lines[closing_index + 1 :])
    loaded = yaml.safe_load(frontmatter_text) if frontmatter_text.strip() else {}
    if loaded is None:
        return {}, body
    if not isinstance(loaded, dict):
        raise ValueError("frontmatter must be a mapping")
    return _json_safe(dict(loaded)), body


def _clean_wikilink_target(value: str) -> str:
    """Strip Obsidian alias, heading, and block anchors from a wikilink target."""
    target = value.split("|", 1)[0].split("#", 1)[0].strip()
    return target.removesuffix(".md") if target.endswith(".md") else target


def _extract_wikilinks(body: str) -> list[ParsedWikilink]:
    """Extract non-embedded Obsidian wikilinks from Markdown content."""
    links: list[ParsedWikilink] = []
    for match in _WIKILINK_RE.finditer(body):
        raw = match.group(0)
        target = _clean_wikilink_target(match.group(1))
        if target:
            links.append(ParsedWikilink(raw=raw, target=target))
    return links


def _is_external_http_reference(value: str) -> bool:
    """Return true for standard or single-slash HTTP(S) web references."""
    return _EXTERNAL_HTTP_REFERENCE_RE.match(value.strip()) is not None


def _is_attachment_target(value: str) -> bool:
    """Return true when a link target looks like an attachment reference."""
    if _is_external_http_reference(value):
        return False
    suffix = Path(value.split("#", 1)[0].split("|", 1)[0].strip()).suffix.lower()
    return suffix in _ATTACHMENT_SUFFIXES


def _extract_attachments(body: str, frontmatter: dict[str, Any]) -> list[str]:
    """Extract attachment references from frontmatter and Markdown content."""
    attachments: list[str] = []

    raw_frontmatter_attachments = frontmatter.get("attachments")
    if isinstance(raw_frontmatter_attachments, list):
        for value in raw_frontmatter_attachments:
            if (
                isinstance(value, str)
                and value.strip()
                and not _is_external_http_reference(value)
            ):
                attachments.append(value.strip())

    for match in _EMBED_RE.finditer(body):
        target = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
        if target and _is_attachment_target(target):
            attachments.append(target)

    for match in _MARKDOWN_LINK_RE.finditer(body):
        target = match.group(1).split("#", 1)[0].strip()
        if target and _is_attachment_target(target):
            attachments.append(target)

    deduped: list[str] = []
    seen: set[str] = set()
    for attachment in attachments:
        normalized = _to_posix(Path(attachment))
        if normalized not in seen:
            seen.add(normalized)
            deduped.append(normalized)
    return deduped


def _frontmatter_type(frontmatter: dict[str, Any]) -> str | None:
    """Return a normalized frontmatter type string when present."""
    value = frontmatter.get("type")
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


def map_note_type(source_ref: str, frontmatter: dict[str, Any]) -> str:
    """Map note metadata and path to the canonical Open Brain vocabulary."""
    explicit = _frontmatter_type(frontmatter)
    type_map = {
        "project": "project",
        "resource": "resource",
        "reference": "resource",
        "article": "resource",
        "reading": "resource",
        "concept": "concept",
        "journal": "journal",
        "daily": "journal",
        "daily_note": "journal",
        "correspondence": "correspondence",
        "email": "correspondence",
        "message": "correspondence",
        "person": "person",
        "contact": "person",
    }
    if explicit in type_map:
        return type_map[explicit]

    path = Path(source_ref)
    folder = path.parts[0].lower() if path.parts else ""
    folder_map = {
        "projects": "project",
        "resources": "resource",
        "references": "resource",
        "concepts": "concept",
        "daily": "journal",
        "journal": "journal",
        "correspondence": "correspondence",
        "people": "person",
        "persons": "person",
        "contacts": "person",
    }
    if folder in folder_map:
        return folder_map[folder]
    if _DATE_STEM_RE.match(path.stem):
        return "journal"
    return "observation"


def _as_str(value: Any) -> str | None:
    """Return a stripped string value or None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_str_list(value: Any) -> list[str]:
    """Normalize scalar/list metadata values into a string list."""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = _as_str(value)
    return [text] if text else []


def _type_specific_metadata(
    memory_type: str,
    source_ref: str,
    frontmatter: dict[str, Any],
    title: str,
) -> dict[str, Any]:
    """Extract canonical type metadata from note properties."""
    if memory_type == "project":
        return {
            key: value
            for key, value in {
                "name": _as_str(frontmatter.get("name") or title),
                "status": _as_str(frontmatter.get("status")),
                "owner": _as_str(frontmatter.get("owner")),
                "goals": _as_str_list(frontmatter.get("goals")),
                "next_actions": _as_str_list(frontmatter.get("next_actions")),
                "repository": _as_str(frontmatter.get("repository")),
                "due_date": _as_str(frontmatter.get("due_date")),
            }.items()
            if value not in (None, [])
        }
    if memory_type == "resource":
        return {
            key: value
            for key, value in {
                "title": _as_str(frontmatter.get("title") or title),
                "url": _as_str(frontmatter.get("url")),
                "source_type": _as_str(frontmatter.get("source_type")),
                "author": _as_str(frontmatter.get("author")),
                "summary": _as_str(frontmatter.get("summary")),
                "published_at": _as_str(frontmatter.get("published_at")),
            }.items()
            if value is not None
        }
    if memory_type == "concept":
        return {
            key: value
            for key, value in {
                "name": _as_str(frontmatter.get("name") or title),
                "domain": _as_str(frontmatter.get("domain")),
                "summary": _as_str(frontmatter.get("summary")),
                "related_concepts": _as_str_list(frontmatter.get("related_concepts")),
            }.items()
            if value not in (None, [])
        }
    if memory_type == "journal":
        entry_date = _as_str(frontmatter.get("entry_date") or frontmatter.get("date"))
        if entry_date is None and _DATE_STEM_RE.match(Path(source_ref).stem):
            entry_date = Path(source_ref).stem
        return {
            key: value
            for key, value in {
                "entry_date": entry_date,
                "mood": _as_str(frontmatter.get("mood")),
                "themes": _as_str_list(frontmatter.get("themes")),
                "reflection": _as_str(frontmatter.get("reflection")),
            }.items()
            if value not in (None, [])
        }
    if memory_type == "person":
        return {
            key: value
            for key, value in {
                "name": _as_str(frontmatter.get("name") or title),
                "org": _as_str(frontmatter.get("org")),
                "role": _as_str(frontmatter.get("role")),
                "relationship": _as_str(frontmatter.get("relationship")),
                "last_contact": _as_str(frontmatter.get("last_contact")),
            }.items()
            if value is not None
        }
    if memory_type == "correspondence":
        return {
            key: value
            for key, value in {
                "with": _as_str_list(frontmatter.get("with")),
                "channel": _as_str(frontmatter.get("channel")),
                "direction": _as_str(frontmatter.get("direction")),
                "subject": _as_str(frontmatter.get("subject") or title),
                "summary": _as_str(frontmatter.get("summary")),
                "occurred_at": _as_str(frontmatter.get("occurred_at")),
                "follow_up_needed": frontmatter.get("follow_up_needed"),
            }.items()
            if value not in (None, [])
        }
    return {}


def _build_metadata(
    path: Path,
    source_ref: str,
    frontmatter: dict[str, Any],
    memory_type: str,
    title: str,
) -> dict[str, Any]:
    """Build Open Brain memory metadata for a note."""
    stat = path.stat()
    created_at = getattr(stat, "st_birthtime", stat.st_ctime)
    metadata: dict[str, Any] = {
        "source": "second_brain",
        "source_ref": source_ref,
        "source_path": str(path),
        "source_mtime": _iso_from_timestamp(stat.st_mtime),
        "source_created_at": _iso_from_timestamp(created_at),
        "frontmatter": frontmatter,
    }
    metadata.update(_type_specific_metadata(memory_type, source_ref, frontmatter, title))
    return metadata


def _parse_note(path: Path, vault_path: Path) -> ParsedNote | SkippedNote:
    """Parse a Markdown note or return a skipped-note result."""
    source_ref = _to_posix(path.relative_to(vault_path))
    try:
        text = path.read_text(encoding="utf-8")
        frontmatter, body = _split_frontmatter(text)
    except (OSError, UnicodeDecodeError):
        return SkippedNote(source_ref=source_ref, reason="unreadable")
    except (ValueError, yaml.YAMLError):
        return SkippedNote(source_ref=source_ref, reason="malformed_yaml")

    title = _as_str(frontmatter.get("title") or frontmatter.get("name")) or path.stem
    memory_type = map_note_type(source_ref, frontmatter)
    try:
        metadata = _build_metadata(path, source_ref, frontmatter, memory_type, title)
    except OSError:
        return SkippedNote(source_ref=source_ref, reason="unreadable")
    return ParsedNote(
        source_ref=source_ref,
        path=path,
        title=title,
        body=body,
        frontmatter=frontmatter,
        memory_type=memory_type,
        metadata=metadata,
        wikilinks=_extract_wikilinks(body),
        attachments=_extract_attachments(body, frontmatter),
    )


def _is_template_path(relative_path: Path) -> bool:
    """True if any parent directory component is an Obsidian template folder.

    Matches directory components named ``Templates``, ``templates``, or
    ``NN-Templates`` (e.g. ``80-Templates``), case-insensitively, anywhere in
    the path. Only DIRECTORY components are inspected (the filename is excluded
    via ``[:-1]``) so a real note whose filename merely contains "template" is
    not excluded.
    """
    return any(_TEMPLATE_DIR_RE.fullmatch(part) for part in relative_path.parts[:-1])


def _scan_vault(vault_path: Path) -> tuple[list[ParsedNote], list[SkippedNote]]:
    """Read and parse all Markdown notes in a vault.

    Files living under an Obsidian template directory are excluded entirely:
    template notes contain ``{{placeholder}}`` syntax that is not knowledge
    content, so they are neither parsed into ``notes`` nor recorded as
    ``skipped`` (which would otherwise block the cutover reconciliation gate).
    """
    notes: list[ParsedNote] = []
    skipped: list[SkippedNote] = []
    for path in sorted(vault_path.rglob("*.md")):
        relative = path.relative_to(vault_path)
        if _is_template_path(relative):
            continue
        parsed = _parse_note(path, vault_path)
        if isinstance(parsed, SkippedNote):
            skipped.append(parsed)
        else:
            notes.append(parsed)
    return notes, skipped


def _build_note_index(
    notes: list[ParsedNote],
) -> tuple[dict[str, ParsedNote], dict[str, list[ParsedNote]]]:
    """Build exact-path and basename indexes for two-pass wikilink resolution."""
    by_path: dict[str, ParsedNote] = {}
    by_basename: dict[str, list[ParsedNote]] = {}
    for note in notes:
        path_without_suffix = note.source_ref.removesuffix(".md")
        by_path[note.source_ref] = note
        by_path[path_without_suffix] = note
        basename = Path(note.source_ref).stem
        by_basename.setdefault(basename, []).append(note)
        by_basename.setdefault(f"{basename}.md", []).append(note)
    return by_path, by_basename


def _resolve_note_target(
    target: str,
    by_path: dict[str, ParsedNote],
    by_basename: dict[str, list[ParsedNote]],
) -> NoteResolution:
    """Resolve a normalized wikilink target to a parsed note."""
    target_path = target if target.endswith(".md") else f"{target}.md"
    if target in by_path:
        return NoteResolution(note=by_path[target], reason=None)
    if target_path in by_path:
        return NoteResolution(note=by_path[target_path], reason=None)
    if "/" in target:
        return NoteResolution(note=None, reason="target_not_found")

    candidates = by_basename.get(target) or by_basename.get(target_path) or []
    unique = {candidate.source_ref: candidate for candidate in candidates}
    if len(unique) == 1:
        return NoteResolution(note=next(iter(unique.values())), reason=None)
    if len(unique) > 1:
        return NoteResolution(note=None, reason="ambiguous_basename")
    return NoteResolution(note=None, reason="target_not_found")


def _load_paperless_mapping(path: Path | None) -> dict[str, dict[str, Any]]:
    """Load an attachment-to-Paperless mapping fixture."""
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("paperless mapping must be a JSON object")
    mapping: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, dict):
            mapping[_to_posix(Path(key))] = dict(value)
    return mapping


def _lookup_attachment_mapping(
    attachment: str,
    mapping: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Find a Paperless mapping entry by path or basename."""
    normalized = _to_posix(Path(attachment))
    candidates = [normalized, Path(normalized).name]
    for candidate in candidates:
        if candidate in mapping:
            return mapping[candidate]
    return None


async def _verify_attachment(
    *,
    note: ParsedNote,
    attachment: str,
    mapping: dict[str, dict[str, Any]],
    paperless_client: PaperlessClient,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Verify one attachment against Paperless and return reference or unresolved report."""
    mapped = _lookup_attachment_mapping(attachment, mapping)
    if mapped is None:
        return None, {
            "source_ref": note.source_ref,
            "attachment": attachment,
            "document_id": None,
            "reason": "no_mapping",
        }

    document_id = mapped.get("document_id")
    if not isinstance(document_id, int) or isinstance(document_id, bool) or document_id <= 0:
        return None, {
            "source_ref": note.source_ref,
            "attachment": attachment,
            "document_id": None,
            "reason": "malformed",
        }

    try:
        result = await paperless_client.resolve_reference(document_id)
    except Exception as exc:
        result = PaperlessResolveResult(
            status="transport_error",
            document_id=document_id,
            error=str(exc),
        )

    if result.status != "found":
        problem = {
            "source_ref": note.source_ref,
            "attachment": attachment,
            "document_id": document_id,
            "reason": result.status,
        }
        if result.status == "transport_error" and result.error:
            problem["error"] = result.error
        return None, problem

    reference = {
        "document_id": result.document_id or document_id,
        "instance": str(mapped.get("instance") or "paperless"),
        "title": result.title or f"Paperless document {document_id}",
        "added": result.added or "",
    }
    return reference, None


async def _verify_attachments(
    notes: list[ParsedNote],
    mapping: dict[str, dict[str, Any]],
    paperless_client: PaperlessClient,
) -> list[dict[str, Any]]:
    """Verify all note attachments and collect unresolved attachment entries."""
    unresolved: list[dict[str, Any]] = []
    for note in notes:
        for attachment in note.attachments:
            reference, problem = await _verify_attachment(
                note=note,
                attachment=attachment,
                mapping=mapping,
                paperless_client=paperless_client,
            )
            if reference is not None:
                note.paperless_references.append(reference)
            if problem is not None:
                unresolved.append(problem)
    return unresolved


def _item(
    *,
    source_ref: str,
    memory_type: str | None,
    action: ItemAction,
    memory_id: int | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build one reconciliation report item."""
    return {
        "source_ref": source_ref,
        "type": memory_type,
        "action": action,
        "memory_id": memory_id,
        "reason": reason,
    }


def _summary(
    items: list[dict[str, Any]],
    unresolved_links: list[dict[str, Any]],
    unresolved_attachments: list[dict[str, Any]],
) -> dict[str, int]:
    """Build reconciliation summary counts."""
    return {
        "importable": sum(1 for item in items if item["action"] == "import"),
        "imported": sum(
            1
            for item in items
            if item["action"] == "import" and item["memory_id"] is not None
        ),
        "duplicate": sum(1 for item in items if item["action"] == "duplicate"),
        "skipped": sum(1 for item in items if item["action"] == "skip"),
        "unresolved_links": len(unresolved_links),
        "unresolved_attachments": len(unresolved_attachments),
    }


async def import_vault(
    *,
    vault_path: str | Path,
    paperless_mapping_path: str | Path | None = None,
    data_layer: DataLayer | None = None,
    paperless_client: PaperlessClient | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Import or dry-run a Second Brain vault and return a reconciliation report."""
    resolved_vault_path = Path(vault_path).expanduser().resolve()
    if not resolved_vault_path.is_dir():
        raise ValueError(
            f"Vault path does not exist or is not a directory: {resolved_vault_path}"
        )
    if data_layer is None:
        from open_brain.data_layer.postgres import PostgresDataLayer

        data_layer = PostgresDataLayer()
    if paperless_client is None:
        paperless_client = PaperlessClient()

    if apply:
        with ingest_run() as run_id:
            return await _import_vault_reconcile(
                vault_path=resolved_vault_path,
                paperless_mapping_path=paperless_mapping_path,
                data_layer=data_layer,
                paperless_client=paperless_client,
                mode="apply",
                run_id=run_id,
            )

    return await _import_vault_reconcile(
        vault_path=resolved_vault_path,
        paperless_mapping_path=paperless_mapping_path,
        data_layer=data_layer,
        paperless_client=paperless_client,
        mode="dry_run",
        run_id=None,
    )


async def _import_vault_reconcile(
    *,
    vault_path: Path,
    paperless_mapping_path: str | Path | None,
    data_layer: DataLayer,
    paperless_client: PaperlessClient,
    mode: ImportMode,
    run_id: str | None,
) -> dict[str, Any]:
    """Run reconciliation and optional writes for a vault import."""
    mapping_path = (
        Path(paperless_mapping_path).expanduser().resolve()
        if paperless_mapping_path
        else None
    )
    mapping = _load_paperless_mapping(mapping_path)
    notes, skipped_notes = _scan_vault(vault_path)
    statuses = await data_layer.ingest_status_by_source_refs(
        [note.source_ref for note in notes],
        memory_type=None,
    )
    content_hashes_by_source_ref: dict[str, str] = {}
    memory_ids_by_content_hash: dict[str, int] = {}
    if mode == "dry_run":
        content_hashes_by_source_ref = {
            note.source_ref: hashlib.sha256(note.body.encode()).hexdigest()
            for note in notes
            if not statuses.get(note.source_ref, {}).get("ingested")
        }
        content_hash_lookup = getattr(data_layer, "memory_ids_by_content_hashes", None)
        if callable(content_hash_lookup):
            memory_ids_by_content_hash = await content_hash_lookup(
                list(content_hashes_by_source_ref.values()),
                index_id=1,
            )

    by_path, by_basename = _build_note_index(notes)
    items_by_source_ref: dict[str, dict[str, Any]] = {}
    importable_notes: list[ParsedNote] = []
    importable_content_hashes: set[str] = set()
    memory_ids_by_source_ref: dict[str, int] = {}
    for skipped in skipped_notes:
        items_by_source_ref[skipped.source_ref] = _item(
            source_ref=skipped.source_ref,
            memory_type=None,
            action="skip",
            reason=skipped.reason,
        )

    for note in notes:
        status = statuses.get(note.source_ref, {})
        if status.get("ingested"):
            memory_id = status.get("memory_id")
            if isinstance(memory_id, int):
                memory_ids_by_source_ref[note.source_ref] = memory_id
            items_by_source_ref[note.source_ref] = _item(
                source_ref=note.source_ref,
                memory_type=note.memory_type,
                action="duplicate",
                memory_id=memory_id,
                reason="already_ingested",
            )
            continue
        if mode == "dry_run":
            content_hash = content_hashes_by_source_ref[note.source_ref]
            content_memory_id = memory_ids_by_content_hash.get(content_hash)
            if content_memory_id is not None or content_hash in importable_content_hashes:
                if content_memory_id is not None:
                    memory_ids_by_source_ref[note.source_ref] = content_memory_id
                items_by_source_ref[note.source_ref] = _item(
                    source_ref=note.source_ref,
                    memory_type=note.memory_type,
                    action="duplicate",
                    memory_id=content_memory_id,
                    reason="content_hash_collision",
                )
                continue
            importable_content_hashes.add(content_hash)
        importable_notes.append(note)
        items_by_source_ref[note.source_ref] = _item(
            source_ref=note.source_ref,
            memory_type=note.memory_type,
            action="import",
        )

    unresolved_links: list[dict[str, Any]] = []
    for note in importable_notes:
        for link in note.wikilinks:
            resolution = _resolve_note_target(link.target, by_path, by_basename)
            if resolution.reason is not None:
                unresolved_links.append({
                    "source_ref": note.source_ref,
                    "wikilink": link.raw,
                    "reason": resolution.reason,
                })

    unresolved_attachments = await _verify_attachments(
        importable_notes,
        mapping,
        paperless_client,
    )

    if mode == "apply":
        relationship_source_refs: set[str] = set()
        for note in importable_notes:
            metadata = dict(note.metadata)
            if note.paperless_references:
                references = [dict(reference) for reference in note.paperless_references]
                metadata["paperless_references"] = references
                metadata["paperless_reference"] = references[0]
            binary_keys = paperless_reference_binary_keys(metadata)
            if binary_keys:
                raise ValueError(
                    "paperless_reference metadata must not include binary payload keys: "
                    + ", ".join(sorted(binary_keys))
                )
            result = await data_layer.save_memory(
                SaveMemoryParams(
                    text=note.body,
                    type=note.memory_type,
                    title=note.title,
                    metadata=metadata,
                    provenance={
                        "producer": "second-brain-import",
                        "source_ref": f"second-brain:{note.source_ref}",
                    },
                    dedup_mode="skip",
                )
            )
            items_by_source_ref[note.source_ref]["memory_id"] = result.id
            memory_ids_by_source_ref[note.source_ref] = result.id
            if result.duplicate_of is not None:
                items_by_source_ref[note.source_ref] = _item(
                    source_ref=note.source_ref,
                    memory_type=note.memory_type,
                    action="duplicate",
                    memory_id=result.id,
                    reason="content_hash_collision",
                )
                continue
            relationship_source_refs.add(note.source_ref)

        for note in importable_notes:
            if note.source_ref not in relationship_source_refs:
                continue
            source_id = memory_ids_by_source_ref.get(note.source_ref)
            if source_id is None:
                continue
            for link in note.wikilinks:
                resolution = _resolve_note_target(link.target, by_path, by_basename)
                if resolution.note is None:
                    continue
                target_id = memory_ids_by_source_ref.get(resolution.note.source_ref)
                if target_id is None:
                    continue
                await data_layer.create_relationship(
                    source_id=source_id,
                    target_id=target_id,
                    link_type="references",
                    metadata={
                        "source_ref": note.source_ref,
                        "target_source_ref": resolution.note.source_ref,
                        "wikilink": link.raw,
                    },
                )

    items = [items_by_source_ref[source_ref] for source_ref in sorted(items_by_source_ref)]
    unresolved_links.sort(key=lambda row: (row["source_ref"], row["wikilink"]))
    unresolved_attachments.sort(key=lambda row: (row["source_ref"], row["attachment"]))

    return {
        "mode": mode,
        "run_id": run_id,
        "vault_path": str(vault_path),
        "summary": _summary(items, unresolved_links, unresolved_attachments),
        "items": items,
        "unresolved_links": unresolved_links,
        "unresolved_attachments": unresolved_attachments,
    }


async def _main_async(argv: list[str] | None = None) -> int:
    """CLI entry point for one-shot vault imports."""
    from open_brain.data_layer.postgres import suppress_migrations

    parser = argparse.ArgumentParser(description="Import a Second Brain Markdown vault.")
    parser.add_argument("vault_path", help="Path to the vault root.")
    parser.add_argument(
        "--paperless-map",
        dest="paperless_mapping_path",
        help="JSON mapping from attachment names to Paperless document ids.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write importable notes. Without this flag the importer runs in dry-run mode.",
    )
    parser.add_argument(
        "--output",
        help="Optional path for the machine-readable reconciliation report.",
    )
    args = parser.parse_args(argv)

    # Dry-run is read-only and must perform zero writes, so it opts out of the
    # migration battery. Apply mode writes memories and relationships, so it keeps
    # the default migrating behavior to ensure the schema is current before writing.
    if not args.apply:
        suppress_migrations()

    report = await import_vault(
        vault_path=args.vault_path,
        paperless_mapping_path=args.paperless_mapping_path,
        apply=args.apply,
    )
    payload = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the Second Brain importer CLI."""
    return asyncio.run(_main_async(argv))


if __name__ == "__main__":
    sys.exit(main())
