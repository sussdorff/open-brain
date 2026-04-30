"""Tests for MacWhisperConnector — cr3.11.

Acceptance criteria covered:
1. test_discover_history_path_finds_container_path — fake fs has container path → finds it
2. test_discover_history_path_finds_app_support_path — container missing, app support exists → finds it
3. test_discover_history_path_config_override — MACWHISPER_HISTORY_PATH set → uses it
4. test_discover_history_path_no_macwhisper_raises — no paths exist, mw fails → MacWhisperNotFoundError
4b. test_discover_history_path_mw_cli_fallback — mw --help reports a path, fake fs has it → finds it
5. test_list_recent_returns_entries — fake fs with 3 JSON files → list_recent(3) returns 3 refs
6. test_list_recent_empty_dir — empty dir → returns []
7. test_ingest_entry_delegates_to_transcript_ingestor — injected ingestor → verifies delegation
8. test_ingest_entry_idempotency — calling ingest_entry twice uses same source_ref format
"""

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from open_brain.data_layer.interface import DataLayer
from open_brain.ingest.adapters.base import ADAPTERS
from open_brain.ingest.adapters.macwhisper import (
    MacWhisperConnector,
    MacWhisperNotFoundError,
    TranscriptRef,
)
from tests._fakes import MockCommandRunner


# ─── Helpers ─────────────────────────────────────────────────────────────────

CONTAINER_PATH = (
    Path.home()
    / "Library/Containers/com.goodsnooze.MacWhisper/Data/Library/Application Support/MacWhisper"
)
APP_SUPPORT_PATH = Path.home() / "Library/Application Support/MacWhisper"

SAMPLE_ENTRY = {
    "id": "abc123",
    "text": "Meeting transcript about project planning.",
    "created_at": "2026-04-24T10:00:00",
}

SESSION_ID_HEX = "1fe090fa10084fe792b92278532f76b3"
DICTATION_ID_HEX = "9e68fb295680454cb2db649d536e0894"
RECORDED_MEETING_ID_HEX = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"
SPEAKER_ALICE_ID_HEX = "b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1"
SPEAKER_BOB_ID_HEX = "c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1"


def _make_data_layer() -> MagicMock:
    """Return a minimal mock DataLayer with spec."""
    return MagicMock(spec=DataLayer)


def _make_connector(
    *,
    history_path: str = "",
    command_runner=None,
    ingestor=None,
) -> MacWhisperConnector:
    """Build a MacWhisperConnector with test defaults."""
    dl = _make_data_layer()
    runner = command_runner or MockCommandRunner(default=(1, "", "mw not found"))
    with __import__("unittest.mock", fromlist=["patch"]).patch.dict(
        os.environ, {"MACWHISPER_HISTORY_PATH": history_path}
    ):
        connector = MacWhisperConnector(
            data_layer=dl,
            command_runner=runner,
            ingestor=ingestor,
            skip_platform_check=True,
        )
    if history_path:
        connector._cached_path = Path(history_path)
    return connector


