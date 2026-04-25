"""MacWhisperConnector — ingests MacWhisper transcripts into open-brain memory.

Discovery order for history path:
1. MACWHISPER_HISTORY_PATH config field (env var override)
2. ~/Library/Containers/com.goodsnooze.MacWhisper/Data/Library/Application Support/MacWhisper/
3. ~/Library/Application Support/MacWhisper/
4. mw --help / mw --version + parse stdout+stderr for path hint (via CommandRunner)
5. Raise MacWhisperNotFoundError(tried_paths=[...])
"""

import json
import logging
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from open_brain.data_layer.interface import DataLayer
from open_brain.ingest.adapters.transcript import TranscriptIngestor
from open_brain.ingest.models import IngestResult

logger = logging.getLogger(__name__)

# ─── CommandRunner Protocol and implementations ───────────────────────────────

_SAFE_SUBPROCESS_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "HOME": os.environ.get("HOME", ""),
}


class CommandRunner(Protocol):
    """Protocol for running subprocess commands."""

    def run(self, cmd: list[str]) -> tuple[int, str, str]:
        """Run a command and return (returncode, stdout, stderr)."""
        ...


class DefaultCommandRunner:
    """Default CommandRunner using subprocess."""

    def run(self, cmd: list[str]) -> tuple[int, str, str]:
        """Run a command via subprocess.

        Uses a safe env allowlist (PATH + HOME only).
        Returns (124, "", "timeout") if the command takes too long.
        """
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=_SAFE_SUBPROCESS_ENV,
                timeout=5,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"


# ─── Data models ─────────────────────────────────────────────────────────────


@dataclass
class TranscriptRef:
    """Reference to a MacWhisper transcript entry."""

    entry_id: str
    created_at: str
    text_preview: str


# ─── Errors ──────────────────────────────────────────────────────────────────


class MacWhisperNotFoundError(Exception):
    """Raised when MacWhisper history directory cannot be discovered.

    Includes the list of paths that were tried so callers can produce
    informative error messages.
    """

    def __init__(self, tried_paths: list[Path]) -> None:
        self.tried_paths = tried_paths
        paths_str = "\n  ".join(str(p) for p in tried_paths)
        super().__init__(
            f"MacWhisper history directory not found. Tried:\n  {paths_str}\n"
            "Install MacWhisper or set MACWHISPER_HISTORY_PATH to the correct path."
        )


# ─── Connector ───────────────────────────────────────────────────────────────

_CANDIDATE_PATHS = [
    Path.home() / "Library/Containers/com.goodsnooze.MacWhisper/Data/Library/Application Support/MacWhisper",
    Path.home() / "Library/Application Support/MacWhisper",
]


