"""CLI entry point for the ob command."""

import argparse
import asyncio
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, NoReturn

from open_brain.cli.client import MCPError, call_tool
from open_brain.data_layer.postgres import PostgresDataLayer, suppress_migrations
from open_brain.portable_backup import export_bundle, restore_bundle, verify_round_trip
from open_brain.runtime import run_server
from open_brain.session_learning_analysis import analyze_session_learnings


def _output(data: Any, pretty: bool) -> None:
    """Print data as JSON to stdout.

    Args:
        data: Data to serialize.
        pretty: If True, use indented formatting.
    """
    if pretty:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(data, ensure_ascii=False))


def _wants_json(args: argparse.Namespace) -> bool:
    """Return True when a command should emit machine-readable JSON."""
    return bool(getattr(args, "json_output", False))


def _should_render_people_list(args: argparse.Namespace) -> bool:
    """Return True when people list should use terminal-oriented output."""
    return (
        args.command == "people"
        and args.people_command == "list"
        and not _wants_json(args)
    )


def _should_render_macwhisper(args: argparse.Namespace) -> bool:
    """Return True when MacWhisper output should be terminal-oriented."""
    return (
        args.command == "ingest"
        and args.ingest_command == "macwhisper"
        and args.macwhisper_command in {"list", "entry", "ingest"}
        and not _wants_json(args)
    )


def _should_render_learning_analysis(args: argparse.Namespace) -> bool:
    """Return True when learning analysis should use terminal output."""
    return (
        args.command == "learnings"
        and args.learnings_command == "analyze"
        and not _wants_json(args)
    )


def _output_result(data: Any, args: argparse.Namespace) -> None:
    """Print command result using the command's default presentation."""
    if _should_render_people_list(args) and isinstance(data, dict):
        from open_brain.people.merge import render_persons_payload

        print(render_persons_payload(data), end="")
        return

    if _should_render_macwhisper(args) and isinstance(data, dict):
        print(_render_macwhisper_payload(data, args), end="")
        return

    if _should_render_learning_analysis(args) and isinstance(data, dict):
        print(_render_learning_analysis(data), end="")
        return

    _output(data, pretty=args.pretty)


def _render_macwhisper_payload(data: dict[str, Any], args: argparse.Namespace) -> str:
    """Render MacWhisper command output for humans."""
    if args.macwhisper_command == "list":
        return _render_macwhisper_list(data)
    return _render_macwhisper_entry(data, args)


