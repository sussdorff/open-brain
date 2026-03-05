"""AK 1: Integration tests for all 8 MCP tools (mocked DataLayer)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.interface import (
    Memory,
    RefineAction,
    RefineResult,
    SaveMemoryResult,
    SearchResult,
    TimelineResult,
)


def _make_memory(id: int = 1, **kwargs) -> Memory:
    """Create a sample Memory for testing."""
    defaults = dict(
        index_id=1,
        session_id=None,
        type="observation",
        title="Test Memory",
        subtitle=None,
        narrative=None,
        content="Test content",
        metadata={},
        priority=0.5,
        stability="stable",
        access_count=0,
        last_accessed_at=None,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    defaults.update(kwargs)
    return Memory(id=id, **defaults)


@pytest.fixture
def mock_dl():
    """Mock DataLayer that returns predetermined results."""
    dl = AsyncMock()
    dl.search.return_value = SearchResult(results=[_make_memory()], total=1)
    dl.timeline.return_value = TimelineResult(results=[_make_memory()], anchor_id=1)
    dl.get_observations.return_value = [_make_memory(id=1), _make_memory(id=2)]
    dl.save_memory.return_value = SaveMemoryResult(id=42, message="Memory saved")
    dl.search_by_concept.return_value = {"results": [_make_memory()]}
    dl.get_context.return_value = {"sessions": []}
    dl.stats.return_value = {
        "memories": 100, "sessions": 10, "relationships": 50,
        "db_size_bytes": 1048576, "db_size_mb": 1.0,
    }
    dl.refine_memories.return_value = RefineResult(
        analyzed=5,
        actions=[RefineAction(action="merge", memory_ids=[1, 2], reason="duplicate", executed=True)],
        summary="Analyzed 5 memories, suggested 1 actions",
    )
    return dl


# ─── Search tool ──────────────────────────────────────────────────────────────

class TestSearchTool:
    @pytest.mark.asyncio
    async def test_search_with_query(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import search
            result = await search(query="test query")
            data = json.loads(result)
            assert data["total"] == 1
            assert len(data["results"]) == 1
            assert data["results"][0]["id"] == 1
            mock_dl.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_passes_all_params(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import search
            await search(
                query="q",
                limit=10,
                project="proj",
                type="decision",
                date_start="2026-01-01",
                date_end="2026-12-31",
                offset=5,
                order_by="oldest",
            )
            call_args = mock_dl.search.call_args[0][0]
            assert call_args.query == "q"
            assert call_args.limit == 10
            assert call_args.project == "proj"
            assert call_args.type == "decision"
            assert call_args.offset == 5
            assert call_args.order_by == "oldest"

    @pytest.mark.asyncio
    async def test_search_no_params(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import search
            result = await search()
            data = json.loads(result)
            assert "total" in data
            assert "results" in data


# ─── Timeline tool ────────────────────────────────────────────────────────────

class TestTimelineTool:
    @pytest.mark.asyncio
    async def test_timeline_with_anchor(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import timeline
            result = await timeline(anchor=42)
            data = json.loads(result)
            assert data["anchor_id"] == 1
            assert len(data["results"]) == 1
            call_args = mock_dl.timeline.call_args[0][0]
            assert call_args.anchor == 42

    @pytest.mark.asyncio
    async def test_timeline_with_query(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import timeline
            result = await timeline(query="find this")
            data = json.loads(result)
            assert "anchor_id" in data
            call_args = mock_dl.timeline.call_args[0][0]
            assert call_args.query == "find this"

    @pytest.mark.asyncio
    async def test_timeline_depth_params(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import timeline
            await timeline(anchor=1, depth_before=3, depth_after=7, project="myproject")
            call_args = mock_dl.timeline.call_args[0][0]
            assert call_args.depth_before == 3
            assert call_args.depth_after == 7
            assert call_args.project == "myproject"


# ─── GetObservations tool ─────────────────────────────────────────────────────

class TestGetObservationsTool:
    @pytest.mark.asyncio
    async def test_get_observations_returns_memories(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import get_observations
            result = await get_observations(ids=[1, 2])
            data = json.loads(result)
            assert len(data) == 2
            mock_dl.get_observations.assert_called_once_with([1, 2])

    @pytest.mark.asyncio
    async def test_get_observations_empty_ids(self, mock_dl):
        mock_dl.get_observations.return_value = []
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import get_observations
            result = await get_observations(ids=[])
            data = json.loads(result)
            assert data == []


# ─── SaveMemory tool ──────────────────────────────────────────────────────────

class TestSaveMemoryTool:
    @pytest.mark.asyncio
    async def test_save_memory_returns_id(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import save_memory
            result = await save_memory(text="Important observation")
            data = json.loads(result)
            assert data["id"] == 42
            assert data["message"] == "Memory saved"

    @pytest.mark.asyncio
    async def test_save_memory_passes_all_params(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import save_memory
            await save_memory(
                text="content",
                type="decision",
                project="myproj",
                title="My Title",
                subtitle="My Subtitle",
                narrative="Context here",
            )
            call_args = mock_dl.save_memory.call_args[0][0]
            assert call_args.text == "content"
            assert call_args.type == "decision"
            assert call_args.project == "myproj"
            assert call_args.title == "My Title"
            assert call_args.subtitle == "My Subtitle"
            assert call_args.narrative == "Context here"


# ─── SearchByConcept tool ─────────────────────────────────────────────────────

class TestSearchByConceptTool:
    @pytest.mark.asyncio
    async def test_search_by_concept_returns_results(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import search_by_concept
            result = await search_by_concept(query="semantic concept")
            data = json.loads(result)
            assert "results" in data
            assert len(data["results"]) == 1
            mock_dl.search_by_concept.assert_called_once_with("semantic concept", None, None)

    @pytest.mark.asyncio
    async def test_search_by_concept_with_limit_and_project(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import search_by_concept
            await search_by_concept(query="test", limit=5, project="proj")
            mock_dl.search_by_concept.assert_called_once_with("test", 5, "proj")


# ─── GetContext tool ──────────────────────────────────────────────────────────

class TestGetContextTool:
    @pytest.mark.asyncio
    async def test_get_context_returns_sessions(self, mock_dl):
        mock_dl.get_context.return_value = {"sessions": [{"id": 1, "project": "proj"}]}
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import get_context
            result = await get_context()
            data = json.loads(result)
            assert "sessions" in data

    @pytest.mark.asyncio
    async def test_get_context_passes_limit_and_project(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import get_context
            await get_context(limit=3, project="myproject")
            mock_dl.get_context.assert_called_once_with(3, "myproject")


# ─── Stats tool ───────────────────────────────────────────────────────────────

class TestStatsTool:
    @pytest.mark.asyncio
    async def test_stats_returns_all_fields(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import stats
            result = await stats()
            data = json.loads(result)
            assert "memories" in data
            assert "sessions" in data
            assert "relationships" in data
            assert "db_size_bytes" in data
            assert "db_size_mb" in data
            assert data["memories"] == 100
            assert data["db_size_mb"] == 1.0


# ─── RefineMemories tool ──────────────────────────────────────────────────────

class TestRefineMemoriesTool:
    @pytest.mark.asyncio
    async def test_refine_memories_returns_summary(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import refine_memories
            result = await refine_memories()
            data = json.loads(result)
            assert "analyzed" in data
            assert "summary" in data
            assert "actions" in data
            assert data["analyzed"] == 5

    @pytest.mark.asyncio
    async def test_refine_memories_dry_run(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import refine_memories
            await refine_memories(scope="recent", limit=10, dry_run=True)
            call_args = mock_dl.refine_memories.call_args[0][0]
            assert call_args.dry_run is True
            assert call_args.scope == "recent"
            assert call_args.limit == 10

    @pytest.mark.asyncio
    async def test_refine_memories_actions_structure(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import refine_memories
            result = await refine_memories()
            data = json.loads(result)
            assert len(data["actions"]) == 1
            action = data["actions"][0]
            assert action["action"] == "merge"
            assert action["memory_ids"] == [1, 2]
            assert action["executed"] is True


# ─── IMPORTANT tool ───────────────────────────────────────────────────────────

class TestImportantTool:
    @pytest.mark.asyncio
    async def test_important_tool_is_registered(self):
        """Verify __IMPORTANT is registered in the MCP server."""
        import open_brain.server as server_module
        # Use getattr to bypass Python's name mangling in class scope
        important_fn = getattr(server_module, "__IMPORTANT")
        result = await important_fn()
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_important_tool_via_mcp(self):
        """Verify __IMPORTANT is listed in MCP tools."""
        from open_brain.server import mcp
        tools = await mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert "____IMPORTANT" in tool_names or "__IMPORTANT" in tool_names


# ─── Integration tests (skipped by default) ───────────────────────────────────

@pytest.mark.integration
class TestToolsIntegration:
    """Integration tests requiring a real database. Run with INTEGRATION_TEST=1."""

    @pytest.mark.asyncio
    async def test_save_and_search_memory(self):
        """Save a memory and then find it via search."""
        from open_brain.server import save_memory, search
        save_result = json.loads(await save_memory(text="Integration test memory", type="test"))
        assert save_result["id"] > 0

        # Search for it
        search_result = json.loads(await search(query="Integration test memory"))
        assert search_result["total"] >= 1

    @pytest.mark.asyncio
    async def test_stats_returns_counts(self):
        """Stats should return non-negative counts."""
        from open_brain.server import stats as stats_tool
        result = json.loads(await stats_tool())
        assert result["memories"] >= 0
        assert result["sessions"] >= 0
