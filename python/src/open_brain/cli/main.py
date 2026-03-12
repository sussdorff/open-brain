"""CLI entry point for the ob command — thin wrapper for open-brain MCP tools."""

import argparse
import asyncio
import json
import sys
from typing import Any

from open_brain.cli.client import MCPError, call_tool


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


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        prog="ob",
        description="open-brain CLI — query and manage your memory store",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Output pretty-printed JSON",
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

    return parser


_COMMAND_MAP = {
    "search": _cmd_search,
    "concept": _cmd_concept,
    "save": _cmd_save,
    "get": _cmd_get,
    "timeline": _cmd_timeline,
    "context": _cmd_context,
    "stats": _cmd_stats,
    "update": _cmd_update,
}


def main() -> None:
    """CLI entry point for the ob command."""
    parser = _build_parser()
    args = parser.parse_args()

    handler = _COMMAND_MAP.get(args.command)
    if handler is None:
        _error(f"Unknown command: {args.command}")

    try:
        result = asyncio.run(handler(args))
        _output(result, pretty=args.pretty)
    except MCPError as e:
        _error(str(e))
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
