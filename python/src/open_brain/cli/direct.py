"""Direct-mode runner for open-brain CLI.

Provides an in-process alternative to the MCP transport for operator workflows
where spawning a full MCP server round-trip is unnecessary overhead.

When to use --direct:
  - Local operator scripts that have direct database access (DATABASE_URL)
  - Batch ingestion pipelines where MCP transport latency adds up
  - Dev/test environments without a running open-brain server

When NOT to use --direct:
  - Multi-user setups (bypasses all MCP-layer auth and rate limiting)
  - Sandboxed agents that should not have direct DB access
  - When you do not control DATABASE_URL (rely on MCP auth instead)

Requires: DATABASE_URL env var (or DATABASE_URL in .env in the current
working directory). VOYAGE_API_KEY must also be set for embedding calls.
"""

import dataclasses
import os
from pathlib import Path

from open_brain.data_layer.postgres import PostgresDataLayer, close_pool
from open_brain.ingest.adapters.transcript import TranscriptIngestor


def load_database_url() -> str:
    """Load DATABASE_URL from environment or .env file in the current directory.

    Priority:
      1. DATABASE_URL environment variable
      2. DATABASE_URL line in ./.env file
      3. Empty string if neither is set

    .env file support is limited to lines of the form:
        DATABASE_URL=value
        DATABASE_URL="value"
        DATABASE_URL='value'
    Lines starting with # are skipped. Inline comments, export prefix, and
    multiline values are not supported.

    Returns:
        The DATABASE_URL value, or an empty string if not found.
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        return url

    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#"):
                continue
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip().strip("\"'")
                if url:
                    return url

    return ""


def prepare_direct_env(database_url: str) -> None:
    """Set environment variables so Config can load in direct mode.

    Config (pydantic-settings) requires several fields that are not needed
    for direct database access. This function injects dummy values for
    server-only fields while ensuring DATABASE_URL is set correctly.

    This does NOT override values that are already set — operators may
    have the full config available.

    Args:
        database_url: The DATABASE_URL to inject into the environment.
    """
    os.environ["DATABASE_URL"] = database_url
    # These server-only fields are required by pydantic-settings Config but not used in
    # direct mode. Set harmless defaults so Config.__init__ does not raise a
    # ValidationError. TODO: refactor Config to make server-only fields optional when
    # DATABASE_URL is the only credential needed (see follow-up bead after open-brain-c5e).
    os.environ.setdefault("MCP_SERVER_URL", "http://localhost:8091")
    os.environ.setdefault("AUTH_USER", "direct-mode")
    os.environ.setdefault("AUTH_PASSWORD", "direct-mode-local-placeholder")
    os.environ.setdefault("JWT_SECRET", "direct-mode-placeholder-jwt-secret-key-32chr")


async def run_ingest_transcript_direct(
    text: str,
    source_ref: str,
    medium_hint: str | None = None,
) -> dict:
    """Run transcript ingest directly, bypassing MCP transport.

    Creates a PostgresDataLayer and TranscriptIngestor in-process, calls
    ingest(), and returns the result as a plain dict with the same shape
    as the MCP ingest_transcript tool output.

    The database connection pool is always closed after the call (success
    or failure) to avoid leaving open connections.

    Args:
        text: The full transcript text to ingest.
        source_ref: Unique identifier for the transcript source.
        medium_hint: Optional medium hint (e.g. "macwhisper", "dictation").

    Returns:
        A dict matching the IngestResult dataclass fields:
        meeting_memory_id, person_memory_ids, mention_memory_ids,
        interaction_memory_ids, relationship_ids, follow_up_candidates,
        run_id, skipped_count.
    """
    try:
        dl = PostgresDataLayer()
        ingestor = TranscriptIngestor(data_layer=dl)
        result = await ingestor.ingest(
            text=text,
            source_ref=source_ref,
            medium_hint=medium_hint,
        )
        return dataclasses.asdict(result)
    finally:
        await close_pool()