class MacWhisperConnector:
    """Discovers MacWhisper history and delegates ingest to TranscriptIngestor.

    Uses CommandRunner DI for mw subprocess calls (testable).
    Uses instance-level caching for discovered path (not module-level, to
    allow test isolation).

    Implements the IngestAdapter Protocol (ADR-0001).

    Args:
        data_layer: DataLayer implementation for persistence.
        command_runner: Optional CommandRunner for subprocess calls.
            Defaults to DefaultCommandRunner.
        ingestor: Optional TranscriptIngestor. Defaults to creating one from
            data_layer. Inject in tests to avoid patching.
        skip_platform_check: If True, skip the macOS platform check.
            Use in tests running on non-macOS platforms.
    """

    name = "macwhisper"

    def __init__(
        self,
        data_layer: DataLayer,
        command_runner: CommandRunner | None = None,
        ingestor: TranscriptIngestor | None = None,
        *,
        skip_platform_check: bool = False,
    ) -> None:
        if not skip_platform_check and platform.system() != "Darwin":
            raise RuntimeError(
                "MacWhisperConnector can only run on macOS. "
                "Use skip_platform_check=True in tests."
            )
        self._dl = data_layer
        self._runner = command_runner or DefaultCommandRunner()
        self._ingestor = ingestor if ingestor is not None else TranscriptIngestor(data_layer=data_layer)
        self._cached_path: Path | None = None

    def discover_history_path(self) -> Path:
        """Discover the MacWhisper history directory.

        Discovery order:
        1. MACWHISPER_HISTORY_PATH env var / config field
        2. Container sandbox path
        3. Application Support path
        4. mw CLI stdout+stderr parse
        5. Raise MacWhisperNotFoundError

        Returns:
            Path to the MacWhisper history directory.

        Raises:
            MacWhisperNotFoundError: If the directory cannot be found.
        """
        if self._cached_path is not None:
            return self._cached_path

        tried: list[Path] = []

        # 1. Config override via environment variable
        # Use os.environ.get() directly to avoid requiring a fully valid
        # app config (DATABASE_URL, VOYAGE_API_KEY, etc.) just to check
        # a single env var — a Pydantic validation error from get_config()
        # would otherwise mask the MacWhisper discovery entirely.
        override_str = os.environ.get("MACWHISPER_HISTORY_PATH", "")
        if override_str:
            override = Path(override_str)
            if override.exists():
                self._cached_path = override
                return override
            # If override is set but doesn't exist, add to tried and fall through
            tried.append(override)

        # 2 & 3. Standard candidate paths
        for candidate in _CANDIDATE_PATHS:
            if candidate.exists():
                self._cached_path = candidate
                return candidate
            tried.append(candidate)

        # 4. Try mw CLI to get a hint from stdout+stderr
        mw_path = self._try_mw_cli_path()
        if mw_path is not None and mw_path.exists():
            self._cached_path = mw_path
            return mw_path
        if mw_path is not None:
            tried.append(mw_path)

        raise MacWhisperNotFoundError(tried_paths=tried)

    def _try_mw_cli_path(self) -> Path | None:
        """Try to discover history path by running the mw CLI and parsing stdout+stderr.

        Uses safe introspection commands (--help or --version) rather than
        production commands.

        Returns:
            Discovered path or None if not found.
        """
        try:
            returncode, stdout, stderr = self._runner.run(["mw", "--help"])
            # Parse stdout+stderr for path hints.
            # Extract from the first '/' to end-of-line so that paths
            # containing spaces (e.g. "Application Support/MacWhisper")
            # are captured correctly instead of being truncated by split().
            for raw_line in (stdout + "\n" + stderr).splitlines():
                line = raw_line.strip()
                if "MacWhisper" in line and "/" in line:
                    candidate = line[line.index("/"):].strip()
                    if "MacWhisper" in candidate:
                        return Path(candidate)
        except Exception as exc:
            logger.debug("mw --help path discovery failed: %s", exc)

        try:
            returncode, stdout, stderr = self._runner.run(["mw", "--version"])
            for raw_line in (stdout + "\n" + stderr).splitlines():
                line = raw_line.strip()
                if "MacWhisper" in line and "/" in line:
                    candidate = line[line.index("/"):].strip()
                    if "MacWhisper" in candidate:
                        return Path(candidate)
        except Exception as exc:
            logger.debug("mw --version path discovery failed: %s", exc)

        return None

    async def list_recent(self, n: int = 10) -> list[TranscriptRef]:
        """List the most recent n transcript entries (ADR-0001 Protocol method).

        Reads JSON files from the history directory, sorted by created_at
        descending.

        Args:
            n: Maximum number of entries to return.

        Returns:
            List of TranscriptRef objects, newest first.

        Raises:
            MacWhisperNotFoundError: If the history directory cannot be found.
        """
        history_dir = self.discover_history_path()
        entries: list[dict] = []

        for json_file in history_dir.glob("*.json"):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                entries.append(data)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to parse %s: %s", json_file, exc)

        # Sort by created_at descending (newest first)
        entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
        entries = entries[:n]

        return [
            TranscriptRef(
                entry_id=entry.get("id", ""),
                created_at=entry.get("created_at", ""),
                text_preview=entry.get("text", "")[:200],
            )
            for entry in entries
        ]

    def read_entry(self, entry_id: str) -> tuple[str, dict]:
        """Read a single transcript entry by ID.

        Args:
            entry_id: The entry ID (filename without .json extension).

        Returns:
            Tuple of (text, metadata dict).

        Raises:
            FileNotFoundError: If the entry file does not exist.
            MacWhisperNotFoundError: If the history directory cannot be found.
        """
        history_dir = self.discover_history_path()
        entry_path = history_dir / f"{entry_id}.json"

        if not entry_path.exists():
            raise FileNotFoundError(
                f"MacWhisper entry not found: {entry_path}"
            )

        data = json.loads(entry_path.read_text(encoding="utf-8"))
        text = data.get("text", "")
        metadata = {k: v for k, v in data.items() if k != "text"}
        return text, metadata

    async def ingest_entry(self, entry_id: str) -> IngestResult:
        """Ingest a single MacWhisper transcript entry into open-brain memory.

        Reads the entry from the history directory and delegates to
        TranscriptIngestor.

        Args:
            entry_id: The entry ID to ingest.

        Returns:
            IngestResult from TranscriptIngestor.

        Raises:
            FileNotFoundError: If the entry does not exist.
            MacWhisperNotFoundError: If the history directory cannot be found.
        """
        text, meta = self.read_entry(entry_id)
        source_ref = f"macwhisper:{entry_id}"
        return await self._ingestor.ingest(text, source_ref, medium_hint=meta.get("medium"))

    async def ingest(self, ref: Any, run_id: str) -> IngestResult:
        """ADR-0001 Protocol method: ingest a single item identified by ref.

        Delegates to ingest_entry, treating ref as an entry_id string.

        Args:
            ref: The entry ID (str or coercible to str) to ingest.
            run_id: UUID string created by the orchestrator for this ingest run.

        Returns:
            IngestResult from TranscriptIngestor.
        """
        return await self.ingest_entry(str(ref))
