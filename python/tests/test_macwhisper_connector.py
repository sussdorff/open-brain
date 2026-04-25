"""Tests for MacWhisperConnector — cr3.11.

Acceptance criteria covered:
1. test_discover_history_path_finds_container_path — fake fs has container path → finds it
2. test_discover_history_path_finds_app_support_path — container missing, app support exists → finds it
3. test_discover_history_path_config_override — MACWHISPER_HISTORY_PATH set → uses it
4. test_discover_history_path_no_macwhisper_raises — no paths exist, mw fails → MacWhisperNotFoundError
5. test_list_recent_returns_entries — fake fs with 3 JSON files → list_recent(3) returns 3 refs
6. test_list_recent_empty_dir — empty dir → returns []
7. test_ingest_entry_delegates_to_transcript_ingestor — mock TranscriptIngestor → verifies delegation
"""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.ingest.adapters.macwhisper import (
    MacWhisperConnector,
    MacWhisperNotFoundError,
    MockCommandRunner,
    TranscriptRef,
)


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


def _make_data_layer() -> MagicMock:
    """Return a minimal mock DataLayer."""
    dl = MagicMock()
    return dl


def _make_connector(
    *,
    history_path: str = "",
    command_runner=None,
) -> MacWhisperConnector:
    """Build a MacWhisperConnector with test defaults."""
    dl = _make_data_layer()
    runner = command_runner or MockCommandRunner(default=(1, "", "mw not found"))
    with patch.dict(os.environ, {"MACWHISPER_HISTORY_PATH": history_path}):
        return MacWhisperConnector(
            data_layer=dl,
            command_runner=runner,
            skip_platform_check=True,
        )


# ─── AC1: discover_history_path — container path ─────────────────────────────


class TestDiscoverHistoryPathContainerPath:
    def test_finds_container_path(self, fake_filesystem):
        """AC1: discover_history_path finds container path when it exists."""
        fake_filesystem.create_dir(str(CONTAINER_PATH))
        connector = _make_connector()
        result = connector.discover_history_path()
        assert result == CONTAINER_PATH


# ─── AC2: discover_history_path — app support fallback ───────────────────────


class TestDiscoverHistoryPathAppSupportPath:
    def test_finds_app_support_path(self, fake_filesystem):
        """AC2: discover_history_path finds app support path when container is missing."""
        fake_filesystem.create_dir(str(APP_SUPPORT_PATH))
        connector = _make_connector()
        result = connector.discover_history_path()
        assert result == APP_SUPPORT_PATH


# ─── AC3: discover_history_path — config override ────────────────────────────


class TestDiscoverHistoryPathConfigOverride:
    def test_uses_config_override(self, fake_filesystem):
        """AC3: MACWHISPER_HISTORY_PATH env var overrides discovery."""
        custom_path = Path.home() / "custom/macwhisper/history"
        fake_filesystem.create_dir(str(custom_path))
        dl = _make_data_layer()
        runner = MockCommandRunner(default=(1, "", ""))
        with patch.dict(os.environ, {"MACWHISPER_HISTORY_PATH": str(custom_path)}):
            connector = MacWhisperConnector(
                data_layer=dl,
                command_runner=runner,
                skip_platform_check=True,
            )
        result = connector.discover_history_path()
        assert result == custom_path


# ─── AC4: discover_history_path — no macwhisper raises ───────────────────────


class TestDiscoverHistoryPathNoMacWhisperRaises:
    def test_raises_with_tried_paths(self, fake_filesystem):
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


# ─── AC2 (list_recent): returns entries ──────────────────────────────────────


class TestListRecentReturnsEntries:
    def test_returns_three_entries(self, fake_filesystem):
        """AC2 (list_recent): list_recent returns at least 1 entry when history exists."""
        fake_filesystem.create_dir(str(APP_SUPPORT_PATH))
        entries = [
            {"id": f"entry{i}", "text": f"Transcript {i}", "created_at": f"2026-04-2{i}T10:00:00"}
            for i in range(1, 4)
        ]
        for entry in entries:
            path = APP_SUPPORT_PATH / f"{entry['id']}.json"
            fake_filesystem.create_file(str(path), contents=json.dumps(entry))

        connector = _make_connector()
        results = connector.list_recent(3)

        assert len(results) == 3
        assert all(isinstance(r, TranscriptRef) for r in results)
        # Should be sorted descending by created_at — last entry first
        assert results[0].entry_id == "entry3"


class TestListRecentEmptyDir:
    def test_empty_dir_returns_empty_list(self, fake_filesystem):
        """AC2 (list_recent): empty directory returns empty list."""
        fake_filesystem.create_dir(str(APP_SUPPORT_PATH))
        connector = _make_connector()
        results = connector.list_recent()
        assert results == []


# ─── AC3 (ingest_entry): delegates to TranscriptIngestor ─────────────────────


class TestIngestEntryDelegates:
    async def test_ingest_entry_delegates_to_transcript_ingestor(self, fake_filesystem):
        """AC3 (ingest_entry): ingest_entry calls TranscriptIngestor.ingest with correct args."""
        fake_filesystem.create_dir(str(APP_SUPPORT_PATH))
        entry_path = APP_SUPPORT_PATH / f"{SAMPLE_ENTRY['id']}.json"
        fake_filesystem.create_file(str(entry_path), contents=json.dumps(SAMPLE_ENTRY))

        dl = _make_data_layer()
        runner = MockCommandRunner(default=(1, "", ""))

        mock_result = MagicMock()
        mock_ingest = AsyncMock(return_value=mock_result)

        with patch("open_brain.ingest.adapters.macwhisper.TranscriptIngestor") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.ingest = mock_ingest
            mock_cls.return_value = mock_instance

            connector = MacWhisperConnector(
                data_layer=dl,
                command_runner=runner,
                skip_platform_check=True,
            )
            result = await connector.ingest_entry(SAMPLE_ENTRY["id"])

        mock_cls.assert_called_once_with(data_layer=dl)
        mock_ingest.assert_called_once_with(
            SAMPLE_ENTRY["text"],
            f"macwhisper:{SAMPLE_ENTRY['id']}",
            medium_hint="macwhisper",
        )
        assert result is mock_result
