"""AK 1: Integration tests for all 8 MCP tools (mocked DataLayer)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

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
    dl.ingest_status_by_source_refs.return_value = {
        "macwhisper:session:abc123": {
            "source_ref": "macwhisper:session:abc123",
            "ingested": True,
            "memory_id": 42,
            "run_id": "run-123",
            "ingested_at": "2026-04-30T12:00:00",
            "title": "Meeting: macwhisper:session:abc123",
        }
    }
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


class _AcquireContext:
    async def __aenter__(self):
        return "conn"

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _Pool:
    def acquire(self):
        return _AcquireContext()


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


class TestIngestStatusTool:
    @pytest.mark.asyncio
    async def test_ingest_status_returns_status_items(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import ingest_status

            result = await ingest_status([
                "macwhisper:session:abc123",
                "macwhisper:session:abc123",
                " ",
            ])

        data = json.loads(result)
        assert data["count"] == 1
        assert data["items"][0]["source_ref"] == "macwhisper:session:abc123"
        assert data["items"][0]["ingested"] is True
        mock_dl.ingest_status_by_source_refs.assert_awaited_once_with([
            "macwhisper:session:abc123"
        ])

    @pytest.mark.asyncio
    async def test_ingest_status_rejects_too_many_refs(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import ingest_status

            with pytest.raises(ValueError, match="at most 500"):
                await ingest_status([f"ref-{i}" for i in range(501)])


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


class TestSessionLearningAnalysisTool:
    @pytest.mark.asyncio
    async def test_analysis_tool_rejects_memory_only_scope(self):
        import open_brain.server as server

        token = server._current_scopes.set(("memory",))
        try:
            with pytest.raises(server.ScopeDeniedError, match="evolution"):
                await server.analyze_session_learnings(limit=5)
        finally:
            server._current_scopes.reset(token)

    @pytest.mark.asyncio
    async def test_regression_session_learning_analysis_tool_is_read_only(self):
        """The scoped server tool delegates to the shared observational analyzer."""
        import open_brain.server as server

        tool = getattr(server, "analyze_session_learnings", None)
        assert tool is not None
        expected = {
            "read_only": True,
            "write_side_effects": False,
            "counts": {"source_summaries": 5},
            "queues": {},
        }
        with patch.object(
            server,
            "_analyze_session_learnings",
            new_callable=AsyncMock,
            return_value=expected,
        ) as analyze:
            token = server._current_scopes.set(("memory", "evolution"))
            try:
                result = json.loads(
                    await tool(
                        limit=5,
                        project="open-brain",
                        source="session-close",
                        model=None,
                    )
                )
            finally:
                server._current_scopes.reset(token)

        assert result == expected
        analyze.assert_awaited_once_with(
            limit=5,
            project="open-brain",
            source="session-close",
            model=None,
        )


# ─── SaveMemory tool ──────────────────────────────────────────────────────────

class TestSaveMemoryTool:
    @pytest.mark.asyncio
    async def test_save_memory_requires_typed_origin_before_database_guards(self, mock_dl):
        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.get_pool", new_callable=AsyncMock) as get_pool,
        ):
            from open_brain.server import save_memory

            result = await save_memory(text="Missing origin")

        assert json.loads(result)["error"] == "invalid_origin_provenance"
        get_pool.assert_not_awaited()
        mock_dl.save_memory.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_save_memory_forwards_typed_origin_provenance(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import save_memory

            result = await save_memory(
                text="Canonical origin",
                provenance={
                    "producer": "agent",
                    "source_ref": "agent-session:codex:session-123",
                },
            )

        assert json.loads(result)["id"] == 42
        call_args = mock_dl.save_memory.call_args[0][0]
        assert call_args.provenance == {
            "producer": "agent",
            "source_ref": "agent-session:codex:session-123",
        }

    @pytest.mark.asyncio
    async def test_save_memory_returns_id(self, mock_dl):
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import save_memory
            result = await save_memory(text="Important observation", provenance={"producer": "test-suite", "source_ref": "test-suite:test_tools"})
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
                provenance={"producer": "test-suite", "source_ref": "test-suite:test_tools"},
            )
            call_args = mock_dl.save_memory.call_args[0][0]
            assert call_args.text == "content"
            assert call_args.type == "decision"
            assert call_args.project == "myproj"
            assert call_args.title == "My Title"
            assert call_args.subtitle == "My Subtitle"
            assert call_args.narrative == "Context here"

    @pytest.mark.asyncio
    async def test_save_memory_session_ref_accepted(self, mock_dl):
        """session_ref param is accepted and forwarded to the data layer."""
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import save_memory
            await save_memory(
                text="Session content",
                type="session_summary",
                project="myproj",
                session_ref="open-brain-193",
                provenance={"producer": "test-suite", "source_ref": "test-suite:test_tools"},
            )
            call_args = mock_dl.save_memory.call_args[0][0]
            assert call_args.session_ref == "open-brain-193"
            assert call_args.type == "session_summary"

    @pytest.mark.asyncio
    async def test_save_memory_upsert_returns_updated_message(self, mock_dl):
        """When upsert occurs, response message reflects the update."""
        mock_dl.save_memory.return_value = SaveMemoryResult(id=42, message="Memory updated (upsert)")
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import save_memory
            result = await save_memory(
                text="Updated summary",
                type="session_summary",
                session_ref="open-brain-193",
                provenance={"producer": "test-suite", "source_ref": "test-suite:test_tools"},
            )
            data = json.loads(result)
            assert data["id"] == 42
            assert data["message"] == "Memory updated (upsert)"

    @pytest.mark.asyncio
    async def test_save_memory_filters_test_artifacts(self, mock_dl):
        """When is_test=True, data layer is NOT called and response signals non-persistence."""
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import save_memory
            result = await save_memory(
                text="Integration test memory",
                title="API test probe",
                is_test=True,
            )
            data = json.loads(result)
            assert data["id"] == -1
            assert "not persisted" in data["message"]
            mock_dl.save_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_save_memory_duplicate_skips_enrichment(self, mock_dl):
        """When save_memory returns duplicate_of, no LLM calls are made and update_memory is NOT called."""
        mock_dl.save_memory.return_value = SaveMemoryResult(
            id=7, message="Duplicate content detected", duplicate_of=7
        )
        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", return_value={"capture_template": "X"}) as mock_classify,
            patch("open_brain.server._extract_entities", return_value={"people": ["Alice"]}) as mock_entities,
        ):
            from open_brain.server import save_memory
            result = await save_memory(text="Duplicate text", provenance={"producer": "test-suite", "source_ref": "test-suite:test_tools"})
            data = json.loads(result)
            assert data["duplicate_of"] == 7
            mock_dl.update_memory.assert_not_called()
            mock_classify.assert_not_called()
            mock_entities.assert_not_called()

    @pytest.mark.asyncio
    async def test_save_memory_blocks_rejected_proposal(self, mock_dl):
        """Memory-Write Judge rejection prevents persistence."""
        proposal = {
            "intended_memory_content": "API token begins with redacted.",
            "category": "fact",
            "source_citation": {"ref": "terminal://env", "label": "observed"},
            "authorization_basis": {
                "ref": "conversation://current",
                "label": "observed",
                "granted_by": "user",
            },
            "expected_use": "evidence",
            "retention_scope": "personal",
            "risk_flags": ["secret"],
        }
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import save_memory
            result = await save_memory(text="API token begins with redacted.", proposal=proposal, provenance={"producer": "test-suite", "source_ref": "test-suite:test_tools"})

        data = json.loads(result)
        assert data["error"] == "memory_write_judge_rejected"
        assert data["judge"]["decision"] == "BLOCK"
        mock_dl.save_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_save_memory_persists_allowed_proposal_metadata(self, mock_dl):
        """Allowed proposals write provenance and policy-version metadata."""
        proposal = {
            "intended_memory_content": "User prefers concise status updates.",
            "category": "preference",
            "source_citation": {"ref": "conversation://current/preference", "label": "observed"},
            "authorization_basis": {
                "ref": "conversation://current/preference",
                "label": "observed",
                "granted_by": "user",
            },
            "expected_use": "instruction",
            "retention_scope": "personal",
            "risk_flags": [],
        }
        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", return_value={}),
            patch("open_brain.server._extract_entities", return_value={}),
        ):
            from open_brain.server import save_memory
            result = await save_memory(
                text="User prefers concise status updates.",
                metadata={"existing": True},
                proposal=proposal,
                provenance={"producer": "test-suite", "source_ref": "test-suite:test_tools"},
            )
        data = json.loads(result)
        call_args = mock_dl.save_memory.call_args[0][0]
        assert data["id"] == 42
        assert call_args.metadata["existing"] is True
        assert call_args.metadata["memory_write_judge"]["decision"] == "ALLOW"
        assert call_args.metadata["memory_write_judge"]["policy_version"] == "memory-write-judge.v1"
        assert call_args.metadata["provenance"]["source_label"] == "observed"
        assert call_args.metadata["provenance"]["expected_use"] == "instruction"


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


# ─── People operator tools ────────────────────────────────────────────────────

class TestPeopleOperatorTools:
    @pytest.mark.asyncio
    async def test_people_list_returns_structured_payload(self):
        payload = {"mode": "list", "total": 1, "persons": [{"id": 10}]}
        with (
            patch("open_brain.server.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("open_brain.server.list_persons_payload", new_callable=AsyncMock) as mock_list,
        ):
            mock_pool.return_value = _Pool()
            mock_list.return_value = payload
            from open_brain.server import people_list

            result = await people_list(include_merged=True, collisions_only=True)

            data = json.loads(result)
            assert data == payload
            mock_list.assert_called_once_with(
                "conn",
                include_merged=True,
                collisions_only=True,
            )

    @pytest.mark.asyncio
    async def test_people_merge_dry_run_returns_report(self):
        with (
            patch("open_brain.server.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("open_brain.server.dry_run_people_merge", new_callable=AsyncMock) as mock_dry_run,
        ):
            mock_pool.return_value = _Pool()
            mock_dry_run.return_value = "would merge"
            from open_brain.server import people_merge

            result = await people_merge(
                source_id=10,
                target_id=20,
                dry_run=True,
                absorb_text=True,
            )

            data = json.loads(result)
            assert data["status"] == "dry_run"
            assert data["report"] == "would merge"
            mock_dry_run.assert_called_once_with(
                "conn",
                10,
                20,
                absorb_text=True,
            )

    @pytest.mark.asyncio
    async def test_people_merge_write_path_delegates_to_merge_helper(self):
        summary = {"status": "merged", "source_id": 10, "target_id": 20}
        with (
            patch("open_brain.server.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("open_brain.server.merge_people_records", new_callable=AsyncMock) as mock_merge,
        ):
            mock_pool.return_value = _Pool()
            mock_merge.return_value = summary
            from open_brain.server import people_merge

            result = await people_merge(source_id=10, target_id=20)

            data = json.loads(result)
            assert data == summary
            mock_merge.assert_called_once_with(
                "conn",
                10,
                20,
                absorb_text=False,
            )


# ─── User Attribution Tests ───────────────────────────────────────────────────

class TestUserAttribution:
    """Tests for user_id tagging and author filtering."""

    @pytest.mark.asyncio
    async def test_save_memory_passes_user_id_from_context(self, mock_dl):
        """save_memory reads user_id from _current_user_id ContextVar."""
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            import open_brain.server as server_module
            token = server_module._current_user_id.set("alice")
            try:
                from open_brain.server import save_memory
                await save_memory(text="Alice's memory", provenance={"producer": "test-suite", "source_ref": "test-suite:test_tools"})
                call_args = mock_dl.save_memory.call_args[0][0]
                assert call_args.user_id == "alice"
            finally:
                server_module._current_user_id.reset(token)

    @pytest.mark.asyncio
    async def test_save_memory_user_id_none_when_no_context(self, mock_dl):
        """save_memory sets user_id=None when no user in context (e.g., API key auth)."""
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            import open_brain.server as server_module
            # Ensure ContextVar is cleared
            token = server_module._current_user_id.set(None)
            try:
                from open_brain.server import save_memory
                await save_memory(text="Anonymous memory", provenance={"producer": "test-suite", "source_ref": "test-suite:test_tools"})
                call_args = mock_dl.save_memory.call_args[0][0]
                assert call_args.user_id is None
            finally:
                server_module._current_user_id.reset(token)

    @pytest.mark.asyncio
    async def test_search_passes_author_param(self, mock_dl):
        """search tool forwards author param to SearchParams."""
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import search
            await search(query="test", author="alice")
            call_args = mock_dl.search.call_args[0][0]
            assert call_args.author == "alice"

    @pytest.mark.asyncio
    async def test_search_author_none_by_default(self, mock_dl):
        """search author defaults to None (no filter)."""
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import search
            await search(query="test")
            call_args = mock_dl.search.call_args[0][0]
            assert call_args.author is None

    @pytest.mark.asyncio
    async def test_stats_returns_by_user(self, mock_dl):
        """stats() includes by_user breakdown."""
        mock_dl.stats.return_value = {
            "memories": 100, "sessions": 10, "relationships": 50,
            "db_size_bytes": 1048576, "db_size_mb": 1.0,
            "types": {"observation": 80, "decision": 20},
            "by_user": {"alice": 60, "bob": 40},
        }
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import stats
            result = await stats()
            data = json.loads(result)
            assert "by_user" in data
            assert data["by_user"]["alice"] == 60
            assert data["by_user"]["bob"] == 40

    def test_memory_has_user_id_field(self):
        """Memory dataclass has user_id field."""
        from open_brain.data_layer.interface import Memory
        m = Memory(
            id=1, index_id=1, session_id=None, type="observation",
            title=None, subtitle=None, narrative=None,
            content="test", metadata={}, priority=0.5, stability="stable",
            access_count=0, last_accessed_at=None,
            created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
            user_id="alice",
        )
        assert m.user_id == "alice"

    def test_memory_user_id_defaults_to_none(self):
        """Memory user_id defaults to None for backward compat."""
        from open_brain.data_layer.interface import Memory
        m = Memory(
            id=1, index_id=1, session_id=None, type="observation",
            title=None, subtitle=None, narrative=None,
            content="test", metadata={}, priority=0.5, stability="stable",
            access_count=0, last_accessed_at=None,
            created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
        )
        assert m.user_id is None

    def test_save_memory_params_has_user_id_field(self):
        """SaveMemoryParams accepts user_id."""
        from open_brain.data_layer.interface import SaveMemoryParams
        p = SaveMemoryParams(text="test", user_id="bob", provenance={"producer": "test-suite", "source_ref": "test-suite:test_tools"})
        assert p.user_id == "bob"

    def test_search_params_has_author_field(self):
        """SearchParams accepts author field."""
        from open_brain.data_layer.interface import SearchParams
        p = SearchParams(query="test", author="alice")
        assert p.author == "alice"


# ─── Integration tests (skipped by default) ───────────────────────────────────

@pytest.mark.integration
class TestToolsIntegration:
    """Integration tests requiring a real database. Run with INTEGRATION_TEST=1."""

    @pytest.mark.asyncio
    async def test_save_and_search_memory(self):
        """Save a memory and then find it via search."""
        from open_brain.server import save_memory, search
        save_result = json.loads(await save_memory(text="Integration test memory", type="test", provenance={"producer": "test-suite", "source_ref": "test-suite:test_tools"}))
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