def _render_learning_analysis(data: dict[str, Any]) -> str:
    """Render the manual analysis queues for terminal review."""
    def append_review_details(item: dict[str, Any]) -> None:
        confidence = item.get("confidence")
        if isinstance(confidence, (int, float)):
            lines.append(f"  Confidence: {confidence:.2f}")
        severity = item.get("severity")
        if severity:
            lines.append(f"  Severity: {severity}")
        evidence = item.get("evidence") or []
        if evidence:
            lines.append(f"  Evidence: {'; '.join(str(value) for value in evidence)}")

    counts = data.get("counts") or {}
    queues = data.get("queues") or {}
    lines = [
        "Session learning analysis",
        f"Source summaries: {counts.get('source_summaries', 0)}",
        f"Candidates: {counts.get('candidates', 0)}",
        f"Reviewable learning clusters: {counts.get('reviewable_learning_clusters', 0)}",
        f"Reviewed learning clusters: {counts.get('reviewed_learning_clusters', 0)}",
        f"Held learning clusters: {counts.get('held_learning_clusters', 0)}",
        f"Concrete work items: {counts.get('todos', 0)}",
        f"Decisions: {counts.get('decisions', 0)}",
        f"Standard candidates: {counts.get('standard_candidates', 0)}",
        f"Skill candidates: {counts.get('skill_candidates', 0)}",
        f"Duplicate doctrine: {counts.get('duplicate_doctrine', 0)}",
        f"Noise: {counts.get('noise', 0)}",
    ]

    reviewable = queues.get("reviewable_learning_clusters") or []
    if reviewable:
        lines.extend(["", "Reviewable learning clusters"])
        for cluster in reviewable:
            source_ids = ", ".join(
                str(memory_id) for memory_id in cluster.get("source_memory_ids", [])
            )
            lines.append(f"- {cluster.get('canonical_learning', '')}")
            lines.append(f"  Sources: {source_ids or '-'}")
            lines.append(f"  Review key: {cluster.get('review_key', '-')}")
            if cluster.get("review_identity_conflict"):
                lines.append(
                    "  Review identity conflict: multiple active clusters share this key"
                )
            conflicting_review = cluster.get("conflicting_review") or {}
            if conflicting_review:
                lines.append(
                    "  Prior review (identity conflict): "
                    f"{conflicting_review.get('decision', '-')} - "
                    f"{conflicting_review.get('canonical_learning', '-')} @ "
                    f"{conflicting_review.get('created_at', '-')}"
                )
            stale_review = cluster.get("stale_review") or {}
            if stale_review:
                lines.append(
                    "  Prior review (stale): "
                    f"{stale_review.get('decision', '-')} - "
                    f"{stale_review.get('canonical_learning', '-')} @ "
                    f"{stale_review.get('created_at', '-')}"
                )
            append_review_details(cluster)

    reviewed = queues.get("reviewed_learning_clusters") or []
    if reviewed:
        lines.extend(["", "Reviewed learning clusters"])
        for cluster in reviewed:
            review = cluster.get("review") or {}
            lines.append(f"- {cluster.get('canonical_learning', '')}")
            lines.append(f"  Review key: {cluster.get('review_key', '-')}")
            lines.append(f"  Decision: {review.get('decision', '-')}")
            lines.append(f"  Reason: {review.get('reason', '-')}")
            lines.append(f"  Reviewed by: {review.get('reviewed_by', '-')}")
            lines.append(f"  Reviewed at: {review.get('created_at', '-')}")
            if cluster.get("review_canonical_paraphrased"):
                lines.append("  Review match: bounded canonical paraphrase")
                lines.append(
                    f"  Approved snapshot: {review.get('canonical_learning', '-')}"
                )

    held = queues.get("held_learning_clusters") or []
    if held:
        lines.extend(["", "Held learning clusters"])
        for cluster in held:
            lines.append(f"- {cluster.get('canonical_learning', '')}")
            source_ids = ", ".join(
                str(memory_id) for memory_id in cluster.get("source_memory_ids", [])
            )
            lines.append(f"  Sources: {source_ids or '-'}")
            append_review_details(cluster)

    routed_sections = (
        ("Concrete work items", "todos"),
        ("Decisions", "decisions"),
        ("Standard candidates", "standard_candidates"),
        ("Skill candidates", "skill_candidates"),
        ("Duplicate doctrine", "duplicate_doctrine"),
        ("Noise", "noise"),
    )
    for heading, key in routed_sections:
        items = queues.get(key) or []
        if not items:
            continue
        lines.extend(["", heading])
        for item in items:
            source_id = item.get("source_memory_id", "-")
            lines.append(f"- [{source_id}] {item.get('statement', '')}")
            append_review_details(item)

    lines.extend(
        [
            "",
            "No memories, priorities, lifecycle states, or work items were changed.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_macwhisper_list(data: dict[str, Any]) -> str:
    """Render a MacWhisper list payload."""
    items = data.get("items") or []
    lines = [
        f"MacWhisper history: {data.get('history_path', '-')}",
        f"Entries shown: {data.get('count', len(items))}",
    ]
    scanned_count = data.get("scanned_count")
    if scanned_count is not None and scanned_count != data.get("count", len(items)):
        lines.append(f"Entries scanned: {scanned_count}")

    if not items:
        lines.append("No transcripts found.")
        return "\n".join(lines) + "\n"

    lines.append("")
    for item in items:
        entry_id = item.get("entry_id", "-")
        created_at = item.get("created_at") or "-"
        title = _single_line_preview(item.get("title") or "", limit=100)
        source_app = item.get("source_app") or ""
        duration = _format_duration(item.get("duration_seconds"))
        participants = item.get("participants") or []
        status = _format_ingest_status(item)
        preview = _single_line_preview(item.get("text_preview", ""))
        lines.append(f"{created_at}  {entry_id}")
        details = "  ".join(
            part for part in [title, source_app, duration] if part
        )
        if details:
            lines.append(f"  {details}")
        if status:
            lines.append(f"  Status: {status}")
        if participants:
            lines.append(f"  Participants: {', '.join(str(p) for p in participants)}")
        if preview:
            lines.append(f"  {preview}")

    lines.extend(["", "Ingest one entry with:", "  ob ingest macwhisper entry <entry-id>"])
    return "\n".join(lines) + "\n"


def _render_macwhisper_entry(data: dict[str, Any], args: argparse.Namespace) -> str:
    """Render a MacWhisper entry ingest result."""
    source_ref = args.source_ref or f"macwhisper:{args.entry_id}"
    lines = [
        "MacWhisper entry ingested",
        f"Entry: {args.entry_id}",
        f"Source ref: {source_ref}",
    ]

    if "meeting_memory_id" in data:
        lines.append(f"Meeting memory: {data['meeting_memory_id']}")
    if "run_id" in data:
        lines.append(f"Run ID: {data['run_id']}")
    if "skipped_count" in data and data["skipped_count"]:
        lines.append(f"Skipped: {data['skipped_count']}")

    count_fields = [
        ("People", "person_memory_ids"),
        ("Mentions", "mention_memory_ids"),
        ("Interactions", "interaction_memory_ids"),
        ("Relationships", "relationship_ids"),
        ("Follow-up candidates", "follow_up_candidates"),
    ]
    for label, key in count_fields:
        if isinstance(data.get(key), list):
            lines.append(f"{label}: {len(data[key])}")

    return "\n".join(lines) + "\n"


def _single_line_preview(text: str, limit: int = 120) -> str:
    """Collapse and shorten text for terminal previews."""
    preview = " ".join(str(text).split())
    if len(preview) <= limit:
        return preview
    return preview[: limit - 3].rstrip() + "..."


def _format_duration(value: Any) -> str:
    """Format a duration in seconds for compact terminal output."""
    if value is None or value == "":
        return ""
    try:
        seconds = int(round(float(value)))
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}h {minutes:02d}m"
    if minutes:
        return f"{minutes:d}m {seconds:02d}s"
    return f"{seconds:d}s"


def _format_ingest_status(item: dict[str, Any]) -> str:
    """Format optional ingest status for terminal output."""
    if "ingested" not in item:
        return ""
    if not item.get("ingested"):
        return "new"

    memory_id = item.get("memory_id")
    run_id = item.get("run_id")
    details: list[str] = []
    if memory_id is not None:
        details.append(f"memory {memory_id}")
    if run_id:
        details.append(f"run {run_id}")
    suffix = f" ({', '.join(details)})" if details else ""
    return f"ingested{suffix}"


def _error(msg: str) -> NoReturn:
    """Print error message to stderr and exit.

    Args:
        msg: Error message to display.
    """
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def _parse_json_object(value: str) -> dict[str, Any]:
    """Parse a CLI argument as a JSON object."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("metadata must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("metadata must be a JSON object")
    return parsed


async def _cmd_search(args: argparse.Namespace) -> Any:
    """Execute hybrid search.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Search results from MCP tool.
    """
    kwargs: dict[str, Any] = {"query": args.query}
    if args.limit:
        kwargs["limit"] = args.limit
    if args.project:
        kwargs["project"] = args.project
    if args.type:
        kwargs["type"] = args.type
    return await call_tool("search", kwargs)


async def _cmd_inbox(args: argparse.Namespace) -> Any:
    """List pending capture inbox memories."""
    kwargs: dict[str, Any] = {"capture_status": "inbox"}
    if args.limit:
        kwargs["limit"] = args.limit
    if args.project:
        kwargs["project"] = args.project
    return await call_tool("search", kwargs)


async def _cmd_concept(args: argparse.Namespace) -> Any:
    """Execute semantic-only search.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Search results from MCP tool.
    """
    kwargs: dict[str, Any] = {"query": args.query}
    if args.limit:
        kwargs["limit"] = args.limit
    if args.project:
        kwargs["project"] = args.project
    return await call_tool("search_by_concept", kwargs)


async def _cmd_save(args: argparse.Namespace) -> Any:
    """Save a new observation.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Save result from MCP tool.
    """
    kwargs: dict[str, Any] = {
        "text": args.text,
        "provenance": {
            "producer": args.producer,
            "source_ref": args.source_ref,
        },
    }
    if args.project:
        kwargs["project"] = args.project
    if args.type:
        kwargs["type"] = args.type
    if args.title:
        kwargs["title"] = args.title
    return await call_tool("save_memory", kwargs)


async def _cmd_get(args: argparse.Namespace) -> Any:
    """Fetch full observations by ID.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Observation data from MCP tool.
    """
    ids = [int(i) for i in args.ids]
    return await call_tool("get_observations", {"ids": ids})


async def _cmd_timeline(args: argparse.Namespace) -> Any:
    """Show timeline view.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Timeline data from MCP tool.
    """
    kwargs: dict[str, Any] = {}
    if args.anchor:
        kwargs["anchor"] = args.anchor
    if args.query:
        kwargs["query"] = args.query
    if args.project:
        kwargs["project"] = args.project
    if args.depth_before is not None:
        kwargs["depth_before"] = args.depth_before
    if args.depth_after is not None:
        kwargs["depth_after"] = args.depth_after
    return await call_tool("timeline", kwargs)


async def _cmd_daily(args: argparse.Namespace) -> Any:
    """Generate a daily review via MCP."""
    kwargs: dict[str, Any] = {
        "date": args.date or datetime.now().astimezone().date().isoformat()
    }
    if args.project:
        kwargs["project"] = args.project
    return await call_tool("daily_review", kwargs)


async def _cmd_context(args: argparse.Namespace) -> Any:
    """Get recent session context.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Context data from MCP tool.
    """
    kwargs: dict[str, Any] = {}
    if args.project:
        kwargs["project"] = args.project
    if args.limit:
        kwargs["limit"] = args.limit
    return await call_tool("get_context", kwargs)


async def _cmd_learnings(args: argparse.Namespace) -> Any:
    """Run manual session-learning analysis or explicit cluster review."""
    if args.learnings_command == "review":
        return await call_tool(
            "review_session_learning",
            {
                "review_key": args.review_key,
                "decision": args.decision,
                "reason": args.reason,
                "canonical_learning": args.canonical_learning,
            },
        )
    if args.learnings_command != "analyze":
        raise ValueError(f"Unknown learnings command: {args.learnings_command}")
    parameters = {
        "limit": args.limit,
        "project": args.project,
        "source": args.source,
        "model": args.model,
    }
    if args.direct or os.environ.get("OB_DIRECT") == "1":
        import open_brain.cli.direct as _direct

        database_url = _direct.load_database_url()
        if not database_url:
            _error(
                "--direct requires DATABASE_URL env var or DATABASE_URL in .env file"
            )
        _direct.prepare_direct_env(database_url)
        suppress_migrations()
        return await analyze_session_learnings(
            **parameters,
            allow_missing_review_ledger=True,
        )
    return await call_tool("analyze_session_learnings", parameters)


async def _cmd_stats(_args: argparse.Namespace) -> Any:
    """Get database statistics.

    Args:
        _args: Parsed CLI arguments (unused).

    Returns:
        Stats data from MCP tool.
    """
    return await call_tool("stats", {})


async def _cmd_doctor(_args: argparse.Namespace) -> Any:
    """Run server diagnostics through the MCP doctor tool."""
    return await call_tool("doctor", {})


async def _cmd_provenance(args: argparse.Namespace) -> Any:
    """Run a manual, read-only provenance report."""
    if args.provenance_command == "report":
        return await call_tool("origin_provenance_report", {})
    raise ValueError(f"Unknown provenance command: {args.provenance_command}")


async def _cmd_export(args: argparse.Namespace) -> Any:
    """Export a portable knowledge bundle."""
    suppress_migrations()
    return await export_bundle(
        Path(args.bundle_path),
        PostgresDataLayer(),
        source_label=args.source_label,
    )


async def _cmd_restore(args: argparse.Namespace) -> Any:
    """Restore a portable knowledge bundle."""
    return await restore_bundle(
        Path(args.bundle_path),
        PostgresDataLayer(),
        regenerate_embeddings=args.regenerate_embeddings,
    )


async def _cmd_verify(args: argparse.Namespace) -> Any:
    """Verify a portable knowledge bundle against the current store."""
    suppress_migrations()
    return await verify_round_trip(
        Path(args.bundle_path),
        PostgresDataLayer(),
    )


def _cmd_server(args: argparse.Namespace) -> None:
    """Start the open-brain MCP/HTTP server."""
    run_server(host=args.host, port=args.port)


@contextmanager
def _temporary_env_var(name: str, value: str | None) -> Iterator[None]:
    """Temporarily set one environment variable inside the CLI process."""
    if value is None:
        yield
        return

    had_old_value = name in os.environ
    old_value = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if not had_old_value:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old_value or ""


def _new_macwhisper_connector() -> Any:
    """Create a MacWhisper connector lazily so normal CLI startup stays cheap."""
    from open_brain.ingest.adapters.macwhisper import MacWhisperConnector

    return MacWhisperConnector()


async def _cmd_update(args: argparse.Namespace) -> Any:
    """Update an existing memory.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Update result from MCP tool.
    """
    kwargs: dict[str, Any] = {"id": int(args.id)}
    if args.text:
        kwargs["text"] = args.text
    if args.type:
        kwargs["type"] = args.type
    if args.project:
        kwargs["project"] = args.project
    if args.title:
        kwargs["title"] = args.title
    if args.subtitle:
        kwargs["subtitle"] = args.subtitle
    if args.narrative:
        kwargs["narrative"] = args.narrative
    if args.metadata is not None:
        kwargs["metadata"] = args.metadata
    return await call_tool("update_memory", kwargs)


async def _cmd_capture(args: argparse.Namespace) -> Any:
    """Manage capture inbox status."""
    if args.capture_command == "set-status":
        kwargs: dict[str, Any] = {
            "memory_id": args.memory_id,
            "capture_status": args.capture_status,
        }
        if args.lifecycle_status:
            kwargs["lifecycle_status"] = args.lifecycle_status
        return await call_tool("set_capture_status", kwargs)


async def _cmd_ingest_email(args: argparse.Namespace) -> Any:
    """Ingest emails from an IMAP inbox.

    Args:
        args: Parsed CLI arguments. Must have 'config' (op:// reference)
            and 'max_messages' (int).

    Returns:
        Ingest summary from MCP tool: {"ingested": N, "skipped": M, "run_id": "..."}.
    """
    kwargs: dict[str, Any] = {
        "config_ref": args.config,
        "max_messages": args.max_messages,
    }
    return await call_tool("ingest_email_inbox", kwargs)


async def _ingest_transcript_text(
    *,
    text: str,
    source_ref: str,
    medium_hint: str | None,
    direct: bool,
) -> Any:
    """Ingest transcript text through MCP or direct mode."""
    if not text.strip():
        _error("Empty input: transcript text must not be empty")

    if direct or os.environ.get("OB_DIRECT") == "1":
        import open_brain.cli.direct as _direct

        database_url = _direct.load_database_url()
        if not database_url:
            _error(
                "--direct requires DATABASE_URL env var or DATABASE_URL in .env file"
            )
        _direct.prepare_direct_env(database_url)
        return await _direct.run_ingest_transcript_direct(
            text=text,
            source_ref=source_ref,
            medium_hint=medium_hint,
        )

    kwargs: dict[str, Any] = {
        "text": text,
        "source_ref": source_ref,
    }
    if medium_hint:
        kwargs["medium_hint"] = medium_hint
    return await call_tool("ingest_transcript", kwargs)


async def _cmd_ingest_transcript(args: argparse.Namespace) -> Any:
    """Ingest a transcript from a file or stdin.

    Args:
        args: Parsed CLI arguments. Must have 'source_ref' (str), optionally
            'file' (str path) or 'stdin' (bool), and optionally 'medium_hint' (str).
            Pass '--direct' (or set OB_DIRECT=1) to bypass MCP transport and
            call PostgresDataLayer in-process. Requires DATABASE_URL to be set.

    Returns:
        Ingest summary from MCP tool (or direct-mode equivalent dict).
    """
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()

    return await _ingest_transcript_text(
        text=text,
        source_ref=args.source_ref,
        medium_hint=args.medium_hint,
        direct=getattr(args, "direct", False),
    )


async def _cmd_ingest_macwhisper_list(args: argparse.Namespace) -> Any:
    """List recent transcripts from the local MacWhisper history."""
    from open_brain.ingest.adapters.macwhisper import MacWhisperNotFoundError

    local_limit = args.limit
    if args.not_ingested:
        local_limit = args.scan_limit or max(args.limit * 5, 50)

    try:
        with _temporary_env_var("MACWHISPER_HISTORY_PATH", args.history_path):
            connector = _new_macwhisper_connector()
            history_path = connector.discover_history_path()
            refs = await connector.list_recent(n=local_limit)
    except (MacWhisperNotFoundError, RuntimeError) as exc:
        _error(str(exc))

    items = [asdict(ref) for ref in refs]
    if args.status or args.not_ingested:
        try:
            items = await _attach_ingest_status(items)
        except MCPError as exc:
            _error(f"Could not check ingest status: {exc}")

    scanned_count = len(items)
    if args.not_ingested:
        items = [item for item in items if not item.get("ingested")]
    items = items[: args.limit]

    payload = {
        "history_path": str(history_path),
        "count": len(items),
        "items": items,
    }
    if args.status or args.not_ingested:
        payload["scanned_count"] = scanned_count
    return payload


async def _attach_ingest_status(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach open-brain ingest status to MacWhisper list items."""
    if not items:
        return items

    source_ref_candidates = [
        _macwhisper_source_ref_candidates(str(item.get("entry_id", "")))
        for item in items
    ]
    source_refs = _dedupe_preserve_order(
        ref
        for candidates in source_ref_candidates
        for ref in candidates
    )
    statuses = await _fetch_ingest_statuses(source_refs)

    enriched: list[dict[str, Any]] = []
    for item, candidates in zip(items, source_ref_candidates):
        status = next(
            (
                statuses.get(source_ref)
                for source_ref in candidates
                if (statuses.get(source_ref) or {}).get("ingested")
            ),
            None,
        )
        primary_ref = candidates[0] if candidates else ""
        status = status or statuses.get(primary_ref) or {
            "source_ref": primary_ref,
            "ingested": False,
            "memory_id": None,
            "run_id": None,
            "ingested_at": None,
            "title": None,
        }
        status = dict(status)
        ingested_title = status.pop("title", None)
        updated = dict(item)
        updated.update(status)
        if ingested_title:
            updated["ingested_title"] = ingested_title
        enriched.append(updated)
    return enriched


async def _fetch_ingest_statuses(source_refs: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch ingest statuses in server-sized chunks."""
    statuses: dict[str, dict[str, Any]] = {}
    chunk_size = 500
    for start in range(0, len(source_refs), chunk_size):
        chunk = source_refs[start: start + chunk_size]
        payload = await call_tool("ingest_status", {"source_refs": chunk})
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            source_ref = item.get("source_ref")
            if isinstance(source_ref, str):
                statuses[source_ref] = item
    return statuses


def _macwhisper_source_ref_candidates(entry_id: str) -> list[str]:
    """Return canonical and legacy source_ref candidates for a MacWhisper entry."""
    normalized = entry_id.strip()
    if not normalized:
        return []

    candidates = [f"macwhisper:{normalized}"]
    if ":" in normalized:
        _prefix, raw_id = normalized.split(":", 1)
        if raw_id:
            candidates.append(f"macwhisper:{raw_id}")
    return _dedupe_preserve_order(candidates)


def _dedupe_preserve_order(values: Any) -> list[str]:
    """Return non-empty unique strings in input order."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


async def _cmd_ingest_macwhisper_ingest(args: argparse.Namespace) -> Any:
    """Read one local MacWhisper transcript and ingest it through open-brain."""
    from open_brain.ingest.adapters.macwhisper import MacWhisperNotFoundError

    try:
        with _temporary_env_var("MACWHISPER_HISTORY_PATH", args.history_path):
            connector = _new_macwhisper_connector()
            text, metadata = connector.read_entry(args.entry_id)
    except (FileNotFoundError, MacWhisperNotFoundError, RuntimeError) as exc:
        _error(str(exc))

    source_ref = args.source_ref or f"macwhisper:{args.entry_id}"
    medium_hint = args.medium_hint or metadata.get("medium") or "macwhisper"
    return await _ingest_transcript_text(
        text=text,
        source_ref=source_ref,
        medium_hint=medium_hint,
        direct=getattr(args, "direct", False),
    )


async def _cmd_ingest_macwhisper(args: argparse.Namespace) -> Any:
    """Dispatch MacWhisper ingest subcommands."""
    if args.macwhisper_command == "list":
        return await _cmd_ingest_macwhisper_list(args)
    if args.macwhisper_command in {"entry", "ingest"}:
        return await _cmd_ingest_macwhisper_ingest(args)
    raise ValueError(f"Unknown macwhisper command: {args.macwhisper_command}")


async def _cmd_ingest(args: argparse.Namespace) -> Any:
    """Dispatch ingest subcommands.

    Args:
        args: Parsed CLI arguments. Must have 'ingest_command' set by argparse.

    Returns:
        Result from the dispatched ingest subcommand.
    """
    if args.ingest_command == "email":
        return await _cmd_ingest_email(args)
    if args.ingest_command == "transcript":
        return await _cmd_ingest_transcript(args)
    if args.ingest_command == "macwhisper":
        return await _cmd_ingest_macwhisper(args)
    raise ValueError(f"Unknown ingest command: {args.ingest_command}")


async def _cmd_people_list(args: argparse.Namespace) -> Any:
    """List person memories through the server-side MCP tool."""
    kwargs: dict[str, Any] = {}
    if args.include_merged:
        kwargs["include_merged"] = True
    if args.collisions:
        kwargs["collisions_only"] = True
    return await call_tool("people_list", kwargs)


async def _cmd_people_merge(args: argparse.Namespace) -> Any:
    """Merge duplicate person memories through the server-side MCP tool."""
    kwargs: dict[str, Any] = {
        "source_id": int(args.source),
        "target_id": int(args.target),
    }
    if args.dry_run:
        kwargs["dry_run"] = True
    if args.absorb_text:
        kwargs["absorb_text"] = True
    return await call_tool("people_merge", kwargs)


async def _cmd_people_enrichment(args: argparse.Namespace) -> Any:
    """Run people enrichment: search and optionally apply org/role data.

    Operates in direct mode only (requires DATABASE_URL). Lists all person
    memories with enrich_pending=True, queries SearXNG for each, and either
    prompts the user (interactive) or auto-applies when confidence is
    sufficient (--auto-apply).

    Args:
        args: Parsed CLI arguments. Must have 'auto_apply' (bool),
            'min_confidence' (float), and optionally 'searxng_url' (str).
    """
    import open_brain.cli.direct as _direct

    database_url = _direct.load_database_url()
    if not database_url:
        _error("people enrichment requires DATABASE_URL env var or DATABASE_URL in .env file")

    _direct.prepare_direct_env(database_url)

    from open_brain.config import get_config
    from open_brain.data_layer.postgres import PostgresDataLayer, close_pool
    from open_brain.people.enrichment import (
        apply_enrichment,
        list_enrichment_candidates,
        search_person_web,
        should_auto_apply,
    )

    from open_brain.cli.client import _load_searxng_url

    # Resolution order: --searxng-url arg → OB_SEARXNG_URL env / XDG config → server SEARXNG_URL
    searxng_url: str = (
        getattr(args, "searxng_url", None)
        or _load_searxng_url()
        or get_config().SEARXNG_URL
    )
    auto_apply: bool = getattr(args, "auto_apply", False)
    min_confidence: float = getattr(args, "min_confidence", 0.8)

    if not searxng_url:
        _error(
            "SEARXNG_URL is not configured. Options:\n"
            "  1. Add to ~/.config/open-brain/config.json: {\"searxng_url\": \"http://...\"}\n"
            "  2. Set OB_SEARXNG_URL env var\n"
            "  3. Set SEARXNG_URL in the server .env\n"
            "  4. Pass --searxng-url <URL>"
        )

    applied = 0
    skipped = 0

    try:
        dl = PostgresDataLayer()
        candidates = await list_enrichment_candidates(dl)

        if not candidates:
            print("No enrichment candidates found.")
            return None

        print(f"Found {len(candidates)} enrichment candidate(s).")

        for candidate in candidates:
            print(f"\n--- {candidate.name} (memory {candidate.memory_id}) ---")
            if candidate.transcript_context:
                preview = candidate.transcript_context[:200]
                print(f"Context: {preview}")

            results = await search_person_web(
                name=candidate.name,
                context=candidate.transcript_context,
                searxng_url=searxng_url,
            )

            if not results:
                print("  No web results found.")
                skipped += 1
                continue

            best = results[0]
            print("  Best match:")
            print(f"    Org:         {best.org or '—'}")
            print(f"    Role:        {best.role or '—'}")
            print(f"    Profile URL: {best.profile_url or '—'}")
            print(f"    Confidence:  {best.confidence:.2f}")
            print(f"    Source:      {best.provenance_url or '—'}")
            if best.provenance_snippet:
                snippet = best.provenance_snippet[:150]
                print(f"    Snippet:     {snippet}")

            if auto_apply:
                if should_auto_apply(best, min_confidence=min_confidence):
                    await apply_enrichment(dl, candidate.memory_id, best)
                    print(f"  Auto-applied enrichment for {candidate.name}.")
                    applied += 1
                else:
                    print(
                        f"  Skipped (confidence {best.confidence:.2f} < threshold "
                        f"{min_confidence:.2f} or < 0.6 minimum)."
                    )
                    skipped += 1
            else:
                # Interactive prompt
                try:
                    answer = input(f"Apply enrichment for {candidate.name}? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\nAborted.")
                    break

                if answer == "y":
                    await apply_enrichment(dl, candidate.memory_id, best)
                    print(f"  Applied enrichment for {candidate.name}.")
                    applied += 1
                else:
                    print("  Skipped.")
                    skipped += 1

        print(f"\nSummary: {applied} applied, {skipped} skipped.")
        return None

    finally:
        await close_pool()


async def _cmd_people(args: argparse.Namespace) -> Any:
    """Dispatch people subcommands."""
    if args.people_command == "list":
        return await _cmd_people_list(args)
    if args.people_command == "merge":
        return await _cmd_people_merge(args)
    if args.people_command in {"enrichment", "enrich"}:
        return await _cmd_people_enrichment(args)
    raise ValueError(f"Unknown people command: {args.people_command}")


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        prog="ob",
        description="open-brain CLI — run, query, and manage your memory store",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output machine-readable JSON where commands have human defaults",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    # search
    p_search = subparsers.add_parser(
        "search",
        help="Hybrid search (vector + FTS)",
    )
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, help="Maximum number of results")
    p_search.add_argument("--project", help="Filter by project")
    p_search.add_argument("--type", help="Filter by memory type")

    # inbox
    p_inbox = subparsers.add_parser(
        "inbox",
        help="List pending capture inbox memories",
    )
    p_inbox.add_argument("--limit", type=int, help="Maximum number of results")
    p_inbox.add_argument("--project", help="Filter by project")

    # concept
    p_concept = subparsers.add_parser(
        "concept",
        help="Semantic-only (vector) search",
    )
    p_concept.add_argument("query", help="Search query")
    p_concept.add_argument("--limit", type=int, help="Maximum number of results")
    p_concept.add_argument("--project", help="Filter by project")

    # save
    p_save = subparsers.add_parser(
        "save",
        help="Save a new observation",
    )
    p_save.add_argument("text", help="Text content to save")
    p_save.add_argument("--project", help="Project to associate with")
    p_save.add_argument("--type", help="Memory type (observation, decision, etc.)")
    p_save.add_argument("--title", help="Optional title")
    p_save.add_argument(
        "--producer",
        default="ob-cli",
        help="Origin producer (default: ob-cli)",
    )
    p_save.add_argument(
        "--source-ref",
        required=True,
        dest="source_ref",
        help="Stable namespaced origin, e.g. agent-session:codex:<session-id>",
    )

    # get
    p_get = subparsers.add_parser(
        "get",
        help="Fetch full observations by ID",
    )
    p_get.add_argument("ids", nargs="+", metavar="ID", help="Observation IDs to fetch")

    # timeline
    p_timeline = subparsers.add_parser(
        "timeline",
        help="Show timeline view",
    )
    p_timeline.add_argument("--anchor", type=int, help="Anchor observation ID")
    p_timeline.add_argument("--query", help="Query to find timeline anchor")
    p_timeline.add_argument("--project", help="Filter by project")
    p_timeline.add_argument(
        "--depth-before",
        type=int,
        dest="depth_before",
        help="Number of entries before anchor",
    )
    p_timeline.add_argument(
        "--depth-after",
        type=int,
        dest="depth_after",
        help="Number of entries after anchor",
    )

    # daily
    p_daily = subparsers.add_parser(
        "daily",
        help="Generate a daily memory review",
    )
    p_daily.add_argument("date", nargs="?", help="Date to review (YYYY-MM-DD; default: today)")
    p_daily.add_argument("--project", help="Filter by project")

    # context
    p_context = subparsers.add_parser(
        "context",
        help="Get recent session context",
    )
    p_context.add_argument("--project", help="Filter by project")
    p_context.add_argument("--limit", type=int, help="Maximum number of results")

    # learnings
    p_learnings = subparsers.add_parser(
        "learnings",
        help="Analyze session summaries into typed learning and work queues",
    )
    learnings_sub = p_learnings.add_subparsers(
        dest="learnings_command",
        metavar="ACTION",
    )
    learnings_sub.required = True
    p_learnings_analyze = learnings_sub.add_parser(
        "analyze",
        help="Analyze recent session summaries without writing results",
    )
    p_learnings_analyze.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Newest session summaries to analyze (default: 50; maximum: 200)",
    )
    p_learnings_analyze.add_argument("--project", help="Filter by project")
    p_learnings_analyze.add_argument(
        "--source",
        help="Filter by session-summary metadata.source",
    )
    p_learnings_analyze.add_argument(
        "--model",
        help="Override the configured LLM model",
    )
    p_learnings_analyze.add_argument(
        "--direct",
        action="store_true",
        help="Bypass MCP transport and use a local DATABASE_URL",
    )
    p_learnings_review = learnings_sub.add_parser(
        "review",
        help="Record an explicit manual review decision for one learning cluster",
    )
    p_learnings_review.add_argument(
        "review_key",
        help="Stable review key printed by learnings analyze",
    )
    p_learnings_review.add_argument(
        "--decision",
        required=True,
        choices=("accept", "covered_obsolete", "project_only", "dismiss"),
        help="Manual classification only; no promotion or memory mutation",
    )
    p_learnings_review.add_argument(
        "--reason",
        required=True,
        help="Auditable reason for the decision",
    )
    p_learnings_review.add_argument(
        "--canonical-learning",
        required=True,
        dest="canonical_learning",
        help="Canonical learning snapshot shown by the analyzer",
    )

    # stats
    subparsers.add_parser(
        "stats",
        help="Show database statistics",
    )

    # doctor
    subparsers.add_parser(
        "doctor",
        help="Run server diagnostics",
    )

    # provenance
    p_provenance = subparsers.add_parser(
        "provenance",
        help="Inspect canonical origin-provenance coverage",
    )
    provenance_sub = p_provenance.add_subparsers(
        dest="provenance_command",
        metavar="ACTION",
    )
    provenance_sub.required = True
    provenance_sub.add_parser(
        "report",
        help="Classify coverage without modifying memories",
    )

    # export
    p_export = subparsers.add_parser(
        "export",
        help="Export a portable knowledge bundle",
    )
    p_export.add_argument("bundle_path", help="Bundle directory to write")
    p_export.add_argument(
        "--source-label",
        dest="source_label",
        help="Optional non-identifying source label for the manifest",
    )

    # restore
    p_restore = subparsers.add_parser(
        "restore",
        help="Restore a portable knowledge bundle into an empty store",
    )
    p_restore.add_argument("bundle_path", help="Bundle directory to restore")
    p_restore.add_argument(
        "--skip-embeddings",
        action="store_false",
        dest="regenerate_embeddings",
        default=True,
        help="Do not regenerate embeddings after restore",
    )

    # verify
    p_verify = subparsers.add_parser(
        "verify",
        help="Verify a portable bundle against the current store",
    )
    p_verify.add_argument("bundle_path", help="Bundle directory to verify")

    # server
    p_server = subparsers.add_parser(
        "server",
        help="Start the open-brain MCP/HTTP server",
    )
    p_server.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind host (default: 0.0.0.0)",
    )
    p_server.add_argument(
        "--port",
        type=int,
        help="Bind port (default: PORT env var or 8091)",
    )

    # update
    p_update = subparsers.add_parser(
        "update",
        help="Update an existing memory",
    )
    p_update.add_argument("id", help="Memory ID to update")
    p_update.add_argument("--text", help="New text content")
    p_update.add_argument("--type", help="New memory type")
    p_update.add_argument("--project", help="New project")
    p_update.add_argument("--title", help="New title")
    p_update.add_argument("--subtitle", help="New subtitle")
    p_update.add_argument("--narrative", help="New narrative")
    p_update.add_argument(
        "--metadata",
        type=_parse_json_object,
        help="Metadata fields to merge, as a JSON object",
    )

    # capture
    p_capture = subparsers.add_parser(
        "capture",
        help="Manage capture inbox status",
    )
    capture_sub = p_capture.add_subparsers(dest="capture_command", metavar="ACTION")
    capture_sub.required = True

    p_capture_status = capture_sub.add_parser(
        "set-status",
        help="Set capture status for a memory",
    )
    p_capture_status.add_argument("memory_id", type=int, help="Memory ID to update")
    p_capture_status.add_argument(
        "capture_status",
        help="Capture status: inbox, processed, or dismissed",
    )
    p_capture_status.add_argument(
        "--lifecycle-status",
        dest="lifecycle_status",
        help="Optionally update metadata.status explicitly",
    )

    # ingest
    p_ingest = subparsers.add_parser(
        "ingest",
        help="Ingest data from external sources",
    )
    ingest_sub = p_ingest.add_subparsers(dest="ingest_command", metavar="SOURCE")
    ingest_sub.required = True

    # ingest email
    p_ingest_email = ingest_sub.add_parser(
        "email",
        help="Ingest emails from an IMAP inbox",
    )
    p_ingest_email.add_argument(
        "--config",
        required=True,
        metavar="OP_REF",
        help="1Password op:// reference for IMAP app password",
    )
    p_ingest_email.add_argument(
        "--max-messages",
        type=int,
        default=50,
        dest="max_messages",
        help="Maximum number of emails to fetch (default: 50)",
    )

    # ingest transcript
    p_ingest_transcript = ingest_sub.add_parser(
        "transcript",
        help="Ingest transcript text from a file or stdin",
    )
    p_ingest_transcript.add_argument(
        "--source-ref",
        required=True,
        dest="source_ref",
        metavar="SOURCE_REF",
        help="Unique identifier for the transcript",
    )
    p_ingest_transcript.add_argument(
        "--medium-hint",
        dest="medium_hint",
        metavar="MEDIUM",
        help="Optional medium hint (e.g. macwhisper, dictation)",
    )
    _transcript_input = p_ingest_transcript.add_mutually_exclusive_group()
    _transcript_input.add_argument(
        "--file",
        metavar="PATH",
        help="Read transcript from file (default: read from stdin)",
    )
    _transcript_input.add_argument(
        "--stdin",
        action="store_true",
        help="Read transcript from stdin (default when --file is not given)",
    )
    p_ingest_transcript.add_argument(
        "--direct",
        action="store_true",
        help=(
            "Bypass MCP transport: call PostgresDataLayer directly in-process. "
            "Equivalent to setting OB_DIRECT=1 env var. "
            "Requires DATABASE_URL env var (or DATABASE_URL in .env). "
            "Use for local operator workflows. "
            "Not suitable for multi-user or sandboxed setups."
        ),
    )

    # ingest macwhisper
    p_ingest_macwhisper = ingest_sub.add_parser(
        "macwhisper",
        help="List/read local MacWhisper sessions and ingest transcript text",
    )
    macwhisper_sub = p_ingest_macwhisper.add_subparsers(
        dest="macwhisper_command",
        metavar="ACTION",
    )
    macwhisper_sub.required = True

    p_macwhisper_list = macwhisper_sub.add_parser(
        "list",
        help="List recent local MacWhisper transcript sessions/meetings",
    )
    p_macwhisper_list.add_argument(
        "--limit",
        "-n",
        type=int,
        default=10,
        help="Maximum number of entries to list (default: 10)",
    )
    p_macwhisper_list.add_argument(
        "--history-path",
        metavar="PATH",
        help="Override the MacWhisper history directory",
    )
    p_macwhisper_list.add_argument(
        "--status",
        action="store_true",
        help="Check the open-brain server and show whether each entry is already ingested",
    )
    p_macwhisper_list.add_argument(
        "--not-ingested",
        action="store_true",
        help="Only show entries not yet ingested; implies --status",
    )
    p_macwhisper_list.add_argument(
        "--scan-limit",
        type=int,
        metavar="N",
        help=(
            "Number of local entries to scan before filtering with --not-ingested "
            "(default: max(limit*5, 50))"
        ),
    )
    p_macwhisper_list.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=argparse.SUPPRESS,
        help="Output machine-readable JSON instead of the terminal display",
    )

    p_macwhisper_ingest = macwhisper_sub.add_parser(
        "entry",
        aliases=["ingest"],
        help="Ingest one local MacWhisper transcript by entry ID",
    )
    p_macwhisper_ingest.add_argument(
        "entry_id",
        metavar="ENTRY_ID",
        help="MacWhisper entry ID, usually the JSON filename without .json",
    )
    p_macwhisper_ingest.add_argument(
        "--history-path",
        metavar="PATH",
        help="Override the MacWhisper history directory",
    )
    p_macwhisper_ingest.add_argument(
        "--source-ref",
        dest="source_ref",
        metavar="SOURCE_REF",
        help="Override the default source_ref macwhisper:<ENTRY_ID>",
    )
    p_macwhisper_ingest.add_argument(
        "--medium-hint",
        dest="medium_hint",
        metavar="MEDIUM",
        help="Override the default medium hint from metadata or macwhisper",
    )
    p_macwhisper_ingest.add_argument(
        "--direct",
        action="store_true",
        help=(
            "Bypass MCP transport and call PostgresDataLayer directly. "
            "Requires DATABASE_URL env var or DATABASE_URL in .env."
        ),
    )
    p_macwhisper_ingest.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=argparse.SUPPRESS,
        help="Output machine-readable JSON instead of the terminal display",
    )

    # people
    p_people = subparsers.add_parser(
        "people",
        help="Manage people memories",
    )
    people_sub = p_people.add_subparsers(dest="people_command", metavar="ACTION")
    people_sub.required = True

    p_people_list = people_sub.add_parser(
        "list",
        help="List person memories",
    )
    p_people_list.add_argument(
        "--include-merged",
        action="store_true",
        help="Include soft-deleted records with metadata.merged_into",
    )
    p_people_list.add_argument(
        "--collisions",
        action="store_true",
        help="Show only first-token collision groups for manual merge review",
    )
    p_people_list.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=argparse.SUPPRESS,
        help="Output machine-readable JSON instead of the terminal display",
    )

    p_people_merge = people_sub.add_parser(
        "merge",
        help="Merge a duplicate person memory into the canonical one",
    )
    p_people_merge.add_argument(
        "--source",
        type=int,
        required=True,
        help="Source person memory ID to soft-delete",
    )
    p_people_merge.add_argument(
        "--target",
        type=int,
        required=True,
        help="Target person memory ID to keep",
    )
    p_people_merge.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing",
    )
    p_people_merge.add_argument(
        "--absorb-text",
        action="store_true",
        help="Append source content to target content as provenance",
    )

    # people enrichment (also accepts 'enrich' alias)
    p_people_enrich = people_sub.add_parser(
        "enrichment",
        aliases=["enrich"],
        help="Enrich person memories with org/role data from web search",
    )
    p_people_enrich.add_argument(
        "--auto-apply",
        action="store_true",
        dest="auto_apply",
        help=(
            "Apply enrichments non-interactively when confidence >= --min-confidence. "
            "Matches with confidence < 0.6 are NEVER auto-applied."
        ),
    )
    p_people_enrich.add_argument(
        "--min-confidence",
        type=float,
        default=0.8,
        dest="min_confidence",
        metavar="THRESHOLD",
        help=(
            "Minimum confidence score for auto-apply (default: 0.8). "
            "The hard floor of 0.6 applies regardless of this setting."
        ),
    )
    p_people_enrich.add_argument(
        "--searxng-url",
        dest="searxng_url",
        metavar="URL",
        help=(
            "SearXNG instance URL (overrides SEARXNG_URL env var). "
            "Example: http://localhost:8888"
        ),
    )

    return parser


_COMMAND_MAP = {
    "search": _cmd_search,
    "inbox": _cmd_inbox,
    "concept": _cmd_concept,
    "save": _cmd_save,
    "get": _cmd_get,
    "timeline": _cmd_timeline,
    "daily": _cmd_daily,
    "context": _cmd_context,
    "learnings": _cmd_learnings,
    "stats": _cmd_stats,
    "doctor": _cmd_doctor,
    "provenance": _cmd_provenance,
    "export": _cmd_export,
    "restore": _cmd_restore,
    "verify": _cmd_verify,
    "update": _cmd_update,
    "capture": _cmd_capture,
    "ingest": _cmd_ingest,
    "people": _cmd_people,
}


_SYNC_COMMAND_MAP = {
    "server": _cmd_server,
}


def main() -> None:
    """CLI entry point for the ob command."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.command in _SYNC_COMMAND_MAP:
        _SYNC_COMMAND_MAP[args.command](args)
        return

    handler = _COMMAND_MAP.get(args.command)
    if handler is None:
        _error(f"Unknown command: {args.command}")

    try:
        result = asyncio.run(handler(args))
        if result is not None:
            _output_result(result, args)
    except MCPError as e:
        _error(str(e))
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
