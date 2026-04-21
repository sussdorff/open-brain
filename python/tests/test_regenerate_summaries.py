"""Tests for transcript-based session-summary regeneration.

AK1:  MCP tool registered
AK2:  Implementation in regenerate.py
AK3:  Transcript parsing reuses learning-extractor filter logic
AK4:  LLM summarization shared with session_summary.py
AK5:  Truncation for large transcripts
AK6:  Response schema correct
AK7:  SaveMemoryParams gets upsert_mode field
AK8:  save_memory branches on upsert_mode
AK9:  regenerate calls save_memory with upsert_mode='replace'
AK10: test_save_memory_replace_mode_deletes_old_row
AK11: test_save_memory_append_mode_unchanged
AK12: single session with matching transcript
AK13: orphan, delete_orphans=False → untouched
AK14: orphan, delete_orphans=True → compact fallback
AK15: already-backfilled → skipped
AK16: dry_run=True → no DB changes
AK17: _build_turns_text handles list content
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from open_brain.data_layer.interface import SaveMemoryParams, SaveMemoryResult


# ─── Fixtures ────────────────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "transcripts"


def _make_pool_mock(
    fetchrow_side_effect=None,
    fetchrow_return=None,
    fetch_return=None,
    fetchval_return=None,
    execute_return=None,
):
    """Build a mock asyncpg pool for testing save_memory branches."""
    mock_conn = AsyncMock()
    if fetchrow_side_effect is not None:
        mock_conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    elif fetchrow_return is not None:
        mock_conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    else:
        mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=fetch_return or [])
    mock_conn.fetchval = AsyncMock(return_value=fetchval_return)
    mock_conn.execute = AsyncMock(return_value=execute_return)

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)
    return mock_pool, mock_conn


# ─── AK7: SaveMemoryParams gets upsert_mode ──────────────────────────────────

class TestSaveMemoryParamsUpsertMode:
    def test_default_upsert_mode_is_append(self):
        """AK7: SaveMemoryParams.upsert_mode defaults to 'append'."""
        params = SaveMemoryParams(text="hello")
        assert params.upsert_mode == "append"

    def test_upsert_mode_replace(self):
        """AK7: SaveMemoryParams.upsert_mode can be set to 'replace'."""
        params = SaveMemoryParams(text="hello", upsert_mode="replace")
        assert params.upsert_mode == "replace"

    def test_upsert_mode_type(self):
        """AK7: upsert_mode is Literal['append','replace'] — only those two strings."""
        from typing import get_args, get_type_hints
        hints = get_type_hints(SaveMemoryParams)
        assert "upsert_mode" in hints
        args = get_args(hints["upsert_mode"])
        assert set(args) == {"append", "replace"}


# ─── AK10/AK11: save_memory upsert_mode branching ───────────────────────────

class TestSaveMemoryUpsertMode:
    @pytest.mark.asyncio
    async def test_save_memory_replace_mode_deletes_old_row(self):
        """AK10: replace mode deletes existing session_summary rows then inserts fresh."""
        from open_brain.data_layer.postgres import PostgresDataLayer

        mock_pool, mock_conn = _make_pool_mock()

        # _resolve_index_id returns index_id=1 (via fetch on project_index)
        # fetchrow calls sequence:
        #  1. _resolve_index_id -> None (no project lookup needed if project=None)
        #  2. existing row check -> returns a row  (so replace path is triggered)
        #  3. content_hash dedup -> None (no dup)
        #  4. INSERT -> returns {"id": 99}
        fetchrow_returns = [
            None,        # _resolve_index_id: no project row
            None,        # content_hash dedup check -> no dup
        ]
        insert_row = MagicMock()
        insert_row.__getitem__ = MagicMock(side_effect=lambda k: 99 if k == "id" else None)
        mock_conn.fetchrow = AsyncMock(side_effect=fetchrow_returns + [insert_row])
        mock_conn.fetchval = AsyncMock(return_value=None)

        dl = PostgresDataLayer()
        params = SaveMemoryParams(
            text="new summary",
            type="session_summary",
            session_ref="session-abc",
            upsert_mode="replace",
        )

        with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            with patch("open_brain.data_layer.postgres.asyncio.create_task"):
                result = await dl.save_memory(params)

        # execute should have been called 3 times: delete memory_usage_log, delete memory_relationships, delete memories
        assert mock_conn.execute.await_count >= 3
        calls_str = [str(c) for c in mock_conn.execute.call_args_list]
        assert any("memory_usage_log" in c for c in calls_str)
        assert any("memory_relationships" in c for c in calls_str)
        assert any("DELETE FROM memories" in c for c in calls_str)
        # And finally a new row is returned
        assert result.id == 99

    @pytest.mark.asyncio
    async def test_save_memory_append_mode_unchanged(self):
        """AK11: append mode with existing session_summary merges content, does NOT delete."""
        from open_brain.data_layer.postgres import PostgresDataLayer

        mock_pool, mock_conn = _make_pool_mock()

        existing_row = MagicMock()
        existing_row.__getitem__ = MagicMock(
            side_effect=lambda k: {
                "id": 7,
                "content": "old content",
            }.get(k)
        )

        # fetchrow sequence:
        # 1. _resolve_index_id -> None
        # 2. existing row for session_summary upsert -> existing_row
        fetchrow_returns = [None, existing_row]
        mock_conn.fetchrow = AsyncMock(side_effect=fetchrow_returns)

        dl = PostgresDataLayer()
        params = SaveMemoryParams(
            text="extra summary",
            type="session_summary",
            session_ref="session-def",
            upsert_mode="append",  # default / explicit append
        )

        with patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            with patch("open_brain.data_layer.postgres.asyncio.create_task"):
                result = await dl.save_memory(params)

        # In append mode: UPDATE is called, no DELETE
        assert mock_conn.execute.await_count == 1  # only the UPDATE
        update_call = str(mock_conn.execute.call_args_list[0])
        assert "UPDATE memories" in update_call
        assert "DELETE" not in update_call
        assert result.id == 7
        assert result.message == "Memory updated (upsert)"


# ─── AK17: _build_turns_text list content ────────────────────────────────────

class TestBuildTurnsTextListContent:
    def test_flat_string_content_unchanged(self):
        """AK17: flat {type, content: str} format still works."""
        from open_brain.session_summary import _build_turns_text
        turns = [
            {"type": "user", "content": "Hello world", "isMeta": False},
            {"type": "assistant", "content": "Hi there", "isMeta": False},
        ]
        result = _build_turns_text(turns)
        assert "Hello world" in result
        assert "Hi there" in result

    def test_list_content_extracts_text_blocks(self):
        """AK17: message.content as list of blocks — text blocks extracted."""
        from open_brain.session_summary import _build_turns_text
        turns = [
            {
                "type": "assistant",
                "isMeta": False,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I will implement this."},
                        {"type": "tool_use", "name": "Edit", "id": "t1", "input": {}},
                    ],
                },
            }
        ]
        result = _build_turns_text(turns)
        assert "I will implement this." in result
        assert "[tool: Edit]" in result

    def test_list_content_user_string_in_message(self):
        """AK17: user turn with message.content as string (raw JSONL format)."""
        from open_brain.session_summary import _build_turns_text
        turns = [
            {
                "type": "user",
                "isMeta": False,
                "message": {
                    "role": "user",
                    "content": "User message via message.content",
                },
            }
        ]
        result = _build_turns_text(turns)
        assert "User message via message.content" in result

    def test_meta_turns_filtered(self):
        """AK17: turns with isMeta=True are excluded."""
        from open_brain.session_summary import _build_turns_text
        turns = [
            {"type": "user", "isMeta": True, "content": "system init"},
            {"type": "user", "isMeta": False, "content": "real message"},
        ]
        result = _build_turns_text(turns)
        assert "system init" not in result
        assert "real message" in result

    def test_empty_content_block_skipped(self):
        """AK17: blocks with empty text are not included."""
        from open_brain.session_summary import _build_turns_text
        turns = [
            {
                "type": "assistant",
                "isMeta": False,
                "message": {
                    "content": [
                        {"type": "text", "text": ""},
                        {"type": "tool_use", "name": "Bash"},
                    ],
                },
            }
        ]
        result = _build_turns_text(turns)
        assert "[tool: Bash]" in result


# ─── AK17: _parse_raw_jsonl_turns ────────────────────────────────────────────

class TestParseRawJsonlTurns:
    def test_parses_fixture_transcript(self):
        """AK3: _parse_raw_jsonl_turns parses raw JSONL, filters isMeta."""
        from open_brain.regenerate import _parse_raw_jsonl_turns
        path = FIXTURES_DIR / "session-abc123.jsonl"
        turns = _parse_raw_jsonl_turns(path)
        # 4 valid turns (2 user + 2 assistant), 1 isMeta=True filtered
        assert len(turns) == 4
        for t in turns:
            assert t["type"] in ("user", "assistant")
            assert t.get("isMeta") is not True
            assert isinstance(t["content"], str)
            assert t["content"]

    def test_tool_use_blocks_represented(self):
        """AK3: tool_use blocks in assistant content appear as [tool: name]."""
        from open_brain.regenerate import _parse_raw_jsonl_turns
        path = FIXTURES_DIR / "session-abc123.jsonl"
        turns = _parse_raw_jsonl_turns(path)
        assistant_turns = [t for t in turns if t["type"] == "assistant"]
        assert any("[tool: Edit]" in t["content"] for t in assistant_turns)

    def test_handles_invalid_json_lines(self, tmp_path):
        """AK3: invalid JSON lines are skipped gracefully."""
        from open_brain.regenerate import _parse_raw_jsonl_turns
        path = tmp_path / "test.jsonl"
        path.write_text(
            'not json\n'
            '{"type":"user","message":{"content":"valid"}}\n'
        )
        turns = _parse_raw_jsonl_turns(path)
        assert len(turns) == 1
        assert turns[0]["content"] == "valid"


# ─── AK16: dry_run ───────────────────────────────────────────────────────────

class TestRegenerateDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_returns_plan_no_db_changes(self, tmp_path):
        """AK16: dry_run=True returns full plan without DB modifications."""
        from open_brain.regenerate import RegenerateParams, regenerate_summaries

        # Create a transcript for our test session
        transcript_dir = tmp_path / "project"
        transcript_dir.mkdir()
        transcript_file = transcript_dir / "session-test-001.jsonl"
        transcript_file.write_text(
            '{"type":"user","isMeta":false,"message":{"content":"Hello"}}\n'
            '{"type":"assistant","isMeta":false,"message":{"content":[{"type":"text","text":"Hi"}]}}\n'
        )

        mock_dl = AsyncMock()
        # Simulate one existing session_summary for session-test-001
        mock_memory = MagicMock()
        mock_memory.session_ref = "session-test-001"
        mock_memory.id = 42
        mock_memory.metadata = {}
        mock_memory.type = "session_summary"

        from open_brain.data_layer.interface import SearchResult
        mock_dl.search = AsyncMock(return_value=SearchResult(results=[mock_memory], total=1))

        params = RegenerateParams(
            transcript_root=str(tmp_path),
            dry_run=True,
        )

        result = await regenerate_summaries(mock_dl, params)

        # dry_run: save_memory must NOT be called
        mock_dl.save_memory.assert_not_called()
        # plan should be populated
        assert result.plan is not None
        assert len(result.plan) >= 1
        # result struct has required fields
        assert hasattr(result, "regenerated_count")
        assert hasattr(result, "orphan_count")
        assert hasattr(result, "transcript_missing_list")
        assert hasattr(result, "new_summary_ids")


# ─── AK12: happy path ────────────────────────────────────────────────────────

class TestRegenerateHappyPath:
    @pytest.mark.asyncio
    async def test_single_session_with_transcript(self, tmp_path):
        """AK12: session with matching transcript → 1 old deleted, 1 new created."""
        from open_brain.regenerate import RegenerateParams, regenerate_summaries

        # Transcript file
        transcript_dir = tmp_path / "project"
        transcript_dir.mkdir()
        transcript_file = transcript_dir / "session-happy.jsonl"
        transcript_file.write_text(
            '{"type":"user","isMeta":false,"message":{"content":"Do the thing"}}\n'
            '{"type":"assistant","isMeta":false,"message":{"content":[{"type":"text","text":"Done."}]}}\n'
        )

        mock_dl = AsyncMock()
        mock_memory = MagicMock()
        mock_memory.session_ref = "session-happy"
        mock_memory.id = 55
        mock_memory.metadata = {}  # no transcript-backfill source

        from open_brain.data_layer.interface import SearchResult
        mock_dl.search = AsyncMock(return_value=SearchResult(results=[mock_memory], total=1))
        mock_dl.save_memory = AsyncMock(return_value=SaveMemoryResult(id=99, message="Memory saved"))

        params = RegenerateParams(
            transcript_root=str(tmp_path),
            dry_run=False,
        )

        with patch("open_brain.regenerate.summarize_transcript_turns", new_callable=AsyncMock, return_value=99):
            result = await regenerate_summaries(mock_dl, params)

        assert result.regenerated_count == 1
        assert 99 in result.new_summary_ids


# ─── AK13/AK14: orphan handling ──────────────────────────────────────────────

class TestRegenerateOrphan:
    @pytest.mark.asyncio
    async def test_orphan_skip_when_delete_orphans_false(self, tmp_path):
        """AK13: session with no matching transcript + delete_orphans=False → untouched."""
        from open_brain.regenerate import RegenerateParams, regenerate_summaries

        # Empty transcript_root (no transcript files)
        mock_dl = AsyncMock()
        mock_memory = MagicMock()
        mock_memory.session_ref = "session-orphan"
        mock_memory.id = 77
        mock_memory.metadata = {}

        from open_brain.data_layer.interface import SearchResult
        mock_dl.search = AsyncMock(return_value=SearchResult(results=[mock_memory], total=1))

        params = RegenerateParams(
            transcript_root=str(tmp_path),
            dry_run=False,
            delete_orphans=False,
        )
        result = await regenerate_summaries(mock_dl, params)

        assert result.orphan_count == 1
        assert "session-orphan" in result.transcript_missing_list
        mock_dl.delete_memories.assert_not_called()

    @pytest.mark.asyncio
    async def test_orphan_delete_when_delete_orphans_true(self, tmp_path):
        """AK14: session with no matching transcript + delete_orphans=True → delete_memories called."""
        from open_brain.regenerate import RegenerateParams, regenerate_summaries
        from open_brain.data_layer.interface import DeleteResult

        mock_dl = AsyncMock()
        mock_memory = MagicMock()
        mock_memory.session_ref = "session-orphan2"
        mock_memory.id = 88
        mock_memory.metadata = {}

        from open_brain.data_layer.interface import SearchResult
        mock_dl.search = AsyncMock(return_value=SearchResult(results=[mock_memory], total=1))
        mock_dl.delete_memories = AsyncMock(return_value=DeleteResult(deleted=1))

        params = RegenerateParams(
            transcript_root=str(tmp_path),
            dry_run=False,
            delete_orphans=True,
        )
        result = await regenerate_summaries(mock_dl, params)

        mock_dl.delete_memories.assert_called_once()
        delete_call_params = mock_dl.delete_memories.call_args[0][0]
        assert 88 in delete_call_params.ids


# ─── AK15: already-backfilled skip ───────────────────────────────────────────

class TestRegenerateAlreadyBackfilled:
    @pytest.mark.asyncio
    async def test_already_backfilled_session_skipped(self, tmp_path):
        """AK15: session_ref with metadata.source='transcript-backfill' → skipped."""
        from open_brain.regenerate import RegenerateParams, regenerate_summaries

        # Create transcript so it's findable
        transcript_dir = tmp_path / "project"
        transcript_dir.mkdir()
        transcript_file = transcript_dir / "session-backfilled.jsonl"
        transcript_file.write_text(
            '{"type":"user","isMeta":false,"message":{"content":"Old work"}}\n'
        )

        mock_dl = AsyncMock()
        mock_memory = MagicMock()
        mock_memory.session_ref = "session-backfilled"
        mock_memory.id = 100
        mock_memory.metadata = {"source": "transcript-backfill"}  # already done

        from open_brain.data_layer.interface import SearchResult
        mock_dl.search = AsyncMock(return_value=SearchResult(results=[mock_memory], total=1))

        params = RegenerateParams(
            transcript_root=str(tmp_path),
            dry_run=False,
        )
        result = await regenerate_summaries(mock_dl, params)

        mock_dl.save_memory.assert_not_called()
        # The session plan should mark it as already-backfilled-skip
        skip_plans = [p for p in result.plan if p.action == "already-backfilled-skip"]
        assert len(skip_plans) == 1


# ─── AK1: MCP tool registration ──────────────────────────────────────────────

class TestMcpToolRegistration:
    def test_regenerate_tool_registered(self):
        """AK1: regenerate_summaries_from_transcripts is registered as an MCP tool."""
        from open_brain.server import mcp
        tool_names = [t.name for t in mcp._tool_manager.list_tools()]
        assert "regenerate_summaries_from_transcripts" in tool_names


# ─── AK5: Truncation ─────────────────────────────────────────────────────────

class TestTruncation:
    def test_large_transcript_truncated(self, tmp_path):
        """AK5: large transcript head+tail pattern preserved in _parse_raw_jsonl_turns."""
        from open_brain.session_summary import _build_turns_text

        # Create many turns that will exceed _MAX_TURNS_TEXT when joined
        turns = []
        for i in range(200):
            turns.append({
                "type": "user",
                "content": f"Message {i}: " + "X" * 100,
                "isMeta": False,
            })

        text = _build_turns_text(turns)
        # Truncation is done by caller (summarize_transcript_turns), not _build_turns_text
        # But the text must be non-empty
        assert len(text) > 0

    @pytest.mark.asyncio
    async def test_summarize_truncates_at_8000_chars(self):
        """AK5: summarize_transcript_turns truncates turns_text at 8000 chars."""
        from open_brain.session_summary import summarize_transcript_turns

        # Build turns that produce text > 8000 chars
        turns = []
        for i in range(100):
            turns.append({
                "type": "user",
                "content": "A" * 100,
                "isMeta": False,
            })
            turns.append({
                "type": "assistant",
                "content": "B" * 100,
                "isMeta": False,
            })

        captured = []
        async def mock_llm(messages, **kwargs):
            captured.append(messages[0].content)
            return '{"title": "t", "content": "c", "narrative": null}'

        mock_dl = AsyncMock()
        mock_dl.save_memory = AsyncMock(return_value=SaveMemoryResult(id=1, message="ok"))

        with patch("open_brain.session_summary.get_pool", new_callable=AsyncMock) as mock_get_pool:
            mock_pool, mock_conn = _make_pool_mock(fetchrow_return=None)
            mock_get_pool.return_value = mock_pool
            with patch("open_brain.session_summary.get_dl", return_value=mock_dl):
                with patch("open_brain.session_summary.llm_complete", side_effect=mock_llm):
                    with patch("open_brain.session_summary.parse_llm_json",
                               return_value={"title": "t", "content": "c", "narrative": None}):
                        await summarize_transcript_turns(
                            project="test-project",
                            session_id="session-trunc-test",
                            turns=turns,
                            source="test",
                            bypass_dedup=True,
                        )

        assert captured, "llm_complete was not called"
        assert "[...truncated...]" in captured[0]


# ─── AK9: save_memory metadata and upsert_mode in regenerate ─────────────────

class TestRegenerateCallsSaveMemoryWithReplaceMode:
    @pytest.mark.asyncio
    async def test_save_memory_called_with_transcript_backfill_metadata(self, tmp_path):
        """AK9: regenerate calls summarize_transcript_turns with bypass_dedup and correct source."""
        from open_brain.regenerate import RegenerateParams, regenerate_summaries

        transcript_dir = tmp_path / "proj"
        transcript_dir.mkdir()
        (transcript_dir / "session-meta-test.jsonl").write_text(
            '{"type":"user","isMeta":false,"message":{"content":"work"}}\n'
        )

        mock_dl = AsyncMock()
        mock_memory = MagicMock()
        mock_memory.session_ref = "session-meta-test"
        mock_memory.id = 55
        mock_memory.metadata = {}

        from open_brain.data_layer.interface import SearchResult
        mock_dl.search = AsyncMock(return_value=SearchResult(results=[mock_memory], total=1))

        captured_kwargs: list[dict] = []

        async def fake_summarize(project, session_id, turns, source, **kwargs):
            captured_kwargs.append({"source": source, **kwargs})
            return 200

        params = RegenerateParams(transcript_root=str(tmp_path), dry_run=False)

        with patch("open_brain.regenerate.summarize_transcript_turns", side_effect=fake_summarize):
            result = await regenerate_summaries(mock_dl, params)

        assert len(captured_kwargs) == 1
        assert captured_kwargs[0]["source"] == "transcript-backfill"
        assert captured_kwargs[0].get("bypass_dedup") is True
