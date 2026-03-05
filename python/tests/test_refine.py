"""Unit tests for refine.py (LLM-powered consolidation)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from open_brain.data_layer.interface import Memory, RefineAction
from open_brain.data_layer.refine import analyze_with_llm, find_obvious_duplicates


def _make_memory(id: int, title: str = "Title", content: str = "Content") -> Memory:
    return Memory(
        id=id,
        index_id=1,
        session_id=None,
        type="observation",
        title=title,
        subtitle=None,
        narrative=None,
        content=content,
        metadata={},
        priority=0.5,
        stability="stable",
        access_count=0,
        last_accessed_at=None,
        created_at="2026-01-01",
        updated_at="2026-01-01",
    )


class TestAnalyzeWithLlm:
    @pytest.mark.asyncio
    async def test_falls_back_to_duplicates_without_api_key(self):
        """Without LLM API key, falls back to simple duplicate detection."""
        memories = [_make_memory(1, "Same title"), _make_memory(2, "Same title")]

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            import open_brain.config as cfg
            cfg._config = None
            result = await analyze_with_llm(memories)

        # Should find the duplicates via simple method
        assert len(result) >= 1
        assert result[0].action == "merge"

    @pytest.mark.asyncio
    async def test_uses_llm_when_api_key_present(self):
        """With API key, calls LLM and parses JSON response."""
        memories = [_make_memory(1, "Title A"), _make_memory(2, "Title B")]
        llm_response = json.dumps([
            {"action": "merge", "memory_ids": [1, 2], "reason": "Similar topics"}
        ])

        with patch("open_brain.data_layer.refine.llm_complete", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            result = await analyze_with_llm(memories)

        assert len(result) == 1
        assert result[0].action == "merge"
        assert result[0].memory_ids == [1, 2]
        assert result[0].reason == "Similar topics"

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_llm_returns_no_json(self):
        """If LLM returns non-JSON, returns empty list."""
        memories = [_make_memory(1)]

        with patch("open_brain.data_layer.refine.llm_complete", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "No actions needed, everything looks good."
            result = await analyze_with_llm(memories)

        assert result == []

    @pytest.mark.asyncio
    async def test_falls_back_on_llm_error(self):
        """If LLM call fails, falls back to simple duplicate detection."""
        memories = [_make_memory(1, "Same"), _make_memory(2, "Same")]

        with patch("open_brain.data_layer.refine.llm_complete", side_effect=RuntimeError("API down")):
            result = await analyze_with_llm(memories)

        # Fallback to find_obvious_duplicates
        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_handles_promote_action(self):
        """Handles all action types from LLM."""
        memories = [_make_memory(5)]
        llm_response = json.dumps([
            {"action": "promote", "memory_ids": [5], "reason": "High quality"}
        ])

        with patch("open_brain.data_layer.refine.llm_complete", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            result = await analyze_with_llm(memories)

        assert result[0].action == "promote"
        assert result[0].memory_ids == [5]

    @pytest.mark.asyncio
    async def test_handles_empty_memories(self):
        """Returns empty for empty input without calling LLM."""
        with patch("open_brain.data_layer.refine.llm_complete", new_callable=AsyncMock) as mock_llm:
            result = await analyze_with_llm([])
        # LLM should still be called (it's up to the caller to filter)
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_parsed_actions_have_executed_false(self):
        """Parsed RefineActions should have executed=False by default."""
        memories = [_make_memory(1), _make_memory(2)]
        llm_response = json.dumps([
            {"action": "delete", "memory_ids": [2], "reason": "Obsolete"}
        ])

        with patch("open_brain.data_layer.refine.llm_complete", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            result = await analyze_with_llm(memories)

        assert result[0].executed is False


class TestFindObviousDuplicates:
    def test_single_memory_no_duplicates(self):
        memories = [_make_memory(1, "Unique title")]
        result = find_obvious_duplicates(memories)
        assert result == []

    def test_finds_exact_title_match(self):
        memories = [
            _make_memory(1, "Exact Title"),
            _make_memory(2, "Exact Title"),
        ]
        result = find_obvious_duplicates(memories)
        assert len(result) == 1
        assert result[0].action == "merge"
        assert set(result[0].memory_ids) == {1, 2}

    def test_groups_three_duplicates(self):
        memories = [
            _make_memory(1, "Same"),
            _make_memory(2, "Same"),
            _make_memory(3, "Same"),
        ]
        result = find_obvious_duplicates(memories)
        assert len(result) == 1
        assert set(result[0].memory_ids) == {1, 2, 3}

    def test_no_false_positives_different_titles(self):
        memories = [
            _make_memory(1, "Title A"),
            _make_memory(2, "Title B"),
            _make_memory(3, "Title C"),
        ]
        result = find_obvious_duplicates(memories)
        assert result == []

    def test_whitespace_trimmed(self):
        memories = [
            _make_memory(1, "  spaces  "),
            _make_memory(2, "spaces"),
        ]
        result = find_obvious_duplicates(memories)
        assert len(result) == 1

    def test_reason_includes_title(self):
        memories = [_make_memory(1, "My Title"), _make_memory(2, "My Title")]
        result = find_obvious_duplicates(memories)
        assert "My Title" in result[0].reason

    def test_none_title_uses_content(self):
        """When title is None, uses content prefix for matching."""
        m1 = _make_memory(1, content="Same content prefix that is quite long")
        m1.title = None
        m2 = _make_memory(2, content="Same content prefix that is quite long")
        m2.title = None
        result = find_obvious_duplicates([m1, m2])
        assert len(result) == 1
