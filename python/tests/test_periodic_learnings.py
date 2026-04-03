"""Tests for periodic learnings state helper (AK2 + AK3).

Unit tests cover:
  AK2 — dedup prevents duplicate learnings (content_hash matching via save_memory)
  AK3 — processing-state.json updated with last_learnings_run timestamp
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from open_brain.data_layer.interface import SaveMemoryResult
from open_brain.learnings_state import (
    is_extraction_due,
    load_state,
    mark_extraction_ran,
    save_state,
)


# ─── AK3: Processing state tests ─────────────────────────────────────────────


class TestLearningsState:
    def test_load_state_returns_empty_dict_when_file_missing(self, tmp_path):
        result = load_state(tmp_path / "nonexistent.json")
        assert result == {}

    def test_load_state_returns_empty_dict_on_corrupt_file(self, tmp_path):
        bad_file = tmp_path / "corrupt.json"
        bad_file.write_text("not valid json {{{{")
        result = load_state(bad_file)
        assert result == {}

    def test_load_state_returns_existing_content(self, tmp_path):
        state_file = tmp_path / "state.json"
        expected = {"version": "1.0", "processed_conversations": {"file.jsonl": "abc123"}}
        state_file.write_text(json.dumps(expected))
        result = load_state(state_file)
        assert result == expected

    def test_save_state_writes_json(self, tmp_path):
        state_file = tmp_path / "state.json"
        state = {"version": "1.0", "last_learnings_run": "2026-04-03T10:00:00+00:00"}
        save_state(state_file, state)
        written = json.loads(state_file.read_text())
        assert written == state

    def test_save_state_is_atomic(self, tmp_path):
        state_file = tmp_path / "state.json"
        save_state(state_file, {"key": "value"})
        # No .tmp file should be left behind
        tmp_file = Path(str(state_file) + ".tmp")
        assert not tmp_file.exists()

    def test_is_extraction_due_when_never_run(self):
        assert is_extraction_due({}) is True

    def test_is_extraction_due_when_last_run_4h_ago(self):
        four_hours_ago = (
            datetime.now(timezone.utc) - timedelta(hours=4, minutes=1)
        ).isoformat()
        state = {"last_learnings_run": four_hours_ago}
        assert is_extraction_due(state) is True

    def test_is_extraction_due_when_last_run_recently(self):
        one_hour_ago = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        state = {"last_learnings_run": one_hour_ago}
        assert is_extraction_due(state) is False

    def test_is_extraction_due_custom_interval(self):
        two_and_half_hours_ago = (
            datetime.now(timezone.utc) - timedelta(hours=2, minutes=30)
        ).isoformat()
        state = {"last_learnings_run": two_and_half_hours_ago}
        # interval_hours=2 → 2.5h ago > 2h → due
        assert is_extraction_due(state, interval_hours=2.0) is True

    def test_mark_extraction_ran_sets_timestamp(self):
        result = mark_extraction_ran({})
        assert "last_learnings_run" in result
        # Must be a parseable ISO timestamp
        ts = datetime.fromisoformat(result["last_learnings_run"])
        assert ts.tzinfo is not None  # must be timezone-aware

    def test_mark_extraction_ran_preserves_existing_keys(self):
        existing = {"processed_conversations": {"file.jsonl": "abc"}, "version": "1.0"}
        result = mark_extraction_ran(existing)
        assert result["processed_conversations"] == {"file.jsonl": "abc"}
        assert result["version"] == "1.0"
        assert "last_learnings_run" in result


# ─── AK2: Dedup test ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_dl():
    """Mock DataLayer that returns predetermined results."""
    dl = AsyncMock()
    dl.save_memory.return_value = SaveMemoryResult(id=1, message="Memory saved")
    return dl


class TestPeriodicLearningsDedup:
    @pytest.mark.asyncio
    async def test_periodic_learnings_dedup_prevents_duplicates(self, mock_dl):
        """Simulate saving same learning twice: second call hits dedup.

        This test verifies that:
        1. save_memory is called twice with the SAME text content
        2. The server correctly passes through the duplicate_of signal from the data layer
        3. Both calls reach the data layer (no short-circuit before the data layer)
        """
        learning_text = "Always use uv run python, not python directly"

        # First call: normal insert; second call: data layer signals dedup via duplicate_of
        mock_dl.save_memory.side_effect = [
            SaveMemoryResult(id=1, message="Memory saved"),
            SaveMemoryResult(id=1, message="Duplicate content detected", duplicate_of=1),
        ]
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import save_memory

            # First save — should succeed normally
            result1 = json.loads(await save_memory(text=learning_text, type="learning"))
            assert result1["id"] == 1
            assert "saved" in result1["message"].lower()

            # Second save of identical text — data layer returns duplicate_of signal
            result2 = json.loads(
                await save_memory(text=learning_text, type="learning")
            )
            assert result2["duplicate_of"] == 1
            assert "duplicate" in result2["message"].lower()

        # Verify data layer was called exactly twice (no early bail-out)
        assert mock_dl.save_memory.call_count == 2

        # Verify both calls used the SAME text content — confirming content_hash would match.
        # save_memory passes a SaveMemoryParams object as its first positional argument.
        first_params = mock_dl.save_memory.call_args_list[0].args[0]
        second_params = mock_dl.save_memory.call_args_list[1].args[0]
        assert first_params.text == learning_text
        assert second_params.text == learning_text
        assert first_params.text == second_params.text

    @pytest.mark.asyncio
    async def test_periodic_learnings_different_content_inserts_new(self, mock_dl):
        """Two different learning texts: both get inserted."""
        mock_dl.save_memory.side_effect = [
            SaveMemoryResult(id=10, message="Memory saved"),
            SaveMemoryResult(id=11, message="Memory saved"),
        ]
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import save_memory

            result_a = json.loads(
                await save_memory(text="Use pathlib.Path not os.path", type="learning")
            )
            result_b = json.loads(
                await save_memory(
                    text="Always add type hints to public APIs", type="learning"
                )
            )

        assert result_a["id"] == 10
        assert result_b["id"] == 11
        assert mock_dl.save_memory.call_count == 2
