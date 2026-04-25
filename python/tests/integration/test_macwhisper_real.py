"""Integration tests for MacWhisperConnector against a real MacWhisper installation.

These tests require:
- macOS (darwin)
- MacWhisper installed at /Applications/MacWhisper.app

They are skipped automatically on CI and non-macOS platforms.

Observed sandbox path on Malte's dev machine:
~/Library/Containers/com.goodsnooze.MacWhisper/Data/Library/Application Support/MacWhisper
(as of 2026-04-25). Update this docstring and related ADRs if the path changes.
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from open_brain.data_layer.interface import DataLayer
from open_brain.ingest.adapters.macwhisper import (
    MacWhisperConnector,
    TranscriptRef,
)
from open_brain.ingest.models import IngestResult

_MACWHISPER_APP = Path("/Applications/MacWhisper.app")

_SKIP_REASON = "MacWhisper not installed or not running on macOS"
_SKIP_CONDITION = sys.platform != "darwin" or not _MACWHISPER_APP.exists() or bool(os.environ.get("CI"))


@pytest.mark.integration
@pytest.mark.skipif(_SKIP_CONDITION, reason=_SKIP_REASON)
class TestMacWhisperConnectorReal:
    """Real-device integration tests for MacWhisperConnector.

    All tests in this class use a real MacWhisperConnector instance with no
    platform-check bypass — they exercise the actual filesystem discovery on
    the developer's macOS machine.
    """

    def _make_connector(self) -> MacWhisperConnector:
        """Build a connector that uses real macOS filesystem discovery."""
        return MacWhisperConnector(
            data_layer=MagicMock(spec=DataLayer),
            skip_platform_check=False,
        )

    def test_discover_history_path_real(self):
        """Discover the MacWhisper history directory on the real filesystem.

        Prints the discovered path to stdout so it can be used for ADR updates.

        Observed sandbox path on Malte's dev machine:
        ~/Library/Containers/com.goodsnooze.MacWhisper/Data/Library/Application Support/MacWhisper
        (as of 2026-04-25). Update this docstring and related ADRs if the path changes.
        """
        connector = self._make_connector()
        discovered = connector.discover_history_path()

        print(f"\nDiscovered MacWhisper history path: {discovered}")

        assert discovered.exists(), (
            f"discover_history_path() returned {discovered!r} but the path does not exist"
        )
        assert isinstance(discovered, Path)

    async def test_list_recent_real(self):
        """Call list_recent(n=5) and verify it returns a list of TranscriptRef objects.

        An empty list is acceptable (no transcripts yet), but the return type
        must be a list. If any entries are present, they must be TranscriptRef
        instances.
        """
        connector = self._make_connector()
        results = await connector.list_recent(n=5)

        print(f"\nlist_recent returned {len(results)} entries")

        assert isinstance(results, list), (
            f"list_recent() must return a list, got {type(results)!r}"
        )
        for entry in results:
            assert isinstance(entry, TranscriptRef), (
                f"Expected TranscriptRef, got {type(entry)!r}: {entry!r}"
            )

    async def test_ingest_entry_real(self):
        """Ingest the first available transcript entry using a mock DataLayer.

        Uses a mock TranscriptIngestor to avoid real DB writes. Verifies that
        ingest_entry() delegates correctly and returns an IngestResult-like object.

        Skips if no transcript entries are found in the history directory.
        """
        connector = self._make_connector()
        entries = await connector.list_recent(n=1)

        if not entries:
            pytest.skip("No MacWhisper transcript entries found — cannot test ingest_entry")

        first_entry = entries[0]
        print(f"\nTesting ingest_entry with entry_id={first_entry.entry_id!r}")

        mock_result = MagicMock(spec=IngestResult)
        mock_ingestor = MagicMock()
        mock_ingestor.ingest = AsyncMock(return_value=mock_result)

        connector_with_mock_ingestor = MacWhisperConnector(
            data_layer=MagicMock(spec=DataLayer),
            ingestor=mock_ingestor,
            skip_platform_check=False,
        )

        result = await connector_with_mock_ingestor.ingest_entry(first_entry.entry_id)

        mock_ingestor.ingest.assert_called_once()
        call_args = mock_ingestor.ingest.call_args
        source_ref = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("source_ref", "")
        assert source_ref == f"macwhisper:{first_entry.entry_id}", (
            f"Expected source_ref='macwhisper:{first_entry.entry_id}', got {source_ref!r}"
        )
        assert result is mock_result
