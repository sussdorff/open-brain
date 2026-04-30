"""CLI entry point for the ob command."""

import argparse
import asyncio
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

from open_brain.cli.client import MCPError, call_tool
from open_brain.runtime import run_server


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


def _should_render_people_list(args: argparse.Namespace) -> bool:
    """Return True when people list should use terminal-oriented output."""
    return (
        args.command == "people"
        and args.people_command == "list"
        and not getattr(args, "json_output", False)
        and not args.pretty
    )


def _output_result(data: Any, args: argparse.Namespace) -> None:
    """Print command result using the command's default presentation."""
    if _should_render_people_list(args) and isinstance(data, dict):
        from open_brain.people.merge import render_persons_payload

        print(render_persons_payload(data), end="")
        return

    _output(data, pretty=args.pretty)


def _error(msg: str) -> None:
    """Print error message to stderr and exit.

    Args:
        msg: Error message to display.
    """
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


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
    kwargs: dict[str, Any] = {"text": args.text}
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
    return await call_tool("update_memory", kwargs)


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

    try:
        with _temporary_env_var("MACWHISPER_HISTORY_PATH", args.history_path):
            connector = _new_macwhisper_connector()
            history_path = connector.discover_history_path()
            refs = await connector.list_recent(n=args.limit)
    except (MacWhisperNotFoundError, RuntimeError) as exc:
        _error(str(exc))

    return {
        "history_path": str(history_path),
        "count": len(refs),
        "items": [asdict(ref) for ref in refs],
    }


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


async def _cmd_people(args: argparse.Namespace) -> Any:
    """Dispatch people subcommands."""
    if args.people_command == "list":
        return await _cmd_people_list(args)
    if args.people_command == "merge":
        return await _cmd_people_merge(args)
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
        help="Output pretty-printed JSON",
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

    # context
    p_context = subparsers.add_parser(
        "context",
        help="Get recent session context",
    )
    p_context.add_argument("--project", help="Filter by project")
    p_context.add_argument("--limit", type=int, help="Maximum number of results")

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
        help="List/read local MacWhisper history and ingest transcript text",
    )
    macwhisper_sub = p_ingest_macwhisper.add_subparsers(
        dest="macwhisper_command",
        metavar="ACTION",
    )
    macwhisper_sub.required = True

    p_macwhisper_list = macwhisper_sub.add_parser(
        "list",
        help="List recent local MacWhisper transcript entries",
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

    return parser


_COMMAND_MAP = {
    "search": _cmd_search,
    "concept": _cmd_concept,
    "save": _cmd_save,
    "get": _cmd_get,
    "timeline": _cmd_timeline,
    "context": _cmd_context,
    "stats": _cmd_stats,
    "doctor": _cmd_doctor,
    "update": _cmd_update,
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
        _output_result(result, args)
    except MCPError as e:
        _error(str(e))
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
