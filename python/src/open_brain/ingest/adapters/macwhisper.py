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
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from open_brain.data_layer.interface import DataLayer
from open_brain.ingest.adapters.base import register
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
    title: str = ""
    source_type: str = ""
    source_app: str = ""
    duration_seconds: float | None = None
    participants: list[str] = field(default_factory=list)


@dataclass
class TranscriptTurn:
    """One ordered speaker-attributed transcript segment."""

    index: int
    speaker: str
    text: str
    start: float | None = None
    end: float | None = None


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
_SQLITE_RELATIVE_PATH = Path("Database/main.sqlite")


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
        data_layer: DataLayer | None = None,
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
        self._ingestor = ingestor if ingestor is not None else (
            TranscriptIngestor(data_layer=data_layer) if data_layer is not None else None
        )
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

        Reads legacy JSON files and newer SQLite-backed MacWhisper transcript
        sessions, sorted by created_at descending. Modern SQLite dictations are
        intentionally not listed because they are short dictation notes, not
        meeting/session transcripts.

        Args:
            n: Maximum number of entries to return.

        Returns:
            List of TranscriptRef objects, newest first.

        Raises:
            MacWhisperNotFoundError: If the history directory cannot be found.
        """
        history_dir = self.discover_history_path()
        refs = self._list_recent_json(history_dir)
        refs.extend(self._list_recent_sqlite(history_dir, n=n))
        refs.sort(key=lambda ref: ref.created_at, reverse=True)
        return refs[:n]

    def _list_recent_json(self, history_dir: Path) -> list[TranscriptRef]:
        """List legacy JSON transcript refs from the history directory."""
        refs: list[TranscriptRef] = []

        for json_file in history_dir.glob("*.json"):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                refs.append(
                    TranscriptRef(
                        entry_id=data.get("id", ""),
                        created_at=data.get("created_at", ""),
                        text_preview=data.get("text", "")[:200],
                    )
                )
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to parse %s: %s", json_file, exc)

        return refs

    def _list_recent_sqlite(self, history_dir: Path, n: int) -> list[TranscriptRef]:
        """List transcript refs from modern MacWhisper SQLite history."""
        if n <= 0:
            return []

        db_path = history_dir / _SQLITE_RELATIVE_PATH
        if not db_path.exists():
            return []

        refs: list[TranscriptRef] = []
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect_sqlite(db_path)
            rows = conn.execute(
                """
                SELECT
                    'session:' || lower(hex(s.id)) AS entry_id,
                    lower(hex(s.id)) AS session_hex_id,
                    s.dateCreated AS created_at,
                    s.fullText AS text,
                    COALESCE(
                        NULLIF(trim(rm.title), ''),
                        NULLIF(trim(sar.title), ''),
                        NULLIF(trim(s.userChosenTitle), ''),
                        NULLIF(trim(s.aiTitle), ''),
                        NULLIF(trim(s.originalFilename), ''),
                        ''
                    ) AS title,
                    CASE
                        WHEN s.recordedMeetingID IS NOT NULL THEN 'recorded_meeting'
                        WHEN s.systemAudioRecordingID IS NOT NULL THEN 'system_audio_recording'
                        ELSE 'session'
                    END AS source_type,
                    COALESCE(
                        NULLIF(trim(rm.appName), ''),
                        NULLIF(trim(sar.appName), ''),
                        NULLIF(trim(s.sourceAppBundleID), ''),
                        NULLIF(trim(s.originalExtension), ''),
                        ''
                    ) AS source_app,
                    COALESCE(rm.duration, sar.duration, s.playbackDuration) AS duration_seconds
                FROM session s
                LEFT JOIN recordedmeeting rm ON rm.id = s.recordedMeetingID
                LEFT JOIN systemaudiorecording sar ON sar.id = s.systemAudioRecordingID
                WHERE s.dateDeleted IS NULL
                  AND s.fullText IS NOT NULL
                  AND length(trim(s.fullText)) > 0
                ORDER BY s.dateCreated DESC
                LIMIT ?
                """,
                (n,),
            ).fetchall()
            for row in rows:
                refs.append(
                    TranscriptRef(
                        entry_id=row["entry_id"],
                        created_at=row["created_at"] or "",
                        text_preview=(row["text"] or "")[:200],
                        title=row["title"] or "",
                        source_type=row["source_type"] or "",
                        source_app=row["source_app"] or "",
                        duration_seconds=row["duration_seconds"],
                        participants=self._fetch_session_participants(
                            conn, row["session_hex_id"] or ""
                        ),
                    )
                )
        except sqlite3.Error as exc:
            logger.warning("Failed to read MacWhisper SQLite history %s: %s", db_path, exc)
        finally:
            if conn is not None:
                conn.close()

        return refs

    def _connect_sqlite(self, db_path: Path) -> sqlite3.Connection:
        """Open a read-only SQLite connection for MacWhisper's database."""
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

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

        if entry_path.exists():
            data = json.loads(entry_path.read_text(encoding="utf-8"))
            text = data.get("text", "")
            metadata = {k: v for k, v in data.items() if k != "text"}
            return text, metadata

        sqlite_entry = self._read_sqlite_entry(history_dir, entry_id)
        if sqlite_entry is not None:
            return sqlite_entry

        raise FileNotFoundError(
            f"MacWhisper entry not found: {entry_id}. Tried {entry_path} "
            f"and {_SQLITE_RELATIVE_PATH}"
        )

    def _read_sqlite_entry(self, history_dir: Path, entry_id: str) -> tuple[str, dict] | None:
        """Read one transcript from modern MacWhisper SQLite history."""
        db_path = history_dir / _SQLITE_RELATIVE_PATH
        if not db_path.exists():
            return None

        source_type, hex_id = self._parse_sqlite_entry_id(entry_id)
        if not hex_id:
            return None

        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect_sqlite(db_path)
            source_types = [source_type] if source_type else ["session", "dictation"]
            for candidate_type in source_types:
                row = self._fetch_sqlite_entry(conn, candidate_type, hex_id)
                if row is None:
                    continue
                turns = (
                    self._fetch_session_transcript_turns(conn, hex_id)
                    if candidate_type == "session"
                    else []
                )
                text = (
                    self._format_speaker_turns(turns)
                    if turns
                    else row["text"] or ""
                )
                participants = (
                    self._fetch_session_participants(conn, hex_id)
                    if candidate_type == "session"
                    else []
                )
                return text, {
                    "id": hex_id,
                    "source_type": row["source_type"] or candidate_type,
                    "created_at": row["created_at"] or "",
                    "title": row["title"] or "",
                    "source_app": row["source_app"] or "",
                    "duration_seconds": row["duration_seconds"],
                    "participants": participants,
                    "medium": "macwhisper",
                    "transcript_format": (
                        "speaker_turns_v1" if turns else "plain_text"
                    ),
                    "turn_count": len(turns),
                }
        except sqlite3.Error as exc:
            logger.warning("Failed to read MacWhisper SQLite entry %s: %s", entry_id, exc)
        finally:
            if conn is not None:
                conn.close()

        return None

    def _fetch_session_transcript_turns(
        self,
        conn: sqlite3.Connection,
        session_hex_id: str,
    ) -> list[TranscriptTurn]:
        """Return ordered speaker-attributed transcript lines for a session."""
        if not session_hex_id:
            return []

        rows = conn.execute(
            """
            SELECT
                tl.text AS text,
                tl.start AS start,
                tl.end AS end,
                COALESCE(NULLIF(trim(sp.name), ''), 'Unknown speaker') AS speaker
            FROM transcriptline tl
            LEFT JOIN speaker sp ON sp.id = tl.speakerID
            WHERE lower(hex(tl.sessionId)) = ?
              AND tl.text IS NOT NULL
              AND length(trim(tl.text)) > 0
            ORDER BY tl.start ASC, tl.dateCreated ASC, lower(hex(tl.id)) ASC
            """,
            (session_hex_id,),
        ).fetchall()

        turns: list[TranscriptTurn] = []
        for row in rows:
            text = " ".join(str(row["text"] or "").split())
            if not text:
                continue
            turns.append(
                TranscriptTurn(
                    index=len(turns) + 1,
                    speaker=" ".join(str(row["speaker"] or "Unknown speaker").split()),
                    text=text,
                    start=row["start"],
                    end=row["end"],
                )
            )
        return turns

    def _format_speaker_turns(self, turns: list[TranscriptTurn]) -> str:
        """Format ordered speaker turns as readable, searchable transcript text."""
        return "\n".join(
            f"[{turn.index:04d}] {turn.speaker}: {turn.text}"
            for turn in turns
        )

    def _parse_sqlite_entry_id(self, entry_id: str) -> tuple[str | None, str]:
        """Return optional source type and normalized hex id."""
        source_type: str | None = None
        raw_id = entry_id
        if ":" in entry_id:
            prefix, raw_id = entry_id.split(":", 1)
            if prefix in {"session", "dictation"}:
                source_type = prefix

        normalized = raw_id.replace("-", "").lower()
        if len(normalized) != 32:
            return source_type, ""
        try:
            bytes.fromhex(normalized)
        except ValueError:
            return source_type, ""
        return source_type, normalized

    def _fetch_sqlite_entry(
        self,
        conn: sqlite3.Connection,
        source_type: str,
        hex_id: str,
    ) -> sqlite3.Row | None:
        """Fetch a SQLite-backed session or dictation by hex id."""
        if source_type == "session":
            return conn.execute(
                """
                SELECT
                    s.dateCreated AS created_at,
                    CASE
                        WHEN s.recordedMeetingID IS NOT NULL THEN 'recorded_meeting'
                        WHEN s.systemAudioRecordingID IS NOT NULL THEN 'system_audio_recording'
                        ELSE 'session'
                    END AS source_type,
                    COALESCE(
                        NULLIF(trim(rm.title), ''),
                        NULLIF(trim(sar.title), ''),
                        NULLIF(trim(s.userChosenTitle), ''),
                        NULLIF(trim(s.aiTitle), ''),
                        NULLIF(trim(s.originalFilename), ''),
                        NULLIF(trim(s.textPreview), ''),
                        ''
                    ) AS title,
                    COALESCE(
                        NULLIF(trim(rm.appName), ''),
                        NULLIF(trim(sar.appName), ''),
                        NULLIF(trim(s.sourceAppBundleID), ''),
                        NULLIF(trim(s.originalExtension), ''),
                        ''
                    ) AS source_app,
                    COALESCE(rm.duration, sar.duration, s.playbackDuration) AS duration_seconds,
                    s.fullText AS text
                FROM session s
                LEFT JOIN recordedmeeting rm ON rm.id = s.recordedMeetingID
                LEFT JOIN systemaudiorecording sar ON sar.id = s.systemAudioRecordingID
                WHERE lower(hex(s.id)) = ?
                  AND s.dateDeleted IS NULL
                  AND s.fullText IS NOT NULL
                  AND length(trim(s.fullText)) > 0
                LIMIT 1
                """,
                (hex_id,),
            ).fetchone()

        if source_type == "dictation":
            return conn.execute(
                """
                SELECT
                    dateCreated AS created_at,
                    'dictation' AS source_type,
                    COALESCE(aiPromptName, targetAppLocalizedName, '') AS title,
                    COALESCE(targetAppLocalizedName, targetAppBundleID, '') AS source_app,
                    NULL AS duration_seconds,
                    COALESCE(
                        NULLIF(trim(processedText), ''),
                        NULLIF(trim(transcribedText), '')
                    ) AS text
                FROM dictation
                WHERE lower(hex(id)) = ?
                  AND dateDeleted IS NULL
                  AND COALESCE(
                        NULLIF(trim(processedText), ''),
                        NULLIF(trim(transcribedText), '')
                      ) IS NOT NULL
                LIMIT 1
                """,
                (hex_id,),
            ).fetchone()

        return None

    def _fetch_session_participants(
        self,
        conn: sqlite3.Connection,
        session_hex_id: str,
    ) -> list[str]:
        """Return distinct speaker names detected for a session."""
        if not session_hex_id:
            return []

        try:
            rows = conn.execute(
                """
                SELECT sp.name AS name, MIN(tl.start) AS first_start
                FROM transcriptline tl
                JOIN speaker sp ON sp.id = tl.speakerID
                WHERE lower(hex(tl.sessionId)) = ?
                  AND sp.name IS NOT NULL
                  AND length(trim(sp.name)) > 0
                GROUP BY sp.id, sp.name
                ORDER BY first_start ASC, lower(sp.name) ASC
                """,
                (session_hex_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            logger.debug(
                "Failed to read MacWhisper transcriptline speakers for %s: %s",
                session_hex_id,
                exc,
            )
            rows = []

        names = self._dedupe_names(row["name"] for row in rows)
        if names:
            return names

        try:
            rows = conn.execute(
                """
                SELECT sp.name AS name
                FROM session_speaker ss
                JOIN speaker sp ON sp.id = ss.speakerID
                WHERE lower(hex(ss.sessionID)) = ?
                  AND sp.name IS NOT NULL
                  AND length(trim(sp.name)) > 0
                ORDER BY lower(sp.name) ASC
                """,
                (session_hex_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            logger.debug(
                "Failed to read MacWhisper session speakers for %s: %s",
                session_hex_id,
                exc,
            )
            return []

        return self._dedupe_names(row["name"] for row in rows)

    def _dedupe_names(self, names: Any) -> list[str]:
        """Normalize and de-duplicate speaker names while preserving order."""
        result: list[str] = []
        seen: set[str] = set()
        for name in names:
            normalized = " ".join(str(name).split())
            if not normalized:
                continue
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(normalized)
        return result

    async def ingest_entry(self, entry_id: str) -> IngestResult:
        """Ingest a single MacWhisper transcript entry into open-brain memory.

        Reads the entry from the history directory and delegates to
        TranscriptIngestor.

        Args:
            entry_id: The entry ID to ingest.

        Returns:
            IngestResult from TranscriptIngestor.

        Raises:
            RuntimeError: If data_layer was not provided at construction time.
            FileNotFoundError: If the entry does not exist.
            MacWhisperNotFoundError: If the history directory cannot be found.
        """
        if self._ingestor is None:
            raise RuntimeError(
                "data_layer is required for ingest; "
                "use MacWhisperConnector(data_layer=dl)"
            )
        text, meta = self.read_entry(entry_id)
        source_ref = f"macwhisper:{entry_id}"
        return await self._ingestor.ingest(text, source_ref, medium_hint=meta.get("medium"))

    async def ingest(self, ref: Any, run_id: str) -> IngestResult:
        """ADR-0001 Protocol method: ingest a single item identified by ref.

        Extracts the entry_id from a TranscriptRef or coerces ref to str, then
        reads the entry and delegates directly to TranscriptIngestor so that the
        orchestrator-supplied run_id is forwarded.

        Args:
            ref: A TranscriptRef (from list_recent) or an entry_id string.
            run_id: UUID string created by the orchestrator for this ingest run.

        Returns:
            IngestResult from TranscriptIngestor with the supplied run_id embedded.

        Raises:
            RuntimeError: If this is a sentinel instance (data_layer not provided).
        """
        if self._ingestor is None:
            raise RuntimeError(
                "Sentinel instance cannot ingest — provide data_layer"
            )
        entry_id = ref.entry_id if isinstance(ref, TranscriptRef) else str(ref)
        text, meta = self.read_entry(entry_id)
        source_ref = f"macwhisper:{entry_id}"
        return await self._ingestor.ingest(
            text, source_ref, medium_hint=meta.get("medium"), run_id=run_id
        )


# ─── Module-level registration (ADR-0001) ────────────────────────────────────
# Register a sentinel instance for adapter discovery. The sentinel uses
# data_layer=None; real ingest calls require a properly constructed instance
# with data_layer provided. skip_platform_check=True allows registration on
# non-macOS platforms (e.g. CI, Docker).
register(MacWhisperConnector(skip_platform_check=True))