def _create_sqlite_history(history_path: Path) -> None:
    """Create a minimal modern MacWhisper SQLite history fixture."""
    db_dir = history_path / "Database"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "main.sqlite")
    try:
        conn.executescript(
            """
            CREATE TABLE session (
                id BLOB PRIMARY KEY NOT NULL,
                dateCreated DATETIME NOT NULL,
                dateUpdated DATETIME,
                dateLastOpened DATETIME,
                textPreview TEXT,
                aiSummary TEXT,
                fullText TEXT,
                userChosenTitle TEXT,
                transcriptionDidSucceed BOOLEAN,
                modelEngine TEXT,
                modelIdentifer TEXT,
                modelInputLanguage TEXT,
                hasBeenDiarized BOOLEAN NOT NULL DEFAULT 0,
                detectedLanguage TEXT,
                isMergedFromMultipleTracks BOOLEAN NOT NULL DEFAULT 0,
                isFromYoutube BOOLEAN NOT NULL DEFAULT 0,
                originalFilename TEXT,
                originalExtension TEXT,
                startTimeOffset DOUBLE NOT NULL DEFAULT 0.0,
                wasTranslatedToEnglishDuringTranscription BOOLEAN NOT NULL DEFAULT 0,
                timeTakenToTranscribe DOUBLE,
                playbackDuration DOUBLE,
                sourceAppBundleID TEXT,
                aiSummaryShort TEXT,
                recordedMeetingID BLOB,
                systemAudioRecordingID BLOB,
                isTransient BOOLEAN NOT NULL DEFAULT 0,
                voiceMemoID BLOB,
                podcastID BLOB,
                importedFromDefaults BOOLEAN NOT NULL DEFAULT 0,
                isBeingRetranscribed BOOLEAN NOT NULL DEFAULT 0,
                dateRetranscribed DATETIME,
                downloadMetadataID BLOB,
                aiTitle TEXT,
                originalFileHash TEXT,
                dateDeleted DOUBLE
            );
            CREATE TABLE dictation (
                id BLOB PRIMARY KEY NOT NULL,
                dateCreated DATETIME NOT NULL,
                transcribedText TEXT,
                processedText TEXT,
                mediaFileID BLOB,
                transcriptionDidSucceed BOOLEAN,
                aiPromptID BLOB,
                aiPromptName TEXT,
                aiServiceName TEXT,
                aiServiceID BLOB,
                targetAppBundleID TEXT,
                targetAppLocalizedName TEXT,
                dateDeleted DOUBLE,
                transcriptionError TEXT,
                processingError TEXT
            );
            CREATE TABLE recordedmeeting (
                id BLOB PRIMARY KEY NOT NULL,
                date DATETIME,
                title TEXT,
                bundleIdentifier TEXT,
                appName TEXT,
                duration DOUBLE,
                dateCreated DATETIME,
                dateUpdated DATETIME,
                matchedCalendarTitle TEXT,
                dateDeleted DOUBLE
            );
            CREATE TABLE systemaudiorecording (
                id BLOB PRIMARY KEY NOT NULL,
                date DATETIME,
                title TEXT,
                bundleIdentifier TEXT,
                appName TEXT,
                duration DOUBLE,
                dateCreated DATETIME,
                dateUpdated DATETIME,
                dateDeleted DOUBLE
            );
            CREATE TABLE speaker (
                id BLOB PRIMARY KEY NOT NULL,
                name TEXT,
                color TEXT,
                isStub BOOLEAN,
                photoData BLOB
            );
            CREATE TABLE transcriptline (
                id BLOB PRIMARY KEY NOT NULL,
                dateCreated DATETIME,
                dateUpdated DATETIME,
                text TEXT,
                start DOUBLE,
                end DOUBLE,
                isFavorite BOOLEAN,
                sessionId BLOB,
                speakerID BLOB,
                wordsJson TEXT
            );
            CREATE TABLE session_speaker (
                sessionID BLOB NOT NULL,
                speakerID BLOB NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO recordedmeeting (
                id, date, title, bundleIdentifier, appName, duration, dateDeleted
            ) VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                bytes.fromhex(RECORDED_MEETING_ID_HEX),
                "2026-04-29 10:00:00.000",
                "Weekly Product Sync",
                "com.microsoft.teams2",
                "Teams",
                3672.2,
            ),
        )
        conn.execute(
            """
            INSERT INTO session (
                id, dateCreated, textPreview, fullText, userChosenTitle,
                hasBeenDiarized, recordedMeetingID, playbackDuration, dateDeleted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                bytes.fromhex(SESSION_ID_HEX),
                "2026-04-29 10:02:52.120",
                "Session preview",
                "Full session transcript.",
                "Rucksprache zu Mira",
                1,
                bytes.fromhex(RECORDED_MEETING_ID_HEX),
                3600.0,
            ),
        )
        conn.executemany(
            """
            INSERT INTO speaker (id, name, color, isStub, photoData)
            VALUES (?, ?, ?, 0, NULL)
            """,
            [
                (bytes.fromhex(SPEAKER_ALICE_ID_HEX), "Alice Example", "#ff0000"),
                (bytes.fromhex(SPEAKER_BOB_ID_HEX), "Bob Example", "#00ff00"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO transcriptline (
                id, dateCreated, text, start, end, sessionId, speakerID
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    bytes.fromhex("d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1"),
                    "2026-04-29 10:03:00.000",
                    "Hello from Alice.",
                    0.0,
                    2.0,
                    bytes.fromhex(SESSION_ID_HEX),
                    bytes.fromhex(SPEAKER_ALICE_ID_HEX),
                ),
                (
                    bytes.fromhex("e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1"),
                    "2026-04-29 10:03:03.000",
                    "Hello from Bob.",
                    3.0,
                    5.0,
                    bytes.fromhex(SESSION_ID_HEX),
                    bytes.fromhex(SPEAKER_BOB_ID_HEX),
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO session_speaker (sessionID, speakerID)
            VALUES (?, ?)
            """,
            [
                (bytes.fromhex(SESSION_ID_HEX), bytes.fromhex(SPEAKER_ALICE_ID_HEX)),
                (bytes.fromhex(SESSION_ID_HEX), bytes.fromhex(SPEAKER_BOB_ID_HEX)),
            ],
        )
        conn.execute(
            """
            INSERT INTO dictation (
                id, dateCreated, transcribedText, processedText, aiPromptName, dateDeleted
            ) VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                bytes.fromhex(DICTATION_ID_HEX),
                "2026-04-30 08:22:41.412",
                "Raw dictation transcript.",
                "Processed dictation transcript.",
                "Dictation prompt",
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ─── AC1: discover_history_path — container path ─────────────────────────────


class TestDiscoverHistoryPathContainerPath:
    def test_finds_container_path(self, fs):
        """AC1: discover_history_path finds container path when it exists."""
        fs.create_dir(str(CONTAINER_PATH))
        connector = _make_connector()
        result = connector.discover_history_path()
        assert result == CONTAINER_PATH


# ─── AC2: discover_history_path — app support fallback ───────────────────────


class TestDiscoverHistoryPathAppSupportPath:
    def test_finds_app_support_path(self, fs):
        """AC2: discover_history_path finds app support path when container is missing."""
        fs.create_dir(str(APP_SUPPORT_PATH))
        connector = _make_connector()
        result = connector.discover_history_path()
        assert result == APP_SUPPORT_PATH


# ─── AC3: discover_history_path — config override ────────────────────────────


class TestDiscoverHistoryPathConfigOverride:
    def test_uses_config_override(self, fs):
        """AC3: MACWHISPER_HISTORY_PATH env var overrides discovery."""
        custom_path = Path.home() / "custom/macwhisper/history"
        fs.create_dir(str(custom_path))
        dl = _make_data_layer()
        runner = MockCommandRunner(default=(1, "", ""))
        with __import__("unittest.mock", fromlist=["patch"]).patch.dict(
            os.environ, {"MACWHISPER_HISTORY_PATH": str(custom_path)}
        ):
            connector = MacWhisperConnector(
                data_layer=dl,
                command_runner=runner,
                skip_platform_check=True,
            )
            result = connector.discover_history_path()
        assert result == custom_path


# ─── AC4: discover_history_path — no macwhisper raises ───────────────────────


class TestDiscoverHistoryPathNoMacWhisperRaises:
    def test_raises_with_tried_paths(self, fs):
        """AC4: raises MacWhisperNotFoundError with tried paths when nothing found."""
        # No directories created → all paths missing
        runner = MockCommandRunner(default=(1, "", ""))
        connector = _make_connector(command_runner=runner)
        with pytest.raises(MacWhisperNotFoundError) as exc_info:
            connector.discover_history_path()
        error = exc_info.value
        assert len(error.tried_paths) >= 2
        assert CONTAINER_PATH in error.tried_paths
        assert APP_SUPPORT_PATH in error.tried_paths


# ─── AC4b: discover_history_path — mw CLI fallback ───────────────────────────


class TestDiscoverHistoryPathMwCliFallback:
    def test_discover_history_path_mw_cli_fallback(self, fs):
        """AK4: mw --help reports path in stdout/stderr and the path exists → found."""
        custom_path = Path("/tmp/CustomMW/MacWhisper")
        fs.create_dir(str(custom_path))

        # mw --help returns persist dir info on stderr
        runner = MockCommandRunner(
            responses={
                "mw --help": (0, "", f"Persist dir: {custom_path}\n"),
            },
            default=(1, "", ""),
        )
        connector = _make_connector(command_runner=runner)
        result = connector.discover_history_path()
        assert result == custom_path


# ─── AC2 (list_recent): returns entries ──────────────────────────────────────


class TestListRecentReturnsEntries:
    async def test_returns_three_entries(self, fs):
        """AC2 (list_recent): list_recent returns at least 1 entry when history exists."""
        fs.create_dir(str(APP_SUPPORT_PATH))
        entries = [
            {"id": f"entry{i}", "text": f"Transcript {i}", "created_at": f"2026-04-2{i}T10:00:00"}
            for i in range(1, 4)
        ]
        for entry in entries:
            path = APP_SUPPORT_PATH / f"{entry['id']}.json"
            fs.create_file(str(path), contents=json.dumps(entry))

        connector = _make_connector()
        results = await connector.list_recent(n=3)

        assert len(results) == 3
        assert all(isinstance(r, TranscriptRef) for r in results)
        # Should be sorted descending by created_at — last entry first
        assert results[0].entry_id == "entry3"


class TestListRecentEmptyDir:
    async def test_empty_dir_returns_empty_list(self, fs):
        """AC2 (list_recent): empty directory returns empty list."""
        fs.create_dir(str(APP_SUPPORT_PATH))
        connector = _make_connector()
        results = await connector.list_recent()
        assert results == []


class TestListRecentSQLiteHistory:
    async def test_returns_session_entries_with_meeting_metadata(self, tmp_path):
        """Modern MacWhisper SQLite history lists transcript sessions by default."""
        _create_sqlite_history(tmp_path)
        connector = _make_connector(history_path=str(tmp_path))

        results = await connector.list_recent(n=5)

        assert [r.entry_id for r in results] == [f"session:{SESSION_ID_HEX}"]
        assert results[0].created_at == "2026-04-29 10:02:52.120"
        assert results[0].text_preview == "Full session transcript."
        assert results[0].title == "Weekly Product Sync"
        assert results[0].source_type == "recorded_meeting"
        assert results[0].source_app == "Teams"
        assert results[0].duration_seconds == 3672.2
        assert results[0].participants == ["Alice Example", "Bob Example"]


class TestReadSQLiteEntry:
    def test_reads_session_entry(self, tmp_path):
        """read_entry supports session:<hex-id> from modern SQLite history."""
        _create_sqlite_history(tmp_path)
        connector = _make_connector(history_path=str(tmp_path))

        text, metadata = connector.read_entry(f"session:{SESSION_ID_HEX}")

        assert text == (
            "[0001] Alice Example: Hello from Alice.\n"
            "[0002] Bob Example: Hello from Bob."
        )
        assert metadata["source_type"] == "recorded_meeting"
        assert metadata["title"] == "Weekly Product Sync"
        assert metadata["source_app"] == "Teams"
        assert metadata["duration_seconds"] == 3672.2
        assert metadata["participants"] == ["Alice Example", "Bob Example"]
        assert metadata["medium"] == "macwhisper"
        assert metadata["transcript_format"] == "speaker_turns_v1"
        assert metadata["turn_count"] == 2

    def test_reads_dictation_entry(self, tmp_path):
        """read_entry supports dictation:<hex-id> from modern SQLite history."""
        _create_sqlite_history(tmp_path)
        connector = _make_connector(history_path=str(tmp_path))

        text, metadata = connector.read_entry(f"dictation:{DICTATION_ID_HEX}")

        assert text == "Processed dictation transcript."
        assert metadata["source_type"] == "dictation"
        assert metadata["title"] == "Dictation prompt"
        assert metadata["source_app"] == ""
        assert metadata["duration_seconds"] is None
        assert metadata["participants"] == []
        assert metadata["medium"] == "macwhisper"


# ─── AC3 (ingest_entry): delegates to TranscriptIngestor ─────────────────────


class TestIngestEntryDelegates:
    async def test_ingest_entry_delegates_to_transcript_ingestor(self, fs):
        """AC3 (ingest_entry): ingest_entry calls injected TranscriptIngestor.ingest with correct args."""
        fs.create_dir(str(APP_SUPPORT_PATH))
        entry_path = APP_SUPPORT_PATH / f"{SAMPLE_ENTRY['id']}.json"
        fs.create_file(str(entry_path), contents=json.dumps(SAMPLE_ENTRY))

        mock_result = MagicMock()
        mock_ingestor = MagicMock()
        mock_ingestor.ingest = AsyncMock(return_value=mock_result)

        connector = _make_connector(ingestor=mock_ingestor)
        result = await connector.ingest_entry(SAMPLE_ENTRY["id"])

        mock_ingestor.ingest.assert_called_once_with(
            SAMPLE_ENTRY["text"],
            f"macwhisper:{SAMPLE_ENTRY['id']}",
            medium_hint=None,  # SAMPLE_ENTRY has no "medium" field
        )
        assert result is mock_result


# ─── AC (idempotency): same source_ref format used on repeated calls ──────────


class TestIngestEntryIdempotency:
    async def test_ingest_entry_idempotency(self, fs):
        """Calling ingest_entry twice with the same entry_id uses consistent source_ref."""
        fs.create_dir(str(APP_SUPPORT_PATH))
        entry_path = APP_SUPPORT_PATH / f"{SAMPLE_ENTRY['id']}.json"
        fs.create_file(str(entry_path), contents=json.dumps(SAMPLE_ENTRY))

        mock_result = MagicMock()
        mock_ingestor = MagicMock()
        mock_ingestor.ingest = AsyncMock(return_value=mock_result)

        connector = _make_connector(ingestor=mock_ingestor)

        await connector.ingest_entry(SAMPLE_ENTRY["id"])
        await connector.ingest_entry(SAMPLE_ENTRY["id"])

        assert mock_ingestor.ingest.call_count == 2
        calls = mock_ingestor.ingest.call_args_list
        # Both calls must use the same source_ref format: macwhisper:{entry_id}
        expected_source_ref = f"macwhisper:{SAMPLE_ENTRY['id']}"
        assert calls[0].args[1] == expected_source_ref
        assert calls[1].args[1] == expected_source_ref


# ─── AC (ingest protocol): run_id forwarded, TranscriptRef entry_id extracted ─


class TestIngestProtocol:
    async def test_ingest_forwards_run_id_and_extracts_entry_id(self, fs):
        """ingest(ref, run_id) extracts entry_id from TranscriptRef and passes run_id to ingestor."""
        fs.create_dir(str(APP_SUPPORT_PATH))
        entry_path = APP_SUPPORT_PATH / f"{SAMPLE_ENTRY['id']}.json"
        fs.create_file(str(entry_path), contents=json.dumps(SAMPLE_ENTRY))

        supplied_run_id = "test-run-id-1234"
        mock_result = MagicMock()
        mock_result.run_id = supplied_run_id
        mock_ingestor = MagicMock()
        mock_ingestor.ingest = AsyncMock(return_value=mock_result)

        connector = _make_connector(ingestor=mock_ingestor)
        ref = TranscriptRef(
            entry_id=SAMPLE_ENTRY["id"],
            created_at=SAMPLE_ENTRY["created_at"],
            text_preview=SAMPLE_ENTRY["text"][:200],
        )

        result = await connector.ingest(ref, supplied_run_id)

        mock_ingestor.ingest.assert_called_once_with(
            SAMPLE_ENTRY["text"],
            f"macwhisper:{SAMPLE_ENTRY['id']}",
            medium_hint=None,
            run_id=supplied_run_id,
        )
        assert result.run_id == supplied_run_id


# ─── AC (registry): MacWhisperConnector registered in ADAPTERS ───────────────


class TestMacWhisperRegisteredInAdapters:
    def test_macwhisper_registered_in_adapters(self):
        """AC: MacWhisperConnector is registered in ADAPTERS under 'macwhisper' at import time."""
        # The module-level register() call in macwhisper.py ensures the adapter
        # is discoverable as soon as the module is imported — no manual register()
        # call is needed.
        assert "macwhisper" in ADAPTERS, (
            "ADAPTERS must contain 'macwhisper' after importing macwhisper module"
        )
        assert ADAPTERS["macwhisper"].name == "macwhisper"
