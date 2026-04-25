"""MacWhisperConnector — ingests MacWhisper transcripts into open-brain memory.

Discovery order for history path:
1. MACWHISPER_HISTORY_PATH config field (env var override)
2. ~/Library/Containers/com.goodsnooze.MacWhisper/Data/Library/Application Support/MacWhisper/
3. ~/Library/Application Support/MacWhisper/
4. mw transcribe --persist + parse stderr for path hint (via CommandRunner)
5. Raise MacWhisperNotFoundError(tried_paths=[...])
"""

import json
import logging
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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
        """
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=_SAFE_SUBPROCESS_ENV,
        )
        return result.returncode, result.stdout, result.stderr


class MockCommandRunner:
    """CommandRunner for tests that returns pre-configured responses."""

    def __init__(
        self,
        responses: dict[str, tuple[int, str, str]] | None = None,
        default: tuple[int, str, str] = (0, "", ""),
    ) -> None:
        self._responses = responses or {}
        self._default = default

    def run(self, cmd: list[str]) -> tuple[int, str, str]:
        """Return the pre-configured response for the command, or the default."""
        key = " ".join(cmd)
        return self._responses.get(key, self._default)


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

    Args:
        data_layer: DataLayer implementation for persistence.
        command_runner: Optional CommandRunner for subprocess calls.
            Defaults to DefaultCommandRunner.
        skip_platform_check: If True, skip the macOS platform check.
            Use in tests running on non-macOS platforms.
    """

    def __init__(
        self,
        data_layer: DataLayer,
        command_runner: CommandRunner | None = None,
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
        self._cached_path: Path | None = None

    def discover_history_path(self) -> Path:
        """Discover the MacWhisper history directory.

        Discovery order:
        1. MACWHISPER_HISTORY_PATH env var / config field
        2. Container sandbox path
        3. Application Support path
        4. mw CLI stderr parse
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
        from open_brain.config import get_config
        config = get_config()
        if config.MACWHISPER_HISTORY_PATH:
            override = Path(config.MACWHISPER_HISTORY_PATH)
            if override.exists():
                self._cached_path = override
                return override
            # If override is set but doesn't exist, still respect it as a signal
            # but continue looking (don't add to tried yet — user explicitly set it)
            tried.append(override)

        # 2 & 3. Standard candidate paths
        for candidate in _CANDIDATE_PATHS:
            if candidate.exists():
                self._cached_path = candidate
                return candidate
            tried.append(candidate)

        # 4. Try mw CLI to get a hint from stderr
        mw_path = self._try_mw_cli_path()
        if mw_path is not None and mw_path.exists():
            self._cached_path = mw_path
            return mw_path
        if mw_path is not None:
            tried.append(mw_path)

        raise MacWhisperNotFoundError(tried_paths=tried)

    def _try_mw_cli_path(self) -> Path | None:
        """Try to discover history path by running the mw CLI and parsing stderr.

        Returns:
            Discovered path or None if not found.
        """
        try:
            returncode, stdout, stderr = self._runner.run(
                ["mw", "transcribe", "--persist"]
            )
            # Parse stderr for path hints
            for line in (stdout + "\n" + stderr).splitlines():
                line = line.strip()
                if "MacWhisper" in line and "/" in line:
                    # Look for path-like tokens
                    for token in line.split():
                        if token.startswith("/") and "MacWhisper" in token:
                            return Path(token)
        except Exception as exc:
            logger.debug("mw CLI path discovery failed: %s", exc)
        return None

    def list_recent(self, n: int = 10) -> list[TranscriptRef]:
        """List the most recent n transcript entries.

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
                data = json.loads(json_file.read_text())
                entries.append(data)
            except Exception as exc:
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

        data = json.loads(entry_path.read_text())
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
        text, _ = self.read_entry(entry_id)
        source_ref = f"macwhisper:{entry_id}"
        ingestor = TranscriptIngestor(data_layer=self._dl)
        return await ingestor.ingest(text, source_ref, medium_hint="macwhisper")
